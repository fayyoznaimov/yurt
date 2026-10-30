"""escarpe.it (витрина IT группы eobuwie / MODIVO): все товары брендов из config (мужское и женское, без детского),
с проверенными размерами и ценой со страницы товара. Устроено как sources/trendyol.py: выдача → кэш страниц товаров
с ярусами проверки → наружу только товары, у которых проверен хотя бы один размер в наличии.

Как устроено:
  * Сайт — Magento + Nuxt SSR, защиты от ботов нет; перед ним Cloudflare, обычный GET с браузерным User-Agent → 200.
  * Выдача — HTML-страница /c/<раздел>/marca:<бренд>?p=N, 72 карточки на страницу. В странице:
      - JSON-LD data-hid="ldjson-schema-aggregate-offer": Product/AggregateOffer → offerCount (сколько товаров в выдаче),
        lowPrice / highPrice;
      - JSON-LD data-hid="ldjson-schema-collection-page": CollectionPage.mainEntity.ItemList → адреса товаров
        (имена там ПОЛЬСКИЕ — не используются);
      - сами карточки <li data-product-card="N">: адрес /p/<slug>-<цвет>-<13 цифр>, бренд, «Тип · Цвет» по-итальянски,
        цена (и «Prezzo regolare» у товаров со скидкой), фото.
    Цена в карточке — только подсказка для очереди; на сайт идёт цена со страницы товара.
  * Разделы (source_opts.escarpe_it.sections): /c/uomo (мужская обувь), /c/donna (женская обувь),
    /c/abbigliamento/abbigliamento-uomo|-donna (одежда), /c/accessori, /c/borse. Детские (/c/bambini, bambino, bambina,
    abbigliamento-bambini) не обходятся. Пол — по разделу; у товара из мужского и женского раздела сразу — унисекс
    (None); у разделов без пола (аксессуары, сумки) — со страницы товара (audience.suggestedGender, хлебные крошки).
    /c/escarpe/marca:<бренд> (весь магазин, с детским) открывается одной страницей — только чтобы напечатать, сколько
    всего товаров бренда на сайте (root_check).
  * robots.txt (проверено 23.10.2026): Disallow /s/ (поиск), /m-api/ /t-api/ /n-api/ (API — никогда), /c/*%26*,
    /c/*:*:*:* и /c/*:*-*-* (несколько фильтров), /c/*prezzo:* (цена), /*orderDir (сортировка). Разрешены разделы
    с ОДНИМ фильтром (/c/uomo/marca:boss) и ?p=N. Каждый адрес проверяется robots_ok() до запроса.
  * Вежливость: пауза listing_delay (1,5–3 с) между страницами выдачи и pdp_delay (1,2–2 с) между страницами товаров,
    один поток. 403 / 429 / 5xx — остановка (SourceBlocked), без повторов.
  * Страница товара /p/<slug>: JSON-LD ProductGroup (data-hid="ldjson-schema-product") — hasVariant[] по размерам
    ({"size": "40", "offers": {"price": 229.99, "availability": ".../InStock"}}), color, material, brand, category,
    image[] (https://img.eobuwie.cloud/product(…jpg,jpg)/….jpg), audience.suggestedGender; хлебные крошки
    (ldjson-schema-breadcrumbs); блок цены product-price-new («Prezzo regolare» — зачёркнутая цена, если скидка);
    свойства «Dettagli» / «Materiale e cura» (quality-label / quality-value). Описание товара — итальянское.
  * Кэш data/escarpe_pdp_cache.json: {id (13 цифр): {sizes (в наличии), all_sizes, checked_at, price, price_old, gender,
    color, category, category_path, material, attrs, images, name, brand_raw}}; ярусы как у Trendyol
    (trendyol._pdp_plan): горячие (заказы, корзины, верх «Рекомендуем») — раз в pdp_hot_hours, остальные товары сайта —
    раз в pdp_warm_hours, прочая выдача — раз в pdp_cold_hours; за запуск не больше pdp_per_run страниц и не дольше
    pdp_budget_minutes. Проверен и размеров в наличии нет / страница 404 — распродан (LAST_RUN["gone_ids"]).
  * Выдача сохраняется в data/escarpe_listing.json: запуск в режиме pdp берёт её. Раздел, обойдённый не полностью
    (ошибка, max_pages, блокировка), прошлые товары не теряет; товар пропадает из сохранённой выдачи, только если ВСЕ его
    разделы обойдены полностью и его там нет (absence_means_gone — только после полного обхода).

Настройки (config.json → source_opts.escarpe_it):
  brands               {"Boss": ["boss"], "Michael Kors": ["michael_kors", "michael_michael_kors"], …} — обязательно;
                       ключ — каноническое имя бренда на нашем сайте, значение — marca: на escarpe.it (строка или список)
  sections             [{"path": "uomo", "gender": "men"}, {"path": "accessori"}, …] (по умолчанию DEFAULT_SECTIONS)
  max_pages            потолок страниц на одну выдачу (бренд × раздел)
  listing_delay        [от, до] пауза между страницами выдачи, с ([1.5, 3.0])
  pdp_delay            [от, до] пауза между страницами товаров, с ([1.2, 2.0]; число — ±15 %)
  listing_max_age_hours  не обходить выдачу заново, если полный обход свежее (12); listing_window — как у Trendyol
  root_check           печатать, сколько всего товаров бренда на сайте (/c/escarpe/marca:<бренд>, 1 страница; true)
  pdp_per_run, pdp_budget_minutes, pdp_hot_hours, pdp_warm_hours, pdp_cold_hours, pdp_hot_top — как у Trendyol
  pdp_cache_file / listing_file — другие пути к кэшам (для тестов)

Режим запуска (run.py --mode auto|listing|pdp; на сервере — ежечасное задание escarpe в режиме auto):
  auto     выдача, если пора (listing_max_age_hours), + проверка страниц товаров по ярусам;
  listing  только обход выдачи (data/escarpe_listing.json), товаров для сайта не отдаёт;
  pdp      только проверка страниц товаров по сохранённой выдаче.
"""
from __future__ import annotations

import html as _html
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .base import BROWSER_UA, MAX_IMAGES, Product, Query, classify_type, discount_pct, norm_brand
from .trendyol import _age_hours, _pdp_plan, listing_due, tier_hours

if hasattr(sys.stdout, "reconfigure") and (sys.stdout.encoding or "").lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

SOURCE = "escarpe_it"
BASE = "https://escarpe.it"
HOST = "escarpe.it"
PAGE_SIZE = 72
LAST_RUN: dict = {}            # итог последнего fetch() для run.py (см. sources/akinon.py, sources/trendyol.py)

DATA = Path(__file__).resolve().parent.parent / "data"
PDP_CACHE_FILE = DATA / "escarpe_pdp_cache.json"
LISTING_FILE = DATA / "escarpe_listing.json"

DEFAULT_SECTIONS = [
    {"path": "uomo", "gender": "men"},
    {"path": "donna", "gender": "women"},
    {"path": "abbigliamento/abbigliamento-uomo", "gender": "men"},
    {"path": "abbigliamento/abbigliamento-donna", "gender": "women"},
    {"path": "accessori"},
    {"path": "borse"},
]
ROOT_SECTION = "escarpe"                       # весь магазин (с детским) — только для сводки root_check
KIDS_PATH_RE = re.compile(r"(^|/)(bambin[ioa]|abbigliamento-bambini|sport/bambini)(/|$)")
KIDS_RE = re.compile(r"bambin|ragazz|neonat|\bkids?\b|junior|\bbaby\b")

# robots.txt escarpe.it (Disallow, синтаксис Google: * — любые символы); проверено 23.10.2026
ROBOTS_DISALLOW = ("/s/", "/m-api/", "/t-api/", "/n-api/", "/c/*%26*", "/c/*:*:*:*", "/c/*:*-*-*", "/c/*prezzo:*",
                   "/*orderDir")
