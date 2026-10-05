"""Приём заказов с сайта → база заказов + Telegram владельцу. Работает на вашем сервере (сайт на GitHub Pages
статичный, поэтому токен бота живёт только здесь и никогда не попадает в публичные файлы).

    python order_api.py                    # слушать 127.0.0.1:8787 (за Caddy / Cloudflare Tunnel) + бот
    python order_api.py --no-bot           # без опроса Telegram (кнопки статусов не работают)
    python order_api.py --check            # проверить настройки и данные, ничего не запуская
    python order_api.py --test-telegram    # пробное сообщение в чат заказов

API:
    POST /api/order   JSON {items:[{id, size, qty}], customer:{name, phone, telegram, city, comment},
                      consent:true, consent_marketing, cid, client_order_id, city, tg_init_data,
                      page_url, lang, website}  (website — ловушка для ботов, должна быть пустой)
                      → {ok:true, order_no, duplicate, notified, total_uzs, prepay_uzs, unavailable:[...],
                         telegram_linked, bot_url} или {ok:false, error}
                      bot_url — https://t.me/<BOT_USERNAME>?start=o_<номер>_<подпись> (подпись HMAC токена бота,
                      см. orders_db.start_payload); пусто, если не заданы BOT_USERNAME или TELEGRAM_BOT_TOKEN —
                      тогда сайт кнопку «Получать статус в Telegram» не показывает (без подписи бот не привяжет).
                      telegram_linked — к ЭТОМУ заказу привязан проверенный Telegram (initData Mini App).
                      Полное описание полей — data/analysis/contract.md, раздел «T1: order payload & bot».
    GET  /api/health  → {ok:true, products, telegram, db, bot}
    OPTIONS           CORS preflight (только для ORDER_ALLOWED_ORIGINS)

Цены пересчитываются здесь по каталогу сайта (цена с сайта клиента не принимается вовсе), закупочные
данные — из закрытой части каталога. Файлы перечитываются, когда меняются.
Каждый заказ: база data/orders/orders.sqlite (orders_db.py: покупатель по телефону +998…, статусы),
сырой журнал data/orders/orders.jsonl (полные контакты — только на сервере) и сообщение в Telegram
TELEGRAM_ORDERS_CHAT_ID (или TELEGRAM_CHAT_ID) с кнопками статусов. Кнопки обрабатывает tg_bot.py —
он работает потоком внутри этой же службы (единственный потребитель getUpdates). После ответа сайту
в фоне перепроверяется наличие у Trendyol / Pierre Cardin / Cacharel (вторым сообщением продавцу).
Раз в сутки — копия базы в data/orders/backup/ (хранится ORDER_BACKUP_KEEP_DAYS дней).

Настройки — переменные окружения (на сервере /etc/yurt/yurt.env):
    TELEGRAM_BOT_TOKEN, TELEGRAM_ORDERS_CHAT_ID (иначе TELEGRAM_CHAT_ID), TELEGRAM_ADMIN_IDS, SHOP_URL,
    BOT_USERNAME            — см. tg_bot.py
    ORDER_ALLOWED_ORIGINS   через запятую; по умолчанию https://fayyoznaimov.github.io
    ORDER_API_PORT          8787        ORDER_API_BIND   127.0.0.1
    ORDER_RATE_LIMIT        5 заказов   ORDER_RATE_WINDOW 600 секунд (на один IP)
    ORDER_TRUST_PROXY       1 — брать IP клиента из CF-Connecting-IP / X-Forwarded-For
                            (по умолчанию 1, если слушаем 127.0.0.1, т.е. стоим за прокси)
    ORDERS_DB_PATH          файл базы (по умолчанию data/orders/orders.sqlite)
    ORDER_VERIFY            0 — не перепроверять наличие у источников после заказа (по умолчанию 1)
    ORDER_BACKUP_KEEP_DAYS  30
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import importlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import requests

import catalog_files
import orders_db
import tg_bot

ROOT = Path(__file__).resolve().parent
MAX_BODY = 20 * 1024
DRAIN_MAX = 256 * 1024                                     # лишнее тело дочитываем, чтобы клиент увидел 413
MAX_ITEMS = 30
MAX_QTY = 10
TG_LIMIT = 4096
ID_RE = re.compile(r"^[A-Z0-9]{7}$")
TG_USER_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
PHONE_CHARS_RE = re.compile(r"^[0-9+()\-.\s]{5,25}$")
CID_RE = re.compile(r"^[A-Za-z0-9-]{20,40}$")
CLIENT_ORDER_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
UZ_FIRST_DIGITS = set("9876532")                           # первая цифра номера после +998
INIT_DATA_MAX = 4096
INIT_DATA_MAX_AGE = 24 * 3600
VERIFY_SOURCES = {"trendyol", "pcardin_tr", "cacharel_tr"} # проверяются по ссылке автоматически
MANUAL_SOURCES = {"yoox", "yoox_import", "feed_yoox"}      # YOOX — только вручную, никаких запросов отсюда
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏  ‪-‮]")
SOURCE_NAMES = {
    "yoox": "YOOX", "yoox_import": "YOOX", "trendyol": "Trendyol",
    "pcardin_tr": "Pierre Cardin TR", "cacharel_tr": "Cacharel TR",
}
LANGS = {"ru", "uz", "en"}
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # без 0/O и 1/I

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8")


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- данные каталога

def _load_admin(text: str) -> dict:
    """products-admin.js: 'window.DEALS_ADMIN = {...};' → dict."""
    i = text.find("=")
    if not text.lstrip().startswith("window.DEALS_ADMIN") or i < 0:
        raise ValueError("products-admin.js: нет 'window.DEALS_ADMIN ='")
    body = text[i + 1:].strip().rstrip(";").strip()
    data = json.loads(body)
    return {k: v for k, v in data.items() if isinstance(v, dict) and not k.startswith("_")}


class Catalog:
    """Публичные и закрытые данные каталога; перечитываются при изменении файлов. Каталог частями
    (site/data/manifest.json + части индекса, site/admin/) или старые products.json / products-admin.js —
    через catalog_files. Для цен заказа хватает индекса (подробности data/d/ не читаются).
    Если файлы записаны наполовину (идёт сборка) — остаются прежние данные, попытка повторится позже."""

    def __init__(self, site: Path):
        self.site = site
        self.public_path = site / "products.json"       # старый формат (для сообщений и тестов)
        self.admin_path = site / "products-admin.js"
        self.products: dict[str, dict] = {}
        self.admin: dict[str, dict] = {}
        self.generated_at = ""
        self._sig: dict[str, tuple] = {}
        self._lock = threading.Lock()

    def refresh(self) -> None:
        with self._lock:
            sig = catalog_files.public_signature(self.site)
            if sig and sig != self._sig.get("public"):
                try:
                    c = catalog_files.PublicCatalog(self.site)
                    self.products = c.index_rows()
                    self.generated_at = str((c.summary or {}).get("generated_at") or "")
                    self._sig["public"] = sig
                    log(f"каталог: {len(self.products)} товаров ({self.generated_at})")
                except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
                    log(f"каталог: не прочитан {sig[0]} ({e.__class__.__name__}) — оставляю прежний")
            sig = catalog_files.admin_signature(self.site)
            if sig and sig != self._sig.get("admin"):
                try:
                    if sig[0] == "products-admin.js":
                        self.admin = _load_admin(self.admin_path.read_text(encoding="utf-8"))
                    else:
                        self.admin = {k: v for k, v in catalog_files.read_admin(self.site).items()
                                      if isinstance(v, dict) and not k.startswith("_")}
                    self._sig["admin"] = sig
                except (OSError, ValueError, KeyError, TypeError) as e:
                    log(f"каталог: не прочитаны закрытые данные {sig[0]} ({e.__class__.__name__}) — оставляю прежние")

    def get(self, pid: str) -> tuple[dict | None, dict]:
        self.refresh()
        return self.products.get(pid), self.admin.get(pid, {})


# ---------------------------------------------------------------- форматирование

def money(n) -> str:
    try:
        return f"{int(round(float(n))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "?"


def src_price(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    return f"{f:.0f}" if f == int(f) else f"{f:.2f}"


def shop_name(adm: dict) -> str:
    src = adm.get("source") or ""
    if src in SOURCE_NAMES:
        return SOURCE_NAMES[src]
    host = urlparse(adm.get("url") or "").hostname or ""
    return host.removeprefix("www.") or src or "?"


def safe_link(url: str) -> str | None:
    u = str(url or "").strip()
    return u if u.startswith(("https://", "http://")) else None


def mask_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return ("***" + d[-4:]) if d else "-"


def ip_tag(ip: str) -> str:
    return hashlib.sha256(("yurt:" + ip).encode()).hexdigest()[:10]


def availability(prod: dict | None, size: str) -> tuple[bool, str]:
    """(можно ли купить, текст для владельца)."""
    if not prod:
        return False, "НЕТ В КАТАЛОГЕ (товар снят с сайта)"
    sizes = [str(s) for s in prod.get("sizes") or []]
    if not prod.get("in_stock", True):
        return False, "НЕТ В НАЛИЧИИ (помечен распроданным)"
    if size and sizes and size not in sizes:
        return False, f"РАЗМЕРА {size} НЕТ (сейчас есть: {', '.join(sizes)})"
    if not size and sizes:
        return False, f"размер не выбран (есть: {', '.join(sizes)})"
    return True, f"в наличии{', размер ' + size + ' есть' if size else ''}"


# ---------------------------------------------------------------- проверка заказа

class Invalid(Exception):
    pass


def _text(v, field: str, maxlen: int, required: bool = False, minlen: int = 0) -> str:
    if v is None:
        v = ""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise Invalid(f"{field}: неверный тип")
    s = CTRL_RE.sub("", str(v)).strip()
    s = re.sub(r"[ \t]+", " ", s)
    if required and not s:
        raise Invalid(f"{field}: обязательное поле")
    if s and len(s) < minlen:
        raise Invalid(f"{field}: слишком коротко")
    if len(s) > maxlen:
        raise Invalid(f"{field}: длиннее {maxlen} символов")
    return s


def norm_phone(s: str) -> str:
    """Телефон → E.164. Узбекистан: 9 цифр (90 123 45 67) → +998901234567; 998901234567 → +998…;
    00… → +…; иностранный номер — только с явным «+». Остальное — Invalid."""
    if not s:
        return ""
    if not PHONE_CHARS_RE.match(s):
        raise Invalid("customer.phone: неверный номер")
    st = s.strip()
    digits = re.sub(r"\D", "", st)
    plus = st.startswith("+")
    if not plus and digits.startswith("00"):
        digits, plus = digits[2:], True
    if plus:
        if digits.startswith("998"):
            if len(digits) == 12 and digits[3] in UZ_FIRST_DIGITS:
                return "+" + digits
            raise Invalid("customer.phone: неверный номер (+998 и 9 цифр)")
        if not digits or digits[0] == "0" or not 8 <= len(digits) <= 15:
            raise Invalid("customer.phone: неверный номер")
        return "+" + digits
    if len(digits) == 9 and digits[0] in UZ_FIRST_DIGITS:
        return "+998" + digits
    if len(digits) == 12 and digits.startswith("998") and digits[3] in UZ_FIRST_DIGITS:
        return "+" + digits
    raise Invalid("customer.phone: неверный номер (+998 и 9 цифр; другой страны — с «+»)")


def norm_telegram(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/", "", s.strip()).lstrip("@").strip("/")
    if not TG_USER_RE.match(s):
        raise Invalid("customer.telegram: укажите ник вида @name")
    return s


def verify_init_data(init_data: str, bot_token: str, max_age: int = INIT_DATA_MAX_AGE,
                     now: float | None = None) -> dict | None:
    """Проверка Telegram.WebApp.initData (Mini App): HMAC-SHA256 с ключом HMAC_SHA256('WebAppData', токен),
    сравнение за постоянное время, auth_date не старше max_age. {id, username, first_name, start_param} или None."""
    if not init_data or not bot_token or not isinstance(init_data, str) or len(init_data) > INIT_DATA_MAX:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data: dict[str, str] = {}
    for k, v in pairs:
        if k in data:                                      # повтор ключа — подделка
            return None
        data[k] = v
    got = data.pop("hash", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", got):
        return None
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    try:
        auth = int(data.get("auth_date") or "")
    except ValueError:
        return None
    now = time.time() if now is None else now
    if auth > now + 300 or now - auth > max_age:
        return None
    try:
        user = json.loads(data.get("user") or "")
    except ValueError:
        return None
    if not isinstance(user, dict):
        return None
    uid = user.get("id")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        return None
    uname = str(user.get("username") or "")
    return {"id": uid, "username": uname if TG_USER_RE.match(uname) else "",
            "first_name": CTRL_RE.sub("", str(user.get("first_name") or ""))[:64],
            "start_param": str(data.get("start_param") or "")[:64]}


def _flag(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


def validate(payload) -> dict:
    """Строгая проверка тела запроса. Возвращает чистые данные или бросает Invalid."""
    if not isinstance(payload, dict):
        raise Invalid("ожидался JSON-объект")
    cust = payload.get("customer")
    if not isinstance(cust, dict):
        raise Invalid("customer: обязательное поле")
    if _text(payload.get("website"), "website", 500) or _text(cust.get("website"), "website", 500):
        raise Invalid("spam")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise Invalid("items: пустой заказ")
    if len(items) > MAX_ITEMS:
        raise Invalid(f"items: не больше {MAX_ITEMS} позиций")
    clean_items = []
    for n, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise Invalid(f"items[{n}]: ожидался объект")
        pid = _text(it.get("id"), f"items[{n}].id", 7, required=True).upper()
        if not ID_RE.match(pid):
            raise Invalid(f"items[{n}].id: неверный код товара")
        size = _text(it.get("size"), f"items[{n}].size", 20)
        qty = it.get("qty", 1)
        if isinstance(qty, bool) or not isinstance(qty, (int, float, str)):
            raise Invalid(f"items[{n}].qty: неверное количество")
        try:
            qf = float(qty)
        except ValueError:
            raise Invalid(f"items[{n}].qty: неверное количество")
        if qf != int(qf) or not 1 <= int(qf) <= MAX_QTY:
            raise Invalid(f"items[{n}].qty: от 1 до {MAX_QTY}")
        clean_items.append({"id": pid, "size": size, "qty": int(qf)})
    customer = {
        "name": _text(cust.get("name"), "customer.name", 80, required=True, minlen=2),
        "phone": norm_phone(_text(cust.get("phone"), "customer.phone", 25)),
        "telegram": norm_telegram(_text(cust.get("telegram"), "customer.telegram", 64)),
        "city": _text(cust.get("city"), "customer.city", 60) or _text(payload.get("city"), "city", 60),
        "comment": _text(cust.get("comment"), "customer.comment", 500),
    }
    if not (customer["phone"] or customer["telegram"]):
        raise Invalid("customer: укажите телефон или Telegram")
    consent = payload.get("consent", cust.get("consent"))
    if not _flag(consent):
        raise Invalid("consent: нужно согласие на обработку данных для оформления заказа")
    page_url = _text(payload.get("page_url"), "page_url", 300)
    if page_url and not page_url.startswith(("https://", "http://")):
        page_url = ""
    lang = _text(payload.get("lang"), "lang", 8).lower()[:2]
    cid = payload.get("cid")
    cid = cid.strip() if isinstance(cid, str) and CID_RE.match(cid.strip()) else ""
    coid = payload.get("client_order_id")
    coid = coid.strip() if isinstance(coid, str) and CLIENT_ORDER_RE.match(coid.strip()) else ""
    init = payload.get("tg_init_data")
    init = init if isinstance(init, str) and 0 < len(init) <= INIT_DATA_MAX else ""
    return {"items": clean_items, "customer": customer, "page_url": page_url,
            "lang": lang if lang in LANGS else "ru", "cid": cid, "client_order_id": coid,
            "consent_marketing": _flag(payload.get("consent_marketing", cust.get("consent_marketing"))),
            "tg_init_data": init}


def price_order(order: dict, catalog: Catalog) -> dict:
    """Цены и закупочные данные — только с сервера."""
    lines, total, cost, unavailable, known = [], 0, 0, [], 0
    for it in order["items"]:
        prod, adm = catalog.get(it["id"])
        ok, note = availability(prod, it["size"])
        price = int(prod.get("price_uzs") or 0) if prod else 0
        line = {
            "id": it["id"], "size": it["size"], "qty": it["qty"], "ok": ok, "availability": note,
            "brand": (prod or {}).get("brand", ""), "title": (prod or {}).get("title", ""),
            "price_uzs": price, "sum_uzs": price * it["qty"],
            "source": adm.get("source", ""), "shop": shop_name(adm) if adm else "",
            "source_item_id": str(adm.get("source_item_id") or ""),
            "url": adm.get("url", ""), "title_original": adm.get("title_original", ""),
            "price_now": adm.get("price_now"), "price_old": adm.get("price_old"),
            "currency": adm.get("currency", ""), "cost_uzs": adm.get("cost_uzs"), "margin_uzs": adm.get("margin_uzs"),
            "sizes_now": list((prod or {}).get("sizes") or []), "in_stock": bool((prod or {}).get("in_stock", False)),
        }
        if prod:
            known += 1
            total += line["sum_uzs"]
            if isinstance(adm.get("cost_uzs"), (int, float)):
                cost += adm["cost_uzs"] * it["qty"]
        if not ok:
            unavailable.append(it["id"] + (f" ({it['size']})" if it["size"] else ""))
        lines.append(line)
    if not known:
        raise Invalid("товары не найдены в каталоге — обновите страницу")
    return {"lines": lines, "total_uzs": total, "cost_uzs": cost, "margin_uzs": total - cost if cost else None,
            "prepay_uzs": orders_db.prepay_for(total),
            "unavailable": unavailable, "pieces": sum(l["qty"] for l in lines if l["price_uzs"])}


# ---------------------------------------------------------------- сообщение в Telegram

def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def build_message(order_no: str, order: dict, priced: dict, created: datetime, info: dict | None = None) -> str:
    """Сообщение продавцу (только чат заказов: здесь можно источник, ссылку и себестоимость).
    info: customer_orders (сколько заказов у покупателя с этим), tg_user (проверенный initData), source,
    db_ok (False — база недоступна, кнопок статусов нет), bot_url (подписанная ссылка на статусы этого заказа)."""
    info = info or {}
    c = order["customer"]
    out = [f"<b>Заказ {e(order_no)}</b>", tg_bot.status_line("new"),
           f"{created:%d.%m.%Y %H:%M} · язык: {e(order['lang'])}"
           + (" · из Telegram (Mini App)" if info.get("source") == "miniapp" else ""), "",
           "<b>Покупатель</b>", f"Имя: {e(c['name'])}"]
    n = info.get("customer_orders")
    if n:
        out.append(f"Клиент: {int(n)}-й заказ" + (" (новый покупатель)" if int(n) == 1 else " — постоянный покупатель"))
    if c["phone"]:
        out.append(f'Телефон: <a href="tel:{e(c["phone"])}">{e(c["phone"])}</a>')
    if c["telegram"]:
        out.append(f'Telegram: <a href="https://t.me/{e(c["telegram"])}">@{e(c["telegram"])}</a>')
    tgu = info.get("tg_user")
    if tgu:
        who = f'<a href="https://t.me/{e(tgu["username"])}">@{e(tgu["username"])}</a>' if tgu.get("username") \
            else e(tgu.get("first_name") or "без ника")
        out.append(f"Telegram подтверждён: {who} — статусы будут приходить ему от бота")
    if c["city"]:
        out.append(f"Город: {e(c['city'])}")
    if c["comment"]:
        out.append(f"Комментарий: {e(c['comment'])}")
    if order.get("consent_marketing"):
        out.append("Согласие на новинки: да")
    for n, l in enumerate(priced["lines"], 1):
        out.append("")
        head = f"<b>{n}. <code>{e(l['id'])}</code></b>"
        if l["brand"] or l["title"]:
            head += f" · {e(l['brand'])} — {e(l['title'])}"
        out.append(head)
        out.append(f"Размер: {e(l['size'] or '—')} · Кол-во: {l['qty']} · Наша цена: {money(l['price_uzs'])} сум"
                   + (f" (×{l['qty']} = {money(l['sum_uzs'])})" if l["qty"] > 1 else ""))
        if l["shop"] or l["url"]:
            link = safe_link(l["url"])
            shop = f'<a href="{e(link)}">{e(l["shop"])}</a>' if link else e(l["shop"])
            price = ""
            if l["price_now"] is not None:
                price = f" · {src_price(l['price_now'])} {e(l['currency'])}"
                if l["price_old"] and l["price_old"] != l["price_now"]:
                    price += f" (было {src_price(l['price_old'])})"
            out.append(f"Закупка: {shop}{price}")
            if link:
                out.append(f"Ссылка: {e(link)}")
            if l["title_original"]:
                out.append(f"В магазине: {e(l['title_original'])}")
            if l["cost_uzs"] is not None:
                out.append(f"Себестоимость {money(l['cost_uzs'])} · маржа {money(l['margin_uzs'])} сум")
        else:
            out.append("Закупка: нет закрытых данных по этому коду")
        out.append(("Наличие: " if l["ok"] else "⚠ Наличие: ") + e(l["availability"]))
    out.append("")
    tot = f"<b>Итого: {money(priced['total_uzs'])} сум</b> ({priced['pieces']} шт.)"
    if priced.get("prepay_uzs"):
        tot += f"\nПредоплата 50%: {money(priced['prepay_uzs'])} сум · остаток {money(priced['total_uzs'] - priced['prepay_uzs'])} сум"
    if priced["cost_uzs"]:
        tot += f"\nСебестоимость {money(priced['cost_uzs'])} · маржа {money(priced['margin_uzs'])} сум"
    out.append(tot)
    if priced["unavailable"]:
        out.append(f"⚠ Проверить наличие: {e(', '.join(priced['unavailable']))}")
    if order["page_url"]:
        out.append(f"Страница: {e(order['page_url'])}")
    if info.get("bot_url"):                                # если покупатель не нажал кнопку на сайте — переслать ему
        out.append(f"Статусы в Telegram (ссылку можно переслать покупателю): {e(info['bot_url'])}")
    if info.get("db_ok") is False:
        out.append("⚠ Заказ НЕ записан в базу (ошибка базы) — есть только в orders.jsonl; кнопок статусов нет")
    return "\n".join(out)


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет по строкам (не внутри HTML-тега), каждая часть ≤ limit."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                       # одна огромная строка — режем грубо, без тегов
            if cur:
                parts.append(cur); cur = ""
            chunk = re.sub(r"<[^>]+>", "", line)[:limit]
            parts.append(chunk)
            line = re.sub(r"<[^>]+>", "", line)[limit:]
        cand = (cur + "\n" + line) if cur else line
        if len(cand) > limit:
            parts.append(cur); cur = line
        else:
            cur = cand
    if cur:
        parts.append(cur)
    return parts


def orders_chat() -> str:
    return (os.environ.get("TELEGRAM_ORDERS_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()


def send_telegram(text: str, reply_markup: dict | None = None, reply_to: int | None = None):
    """Отправка в чат заказов (длинный текст — несколькими сообщениями; клавиатура — на последнем).
    Возвращает сообщение Telegram последней части (dict с message_id и chat) — или False/None, если не ушло.
    Тесты подменяют эту функцию."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), orders_chat()
    if not (token and chat):
        log("telegram: не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_ORDERS_CHAT_ID)")
        return False
    ok, last = True, None
    parts = split_message(text)
    for i, part in enumerate(parts):
        sent = False
        body = {"chat_id": chat, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup and i == len(parts) - 1:
            body["reply_markup"] = reply_markup
        if reply_to and i == 0:
            body["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        for _ in range(3):
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, json=body)
            except requests.RequestException as ex:
                log(f"telegram: {ex.__class__.__name__}")
                time.sleep(1.5)
                continue
            if r.status_code == 200:
                sent = True
                try:
                    last = (r.json() or {}).get("result") or last
                except ValueError:
                    pass
                break
            if r.status_code == 429:
                try:
                    wait = int((r.json().get("parameters") or {}).get("retry_after", 3))
                except ValueError:
                    wait = 3
                time.sleep(min(wait, 10))
                continue
            log(f"telegram: ответ {r.status_code}: {r.text[:200]}")   # токен в ответе не приходит
            if r.status_code in (400, 401, 403, 404):
                break                                     # неверный чат/токен — повтор не поможет
            time.sleep(1.5)
        ok = ok and sent
    return (last or True) if ok else False


def _sent_ids(res) -> tuple[str | None, int | None]:
    """(chat_id, message_id) из ответа send_telegram (dict сообщения) — или (None, None)."""
    if isinstance(res, dict):
        mid = res.get("message_id")
        chat = (res.get("chat") or {}).get("id")
        if isinstance(mid, int):
            return (str(chat) if chat is not None else orders_chat()) or None, mid
    return None, None


# ---------------------------------------------------------------- проверка наличия после заказа

def load_verifier(source: str):
    """(verify, opts) адаптера источника из sources.ADAPTERS — или None. Тесты подменяют эту функцию."""
    from sources import ADAPTERS
    entry = ADAPTERS.get(source)
    if not entry:
        return None
    mod = importlib.import_module(entry[0])
    fn = getattr(mod, "verify", None)
    return (fn, dict(entry[1])) if callable(fn) else None


def verify_lines(lines: list[dict]) -> list[str]:
    """Строки сообщения «Проверка наличия» (HTML). Пусто — проверять нечего (нет товаров Trendyol/Akinon)."""
    auto = [l for l in lines if l.get("source") in VERIFY_SOURCES and l.get("source_item_id") and safe_link(l.get("url"))]
    if not auto:
        return []
    auto_ids = {id(l) for l in auto}
    results: dict[str, dict] = {}
    for src in sorted({l["source"] for l in auto}):
        rows = [{"source_item_id": l["source_item_id"], "url": l["url"]} for l in auto if l["source"] == src]
        try:
            v = load_verifier(src)
            results[src] = (v[0](rows, None, **v[1]) or {}) if v else {}
        except Exception as ex:                            # сеть, блокировка, разбор страницы
            log(f"проверка наличия {src}: {ex.__class__.__name__}: {str(ex)[:160]}")
            results[src] = {}
    out = []
    for l in lines:
        head = f"<code>{e(l['id'])}</code>" + (f" {e(l['brand'])}" if l.get("brand") else "") \
            + (f", размер {e(l['size'])}" if l.get("size") else "")
        src = l.get("source") or ""
        if id(l) in auto_ids:
            res = results.get(src, {})
            sid = l["source_item_id"]
            if sid not in res:
                out.append(f"{head}: ❓ проверить не удалось — проверить по ссылке вручную")
            elif res[sid] is None:
                out.append(f"{head}: ❌ нет в наличии")
            else:
                sizes = [str(s) for s in (res[sid].get("sizes") or [])]
                now = ", ".join(sizes) or "—"
                if l.get("size") and sizes and l["size"] not in sizes:
                    out.append(f"{head}: ⚠ размера {e(l['size'])} нет, размеры сейчас: {e(now)}")
                else:
                    out.append(f"{head}: ✅ есть, размеры сейчас: {e(now)}")
        elif src in MANUAL_SOURCES:
            out.append(f"{head}: 🔗 YOOX — проверить по ссылке вручную")
        elif src:
            out.append(f"{head}: проверить по ссылке вручную")
    return out


# ---------------------------------------------------------------- HTTP

class RateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        """0 — можно; иначе через сколько секунд."""
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return int(q[0] + self.window - now) + 1
            return 0

    def add(self, key: str) -> None:
        with self.lock:
            self.hits[key].append(time.time())
            if len(self.hits) > 10000:                  # не копим память на тысячах IP
                cutoff = time.time() - self.window
                for k in [k for k, q in self.hits.items() if not q or q[-1] < cutoff]:
                    del self.hits[k]


class OrderApp:
    def __init__(self, root: Path = ROOT, origins: list[str] | None = None, rate_limit: int | None = None,
                 rate_window: float | None = None, trust_proxy: bool = False, db_path: Path | None = None,
                 verify: bool | None = None):
        self.root = root
        self.catalog = Catalog(root / "site")
        self.orders_dir = root / "data" / "orders"
        env_origins = os.environ.get("ORDER_ALLOWED_ORIGINS") or "https://fayyoznaimov.github.io"
        self.origins = {o.strip().rstrip("/") for o in (origins or env_origins.split(",")) if o.strip()}
        limit = rate_limit or int(os.environ.get("ORDER_RATE_LIMIT") or 5)
        window = rate_window or float(os.environ.get("ORDER_RATE_WINDOW") or 600)
        self.orders_rl = RateLimiter(limit, window)            # принятые заказы
        self.attempts_rl = RateLimiter(max(30, limit * 6), window)   # любые POST (мусор, ошибки)
        self.trust_proxy = trust_proxy
        self.write_lock = threading.Lock()
        env_db = os.environ.get("ORDERS_DB_PATH")
        self.db_path = Path(db_path) if db_path else (
            Path(env_db) if env_db and root == ROOT else self.orders_dir / "orders.sqlite")
        self.db: orders_db.OrdersDB | None = None
        self._db_retry_at = 0.0
        self._db()
        self.verify_enabled = (os.environ.get("ORDER_VERIFY", "1").strip() != "0") if verify is None else verify
        self.bot: tg_bot.Bot | None = None                    # работающий бот (ставит main); иначе — только отправка
        self._sender: tg_bot.Bot | None = None
        self._bg: list[threading.Thread] = []
        self._bg_lock = threading.Lock()
        self._verify_sem = threading.Semaphore(1)             # к источникам — по одному заказу за раз

    # ------------------------------------------------------------ база

    def _db(self) -> orders_db.OrdersDB | None:
        """База заказов; если не открылась — новая попытка не чаще раза в минуту (заказы при этом принимаются)."""
        if self.db is None and time.time() >= self._db_retry_at:
            try:
                self.db = orders_db.OrdersDB(self.db_path)
            except (sqlite3.Error, OSError) as ex:
                self._db_retry_at = time.time() + 60
                log(f"база заказов не открылась ({ex.__class__.__name__}: {str(ex)[:120]}) — пишу только orders.jsonl")
        return self.db

    def close(self) -> None:
        self.join_background(5)
        if self.db is not None:
            self.db.close()

    def new_order_no(self, now: datetime) -> str:
        """Случайный номер; уникальность проверяет база (orders_db.add_order повторяет при совпадении)."""
        return f"YR-{now:%y%m%d}-" + "".join(secrets.choice(ALPHABET) for _ in range(4))

    def save(self, record: dict) -> None:
        with self.write_lock:
            self.orders_dir.mkdir(parents=True, exist_ok=True)
            path = self.orders_dir / "orders.jsonl"
            new = not path.exists()
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if new:
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass

    @staticmethod
    def bot_url(order_no: str) -> str:
        """Подписанная ссылка на бота для ЭТОГО заказа (o_<номер>_<подпись>); пусто без BOT_USERNAME или токена."""
        return tg_bot.order_bot_url(os.environ.get("BOT_USERNAME") or "", os.environ.get("TELEGRAM_BOT_TOKEN") or "",
                                    order_no)

    def _duplicate_response(self, o: dict) -> dict:
        """Ответ на повтор того же client_order_id: прежний заказ, без нового сообщения и записи."""
        full = None
        try:
            full = self.db.get_order(o["order_no"], with_events=True) if self.db is not None else None
        except sqlite3.Error:
            pass
        events = (full or {}).get("events") or []
        unavailable = [str(l.get("id")) + (f" ({l['size']})" if l.get("size") else "")
                       for l in (o.get("items") or []) if isinstance(l, dict) and not l.get("ok")]
        return {"ok": True, "order_no": o["order_no"], "duplicate": True,
                "notified": not any(ev.get("note") == "seller_notify_failed" for ev in events),
                "total_uzs": int(o.get("total_uzs") or 0), "prepay_uzs": int(o.get("prepay_uzs") or 0),
                "unavailable": unavailable, "telegram_linked": bool(o.get("telegram_user_id")),
                "bot_url": self.bot_url(o["order_no"])}

    # ------------------------------------------------------------ заказ

    def handle_order(self, payload, ip: str) -> tuple[int, dict]:
        try:
            order = validate(payload)
        except Invalid as ex:
            return 400, {"ok": False, "error": str(ex)}
        db = self._db()
        coid = order["client_order_id"]
        if coid and db is not None:                         # повтор той же отправки (сеть, двойное нажатие)
            try:
                prev = db.get_order_by_client_id(coid)
            except sqlite3.Error:
                prev = None
            if prev:
                log(f"заказ {prev['order_no']}: повтор client_order_id — без нового сообщения")
                return 200, self._duplicate_response(prev)
        wait = self.orders_rl.retry_after(ip)
        if wait:
            return 429, {"ok": False, "error": "слишком много заказов, попробуйте позже", "retry_after": wait}
        try:
            priced = price_order(order, self.catalog)
        except Invalid as ex:
            return 400, {"ok": False, "error": str(ex)}
        now = datetime.now()
        created = now.isoformat(timespec="seconds")
        tg_user = None
        if order["tg_init_data"]:                           # неверная подпись — заказ всё равно принимаем
            tg_user = verify_init_data(order["tg_init_data"], os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
        source = "miniapp" if tg_user else "site"
        info = {"source": source, "tg_user": None, "db_ok": True}
        added = None
        if db is not None:
            try:
                added = db.add_order(
                    order_no=lambda: self.new_order_no(now), customer=order["customer"], items=priced["lines"],
                    total_uzs=priced["total_uzs"], prepay_uzs=priced["prepay_uzs"], created_at=created,
                    client_order_id=coid or None, cid=order["cid"] or None, source=source, lang=order["lang"],
                    comment=order["customer"]["comment"], page_url=order["page_url"],
                    marketing_opt_in=order["consent_marketing"], telegram_user=tg_user)
            except (sqlite3.Error, orders_db.DuplicateOrderNo) as ex:
                log(f"база заказов: {ex.__class__.__name__}: {str(ex)[:160]} — заказ только в orders.jsonl")
                added = None
        if added and added["duplicate"]:                    # одновременный повтор: первый уже записан
            prev = db.get_order(added["order_no"])
            return 200, self._duplicate_response(prev or {"order_no": added["order_no"]})
        linked = False
        if added:
            order_no = added["order_no"]
            info["customer_orders"] = added["orders_count"]
            linked = bool(tg_user) and added.get("telegram_link") == "linked"     # только этот заказ
            info["tg_user"] = tg_user if linked else None
            info["bot_url"] = "" if linked else self.bot_url(order_no)
        else:
            order_no = self.new_order_no(now)
            info["db_ok"] = False
        self.orders_rl.add(ip)
        record = {"order_no": order_no, "created_at": created, "ip": ip_tag(ip), "source": source,
                  "customer": order["customer"], "lang": order["lang"], "page_url": order["page_url"],
                  "items": priced["lines"], "total_uzs": priced["total_uzs"], "prepay_uzs": priced["prepay_uzs"],
                  "cost_uzs": priced["cost_uzs"], "catalog_generated_at": self.catalog.generated_at,
                  "client_order_id": coid or None, "cid": order["cid"] or None, "consent": True,
                  "consent_marketing": order["consent_marketing"],
                  "telegram_user_id": tg_user["id"] if linked else None, "db": bool(added)}
        self.save(record)
        text = build_message(order_no, order, priced, now, info)
        res = send_telegram(text, reply_markup=tg_bot.status_keyboard(order_no, "new") if added else None)
        notified = bool(res)
        chat_id, msg_id = _sent_ids(res)
        if added:
            try:
                if msg_id:
                    db.set_tg_message(order_no, chat_id, msg_id, split_message(text)[-1])
                db.add_event(order_no, "seller_notified" if notified else "seller_notify_failed", by="api")
            except sqlite3.Error as ex:
                log(f"база заказов: {ex.__class__.__name__} при записи сообщения")
        if not notified:
            self.save({"order_no": order_no, "event": "telegram_failed", "at": datetime.now().isoformat(timespec="seconds")})
        log(f"заказ {order_no}: позиций {len(priced['lines'])}, {money(priced['total_uzs'])} сум, "
            f"тел {mask_phone(order['customer']['phone'])}, ip {ip_tag(ip)}, "
            f"клиент: {info.get('customer_orders') or '?'}-й заказ, {source}, telegram {'ok' if notified else 'НЕ ОТПРАВЛЕН'}")
        self.start_background(order_no, priced["lines"], msg_id, tg_user if linked else None)
        return 200, {"ok": True, "order_no": order_no, "duplicate": False, "notified": notified,
                     "total_uzs": priced["total_uzs"], "prepay_uzs": priced["prepay_uzs"],
                     "unavailable": priced["unavailable"], "telegram_linked": linked, "bot_url": self.bot_url(order_no)}

    # ------------------------------------------------------------ фон после ответа сайту

    def start_background(self, order_no: str, lines: list[dict], reply_to: int | None, tg_user: dict | None) -> None:
        if not (self.verify_enabled or tg_user):
            return
        t = threading.Thread(target=self._after_order, args=(order_no, lines, reply_to, tg_user),
                             name=f"after-{order_no}", daemon=True)
        with self._bg_lock:
            self._bg = [x for x in self._bg if x.is_alive()]
            self._bg.append(t)
        t.start()

    def join_background(self, timeout: float = 10) -> None:
        """Дождаться фоновых проверок (для тестов и остановки)."""
        end = time.time() + timeout
        with self._bg_lock:
            threads = list(self._bg)
        for t in threads:
            t.join(max(0.0, end - time.time()))

    def sender(self) -> tg_bot.Bot | None:
        """Бот для сообщений покупателю: работающий (main) или только для отправки (без опроса)."""
        if self.bot is not None:
            return self.bot
        if self._sender is None and os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
            self._sender = tg_bot.Bot.from_env(db=lambda: self._db())
        return self._sender

    def _after_order(self, order_no: str, lines: list[dict], reply_to: int | None, tg_user: dict | None) -> None:
        if tg_user:                                         # заказ из Mini App: подтверждение покупателю в Telegram
            try:
                bot = self.sender()
                o = self.db.get_order(order_no) if self.db is not None else None
                tmpl = bot.messages().get("created") if bot else ""
                if bot and o and tmpl:
                    ok = bot.send(tg_user["id"], bot.render(tmpl, o)) is not None
                    self.db.add_event(order_no, f"customer_msg created: {'sent' if ok else 'failed'}", by="api")
            except Exception as ex:
                log(f"заказ {order_no}: сообщение покупателю не ушло ({ex.__class__.__name__})")
        if not self.verify_enabled:
            return
        with self._verify_sem:
            try:
                out = verify_lines(lines)
            except Exception as ex:
                log(f"заказ {order_no}: проверка наличия упала ({ex.__class__.__name__}: {str(ex)[:160]})")
                out = []
            if not out:
                return
            text = f"<b>Проверка наличия · заказ {e(order_no)}</b>\n" + "\n".join(out)
            ok = bool(send_telegram(text, reply_to=reply_to))
            try:
                if self.db is not None:
                    self.db.add_event(order_no, "verify " + ("sent" if ok else "not sent"), by="api")
            except sqlite3.Error:
                pass
            log(f"заказ {order_no}: проверка наличия — {len(out)} строк, telegram {'ok' if ok else 'НЕ ОТПРАВЛЕН'}")

    # ------------------------------------------------------------ копии базы

    def daily_backup(self, now: datetime | None = None, keep_days: int | None = None) -> Path | None:
        """Копия базы за сегодня (если её ещё нет и уже 03:00+) и чистка старых. Путь новой копии или None."""
        now = now or datetime.now()
        if self.db is None or now.hour < 3:
            return None
        folder = self.orders_dir / "backup"
        path = folder / f"orders-{now:%Y%m%d}.sqlite.gz"
        if path.exists():
            return None
        try:
            made = self.db.backup(path)
        except (sqlite3.Error, OSError) as ex:
            log(f"копия базы не сделана: {ex.__class__.__name__}: {str(ex)[:120]}")
            return None
        keep = keep_days if keep_days is not None else int(os.environ.get("ORDER_BACKUP_KEEP_DAYS") or 30)
        orders_db.prune_backups(folder, keep)
        log(f"копия базы заказов: {made.name}")
        return made

    def start_maintenance(self, stop: threading.Event | None = None) -> threading.Thread:
        stop = stop or threading.Event()

        def loop():
            while not stop.is_set():
                self.daily_backup()
                stop.wait(600)
        t = threading.Thread(target=loop, name="orders-backup", daemon=True)
        t.start()
        return t

    def health(self) -> dict:
        self.catalog.refresh()
        return {"ok": True, "products": len(self.catalog.products), "catalog": self.catalog.generated_at,
                "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and orders_chat()),
                "db": self.db is not None, "bot": self.bot is not None}


def make_handler(app: OrderApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "yurt-orders"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 15                                      # медленные клиенты не держат поток

        def log_message(self, fmt, *args):                # без адресов и тел запросов в журнале
            pass

        def client_ip(self) -> str:
            ip = self.client_address[0]
            if app.trust_proxy:
                cf = self.headers.get("CF-Connecting-IP")
                xff = self.headers.get("X-Forwarded-For")
                if cf:
                    ip = cf.strip()
                elif xff:
                    ip = xff.split(",")[-1].strip()
            return ip[:64]

        def cors(self) -> bool | None:
            """True — origin разрешён, False — запрещён, None — заголовка Origin нет."""
            origin = (self.headers.get("Origin") or "").rstrip("/")
            if not origin:
                return None
            if origin in app.origins:
                self._cors_origin = origin
                return True
            return False

        def send_json(self, code: int, data: dict, extra: dict | None = None) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if getattr(self, "_cors_origin", None):
                self.send_header("Access-Control-Allow-Origin", self._cors_origin)
                self.send_header("Vary", "Origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_OPTIONS(self):
            ok = self.cors()
            if not ok or urlparse(self.path).path not in ("/api/order", "/api/health"):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self._cors_origin)
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self.cors()
            if urlparse(self.path).path == "/api/health":
                self.send_json(200, app.health())
            else:
                self.send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            ip = self.client_ip()
            allowed = self.cors()
            if urlparse(self.path).path != "/api/order":
                self.close_connection = True
                return self.send_json(404, {"ok": False, "error": "not found"})
            if allowed is not True:
                self.close_connection = True
                return self.send_json(403, {"ok": False, "error": "origin not allowed"})
            wait = app.attempts_rl.retry_after(ip)
            if wait:
                self.close_connection = True
                return self.send_json(429, {"ok": False, "error": "слишком много запросов", "retry_after": wait},
                                      {"Retry-After": str(wait)})
            app.attempts_rl.add(ip)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in ("application/json", "text/plain"):
                self.close_connection = True
                return self.send_json(415, {"ok": False, "error": "нужен JSON"})
            try:
                length = int(self.headers.get("Content-Length") or "-1")
            except ValueError:
                length = -1
            if length < 0:
                self.close_connection = True
                return self.send_json(411, {"ok": False, "error": "нужен Content-Length"})
            if length > MAX_BODY:
                self.close_connection = True
                if length <= DRAIN_MAX:                   # дочитать: иначе клиент получит обрыв, а не 413
                    try:
                        left = length
                        while left > 0:
                            chunk = self.rfile.read(min(left, 65536))
                            if not chunk:
                                break
                            left -= len(chunk)
                    except OSError:
                        pass
                return self.send_json(413, {"ok": False, "error": "слишком большой запрос"})
            try:
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                return self.send_json(400, {"ok": False, "error": "неверный JSON"})
            try:
                code, data = app.handle_order(payload, ip)
            except Exception as ex:                       # не роняем сервер и не показываем детали наружу
                log(f"ошибка обработки заказа: {ex.__class__.__name__}: {str(ex)[:200]}")
                code, data = 500, {"ok": False, "error": "внутренняя ошибка, напишите нам в Telegram"}
            extra = {"Retry-After": str(data["retry_after"])} if code == 429 and data.get("retry_after") else None
            self.send_json(code, data, extra)

    return Handler


def make_server(app: OrderApp, bind: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind, port), make_handler(app))
    srv.daemon_threads = True
    return srv


def main() -> int:
    ap = argparse.ArgumentParser(description="Приём заказов с сайта → Telegram")
    ap.add_argument("--bind", default=os.environ.get("ORDER_API_BIND") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ORDER_API_PORT") or 8787))
    ap.add_argument("--check", action="store_true", help="проверить настройки и данные")
    ap.add_argument("--test-telegram", action="store_true", help="пробное сообщение в чат заказов")
    ap.add_argument("--no-bot", action="store_true", help="не опрашивать Telegram (кнопки статусов не работают)")
    args = ap.parse_args()
    tp = os.environ.get("ORDER_TRUST_PROXY")
    trust = (tp == "1") if tp in ("0", "1") else args.bind in ("127.0.0.1", "::1", "localhost")
    app = OrderApp(trust_proxy=trust)
    if args.test_telegram:
        ok = send_telegram("<b>Проверка</b>: заказы с сайта будут приходить сюда.")
        print("Отправлено." if ok else "Не отправлено (см. выше).")
        return 0 if ok else 1
    h = app.health()
    log(f"товаров: {h['products']}, закрытых записей: {len(app.catalog.admin)}, telegram: {'да' if h['telegram'] else 'НЕТ'}, "
        f"база: {'да' if h['db'] else 'НЕТ'}, разрешённые сайты: {', '.join(sorted(app.origins))}, "
        f"IP из прокси: {'да' if trust else 'нет'}")
    if args.check:
        warn = tg_bot.channel_admin_warning()
        if warn:
            log("ВНИМАНИЕ: " + warn)
        if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and not app.bot_url("YR-000101-AAAA"):
            log("ВНИМАНИЕ: BOT_USERNAME не задан — сайт не покажет кнопку «Получать статус в Telegram»")
        return 0 if h["products"] and h["telegram"] and h["db"] else 1
    if not args.no_bot:
        # единственный потребитель getUpdates; база — через функцию: открылась позже — бот её подхватит
        app.bot = tg_bot.start_in_thread(db=lambda: app._db())
        if app.bot is None:
            log("бот не запущен: нет TELEGRAM_BOT_TOKEN (кнопки статусов работать не будут)")
    app.start_maintenance()
    srv = make_server(app, args.bind, args.port)
    log(f"слушаю http://{args.bind}:{args.port}  (POST /api/order, GET /api/health)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.bot is not None:
            app.bot.stop_event.set()
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
