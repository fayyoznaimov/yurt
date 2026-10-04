"""Приём заказов с сайта → Telegram владельцу. Работает на вашем сервере (сайт на GitHub Pages статичный,
поэтому токен бота живёт только здесь и никогда не попадает в публичные файлы).

    python order_api.py                    # слушать 127.0.0.1:8787 (за Caddy / Cloudflare Tunnel)
    python order_api.py --check            # проверить настройки и данные, ничего не запуская
    python order_api.py --test-telegram    # пробное сообщение в чат заказов

API:
    POST /api/order   JSON {items:[{id, size, qty}], customer:{name, phone, telegram, city, comment},
                      page_url, lang, website}  (website — ловушка для ботов, должна быть пустой)
                      → {ok:true, order_no, notified, unavailable:[...]} или {ok:false, error}
    GET  /api/health  → {ok:true, products, telegram}
    OPTIONS           CORS preflight (только для ORDER_ALLOWED_ORIGINS)

Цены пересчитываются здесь по site/products.json (цена с сайта клиента не принимается вовсе),
закупочные данные — из site/products-admin.js. Оба файла перечитываются, когда меняются.
Каждый заказ дописывается в data/orders/orders.jsonl (полные контакты — только там) и уходит
в Telegram: TELEGRAM_ORDERS_CHAT_ID (или TELEGRAM_CHAT_ID).

Настройки — переменные окружения (на сервере /etc/yurt/yurt.env):
    TELEGRAM_BOT_TOKEN, TELEGRAM_ORDERS_CHAT_ID (иначе TELEGRAM_CHAT_ID)
    ORDER_ALLOWED_ORIGINS   через запятую; по умолчанию https://fayyoznaimov.github.io
    ORDER_API_PORT          8787        ORDER_API_BIND   127.0.0.1
    ORDER_RATE_LIMIT        5 заказов   ORDER_RATE_WINDOW 600 секунд (на один IP)
    ORDER_TRUST_PROXY       1 — брать IP клиента из CF-Connecting-IP / X-Forwarded-For
                            (по умолчанию 1, если слушаем 127.0.0.1, т.е. стоим за прокси)
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent
MAX_BODY = 20 * 1024
MAX_ITEMS = 30
MAX_QTY = 10
TG_LIMIT = 4096
ID_RE = re.compile(r"^[A-Z0-9]{7}$")
TG_USER_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
PHONE_CHARS_RE = re.compile(r"^[0-9+()\-.\s]{5,25}$")
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
    """Публичные (products.json) и закрытые (products-admin.js) данные; перечитываются при изменении файлов.
    Если файл записан наполовину (идёт сборка) — остаются прежние данные, попытка повторится позже."""

    def __init__(self, site: Path):
        self.public_path = site / "products.json"
        self.admin_path = site / "products-admin.js"
        self.products: dict[str, dict] = {}
        self.admin: dict[str, dict] = {}
        self.generated_at = ""
        self._sig: dict[str, tuple] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _stat(p: Path):
        try:
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def refresh(self) -> None:
        with self._lock:
            sig = self._stat(self.public_path)
            if sig and sig != self._sig.get("public"):
                try:
                    data = json.loads(self.public_path.read_text(encoding="utf-8"))
                    self.products = {p["id"]: p for p in data.get("products", []) if isinstance(p, dict) and p.get("id")}
                    self.generated_at = str((data.get("summary") or {}).get("generated_at") or "")
                    self._sig["public"] = sig
                    log(f"каталог: {len(self.products)} товаров ({self.generated_at})")
                except (OSError, ValueError) as e:
                    log(f"каталог: не прочитан products.json ({e.__class__.__name__}) — оставляю прежний")
            sig = self._stat(self.admin_path)
            if sig and sig != self._sig.get("admin"):
                try:
                    self.admin = _load_admin(self.admin_path.read_text(encoding="utf-8"))
                    self._sig["admin"] = sig
                except (OSError, ValueError) as e:
                    log(f"каталог: не прочитан products-admin.js ({e.__class__.__name__}) — оставляю прежний")

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
    if not s:
        return ""
    if not PHONE_CHARS_RE.match(s):
        raise Invalid("customer.phone: неверный номер")
    digits = re.sub(r"\D", "", s)
    if not 7 <= len(digits) <= 15:
        raise Invalid("customer.phone: неверный номер")
    return ("+" if s.lstrip().startswith("+") else "") + digits


def norm_telegram(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/", "", s.strip()).lstrip("@").strip("/")
    if not TG_USER_RE.match(s):
        raise Invalid("customer.telegram: укажите ник вида @name")
    return s


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
        "city": _text(cust.get("city"), "customer.city", 60),
        "comment": _text(cust.get("comment"), "customer.comment", 500),
    }
    if not (customer["phone"] or customer["telegram"]):
        raise Invalid("customer: укажите телефон или Telegram")
    page_url = _text(payload.get("page_url"), "page_url", 300)
    if page_url and not page_url.startswith(("https://", "http://")):
        page_url = ""
    lang = _text(payload.get("lang"), "lang", 8).lower()[:2]
    return {"items": clean_items, "customer": customer, "page_url": page_url,
            "lang": lang if lang in LANGS else "ru"}


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
            "unavailable": unavailable, "pieces": sum(l["qty"] for l in lines if l["price_uzs"])}


# ---------------------------------------------------------------- сообщение в Telegram

def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def build_message(order_no: str, order: dict, priced: dict, created: datetime) -> str:
    c = order["customer"]
    out = [f"<b>Заказ {e(order_no)}</b>", f"{created:%d.%m.%Y %H:%M} · язык: {e(order['lang'])}", "",
           "<b>Покупатель</b>", f"Имя: {e(c['name'])}"]
    if c["phone"]:
        out.append(f'Телефон: <a href="tel:{e(c["phone"])}">{e(c["phone"])}</a>')
    if c["telegram"]:
        out.append(f'Telegram: <a href="https://t.me/{e(c["telegram"])}">@{e(c["telegram"])}</a>')
    if c["city"]:
        out.append(f"Город: {e(c['city'])}")
    if c["comment"]:
        out.append(f"Комментарий: {e(c['comment'])}")
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
    if priced["cost_uzs"]:
        tot += f"\nСебестоимость {money(priced['cost_uzs'])} · маржа {money(priced['margin_uzs'])} сум"
    out.append(tot)
    if priced["unavailable"]:
        out.append(f"⚠ Проверить наличие: {e(', '.join(priced['unavailable']))}")
    if order["page_url"]:
        out.append(f"Страница: {e(order['page_url'])}")
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


def send_telegram(text: str) -> bool:
    """Отправка в чат заказов. Тесты подменяют эту функцию."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), orders_chat()
    if not (token and chat):
        log("telegram: не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_ORDERS_CHAT_ID)")
        return False
    ok = True
    for part in split_message(text):
        sent = False
        for _ in range(3):
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, data={
                    "chat_id": chat, "text": part, "parse_mode": "HTML", "disable_web_page_preview": "true"})
            except requests.RequestException as ex:
                log(f"telegram: {ex.__class__.__name__}")
                time.sleep(1.5)
                continue
            if r.status_code == 200:
                sent = True
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
    return ok


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
                 rate_window: float | None = None, trust_proxy: bool = False):
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
        self.issued: set[str] = set()

    def new_order_no(self, now: datetime) -> str:
        while True:
            no = f"YR-{now:%y%m%d}-" + "".join(secrets.choice(ALPHABET) for _ in range(4))
            if no not in self.issued:
                self.issued.add(no)
                return no

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

    def handle_order(self, payload, ip: str) -> tuple[int, dict]:
        try:
            order = validate(payload)
        except Invalid as ex:
            return 400, {"ok": False, "error": str(ex)}
        wait = self.orders_rl.retry_after(ip)
        if wait:
            return 429, {"ok": False, "error": "слишком много заказов, попробуйте позже", "retry_after": wait}
        try:
            priced = price_order(order, self.catalog)
        except Invalid as ex:
            return 400, {"ok": False, "error": str(ex)}
        now = datetime.now()
        order_no = self.new_order_no(now)
        self.orders_rl.add(ip)
        record = {"order_no": order_no, "created_at": now.isoformat(timespec="seconds"), "ip": ip_tag(ip),
                  "customer": order["customer"], "lang": order["lang"], "page_url": order["page_url"],
                  "items": priced["lines"], "total_uzs": priced["total_uzs"], "cost_uzs": priced["cost_uzs"],
                  "catalog_generated_at": self.catalog.generated_at}
        self.save(record)
        notified = send_telegram(build_message(order_no, order, priced, now))
        if not notified:
            self.save({"order_no": order_no, "event": "telegram_failed", "at": datetime.now().isoformat(timespec="seconds")})
        log(f"заказ {order_no}: позиций {len(priced['lines'])}, {money(priced['total_uzs'])} сум, "
            f"тел {mask_phone(order['customer']['phone'])}, ip {ip_tag(ip)}, telegram {'ok' if notified else 'НЕ ОТПРАВЛЕН'}")
        return 200, {"ok": True, "order_no": order_no, "notified": notified, "total_uzs": priced["total_uzs"],
                     "unavailable": priced["unavailable"]}

    def health(self) -> dict:
        self.catalog.refresh()
        return {"ok": True, "products": len(self.catalog.products), "catalog": self.catalog.generated_at,
                "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and orders_chat())}


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
        f"разрешённые сайты: {', '.join(sorted(app.origins))}, IP из прокси: {'да' if trust else 'нет'}")
    if args.check:
        return 0 if h["products"] and h["telegram"] else 1
    srv = make_server(app, args.bind, args.port)
    log(f"слушаю http://{args.bind}:{args.port}  (POST /api/order, GET /api/health)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