_ROBOTS_RE = [re.compile("^" + ".*".join(re.escape(x) for x in p.split("*"))) for p in ROBOTS_DISALLOW]

_ID_RE = re.compile(r"-(\d{13})/?$")
_SLUG_RE = re.compile(r"^[a-z0-9_]+$")
_LD_RE = re.compile(r'<script([^>]*)type="application/ld\+json"([^>]*)>(.*?)</script>', re.S)
_HID_RE = re.compile(r'data-hid="([^"]+)"')
_NUM_RE = re.compile(r"^\s*\d[\d.\s]*(?:,\d{1,2})?\s*(?:€)?\s*$")
_TAG_RE = re.compile(r"<[^>]+>")
_SR_ONLY_RE = re.compile(r'<span class="sr-only"[^>]*>.*?</span>', re.S)
_QUALITY_RE = re.compile(r'<span class="quality-label"[^>]*>(.*?)</span>(.*?)</div>', re.S)
_REGULAR_RE = re.compile(r'Prezzo regolare\s*<span class="price"[^>]*>\s*<span class="value"[^>]*>([^<]+)</span>', re.S)
_PRICE_WRAP_RE = re.compile(r'class="price-wrapper[^"]*"[^>]*>(.*?)</div>', re.S)
_SPAN_RE = re.compile(r"<span[^>]*>([^<]*)</span>")
IN_STOCK = ("instock", "limitedavailability", "onlineonly", "instoreonly")
ONE_SIZE_RE = re.compile(r"^(taglia unica|one ?size|unica|uni|os|tu|ns)$", re.I)
_FRACTIONS = {"1/3": "⅓", "1/2": "½", "2/3": "⅔"}

# Свойства страницы товара, которые не нужны (служебные / уже есть отдельно) — в attrs не кладутся.
ATTR_SKIP = {"marca", "nome prodotto", "index", "codice prodotto", "colore del produttore", "ean"}

# Тип товара по ГЛАВНОМУ слову итальянского названия категории («Stivaletti», «Camicia di jeans», «Pochette per
# cosmetici»): первое слово решает, остальное — уточнения. Порядок важен (pantaloncini раньше pantaloni).
# classify_type (base.py) путает часть итальянских слов (pantaloncini — «брюки», chelsea / polacchine не знает),
# поэтому сначала этот список, потом classify_type.
HEAD_TYPES = [
    ("pantaloncin", "шорты"), ("bermuda", "шорты"), ("shorts", "шорты"),
    ("stivalett", "обувь"), ("stival", "обувь"), ("polacchin", "обувь"), ("chelsea", "обувь"), ("anfibi", "обувь"),
    ("scarponcin", "обувь"), ("sneaker", "обувь"), ("scarpe", "обувь"), ("scarpa", "обувь"), ("mocassin", "обувь"),
    ("loafer", "обувь"), ("francesin", "обувь"), ("decollet", "обувь"), ("sandal", "обувь"), ("ciabatt", "обувь"),
    ("infradito", "обувь"), ("espadrill", "обувь"), ("pantofol", "обувь"), ("ballerin", "обувь"),
    ("zoccol", "обувь"), ("sabot", "обувь"), ("slip-on", "обувь"), ("stringat", "обувь"), ("mules", "обувь"),
    ("derby", "обувь"), ("oxford", "обувь"), ("doposci", "обувь"), ("wellington", "обувь"),
    ("borsellin", "аксессуары"),
    ("borsett", "сумки"), ("borson", "сумки"), ("borsa", "сумки"), ("borse", "сумки"), ("valigett", "сумки"),
    ("zain", "сумки"), ("marsupi", "сумки"), ("pochette", "сумки"), ("shopper", "сумки"), ("clutch", "сумки"),
    ("valigi", "сумки"), ("trolley", "сумки"), ("beauty", "сумки"), ("necessaire", "сумки"),
    ("portafogl", "аксессуары"), ("portamonet", "аксессуары"), ("portacart", "аксессуары"),
    ("portachiav", "аксессуары"), ("portadocument", "сумки"), ("cintur", "аксессуары"), ("sciarp", "аксессуары"),
    ("foulard", "аксессуары"), ("cappell", "аксессуары"), ("berrett", "аксессуары"), ("guant", "аксессуары"),
    ("occhial", "аксессуары"), ("orolog", "аксессуары"), ("cravatt", "аксессуары"), ("papillon", "аксессуары"),
    ("ombrell", "аксессуары"), ("braccial", "аксессуары"), ("collan", "аксессуары"), ("orecchin", "аксессуары"),
    ("anell", "аксессуары"), ("gemell", "аксессуары"), ("ciondol", "аксессуары"), ("basco", "аксессуары"),
    ("balaclava", "аксессуары"), ("visier", "аксессуары"), ("paraorecchie", "аксессуары"), ("fascia", "аксессуары"),
    ("etichetta", "аксессуары"), ("custodi", "аксессуары"),
    ("calzin", "нижнее бельё"), ("calze", "нижнее бельё"), ("pedulin", "нижнее бельё"), ("boxer", "нижнее бельё"),
    ("slip", "нижнее бельё"), ("mutand", "нижнее бельё"), ("perizom", "нижнее бельё"), ("culotte", "нижнее бельё"),
    ("reggiseno", "нижнее бельё"), ("pigiam", "нижнее бельё"), ("accappatoi", "нижнее бельё"),
    ("felpa", "толстовки"), ("felpe", "толстовки"),
    ("t-shirt", "футболки и поло"), ("tshirt", "футболки и поло"), ("maglietta", "футболки и поло"),
    ("longsleeve", "футболки и поло"),
    ("polo", "футболки и поло"), ("canott", "футболки и поло"), ("top", "футболки и поло"),
    ("camici", "рубашки"), ("blusa", "рубашки"),
    ("jeans", "джинсы"),
    ("pantalon", "брюки"), ("leggings", "брюки"), ("jogger", "брюки"), ("chino", "брюки"),
    ("maglion", "свитеры и кардиганы"), ("maglia", "свитеры и кардиганы"), ("cardigan", "свитеры и кардиганы"),
    ("pullover", "свитеры и кардиганы"), ("dolcevita", "свитеры и кардиганы"), ("lupetto", "свитеры и кардиганы"),
    ("giacc", "куртки и пальто"), ("giubbott", "куртки и пальто"), ("giubbin", "куртки и пальто"),
    ("piumin", "куртки и пальто"), ("cappott", "куртки и пальто"), ("parka", "куртки и пальто"),
    ("gilet", "куртки и пальто"), ("smanicat", "куртки и пальто"), ("impermeabil", "куртки и пальто"),
    ("trench", "куртки и пальто"), ("bomber", "куртки и пальто"), ("mantell", "куртки и пальто"),
    ("blazer", "пиджаки и костюмы"), ("complet", "пиджаки и костюмы"),
    ("vestit", "платья"), ("abito", "платья"), ("tuta intera", "платья"), ("jumpsuit", "платья"),
    ("gonn", "юбки"),
]
# «Porta …» / «Custodia per …» — по второму слову; «Tuta» (спортивный костюм), «Costume» (купальник / плавки),
# «Body», «Set regalo» — тип не придумываем; «Set di X» — тип X
_PORTA_TYPES = [(re.compile(r"^(porta|custodi\w*(?: per)?) (pc|computer|laptop|tablet|document|trucchi)"), "сумки"),
                (re.compile(r"^(porta|custodi\w*(?: per)?) (carte|card|tessere|monete|chiavi|occhiali|passaporto)"),
                 "аксессуары"),
                (re.compile(r"\bdel pigiama\b|\bda notte\b|\bintim[oi]\b"), "нижнее бельё")]
_NO_TYPE_HEADS = ("tuta", "costume", "set", "body")
_SWIM_RE = re.compile(r"(pantaloncin|boxer|short)\w* da (bagno|mare)")


