"""Товары из партнёрского фида (product feed) — официальный путь «без кнопки» для YOOX (и любого магазина
в партнёрской сети). Заготовка: включается, когда владелец получит доступ к фиду.

У YOOX (проверено 03.10.2026) партнёрская программа — Partnerize (витрины IT/UK/DE/FR, join.partnerize.com/yoox)
и Rakuten Advertising (витрина US, mid 24285); на Awin YOOX закрыт. Колонки по умолчанию — стандартные Awin;
для Partnerize / Rakuten (Rakuten отдаёт файлы с разделителем «|») имена колонок задаются в "columns".
ВАЖНО: условия партнёрских сетей разрешают использовать фид только для ссылок на магазин (покупатель покупает
сам на yoox.com), а не для перепродажи с наценкой — см. README, «Партнёрские фиды».

    config.json → "sources": [..., "feed_yoox"]   (вместо "yoox_import", не вместе с ним — оба дают товары "yoox")
    config.json → "source_opts": {"feed_yoox": {
        "url_env": "YOOX_FEED_URL",     # имя переменной окружения со ссылкой на фид (в ссылке Awin — ваш API-ключ,
                                        # поэтому саму ссылку в публичный config.json не пишем: /etc/yurt/yurt.env)
        "path": "",                     # или локальный файл (csv / csv.gz / zip) — для проверки
        "delimiter": ",",
        "columns": {},                  # свои имена колонок поверх стандартных Awin (см. AWIN_COLUMNS)
        "currency": "EUR",              # если в фиде нет колонки валюты
        "id_regex": "([0-9]{8}[A-Z]{2})",   # код товара YOOX (как у кнопки) — чтобы коды на сайте не поменялись
        "min_rows": 100,                # меньше строк — фид битый, источник считается упавшим (run.py возьмёт прошлые)
        "cache_hours": 6                # скачанный фид живёт в data/feed_yoox.csv.gz столько часов
    }}

Что берётся из строки фида (стандартные колонки Awin; другие сети — через "columns"):
  id, бренд, название, категория, цена (search_price) и цена до скидки (rrp_price / product_price_old),
  валюта, ссылка на товар, фото (merchant_image_url, aw_image_url, alternate_image…), размеры в наличии
  (Fashion:size или size_stock_status), пол (Fashion:suitable_for / custom), цвет, состав, наличие (in_stock).
Фид — весь каталог магазина, поэтому «нет в фиде» = «нет в продаже» (absence_means_gone); защита run.py от
резкого падения числа товаров (sync.max_drop_pct) остаётся.
"""
from __future__ import annotations

import csv
import gzip
import io
import os
import re
import time
import zipfile
from pathlib import Path

import requests

from .base import BROWSER_UA, MAX_IMAGES, Product, Query, classify_type, discount_pct

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "feed_yoox.csv.gz"
LAST_RUN: dict = {}

# наше поле -> колонки фида по порядку предпочтения (первая непустая)
AWIN_COLUMNS: dict[str, list[str]] = {
    "id": ["merchant_product_id", "aw_product_id"],
    "brand": ["brand_name"],
    "title": ["product_name"],
    "category": ["merchant_category", "category_name", "product_type"],
    "price": ["search_price", "store_price", "display_price"],
    "price_old": ["rrp_price", "product_price_old", "base_price"],
    "currency": ["currency"],
    "url": ["merchant_deep_link", "aw_deep_link"],
    "images": ["merchant_image_url", "aw_image_url", "large_image", "alternate_image", "alternate_image_two",
               "alternate_image_three", "alternate_image_four"],
    "sizes": ["Fashion:size", "size", "size_stock_status"],
    "gender": ["Fashion:suitable_for", "suitable_for", "gender", "custom_1"],
    "color": ["colour", "color", "Fashion:colour"],
    "material": ["Fashion:material", "material"],
    "in_stock": ["in_stock", "is_for_sale", "stock_status"],
    "description": ["description", "product_short_description"],
}
GENDER_WORDS = {"men": r"\b(men|male|mens|man|uomo|erkek|homme)\b", "women": r"\b(women|female|womens|woman|donna|kad[ıi]n|femme)\b"}
YES = {"1", "yes", "true", "y", "in stock", "instock", "available", "si", "sì"}


def _open_text(raw: bytes) -> io.TextIOBase:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif raw[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = next(n for n in z.namelist() if not n.endswith("/"))
            raw = z.read(name)
    return io.StringIO(raw.decode("utf-8-sig", errors="replace"), newline="")


def _load(so: dict) -> bytes:
    if so.get("path"):
        return Path(so["path"]).expanduser().read_bytes()
    url = os.environ.get(so.get("url_env") or "YOOX_FEED_URL", "") or so.get("url", "")
    if not url:
        raise RuntimeError("feed_yoox: нет ссылки на фид (переменная окружения из source_opts.feed_yoox.url_env)")
    max_age = float(so.get("cache_hours", 6)) * 3600
    if CACHE.exists() and time.time() - CACHE.stat().st_mtime < max_age:
        return CACHE.read_bytes()
    r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=300)
    r.raise_for_status()
    data = r.content
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_bytes(data if data[:2] == b"\x1f\x8b" else gzip.compress(data))
    return data