class SourceBlocked(RuntimeError):
    """escarpe.it ответил 403 / 429 / 5xx — дальше в этот запуск не ходим."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data) -> None:
    """Через временный файл: при сбое посередине старый файл остаётся целым."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _clean(s) -> str:
    """Текст со страницы: без тегов, сущностей HTML, невидимых символов (в material бывает \\ufeff) и лишних пробелов."""
    s = _html.unescape(_TAG_RE.sub(" ", str(s or "")))
    s = s.replace("﻿", "").replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _fold(s) -> str:
    """«Décolleté» -> «decollete»: нижний регистр без диакритики."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s.lower()).strip()


def _eur(v) -> float | None:
    """229.99 / "229,99 €" / "1.229,99" -> float."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2) or None
    except ValueError:
        return None


def robots_ok(url: str) -> bool:
    """Можно ли запрашивать адрес по robots.txt escarpe.it (только этот сайт)."""
    u = urlsplit(url if "://" in url else BASE + url)
    if (u.hostname or "").lower() not in (HOST, "www." + HOST):
        return False
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    return not any(rx.match(path) for rx in _ROBOTS_RE)


def listing_url(section: str, slug: str, page: int = 1) -> str:
    return f"{BASE}/c/{section.strip('/')}/marca:{slug}" + (f"?p={page}" if page > 1 else "")


def item_id(url: str) -> str | None:
    m = _ID_RE.search(urlsplit(str(url or "")).path)
    return m.group(1) if m else None


def _image(url: str) -> str:
    """Карточка (productcard, 280 px) или webp -> фото товара 560 px в jpg (как в JSON-LD страницы товара)."""
    u = str(url or "").strip()
    u = re.sub(r"/product(?:card|card_stylization)\(", "/product(", u)
    u = u.replace(",webp)", ",jpg)")
    if u.endswith(".webp"):
        u = u[:-5] + ".jpg"
    return u


def _size_label(v) -> str:
    s = _clean(v)
    if ONE_SIZE_RE.match(s):
        return "one size"
    for k, sym in _FRACTIONS.items():                 # «41 1/3» -> «41 ⅓» (так их понимает sizes_norm)
        s = re.sub(rf"(\d)\s*{re.escape(k)}$", rf"\1 {sym}", s)
    return s


def _brand_matches(raw: str | None, slugs) -> bool:
    """Бренд с сайта («BOSS», «MICHAEL Michael Kors») и marca:-слаг («michael_michael_kors») — один бренд?
    Пустой бренд (формат сайта поменялся) не мешает — проверять нечем."""
    words = set(re.findall(r"[a-z0-9]+", _fold(raw)))
    if not words or not slugs:
        return True
    return any(words & set(str(s).split("_")) for s in slugs)


def head_type(text: str | None) -> str | None:
    """Тип по главному (первому) слову итальянской категории; None — не знаем."""
    t = _fold(text)
    if not t:
        return None
    if _SWIM_RE.search(t):
        return "шорты"
    for rx, typ in _PORTA_TYPES:
        if rx.search(t):
            return typ
    head = t.split()[0]
    if head == "set" and len(t.split()) > 1:              # «Set di boxer», «Set orologio e braccialetto»
        rest = re.sub(r"^set (di |da )?", "", t)
        return head_type(rest) if rest and not rest.startswith("regalo") else None
    if head in _NO_TYPE_HEADS and not t.startswith("tuta intera"):
        return None
    for prefix, typ in HEAD_TYPES:
        if " " in prefix:
            if t.startswith(prefix):
                return typ
        elif head.startswith(prefix):
            return typ
    return None


def type_of(*texts: str | None) -> str | None:
    """Тип товара: главное слово первой подходящей категории (самая точная — первой), потом classify_type."""
    for t in texts:
        typ = head_type(t)
        if typ:
            return typ
    if any(_fold(t).split()[:1] in (["tuta"], ["costume"]) for t in texts if t):
        return None                                   # спортивный костюм / купальник — тип не угадываем
    return classify_type(*[_fold(t) for t in texts if t])


# ---------- сеть ----------

class _Client:
    """Сессия с браузерным UA и паузой перед каждым запросом, кроме первого. Только адреса, разрешённые robots.txt."""

    def __init__(self, delay: tuple[float, float] = (1.5, 3.0)) -> None:
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
        })
        self.delay = (float(delay[0]), float(max(delay[0], delay[-1])))
        self.requests = 0

    def get(self, url: str) -> requests.Response:
        if not robots_ok(url):
            raise ValueError(f"robots.txt escarpe.it запрещает {url} — не запрашиваю")
        if self.requests:
            time.sleep(random.uniform(*self.delay))
        self.requests += 1
        r = self.s.get(url, timeout=30, allow_redirects=False)
        if r.status_code in (403, 429) or r.status_code >= 500:
            raise SourceBlocked(f"escarpe.it ответил {r.status_code} на {url} — останавливаюсь, без повторов")
        r.encoding = "utf-8"
        return r


# ---------- разбор выдачи ----------

def _ld_blocks(html: str) -> list[tuple[str, object]]:
    """[(data-hid, объект JSON-LD)] со страницы."""
    out = []
    for m in _LD_RE.finditer(html):
        hid = _HID_RE.search(m.group(1) + m.group(2))
        try:
            obj = json.loads(m.group(3))
        except ValueError:
            continue
        out.append((hid.group(1) if hid else "", obj))
    return out


def _ld(blocks, hid: str, types: tuple[str, ...]) -> dict | None:
    for h, obj in blocks:
        if h == hid and isinstance(obj, dict):
            return obj
    for _h, obj in blocks:
        if isinstance(obj, dict) and obj.get("@type") in types:
            return obj
    return None


def _card(chunk: str) -> dict | None:
    """Одна карточка выдачи: {id, path, brand, type, color, price, price_old, image, name, sponsored}."""
    chunk = chunk[: chunk.find("</li>")] if "</li>" in chunk else chunk
    href = re.search(r'<a href="(/p/[^"?#]+)"', chunk)
    if not href:
        return None
    cid = item_id(href.group(1))
    if not cid:
        return None
    brand = re.search(r'<div class="title"[^>]*>(.*?)</div>', chunk, re.S)
    desc = re.search(r'<div class="description"[^>]*>(.*?)</div>', chunk, re.S)
    typ, color = None, None
    if desc:
        parts = [p.strip() for p in _clean(desc.group(1)).split("·")]
        typ = parts[0] or None
        color = parts[1] if len(parts) > 1 and parts[1] else None
    now = None
    pw = _PRICE_WRAP_RE.search(chunk)
    if pw:
        for sp in _SPAN_RE.findall(pw.group(1)):
            if _NUM_RE.match(sp):
                now = _eur(sp)
                break
    old = _REGULAR_RE.search(chunk)
    img = re.search(r'<img[^>]+src="(https://img\.eobuwie\.cloud/[^"]+)"', chunk)
    alt = re.search(r'<img[^>]+alt="([^"]*)"', chunk)
    return {"id": cid, "path": href.group(1), "brand": _clean(brand.group(1)) if brand else None,
            "type": typ, "color": color, "price": now, "price_old": _eur(old.group(1)) if old else None,
            "image": _image(img.group(1)) if img else None, "name": _clean(alt.group(1)) if alt else None,
            "sponsored": bool(re.search(r'class="product-card-small[^"]*\bsponsored\b', chunk))}


def parse_listing(html: str) -> dict:
    """Страница выдачи -> {offer_count, low, high, url, urls (ItemList по порядку), cards, canonical, last_page}."""
    blocks = _ld_blocks(html)
    agg = _ld(blocks, "ldjson-schema-aggregate-offer", ())
    if agg is None:
        agg = next((o for _h, o in blocks if isinstance(o, dict)
                    and (o.get("offers") or {}).get("@type") == "AggregateOffer"), None)
    offers = (agg or {}).get("offers") or {}
    try:
        offer_count = int(offers["offerCount"]) if offers.get("offerCount") is not None else None
    except (TypeError, ValueError):
        offer_count = None
    coll = _ld(blocks, "ldjson-schema-collection-page", ("CollectionPage",)) or {}
    urls = []
    for el in ((coll.get("mainEntity") or {}).get("itemListElement") or []):
        u = ((el or {}).get("item") or {}).get("@id") if isinstance(el, dict) else None
        if isinstance(u, str) and item_id(u):
            urls.append(u)
    cards = [c for c in (_card(ch) for ch in html.split('<li data-product-card="')[1:]) if c]
    canon = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', html)
    pages = [int(p) for p in re.findall(r'href="/c/[^"?]+\?p=(\d+)"', html)]
    return {"offer_count": offer_count, "low": _eur(offers.get("lowPrice")), "high": _eur(offers.get("highPrice")),
            "url": (agg or {}).get("url"), "urls": urls, "cards": cards,
            "canonical": canon.group(1) if canon else None, "last_page": max(pages) if pages else None}


# ---------- разбор страницы товара ----------

def _offers(obj) -> list[dict]:
    o = obj.get("offers") if isinstance(obj, dict) else None
    if isinstance(o, dict):
        return [o]
    return [x for x in (o or []) if isinstance(x, dict)]


def _available(offer: dict) -> bool:
    a = str(offer.get("availability") or "").rsplit("/", 1)[-1].lower()
    return a in IN_STOCK


def _qualities(html: str) -> dict[str, str]:
    """Свойства «Dettagli» и «Materiale e cura» (без «Informazioni sul produttore» — адреса и контакты производителя)."""
    i = html.find('class="product-qualities')
    if i < 0:
        return {}
    j = html.find("Informazioni sul produttore", i)
    region = html[i: j if j > 0 else i + 60000]
    out: dict[str, str] = {}
    for m in _QUALITY_RE.finditer(region):
        key = _clean(m.group(1)).rstrip(":").strip()
        val = _clean(_SR_ONLY_RE.sub(" ", m.group(2)))
        if key and val and _fold(key) not in ATTR_SKIP and key not in out:
            out[key] = val
    return out


_REGULAR_TXT_RE = re.compile(r"Prezzo regolare[^0-9]{0,60}?(\d[\d.]*,\d{2})")


def _pdp_prices(html: str) -> tuple[float | None, float | None]:
    """(цена в блоке цены, «Prezzo regolare» — зачёркнутая) со страницы товара. Ищется только в блоке самого товара
    (от product-price-new до панелей с описанием — ниже идут карточки рекомендаций со своими ценами): по разметке
    omnibus, как у карточек выдачи, или по тексту «Prezzo regolare … 159,95 €», или в зачёркнутой цене."""
    i = html.find('class="product-price-new"')
    if i < 0:
        return None, None
    j = html.find('class="product-panels"', i)
    region = html[i: j if 0 < j - i < 60000 else i + 20000]
    now = None
    pw = _PRICE_WRAP_RE.search(region)
    for sp in (_SPAN_RE.findall(pw.group(1)) if pw else []):
        if _NUM_RE.match(sp):
            now = _eur(sp)
            break
    old = _REGULAR_RE.search(region) or _REGULAR_TXT_RE.search(_clean(region))
    if old:
        return now, _eur(old.group(1))
    prev = re.search(r'class="price-previous-wrapper[^"]*"[^>]*>(.*?)</div>', region, re.S)
    for sp in (_SPAN_RE.findall(prev.group(1)) if prev else []):
        if _NUM_RE.match(sp):
            return now, _eur(sp)
    return now, None


def pdp_gender(audience: str | None, crumbs: list[str], meta_category: str | None) -> tuple[str | None, bool]:
    """(пол "men" | "women" | "kids" | None, унисекс ли) по audience.suggestedGender и хлебным крошкам."""
    text = _fold(" / ".join([*crumbs, meta_category or ""]))
    a = _fold(audience)
    if KIDS_RE.search(text) or a in ("kids", "child", "children", "boys", "girls", "boy", "girl"):
        return "kids", False
    if a in ("male", "men", "man", "uomo"):
        return "men", False
    if a in ("female", "women", "woman", "donna"):
        return "women", False
    if a == "unisex":
        return None, True
    men, women = bool(re.search(r"\buomo\b", text)), bool(re.search(r"\bdonna\b", text))
    if men != women:
        return ("men" if men else "women"), False
    return None, False


def parse_pdp(html: str) -> dict | None:
    """Страница товара -> {id, name, brand_raw, sizes (в наличии), all_sizes, price, price_old, color, material,
    category, category_path, gender, unisex, images, attrs}. None — на странице нет JSON-LD товара."""
    blocks = _ld_blocks(html)
    prod = _ld(blocks, "ldjson-schema-product", ("ProductGroup", "Product"))
    if not prod or prod.get("@type") not in ("ProductGroup", "Product"):
        return None
    variants = [v for v in (prod.get("hasVariant") or []) if isinstance(v, dict)]
    sizes, all_sizes, in_prices, prices = [], [], [], []
    for v in variants:
        size = _size_label(v.get("size") or "") or "one size"
        offers = _offers(v)
        ok = any(_available(o) for o in offers)
        p = [x for x in (_eur(o.get("price")) for o in offers) if x]
        prices += p
        if size not in all_sizes:
            all_sizes.append(size)
        if ok:
            in_prices += p
            if size not in sizes:
                sizes.append(size)
    if not variants:                                   # безразмерный товар — предложение у самого товара
        offers = _offers(prod)
        prices = [x for x in (_eur(o.get("price")) for o in offers) if x]
        if offers:
            all_sizes = ["one size"]
            if any(_available(o) for o in offers):
                sizes, in_prices = ["one size"], prices
    meta_price = re.search(r'property="product:price:amount" content="([^"]+)"', html)
    block_now, block_old = _pdp_prices(html)
    price = min(in_prices) if in_prices else (min(prices) if prices else (block_now or _eur(meta_price.group(1) if meta_price else None)))
    if block_now and block_old and price and block_now < price:
        price = block_now                                # видимая скидка, которой нет в JSON-LD
    old = block_old if block_old and price and block_old > price else None
    crumbs_ld = _ld(blocks, "ldjson-schema-breadcrumbs", ("BreadcrumbList",)) or {}
    crumbs = []
    for el in crumbs_ld.get("itemListElement") or []:
        it = (el or {}).get("item") or {} if isinstance(el, dict) else {}
        if isinstance(it, dict) and it.get("name") and "/p/" not in str(it.get("@id") or ""):
            crumbs.append(_clean(it["name"]))
    meta_cat = re.search(r'property="product:category" content="([^"]+)"', html)
    meta_cat = _clean(meta_cat.group(1)) if meta_cat else None
    aud = (prod.get("audience") or {}).get("suggestedGender") if isinstance(prod.get("audience"), dict) else None
    gender, unisex = pdp_gender(aud, crumbs, meta_cat)
    images = []
    for im in prod.get("image") or []:
        u = im.get("url") if isinstance(im, dict) else im
        if isinstance(u, str) and u.startswith("https://") and _image(u) not in images:
            images.append(_image(u))
    brand = prod.get("brand")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    attrs = _qualities(html)
    material = _clean(prod.get("material")) or None
    return {
        "id": str(prod.get("productID") or prod.get("sku") or "") or None,
        "name": _clean(prod.get("name")) or None,
        "brand_raw": _clean(brand) or None,
        "sizes": sizes, "all_sizes": all_sizes, "in_stock": bool(sizes),
        "price": price, "price_old": old,
        "color": _clean(prod.get("color")) or attrs.get("Colore") or None,
        "material": material,
        "category": _clean(prod.get("category")) or None,
        "category_path": "/".join(crumbs) or None,
        "gender": gender, "gender_raw": aud, "unisex": unisex,
        "images": images[:MAX_IMAGES],
        "attrs": attrs,
    }