def _pick(row: dict, cols: list[str]) -> str:
    for c in cols:
        v = (row.get(c) or "").strip()
        if v:
            return v
    return ""


def _num(s: str) -> float | None:
    s = re.sub(r"[^\d,.\-]", "", s or "")
    if not s:
        return None
    if "," in s and "." in s:                 # 1.234,56 или 1,234.56
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _sizes(s: str) -> list[str]:
    """«S, M, L» / «S|M|L» / «S:in stock;M:out of stock» -> размеры в наличии."""
    out = []
    for part in re.split(r"[|;,/]", s or ""):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            size, status = part.split(":", 1)
            if status.strip().lower() not in YES and not status.strip().isdigit():
                continue
            if status.strip().isdigit() and int(status.strip()) <= 0:
                continue
            part = size.strip()
        if part and part not in out:
            out.append(part)
    return out


def _gender(text: str) -> str | None:
    t = (text or "").lower()
    found = [g for g, rx in GENDER_WORDS.items() if re.search(rx, t)]
    return found[0] if len(found) == 1 else None


def parse_feed(text: io.TextIOBase, so: dict, product_source: str = "yoox") -> list[Product]:
    cols = {k: list(v) for k, v in AWIN_COLUMNS.items()}
    for k, v in (so.get("columns") or {}).items():
        cols[k] = [v] if isinstance(v, str) else list(v)
    id_rx = re.compile(so["id_regex"]) if so.get("id_regex") else None
    reader = csv.DictReader(text, delimiter=so.get("delimiter") or ",")
    out: list[Product] = []
    for row in reader:
        raw_id = _pick(row, cols["id"])
        title = _pick(row, cols["title"])
        price = _num(_pick(row, cols["price"]))
        if not raw_id or not title or price is None:
            continue
        item_id = raw_id
        if id_rx:
            m = id_rx.search(raw_id) or id_rx.search(_pick(row, cols["url"]))
            item_id = m.group(1) if m else raw_id
        stock = _pick(row, cols["in_stock"]).lower()
        sizes = _sizes(_pick(row, cols["sizes"]))
        in_stock = (stock in YES or (stock.isdigit() and int(stock) > 0)) if stock else True
        old = _num(_pick(row, cols["price_old"]))
        category = _pick(row, cols["category"])
        imgs = []
        for c in cols["images"]:
            v = (row.get(c) or "").strip()
            if v.startswith("//"):
                v = "https:" + v
            if v.startswith("http") and v not in imgs:
                imgs.append(v)
        color = _pick(row, cols["color"])
        attrs = {k: v for k, v in (("color", color), ("composition", _pick(row, cols["material"])),
                                   ("feed_category", category)) if v}
        out.append(Product(
            source=product_source, source_item_id=item_id, brand=_pick(row, cols["brand"]) or "?", title=title,
            category=category, type=classify_type(category, title),
            gender=_gender(" ".join([_pick(row, cols["gender"]), category])),
            price_now=price, price_old=old, currency=(_pick(row, cols["currency"]) or so.get("currency") or "EUR").upper(),
            discount_pct=discount_pct(price, old), sizes=sizes, colors=[color] if color else [],
            images=imgs[:MAX_IMAGES], url=_pick(row, cols["url"]), in_stock=in_stock,
            attrs=attrs,
        ))
    return out


def fetch(query: Query, **opts) -> list[Product]:
    LAST_RUN.clear()
    so = dict(query.source_opts or {})
    items = parse_feed(_open_text(_load(so)), so, opts.get("product_source", "yoox"))
    if len(items) < int(so.get("min_rows", 100)):
        raise RuntimeError(f"feed_yoox: в фиде всего {len(items)} товаров — похоже, фид битый или пустой")
    # отсекаем только то, что у товара не меняется (бренд, пол). Скидку и наличие проверяет run.py: иначе товар,
    # у которого просто уменьшилась скидка, выглядел бы «распроданным» (absence_means_gone).
    kept = [p for p in items if query.wants_brand(p.brand)
            and (not query.genders or p.gender in query.genders or p.gender is None)]
    print(f"[feed_yoox] в фиде {len(items)} товаров, подходят по бренду/полу: {len(kept)} "
          f"(в наличии {sum(1 for p in kept if p.in_stock)})")
    LAST_RUN["absence_means_gone"] = True        # фид — весь каталог магазина
    return kept