def pdp_details(client: _Client, url: str, info: dict | None = None) -> dict | None:
    """Страница товара (одно перенаправление на другую страницу товара — следуем). info["status"] — HTTP-код,
    info["redirect"] — адрес, если увело не на товар (товара больше нет)."""
    info = info if info is not None else {}
    want = item_id(url)
    r = client.get(url)
    info["status"] = r.status_code
    moved = None
    if r.is_redirect:
        moved = urljoin(url, r.headers.get("location") or "")
        if "/p/" not in urlsplit(moved).path or not robots_ok(moved):
            info["redirect"] = moved
            return None
        r = client.get(moved)
        info["status"] = r.status_code
        if r.is_redirect:
            info["redirect"] = urljoin(moved, r.headers.get("location") or "")
            return None
    if r.status_code != 200:
        return None
    d = parse_pdp(r.text)
    if d is None:
        info["parse_error"] = True
    elif moved and want and d.get("id") and d["id"] != want:
        info["redirect"] = moved                     # увело на другой товар — этого больше нет
        return None
    return d


def _cache_entry(d: dict) -> dict:
    return {"sizes": d["sizes"], "all_sizes": d["all_sizes"], "checked_at": _now_iso(), "price": d["price"],
            "price_old": d["price_old"], "gender": d["gender"], "unisex": d["unisex"], "color": d["color"],
            "material": d["material"], "category": d["category"], "category_path": d["category_path"],
            "attrs": d["attrs"], "images": d["images"], "name": d["name"], "brand_raw": d["brand_raw"]}


# ---------- обход выдачи ----------

def parse_brands(so: dict) -> dict[str, list[str]]:
    """source_opts.brands -> {каноническое имя: [marca:-слаги]} (строка или список; плохие слаги отброшены)."""
    out: dict[str, list[str]] = {}
    for name, slugs in (so.get("brands") or {}).items():
        if str(name).startswith("_"):
            continue
        lst = [slugs] if isinstance(slugs, str) else list(slugs or [])
        good = [str(s).strip().lower() for s in lst if _SLUG_RE.match(str(s).strip().lower())]
        bad = [s for s in lst if not _SLUG_RE.match(str(s).strip().lower())]
        if bad:
            print(f"[escarpe] ВНИМАНИЕ: у бренда {name!r} слаги {bad} не похожи на marca:<слаг> — пропускаю их")
        if good:
            out[str(name)] = good
    return out


def parse_sections(so: dict) -> list[dict]:
    """source_opts.sections -> [{"path", "gender"}]; детские разделы отбрасываются (детское не берём)."""
    raw = so.get("sections") or DEFAULT_SECTIONS
    out = []
    for s in raw:
        sec = {"path": s} if isinstance(s, str) else dict(s or {})
        path = str(sec.get("path") or "").strip("/")
        if not path or not re.fullmatch(r"[a-z0-9-]+(/[a-z0-9-]+)*", path):
            print(f"[escarpe] ВНИМАНИЕ: раздел {s!r} не похож на путь /c/<раздел> — пропускаю")
            continue
        if KIDS_PATH_RE.search(path):
            print(f"[escarpe] раздел {path}: детское не берём — пропускаю")
            continue
        g = sec.get("gender")
        out.append({"path": path, "gender": g if g in ("men", "women") else None})
    return out


def sec_key(slug: str, path: str) -> str:
    return f"{slug}@{path}"


class _Crawl:
    """Обход выдачи одного запуска: бренд × слаг × раздел, до max_pages страниц."""

    def __init__(self, client: _Client, max_pages: int):
        self.client, self.max_pages = client, max(1, int(max_pages or 1))
        self.items: dict[str, dict] = {}
        self.stats: dict[str, dict] = {}        # sec_key -> {brand, slug, path, offer_count, pages, …, complete}
        self.errors = 0
        self.truncated = False
        self.skipped = Counter()
        self.sponsored = 0

    def _add(self, card: dict, brand: str, slug: str, sec: dict) -> bool:
        """Карточка в выдачу. Рекламная («sponsored») карточка того же бренда — тоже товар бренда (бывает, что только
        она и есть на странице), но могла прийти из другого раздела: пол у неё — со страницы товара, не по разделу."""
        cid = card["id"]
        if not _brand_matches(card.get("brand"), [slug]):
            self.skipped[f"чужой бренд в выдаче ({card.get('brand')})"] += 1
            return False
        sponsored = bool(card.get("sponsored"))
        self.sponsored += sponsored
        sec_gender = None if sponsored else sec["gender"]
        key = sec_key(slug, sec["path"])
        rec = self.items.get(cid)
        if rec is None:
            name = card.get("name") or ""
            if card.get("brand") and name.lower().startswith(card["brand"].lower() + " "):
                name = name[len(card["brand"]) + 1:]       # alt фото: «BOSS Stivaletti BOSS Tayil …» -> «Stivaletti BOSS …»
            now, old = card.get("price"), card.get("price_old")
            disc = discount_pct(now, old)
            p = Product(source=SOURCE, source_item_id=cid, brand=brand, title=name or (card.get("type") or ""),
                        category=card.get("type") or "", type=type_of(card.get("type")), gender=sec_gender,
                        price_now=float(now or 0), price_old=old if disc is not None else None, currency="EUR",
                        discount_pct=disc, sizes=[], colors=[card["color"]] if card.get("color") else [],
                        images=[card["image"]] if card.get("image") else [], url=BASE + card["path"])
            rec = p.to_dict()
            rec.update(_sections=[], _genders=[], _seen_at=_now_iso(), _brand_raw=card.get("brand"),
                       _card_price=now, _card_price_old=old, _slug=slug)
            self.items[cid] = rec
        if key not in rec["_sections"]:
            rec["_sections"].append(key)
        if sec_gender and sec_gender not in rec["_genders"]:
            rec["_genders"].append(sec_gender)
        if len(rec["_genders"]) == 1:
            rec["gender"] = rec["_genders"][0]
        elif len(rec["_genders"]) > 1:
            rec["gender"] = None                            # в мужском и женском разделе — унисекс
        return True

    def section(self, brand: str, slug: str, sec: dict) -> dict:
        """Все страницы одной выдачи. complete — дошли до последней страницы без ошибок."""
        key = sec_key(slug, sec["path"])
        st = {"brand": brand, "slug": slug, "path": sec["path"], "offer_count": None, "pages": 0, "pages_total": None,
              "seen": 0, "complete": False, "reason": ""}
        self.stats[key] = st
        page, seen, t0 = 1, set(), time.time()
        while True:
            url = listing_url(sec["path"], slug, page)
            try:
                r = self.client.get(url)
            except SourceBlocked:
                st["reason"] = "доступ ограничен"
                raise
            except Exception as e:
                self.errors += 1
                st["reason"] = f"стр. {page}: {e.__class__.__name__}"
                print(f"[escarpe] {url}: ошибка {e.__class__.__name__}: {e} — выдача прервана")
                break
            if r.status_code in (404, 410) and page == 1:
                st.update(offer_count=0, pages_total=0, complete=True, reason="404 — товаров бренда в разделе нет")
                break
            if page > 1 and page > (st["pages_total"] or 0) and (r.status_code in (404, 410) or r.is_redirect):
                st.update(complete=True, reason=f"стр. {page}: дальше выдачи нет")   # лишняя страница за счётчиком
                break
            if r.is_redirect or r.status_code != 200:
                self.errors += 1
                st["reason"] = f"стр. {page}: HTTP {r.status_code} {r.headers.get('location') or ''}".strip()
                print(f"[escarpe] {url}: {st['reason']} — выдача прервана (прошлые товары раздела остаются)")
                break
            data = parse_listing(r.text)
            if page == 1:
                # та ли это выдача: адрес страницы (canonical / AggregateOffer.url) — тот же раздел и тот же бренд
                urls = [u for u in (data.get("canonical"), data.get("url")) if u]
                if urls and not any(f"marca:{slug}" in u and f"/c/{sec['path']}/" in urlsplit(u).path + "/"
                                    for u in urls):
                    self.errors += 1
                    st["reason"] = f"сайт отдал другую выдачу ({urls[0]})"
                    print(f"[escarpe] {url}: {st['reason']} — пропускаю раздел")
                    return st
                n = data.get("offer_count")
                if n is None and not data["cards"] and not data["urls"]:
                    st.update(offer_count=0, pages_total=0, complete=True, reason="товаров нет")
                    break
                st["offer_count"] = n
                st["pages_total"] = math.ceil(n / PAGE_SIZE) if n is not None else data.get("last_page")
            st["pages"] = page
            before = len(seen)
            for c in data["cards"]:
                if self._add(c, brand, slug, sec):
                    seen.add(c["id"])
            if not data["cards"] and data["urls"]:            # карточки не разобрались — хотя бы адреса из JSON-LD
                for u in data["urls"]:
                    cid = item_id(u)
                    c = {"id": cid, "path": urlsplit(u).path, "brand": None, "type": None, "color": None,
                         "price": None, "price_old": None, "image": None, "name": None, "sponsored": False}
                    if self._add(c, brand, slug, sec):
                        seen.add(cid)
            n_cards = len(data["cards"]) or len(data["urls"])
            total_pages = st["pages_total"]
            if n_cards == 0 and page == 1:
                if not st["offer_count"]:
                    st.update(offer_count=0, pages_total=0, complete=True, reason="товаров нет")
                else:
                    self.errors += 1
                    st["reason"] = f"на сайте {st['offer_count']}, а карточек на странице нет — формат сайта изменился?"
                break
            if page > 1 and (n_cards == 0 or len(seen) == before):
                if total_pages is not None and page <= total_pages:
                    self.errors += 1
                    st["reason"] = f"стр. {page} из {total_pages} пустая / без новых товаров"
                else:
                    st.update(complete=True, reason=f"стр. {page}: новых товаров нет — выдача кончилась")
                break
            # На странице бывает 67–71 карточка вместо 72 (часть мест — реклама), поэтому «короткая страница —
            # последняя» верно только когда число страниц неизвестно; при известном счётчике идём до конца по нему.
            if total_pages is None and n_cards < PAGE_SIZE:
                st["complete"] = True
                break
            if total_pages is not None and page >= total_pages:
                if page > total_pages or len(seen) >= (st["offer_count"] or 0):
                    st["complete"] = True
                    break
                # на последней странице по счётчику собрано меньше обещанного — заглянуть на одну дальше
                # (404 / перенаправление там = конец выдачи, см. выше)
            if page >= self.max_pages:
                self.truncated = True
                st["reason"] = f"ограничено max_pages={self.max_pages} (страниц {total_pages})"
                break
            page += 1
        st["seen"] = len(seen)
        oc = st["offer_count"]
        if st["complete"] and oc and oc > 10 and len(seen) < 0.5 * oc:
            self.errors += 1                                  # «обошли всё», а товаров меньше половины — не верим
            st.update(complete=False, reason=f"собрано {len(seen)} из {oc} — формат сайта изменился?")
        print(f"[escarpe] {brand} · /c/{sec['path']}/marca:{slug}: на сайте {oc if oc is not None else '?'}, "
              f"стр. {st['pages']}/{st['pages_total'] if st['pages_total'] is not None else '?'}, товаров {len(seen)}"
              f"{'' if st['complete'] else ' — НЕ ПОЛНОСТЬЮ: ' + st['reason']} · {time.time() - t0:.0f} с", flush=True)
        return st

    def root_count(self, slug: str) -> int | None:
        """Сколько всего товаров бренда на сайте (весь магазин, /c/escarpe/marca:<слаг>, одна страница)."""
        try:
            r = self.client.get(listing_url(ROOT_SECTION, slug))
        except SourceBlocked:
            raise
        except Exception as e:
            print(f"[escarpe] сводка /c/{ROOT_SECTION}/marca:{slug} не прочитана: {e.__class__.__name__}")
            return None
        if r.status_code != 200:
            return None
        return parse_listing(r.text).get("offer_count")


def merge_listing(prev_items: dict, crawled: dict, stats: dict, sec_keys: set[str], brands: list[str]) -> dict:
    """Новая сохранённая выдача: обойдённое + прошлые товары разделов, обойдённых НЕ полностью. Товар из прошлой
    выдачи, которого нет ни в одном его полностью обойдённом разделе, выбрасывается (его судьбу решит проверка по
    ссылке в run.py), только если все его разделы обойдены полностью."""
    items = dict(crawled)
    for cid, rec in prev_items.items():
        if rec.get("brand") not in brands:
            continue
        old = [k for k in (rec.get("_sections") or []) if k in sec_keys]
        keep = [k for k in old if not (stats.get(k) or {}).get("complete")]
        if cid in items:
            cur = items[cid]
            for k in keep:                                  # раздел не дообошли — прежняя принадлежность в силе
                if k not in cur["_sections"]:
                    cur["_sections"].append(k)
            continue
        if keep:
            r = dict(rec)
            r["_sections"] = keep
            items[cid] = r
    return items


# ---------- проверка страниц товаров ----------

def _pdp_delay(so: dict) -> tuple[float, float]:
    v = so.get("pdp_delay", [1.2, 2.0])
    if isinstance(v, (list, tuple)) and v:
        lo, hi = float(v[0]), float(v[-1])
    else:
        lo, hi = float(v) * 0.85, float(v) * 1.15
    return max(0.5, lo), max(0.5, lo, hi)


def _run_pdp(client: _Client, todo: list[str], items: dict[str, dict], cache: dict, cache_path: Path,
             budget_s: float = 0, slugs: dict[str, list[str]] | None = None) -> dict:
    """Проверяет страницы товаров по очереди (один поток), пишет кэш каждые 50. Останавливается на 403/429/5xx и после
    5 ошибок подряд. budget_s > 0 — новые товары не берутся после стольких секунд."""
    res = {"done": 0, "in_stock": 0, "zero": 0, "gone": 0, "errors": 0, "foreign": 0, "blocked": None, "gone_ids": [],
           "budget_hit": False}
    t0, in_row = time.time(), 0
    for n, cid in enumerate(todo):
        if budget_s and time.time() - t0 >= budget_s:
            res["budget_hit"] = True
            print(f"[escarpe] PDP: время запуска ({budget_s / 60:.0f} мин) вышло — остальные {len(todo) - n} "
                  f"в следующий раз", flush=True)
            break
        rec = items[cid]
        info: dict = {}
        try:
            d = pdp_details(client, rec["url"], info)
        except SourceBlocked as e:
            res["blocked"] = str(e)
            break
        except Exception as e:
            res["errors"] += 1
            in_row += 1
            cache.setdefault(cid, {})["error_at"] = _now_iso()
            print(f"[escarpe] страница товара {cid}: {e.__class__.__name__}: {e}")
            if in_row >= 5:
                res["blocked"] = f"{in_row} ошибок подряд ({e.__class__.__name__}) — сайт недоступен?"
                break
            continue
        in_row = 0
        if d is None:
            if info.get("status") in (404, 410) or info.get("redirect"):
                cache[cid] = {"sizes": [], "all_sizes": [], "checked_at": _now_iso(), "gone": True}
                res["gone"] += 1
                res["gone_ids"].append(cid)
            else:
                res["errors"] += 1
                cache.setdefault(cid, {})["error_at"] = _now_iso()
        else:
            e = _cache_entry(d)
            if slugs is not None and not _brand_matches(d.get("brand_raw"), slugs.get(rec.get("brand"), [])):
                e["foreign"] = True                         # на странице другой бренд — на сайт не идёт
                res["foreign"] += 1
            cache[cid] = e
            if d["sizes"] and not e.get("foreign"):
                res["in_stock"] += 1
            else:
                res["zero"] += 1
                res["gone_ids"].append(cid)
        res["done"] += 1
        if res["done"] % 50 == 0:
            _save_json(cache_path, cache)
            rate = res["done"] / max(1e-9, time.time() - t0)
            print(f"[escarpe] PDP {res['done']}/{len(todo)}: в наличии {res['in_stock']}, без размеров {res['zero']}, "
                  f"удалены {res['gone']}, ошибок {res['errors']} · {rate * 60:.0f}/мин", flush=True)
    _save_json(cache_path, cache)
    res["seconds"] = round(time.time() - t0)
    return res


def same_price(a, b) -> bool:
    return bool(a and b) and abs(float(a) - float(b)) < 0.005


def _fresher_price(rec: dict, e: dict) -> tuple[float | None, float | None]:
    """Цена: со страницы товара; карточка выдачи — только если она свежее проверки страницы. Зачёркнутую цену
    («Prezzo regolare») страница товара не показала, а в карточке выдачи она есть при ТОЙ ЖЕ цене — берём из карточки."""
    now, old = e.get("price"), e.get("price_old")
    card_now, card_old = rec.get("_card_price"), rec.get("_card_price_old")
    if not now:
        return card_now, card_old
    if card_now and _age_hours(rec.get("_seen_at")) < _age_hours(e.get("checked_at")):
        return card_now, card_old
    if not old and card_old and same_price(card_now, now):
        old = card_old
    return now, old


def resolve_gender(rec: dict, e: dict) -> str | None:
    """Пол: по разделам выдачи (мужской + женский сразу — унисекс), у разделов без пола — со страницы товара."""
    sec = set(rec.get("_genders") or [])
    if sec == {"men"} or sec == {"women"}:
        return next(iter(sec))
    if {"men", "women"} <= sec:
        return None
    g = e.get("gender")
    return g if g in ("men", "women") else None


def to_product(rec: dict, e: dict) -> Product:
    """Запись выдачи + проверенная страница товара (кэш) -> Product для run.py."""
    d = {k: v for k, v in rec.items() if not k.startswith("_")}
    p = Product(**d)
    p.sizes = list(e.get("sizes") or [])
    p.in_stock = True
    now, old = _fresher_price(rec, e)
    p.price_now = float(now or 0)
    disc = discount_pct(now, old)
    p.price_old, p.discount_pct = (old if disc is not None else None), disc
    p.gender = resolve_gender(rec, e)
    if e.get("color"):
        p.colors = [e["color"]]
    p.category = rec.get("category") or e.get("category") or ""
    # крошки — только последний узел: в середине пути бывает «T-shirt e polo» (и у футболок, и у поло)
    leaf = str(e.get("category_path") or "").split("/")[-1] or None
    p.type = type_of(rec.get("category"), e.get("category"), leaf, e.get("name"), rec.get("title"))
    if e.get("name"):
        p.title = e["name"]
    attrs = dict(e.get("attrs") or {})
    if e.get("category_path"):
        attrs["category_path"] = e["category_path"]
    if e.get("material"):
        attrs["material"] = e["material"]
    p.attrs = attrs
    if len(e.get("images") or []) >= len(p.images):
        p.images = list(e.get("images") or p.images)[:MAX_IMAGES]
    p.fetched_at = e.get("checked_at") or p.fetched_at
    return p


MODES = ("auto", "listing", "pdp")


def fetch(query: Query, **opts) -> list[Product]:
    LAST_RUN.clear()
    so = query.source_opts or {}
    mode = str(so.get("_mode") or "auto")
    if mode not in MODES:
        raise ValueError(f"escarpe: неизвестный режим {mode!r} (auto | listing | pdp)")
    slugs = parse_brands(so)
    sections = parse_sections(so)
    pdp_per_run = int(so.get("pdp_per_run", 1000) or 0)
    tiers = tier_hours(so)
    budget_s = max(0.0, float(so.get("pdp_budget_minutes", 0) or 0)) * 60
    hot_ids = {str(x) for x in (so.get("_hot") or ())}
    warm_ids = {str(x) for x in (so.get("_warm") or ())}
    ld = so.get("listing_delay") or [1.5, 3.0]
    listing_delay = (max(1.5, float(ld[0])), max(1.5, float(ld[-1])))
    cache_path = Path(so.get("pdp_cache_file") or PDP_CACHE_FILE)
    listing_path = Path(so.get("listing_file") or LISTING_FILE)
    so_due = dict(so)
    so_due.setdefault("listing_max_age_hours", 12)

    have = {norm_brand(b) for b in slugs}
    for b in query.brands:
        if norm_brand(b) not in have:
            print(f"[escarpe] ВНИМАНИЕ: для бренда {b!r} нет marca: в source_opts.escarpe_it.brands — пропускаю")
    brands = [b for b in slugs if query.wants_brand(b)]
    if not brands or not sections:
        print("[escarpe] нет брендов или разделов для обхода")
        LAST_RUN["skipped"] = True
        return []
    keys = {sec_key(s, sec["path"]) for b in brands for s in slugs[b] for sec in sections}

    # ---- 1. выдача ----
    prev = _load_json(listing_path, {}) or {}
    prev_items: dict = prev.get("items") or {}
    if mode == "pdp" and not prev_items:
        raise RuntimeError("нет сохранённой выдачи escarpe.it (data/escarpe_listing.json) — проверять нечего; "
                           "запустите обход выдачи (run.py --only escarpe_it --mode listing)")
    crawl_now, why = listing_due(prev, brands, mode, so_due)
    if not crawl_now and mode == "auto" and keys - set(prev.get("section_keys") or []):
        crawl_now, why = True, "в настройках новые бренды / разделы"
    complete = bool(prev.get("complete")) if not crawl_now else True
    reason = "" if complete else "сохранённая выдача собрана не полностью"
    blocked = False
    t0 = time.time()
    client = _Client(listing_delay)
    if not crawl_now:
        items = {k: v for k, v in prev_items.items() if v.get("brand") in brands}
        print(f"[escarpe] выдачу не обхожу ({why}): беру сохранённую от {prev.get('crawled_at')} — {len(items)} товаров")
    else:
        print(f"[escarpe] обхожу выдачу: {why}")
        crawl = _Crawl(client, query.max_pages)
        roots: dict[str, int | None] = {}
        try:
            for brand in brands:
                for slug in slugs[brand]:
                    for sec in sections:
                        crawl.section(brand, slug, sec)
                    if so.get("root_check", True):
                        roots[slug] = crawl.root_count(slug)
                        in_secs = sum((crawl.stats.get(sec_key(slug, s["path"])) or {}).get("offer_count") or 0
                                      for s in sections)
                        print(f"[escarpe] === {brand} (marca:{slug}): всего на сайте {roots[slug]}, в наших разделах "
                              f"{in_secs} (без детского; товар бывает в двух разделах)", flush=True)
        except SourceBlocked as e:
            blocked = True
            complete = False
            reason = f"доступ ограничен посреди обхода: {e}"
            print(f"[escarpe] {e} — останавливаюсь")
            if not crawl.items and not prev_items:
                raise
        if not any(st["pages"] or st["complete"] for st in crawl.stats.values()) and not blocked:
            raise RuntimeError(f"escarpe: ни одна страница выдачи не разобрана ({crawl.errors} ошибок) — сайт "
                               "недоступен или сменил формат")
        not_done = [k for k in keys if not (crawl.stats.get(k) or {}).get("complete")]
        if not_done and complete:
            complete = False
            reason = (f"выдача обойдена не полностью ({len(not_done)} из {len(keys)} разделов"
                      + (f"; ограничено max_pages={query.max_pages} (тест)" if crawl.truncated else "") + ")")
        items = merge_listing(prev_items, crawl.items, crawl.stats, keys, brands)
        if crawl.skipped:
            print("[escarpe] не берём: " + ", ".join(f"{k} — {v}" for k, v in crawl.skipped.most_common(8)))
        if crawl.sponsored:
            print(f"[escarpe] рекламных карточек бренда: {crawl.sponsored} (берутся, пол — со страницы товара)")
        untyped = Counter(r.get("category") for r in crawl.items.values() if not r.get("type"))
        if untyped:
            print("[escarpe] категории без типа: " + ", ".join(f"{k} {v}" for k, v in untyped.most_common(10)))
        keep_other = {k: v for k, v in prev_items.items() if v.get("brand") not in brands}
        stats_out = {k: v for k, v in crawl.stats.items()}
        _save_json(listing_path, {"crawled_at": _now_iso(), "complete": complete, "brands": brands,
                                  "section_keys": sorted(keys), "sections": stats_out, "roots": roots,
                                  "items": {**keep_other, **items}})
        print(f"[escarpe] выдача: {len(items)} товаров ({len(crawl.items)} в этом обходе), {client.requests} запросов, "
              f"{time.time() - t0:.0f} с" + ("" if complete else f" — {reason}"), flush=True)
        LAST_RUN["sections"] = stats_out
        LAST_RUN["roots"] = roots

    if mode == "listing":
        LAST_RUN.update(no_new_data=True, listing={"items": len(items), "complete": complete, "reason": reason,
                                                    "seconds": round(time.time() - t0)})
        return []

    # ---- 2. страницы товаров (ярусы hot → warm → cold) ----
    cache: dict = _load_json(cache_path, {}) or {}
    todo, plan = _pdp_plan(items, cache, brands, 0 if blocked else pdp_per_run, tiers, hot_ids, warm_ids)
    print(f"[escarpe] проверка страниц товаров: горячих {plan['hot']['total']} (пора {plan['hot']['due']}, раз в "
          f"{tiers['hot']:g} ч), на сайте {plan['warm']['total']} (пора {plan['warm']['due']}, раз в {tiers['warm']:g} ч), "
          f"остальных {plan['cold']['total']} (ни разу не проверены {plan['never']}, пора {plan['cold']['due']}, раз в "
          f"{tiers['cold']:g} ч); в этот запуск до {len(todo)}"
          + (f" и не дольше {budget_s / 60:.0f} мин" if budget_s else ""), flush=True)
    pclient = _Client(_pdp_delay(so))
    pclient.requests = client.requests                # пауза и перед первой страницей товара после выдачи
    res = _run_pdp(pclient, todo, items, cache, cache_path, budget_s, slugs)
    if res["blocked"]:
        print(f"[escarpe] {res['blocked']} — проверка страниц товаров остановлена")
        LAST_RUN.setdefault("reason", f"проверка остановлена: {res['blocked']}")

    # ---- 3. наружу — только проверенные размеры в наличии ----
    out: list[Product] = []
    per: dict[tuple, dict] = {}
    skipped = Counter()
    for cid, rec in items.items():
        e = cache.get(cid) or {}
        g = resolve_gender(rec, e) if e else rec.get("gender")
        s = per.setdefault((rec["brand"], g or "?"), {"listed": 0, "verified": 0, "zero": 0, "unchecked": 0})
        s["listed"] += 1
        if not e.get("checked_at") or e.get("sizes") is None:
            s["unchecked"] += 1
            continue
        if not e.get("sizes"):
            s["zero"] += 1
            continue
        if e.get("gender") == "kids":
            skipped["детское"] += 1
            continue
        if e.get("foreign"):
            skipped["другой бренд на странице"] += 1
            continue
        if query.genders and g and g not in query.genders:
            continue
        try:
            p = to_product(rec, e)
        except Exception as ex:
            print(f"[escarpe] товар {cid} пропущен: {ex}")
            continue
        if not p.price_now:
            skipped["нет цены"] += 1
            continue
        if query.types and p.type not in query.types:
            continue
        s["verified"] += 1
        out.append(p)
    gone = set(res["gone_ids"])
    for cid in (hot_ids | warm_ids) & set(items):        # товары сайта: последняя проверка — нет размеров / удалён
        e = cache.get(cid) or {}
        if e.get("checked_at") and (e.get("gone") or not e.get("sizes") or e.get("gender") == "kids"):
            gone.add(cid)
    LAST_RUN["gone_ids"] = sorted(gone)
    LAST_RUN["absence_means_gone"] = complete and not blocked
    if not complete or blocked:
        LAST_RUN.update(partial=True, reason=reason or LAST_RUN.get("reason") or "обход прерван")
    total_unchecked = sum(s["unchecked"] for s in per.values())
    runs_left = math.ceil(total_unchecked / pdp_per_run) if pdp_per_run else None
    print("[escarpe] по брендам (в выдаче / с размерами / распродано / ещё не проверено):")
    for (b, g), s in sorted(per.items()):
        print(f"[escarpe]   {b[:18]:<18} {g:<6} {s['listed']:>6} / {s['verified']:>6} / {s['zero']:>5} / {s['unchecked']:>6}")
    if skipped:
        print("[escarpe] не отдаю: " + ", ".join(f"{k} — {v}" for k, v in skipped.most_common()))
    _, fresh = _pdp_plan(items, cache, brands, 0, tiers, hot_ids, warm_ids)
    LAST_RUN["stats"] = {"listed": len(items), "returned": len(out), "unchecked": total_unchecked,
                         "pdp": {k: v for k, v in res.items() if k != "gone_ids"}, "runs_left": runs_left,
                         "tiers": {t: fresh[t] for t in ("hot", "warm", "cold")}}
    print(f"[escarpe] итого: в выдаче {len(items)}, отдаю с проверенными размерами {len(out)}, ещё не проверено "
          f"{total_unchecked}" + (f" — при {pdp_per_run} за запуск это ещё ~{runs_left} запусков" if runs_left else "")
          + f"; PDP за запуск: {res['done']} (в наличии {res['in_stock']}, без размеров {res['zero']}, удалены "
            f"{res['gone']}, ошибок {res['errors']}, {res.get('seconds', 0)} с)")
    return out


def verify(rows: list[dict], query: Query | None = None, **opts) -> dict[str, dict | None]:
    """Перепроверка товаров по ссылке (страница товара): размеры в наличии и цена сейчас (для run.py и заказов).
    {id: обновлённая строка | None — точно нет (404 / увело со страницы товара / размеров нет)}; кого нет в ответе —
    неизвестно. Результат пишется и в кэш страниц товаров."""
    so = (query.source_opts if query else None) or {}
    cache_path = Path(so.get("pdp_cache_file") or PDP_CACHE_FILE)
    cache: dict = _load_json(cache_path, {}) or {}
    client = _Client(_pdp_delay(so))
    out: dict[str, dict | None] = {}
    now_iso = _now_iso()
    for r in rows:
        vid, url = str(r.get("source_item_id")), str(r.get("url") or "")
        if not url.startswith(BASE + "/p/") or not robots_ok(url):
            continue
        info: dict = {}
        try:
            d = pdp_details(client, url, info)
        except SourceBlocked as e:
            print(f"[escarpe] перепроверка остановлена: {e}")
            break
        except Exception as e:
            print(f"[escarpe] перепроверка {vid}: {e.__class__.__name__}, пропускаю")
            continue
        if d is None:
            if info.get("status") in (404, 410) or info.get("redirect"):
                out[vid] = None
                cache[vid] = {"sizes": [], "all_sizes": [], "checked_at": now_iso, "gone": True}
            continue
        cache[vid] = _cache_entry(d)
        if not d["in_stock"]:
            out[vid] = None
            continue
        row = dict(r)
        row.update(sizes=d["sizes"], in_stock=True, fetched_at=now_iso)
        if d["price"]:
            old = d["price_old"]
            if not old and same_price(r.get("price_now"), d["price"]):
                old = r.get("price_old")                     # цена та же — прежняя зачёркнутая в силе
            disc = discount_pct(d["price"], old)
            row.update(price_now=d["price"], price_old=old if disc is not None else None, discount_pct=disc)
        out[vid] = row
    if out:
        _save_json(cache_path, cache)
    return out
