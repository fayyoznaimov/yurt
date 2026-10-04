"""Trendyol (витрина TR): ВСЕ товары брендов из config (мужское и женское, без детского), с проверенными размерами.

Как устроено:
  * Листинг — обычная HTML-страница https://www.trendyol.com/<путь>?pi=N (36 карточек на страницу);
    товары лежат в JSON window["__single-search-result__PROPS"] = {...}.data.products[].
  * Пол и категория задаются ПУТЁМ: <slug>-<kadin|erkek>-<категория>-x-b<бренд>-g<пол>-c<категория>
    (g1 = kadın, g2 = erkek, g3 = çocuk; c82 giyim, c114 ayakkabı, c117 çanta, c64 iç giyim, c27 aksesuar).
    Параметры ?wg= ?sst= ?prc= ?q= и пути /sr запрещены robots.txt — не используем; apigw.trendyol.com тоже.
  * ПОТОЛОК ВЫДАЧИ: Trendyol отдаёт не больше 138 страниц (138 × 36 = 4968 товаров) на один листинг —
    pi=139 и дальше отвечают 404 (проверено 04.10.2026 на «Pierre Cardin Erkek», 12 821 товар).
    Поэтому листинг больше 4968 товаров делится на подкатегории. Подкатегории берём из дерева меню,
    которое сайт сам кладёт в страницу (window["__navigation__PROPS"].categories, ссылки вида
    erkek-gomlek-x-g2-c75): /pierre-cardin-erkek-gomlek-x-b122-g2-c75. Агрегаций (фильтров) в HTML нет.
    Если подкатегории меню покрывают раздел плохо, по образцу страниц раздела берём категории товаров,
    их веб-категорию узнаём со страницы товара (product.webCategory; кэш cat_map в trendyol_listing.json)
    и обходим эти категории.
  * РЕКЛАМА: на первых страницах половина карточек — рекламные (cardType = ADS, ~90 слотов). Если листать
    просто ?pi=N, сайт пропускает около половины товаров (Ramsey: 664 из 1275). Сайт сам листает с курсором
    offsetParameters (data.offsetParameters прошлой страницы, «Product_24,Ads_22,LastAdSlot_22») — передаём
    его так же (&offsetParameters=…; robots.txt его не запрещает): 1270 из 1275.
  * Порядок обхода бренда: если вся выдача бренда ≤ 4968 — обходим её целиком (/ramsey-x-b185; пол потом
    со страницы товара). Иначе по полу: выдача пола ≤ 4968 — целиком (/lufian-erkek-x-b364-g2), иначе
    разделы giyim / ayakkabı / çanta / iç giyim, большие — по подкатегориям меню. Аксессуары — без пола
    (/pierre-cardin-aksesuar-x-b122-c27: брендовый «бренд+пол+аксессуары» сайт уводит на /sr).
    Некоторые сочетания сайт уводит на /sr — их пропускаем; если раздел недоступен целиком, берём
    выдачу пола (до потолка).
  * Из-за IP вне Турции сайт перекидывает на /en/select-country — передаём куки витрины TR.
  * Цена — та, что видна на карточке (singlePrice.salePrice, часто «в корзине»), старая — зачёркнутая;
    скидки может не быть (тогда price_old = None).
  * РАЗМЕРЫ — только со страницы товара (window["__envoy_product-image-gallery__PROPS"].product).
    Кэш data/trendyol_pdp_cache.json: {contentId: {sizes (в наличии), all_sizes, checked_at, price,
    merchant, official, gender, color, attrs, category_path}}. За запуск проверяется до pdp_per_run товаров:
    сначала никогда не проверенные (со скидкой раньше, бренды по очереди), потом устаревшие
    (старше pdp_max_age_hours). Наружу (run.py) отдаются ТОЛЬКО товары с проверенными размерами в наличии;
    остальные ждут очереди в data/trendyol_listing.json и появятся после проверки.
    Проверен и размеров в наличии нет — товар распродан (LAST_RUN["gone_ids"]).
  * Свойства товара («Materyal», «Kalıp», «Yaka Tipi»…) — со страницы товара, из JSON-LD.

Настройки (config.json → source_opts.trendyol):
  brand_slugs          {"Pierre Cardin": "pierre-cardin-x-b122", ...} — обязательно
  max_pages            потолок страниц на один листинг (сайт всё равно не даёт больше 138)
  pdp_per_run          сколько страниц товаров проверить за запуск (2500)
  pdp_max_age_hours    через сколько часов перепроверять размеры (72)
  pdp_delay            пауза между страницами товаров у одного потока, с (1.2)
  pdp_concurrency      сколько потоков проверки (1–2; по умолчанию 2)
  listing_delay        [от, до] пауза между страницами листинга, с ([1.0, 2.0])
  listing_max_age_hours  > 0 — не обходить листинги заново, если полный обход свежее (по умолчанию 0)
  official_only        брать только карточки с бейджем официального продавца (false)
  merchant_ids         белый список id продавцов (необязательно)
  image_size           "1200/1800" — размер картинок CDN; "" — оригинал
  pdp_cache_file / listing_file — другие пути к кэшам (для тестов)
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .base import BROWSER_UA, MAX_IMAGES, Product, Query, classify_type, discount_pct, norm_brand

if hasattr(sys.stdout, "reconfigure") and (sys.stdout.encoding or "").lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")  # турецкие буквы в выводе не роняют cp1251-консоль

BASE = "https://www.trendyol.com"
CDN = "https://cdn.dsmcdn.com"
PAGE_SIZE = 36
MAX_LISTING_PAGES = 138                       # дальше сайт отвечает 404 (потолок 4968 товаров на листинг)
CAP_ITEMS = MAX_LISTING_PAGES * PAGE_SIZE
# первые страницы наполовину заняты рекламными карточками (до ~90 слотов) — делим листинг чуть раньше потолка
SPLIT_ABOVE = CAP_ITEMS - 150
STOREFRONT_COOKIES = {"storefrontId": "1", "countryCode": "TR", "language": "tr", "platform": "web"}
# Итог последнего fetch() для run.py (см. sources/akinon.py): partial / skipped / reason / gone_ids.
LAST_RUN: dict = {}
LISTING_KEY = 'window["__single-search-result__PROPS"]='
GALLERY_KEY = 'window["__envoy_product-image-gallery__PROPS"]='
NAV_KEY = 'window["__navigation__PROPS"]='

DATA = Path(__file__).resolve().parent.parent / "data"
PDP_CACHE_FILE = DATA / "trendyol_pdp_cache.json"
LISTING_FILE = DATA / "trendyol_listing.json"

GENDERS = {"women": (1, "kadin"), "men": (2, "erkek")}
# разделы, которые обходятся по полу, когда выдача пола больше потолка: (имя в адресе, id категории)
GENDER_SECTIONS = [("giyim", 82), ("ayakkabi", 114), ("canta", 117), ("ic-giyim", 64)]
ACCESSORY_SECTION = ("aksesuar", 27)
# «Büyük Beden», «Tesettür» — сквозные подборки поверх обычных категорий; для деления не нужны
OVERLAP_CATS = {80, 81}
# если меню на странице не нашлось — минимальный запасной список подкатегорий (из меню 04.10.2026)
FALLBACK_CHILDREN = {
    82: [("t-shirt", 73), ("sort", 119), ("gomlek", 75), ("esofman", 1049), ("pantolon", 70), ("ceket", 1030),
         ("jean", 120), ("yelek", 1207), ("kazak", 1092), ("mont", 118), ("takim-elbise", 78), ("sweatshirt", 1179),
         ("kaban", 1075), ("hirka", 1066), ("trenckot", 79), ("palto", 1130), ("yagmurluk-ruzgarlik", 1205),
         ("polar", 1149), ("elbise", 56), ("bluz", 1019), ("etek", 69)],
    114: [("spor-ayakkabi", 109), ("gunluk-ayakkabi", 1352), ("sneaker", 1172), ("klasik-ayakkabi", 101421),
          ("bot", 1025), ("loafer", 1108), ("terlik", 110), ("sandalet", 111), ("topuklu-ayakkabi", 107),
          ("babet", 113), ("cizme", 1037)],
    117: [("sirt-cantasi", 115), ("omuz-cantasi", 101465), ("bel-cantasi", 104144), ("el-cantasi", 104145),
          ("postaci-canta", 1152), ("laptop-cantasi", 1107), ("spor-cantasi", 1174), ("portfoy", 1151), ("valiz", 1202)],
    27: [("saat", 34), ("gunes-gozlugu", 105), ("cuzdan", 1032), ("kemer", 1093), ("canta", 117), ("sapka", 1181),
         ("cuzdan-kartlik", 1353), ("valiz", 1202), ("kravat", 1101), ("atki", 1003), ("bere", 1015),
         ("eldiven", 1046), ("taki-mucevher", 28), ("sal", 31), ("bileklik", 101), ("papyon", 1133)],
}
GAP_ABS, GAP_PCT = 60, 0.03
SAMPLE_PAGES = 12                              # образец страниц раздела для поиска недостающих категорий                    # сколько «не найденных в подкатегориях» терпим без обхода родителя

# Не одежда / обувь / сумки / аксессуары — в каталог не берём (по названию категории Trendyol).
EXCLUDE_CATEGORY_RE = re.compile(
    r"parf[üu]m|deodorant|kolonya|kozmetik|makyaj|\bruj\b|\boje\b|losyon|t[ıi]ra[şs]|\bkrem\b|[şs]ampuan|"
    r"nevresim|yast[ıi]k|yorgan|battaniye|[çc]ar[şs]af|\bpike\b|havlu|bornoz|perde|\bhal[ıi]\b(?! saha)|k[ıi]rlent|"
    r"masa [öo]rt|bardak|tabak|fincan|mutfak|tencere|oda kokusu|\bmum\b|telefon k[ıi]l[ıi]f|kulakl[ıi]k|"
    r"powerbank|kapak & k[ıi]l[ıi]f|[şs]arj|oyuncak|biberon|emzik|alez|uyku seti|ev tekstil|banyo|paspas|[öo]rt[üu] seti",
    re.I)
KIDS_RE = re.compile(r"[çc]ocuk|\bkids?\b|\bjunior\b|\bbebek (?:tulum|zıbın|body)", re.I)

# Названия категорий Trendyol, которые classify_type не знает или путает
# («Pijama Takımı» иначе станет костюмом). Проверяются по названию категории до classify_type.
TR_CATEGORY_TYPES = [
    (r"pijama|gecelik|sabahl[ıi]k|i[çc] [çc]ama[şs][ıi]r|i[çc] giyim|\batlet|s[üu]tyen|[çc]orap|termal|i[çc]lik|fanila|b[üu]stiyer|korse|boxer|k[üu]lot|slip\b", "нижнее бельё"),
    (r"^ceket$|blazer|takım elbise|smokin", "пиджаки и костюмы"),
    (r"(kot|deri|denim|s[üu]et|bomber|[şs]i[şs]me|kapitone)\s+ceket", "куртки и пальто"),
    (r"pantolon|\btayt|e[şs]ofman alt|kapri|salopet", "брюки"),
    (r"tunik", "рубашки"),
    (r"\btulum", "платья"),
    (r"s[üu]veter", "свитеры и кардиганы"),
    (r"\bpolar\b|e[şs]ofman [üu]st", "толстовки"),
    (r"ya[ğg]murluk|pardes[üu]|palto|k[üu]rk\b|r[üu]zgarl[ıi]k", "куртки и пальто"),
    (r"espadril|panduf|mokasen|oxford|ayakkab|terli[kğg]|kar botu|sneaker|\bbot\b|[çc]izme|loafer|sandalet", "обувь"),
    (r"valiz|bavul|portf[öo]y|evrak|[çc]anta", "сумки"),
    (r"deniz [şs]ortu|mayo [şs]ort|\bbermuda", "шорты"),
    (r"\bsaat|mendil|g[öo]zl[üu][ğk]|kartl[ıi]k|kol d[üu][ğg]me|\bbere\b|[şs]al\b|e[şs]arp|fular|bandana|\btak[ıi]\b|kolye|bileklik|k[üu]pe|y[üu]z[üu]k|anahtarl[ıi]k|[şs]emsiye|boyunluk|papyon|c[üu]zdan|kemer|kravat|[şs]apka|eldiven|atk[ıi]", "аксессуары"),
]
_TR_CAT_RE = [(re.compile(p, re.I), t) for p, t in TR_CATEGORY_TYPES]

_COLOR_RE = re.compile(
    r"\b((?:a[çc][ıi]k|koyu)\s+)?(siyah|beyaz|lacivert|gri|bej|kahverengi|kahve|haki|mavi|k[ıi]rm[ıi]z[ıi]|ye[şs]il|ekru|antrasit|"
    r"bordo|vizon|pembe|mor|sar[ıi]|turuncu|krem|taba|indigo|f[üu]me|petrol|hardal|lila|m[üu]rd[üu]m|kiremit|camel|"
    r"g[üu]m[üu][şs]|alt[ıi]n|mint|pudra|somon|nude|stone|ta[şs])(\s+melanj)?\b",
    re.I,
)
_STYLE_RE = re.compile(r"\s*\b(\d{6,10})-([A-Z]{1,4}\d{2,5})\s*$", re.I)  # 50297487-VR033 (одежда)
_STYLE2_RE = re.compile(r"\s*\b([A-Z]{2,4}-\d{4,6})(?:\s+[A-Z]{1,2})?\s*$")     # PC-54966, PCI-10106 G (обувь)
_SLUG_RE = re.compile(r"^/?([a-z0-9-]+?)-x-b(\d+)/?$", re.I)
_NAV_URL_RE = re.compile(r"^/?([a-z0-9-]+?)-x-(?:g(\d)-)?c(\d+)/?$")
_SR_RE = re.compile(r"(^|/)sr(/|\?|$)")


class SourceBlocked(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_hours(ts) -> float:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 1e9
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


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


def _tr_lower(s: str | None) -> str:
    return (s or "").replace("İ", "i").replace("I", "ı").lower()


def _num(v) -> float | None:
    """1249 / 749.4 / "1.249,40 TL" / "749,40" -> float."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s) or None
    except ValueError:
        return None


def _prices(p: dict) -> tuple[float | None, float | None]:
    """(цена на карточке, зачёркнутая цена)."""
    sp = p.get("singlePrice") or {}
    now = _num(sp.get("salePriceNumeric")) or _num(sp.get("salePriceWihoutCurrency")) or _num(sp.get("salePrice"))
    old = _num(sp.get("strikethroughPriceNumeric")) or _num(sp.get("strikethroughPriceWithoutCurrency"))
    if now is None:
        bp = p.get("binaryPrice") or {}
        now = _num(bp.get("salePriceWihoutCurrency")) or _num(bp.get("salePrice"))
        old = old or _num(bp.get("strikethroughPrice"))
    if now is None:
        pr = p.get("price") or {}
        now = _num(pr.get("discountedPrice"))
        old = old or _num(pr.get("current")) or _num(pr.get("old"))
    return now, old


def _type_of(category: str, title: str) -> str | None:
    cat = _tr_lower(category).strip()
    for rx, t in _TR_CAT_RE:
        if rx.search(cat):
            return t
    # сначала только категория: «Polo Yaka Sweatshirt» — это толстовка, а не поло
    return classify_type(cat) or classify_type(cat, _tr_lower(title))


def _gender_from_text(*texts: str | None) -> str | None:
    t = _tr_lower(" ".join(x for x in texts if x))
    men, women = bool(re.search(r"\berkek\b", t)), bool(re.search(r"\bkad[ıi]n\b", t))
    if men != women:
        return "men" if men else "women"
    return None


def _image(url: str, size: str) -> str:
    if url.startswith("/"):
        url = CDN + url
    url = re.sub(r"/mnresize/[^/]+/[^/]+/", "/", url)
    if size:
        url = url.replace(CDN + "/", f"{CDN}/mnresize/{size.strip('/')}/", 1)
    return url


def _decode_after(html: str, key: str) -> dict | None:
    i = html.find(key)
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, i + len(key))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class _Client:
    """Сессия с куками витрины TR и паузой перед каждым запросом, кроме первого."""

    def __init__(self, delay: tuple[float, float] = (1.0, 2.0)) -> None:
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        })
        for k, v in STOREFRONT_COOKIES.items():
            self.s.cookies.set(k, v, domain=".trendyol.com", path="/")
        self.delay = (float(delay[0]), float(max(delay[0], delay[1])))
        self.requests = 0
        self.listings_ok = 0   # сколько страниц листинга удалось разобрать

    def get(self, url: str) -> requests.Response:
        if self.requests:
            time.sleep(random.uniform(*self.delay))
        self.requests += 1
        try:
            r = self.s.get(url, timeout=30, allow_redirects=False)
        except requests.RequestException as e:  # одна повторная попытка при сетевой ошибке
            print(f"[trendyol] сеть: {e.__class__.__name__}, повтор через паузу")
            time.sleep(random.uniform(5, 8))
            r = self.s.get(url, timeout=30, allow_redirects=False)
        if r.status_code in (403, 429):
            raise SourceBlocked(f"trendyol ответил {r.status_code} на {url} — похоже, доступ ограничен")
        loc = r.headers.get("location") or ""
        if r.is_redirect and "select-country" in loc:
            raise SourceBlocked("trendyol перекидывает на выбор страны — куки витрины TR не приняты")
        return r


# ---------- листинги ----------

def _slug(text: str) -> str:
    t = _tr_lower(text).translate(str.maketrans("çğıöşü", "cgiosu"))
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _strip_gender(text: str) -> str:
    return re.sub(r"^(kadin|erkek|cocuk|unisex)-", "", text)


def _parse_nav(html: str) -> dict[tuple[int, int], list[tuple[str, int]]]:
    """Дерево меню со страницы: {(пол 0/1/2/3, id категории): [(имя в адресе, id подкатегории)]}."""
    nav = _decode_after(html, NAV_KEY) or {}
    out: dict[tuple[int, int], list[tuple[str, int]]] = {}

    def walk(node: dict) -> None:
        kids = [k for k in (node.get("children") or []) if isinstance(k, dict)]
        m = _NAV_URL_RE.match(str(node.get("webUrl") or ""))
        if m:
            key = (int(m.group(2) or 0), int(m.group(3)))
            lst = out.setdefault(key, [])
            for ch in kids:
                mc = _NAV_URL_RE.match(str(ch.get("webUrl") or ""))
                if not mc:
                    continue
                cid = int(mc.group(3))
                if cid != key[1] and cid not in OVERLAP_CATS and all(c != cid for _, c in lst):
                    lst.append((_strip_gender(mc.group(1)), cid))
        for ch in kids:
            walk(ch)

    for top in nav.get("categories") or []:
        if isinstance(top, dict):
            walk(top)
    return out


class _Crawl:
    """Состояние обхода листингов одного запуска."""

    def __init__(self, client: _Client, query: Query, image_size: str, official_only: bool, merchant_ids: set[str]):
        self.client, self.query, self.image_size = client, query, image_size
        self.official_only, self.merchant_ids = official_only, merchant_ids
        self.max_pages = max(1, min(int(query.max_pages or 1), MAX_LISTING_PAGES))
        self.nav: dict | None = None
        self.items: dict[str, dict] = {}        # contentId -> запись листинга (Product.to_dict + служебные «_»)
        self.errors = 0
        self.truncated = False                  # упёрлись в max_pages меньше потолка сайта (тестовый режим)
        self.capped = 0                         # товаров за потолком 138 страниц, которые не удалось достать
        self.excluded = Counter()               # не одежда (категория) / детское
        self.skipped_seller = 0
        # id категории товара (карточка: category.id) -> [id веб-категории для адреса, имя] — узнаётся со страницы
        # товара (product.webCategory) и хранится в data/trendyol_listing.json → cat_map
        self.cat_map: dict[str, list | None] = {}
        self.card_cat: dict[str, tuple[str, str, str]] = {}   # contentId -> (id категории товара, имя, ссылка)
        self.node_seen: dict[tuple[int, int], set[str]] = {}  # уже обойдённые (пол, категория) в этом бренде
        self.discovered = 0
        self.brand = ""
        self.brand_id = ""
        self.slug = ""

    # --- запросы ---
    def get_listing(self, path: str, pi: int = 1, expect: dict | None = None, offset: str | None = None):
        """(data, html, итоговый путь) или (None, причина, None). Перенаправления на другой листинг — следуем.
        offset — data.offsetParameters прошлой страницы (курсор выдачи, его же шлёт сам сайт при листании)."""
        url = BASE + path + (f"?pi={pi}" if pi > 1 else "")
        if pi > 1 and offset and re.fullmatch(r"[A-Za-z_]+_\d+(,[A-Za-z_]+_\d+)*", offset):
            url += "&offsetParameters=" + offset
        for _hop in range(3):
            r = self.client.get(url)
            if r.is_redirect:
                loc = r.headers.get("location") or ""
                lpath = urlsplit(loc).path
                if _SR_RE.search(lpath):
                    return None, "sr", None
                if "-x-" not in lpath:
                    return None, f"redirect {loc}", None
                url = loc if loc.startswith("http") else BASE + loc
                continue
            if r.status_code in (404, 410):
                return None, "404", None
            r.raise_for_status()
            props = _decode_after(r.text, LISTING_KEY)
            if not props or not isinstance(props.get("data"), dict):
                raise ValueError(f"на {url} нет __single-search-result__PROPS — формат сайта изменился?")
            self.client.listings_ok += 1
            if self.nav is None:
                self.nav = _parse_nav(r.text)
                if not self.nav:
                    print("[trendyol] меню на странице не найдено — подкатегории из запасного списка")
            data = props["data"]
            if expect and not self._matches(data, expect):
                return None, "другой листинг", None
            return data, r.text, urlsplit(url).path
        return None, "много перенаправлений", None

    @staticmethod
    def _matches(data: dict, expect: dict) -> bool:
        cf = data.get("canonicalFilters") or {}
        keys = lambda k: {str(x.get("key")) for x in (cf.get(k) or []) if isinstance(x, dict)}  # noqa: E731
        if expect.get("brand") and str(expect["brand"]) not in keys("brands"):
            return False
        if expect.get("cat") and str(expect["cat"]) not in keys("categories"):
            return False
        if expect.get("gender") and str(expect["gender"]) not in keys("genders"):
            return False
        return True

    def children(self, gid: int, cid: int) -> list[tuple[str, int]]:
        nav = self.nav or {}
        keys = [(1, cid), (2, cid), (0, cid)] if gid == 0 else [(gid, cid), (0, cid)]
        out: list[tuple[str, int]] = []
        for k in keys:
            for name, c in nav.get(k, []):
                if all(c != x for _, x in out):
                    out.append((name, c))
        if not out:
            out = list(FALLBACK_CHILDREN.get(cid, []))
        return out

    def path(self, name: str | None, cid: int | None, gender: str | None) -> str:
        parts = [self.slug]
        if gender:
            parts.append(GENDERS[gender][1])
        if name:
            parts.append(name)
        p = "/" + "-".join(parts) + f"-x-b{self.brand_id}"
        if gender:
            p += f"-g{GENDERS[gender][0]}"
        if cid:
            p += f"-c{cid}"
        return p

    # --- обход ---
    def add(self, card: dict, gender: str | None) -> str | None:
        cid = str(card.get("contentId") or card.get("id") or "")
        if not cid:
            return None
        if cid in self.items:
            rec = self.items[cid]
            if gender and not rec.get("gender"):
                rec["gender"] = gender
            return cid
        official = bool(card.get("hasOfficialSellerBadge"))
        if (self.official_only and not official) or (self.merchant_ids and str(card.get("merchantId")) not in self.merchant_ids):
            self.skipped_seller += 1
            return cid
        cat = str((card.get("category") or {}).get("name") or "")
        if EXCLUDE_CATEGORY_RE.search(_tr_lower(cat)):
            self.excluded["не одежда: " + cat] += 1
            return cid
        if KIDS_RE.search(_tr_lower(f"{card.get('name')} {cat}")):
            self.excluded["детское"] += 1
            return cid
        try:
            prod = _card_to_product(card, self.brand, gender, self.image_size)
        except Exception as e:  # одна битая карточка не мешает остальным
            print(f"[trendyol] пропущена карточка {cid}: {e}")
            return cid
        if not prod:
            return cid
        rec = prod.to_dict()
        pc = card.get("category") or {}
        if pc.get("id"):
            self.card_cat[cid] = (str(pc["id"]), str(pc.get("name") or ""), rec["url"])
        rec["_official"] = official
        rec["_merchant_id"] = card.get("merchantId")
        rec["_seen_at"] = _now_iso()
        self.items[cid] = rec
        return cid

    def crawl_pages(self, path: str, data: dict, gender: str | None, label: str, limit: int | None = None) -> set[str]:
        """Все страницы одного листинга (до потолка или limit); data — уже полученная 1-я страница."""
        seen: set[str] = set()
        total = int(data.get("total") or 0)
        need = max(1, math.ceil(total / PAGE_SIZE))
        # с рекламными карточками страниц нужно чуть больше, чем total/36, — листаем, пока сайт отдаёт товары
        last = min(self.max_pages, limit or MAX_LISTING_PAGES)
        pi, t0, idle = 1, time.time(), 0
        while True:
            prods = [p for p in (data.get("products") or []) if isinstance(p, dict)]
            mine = [p for p in prods if _is_brand(p, self.brand_id, self.brand)]
            strategy = data.get("appliedSearchStrategy") or "DEFAULT"
            if strategy != "DEFAULT" or (prods and not mine):
                print(f"[trendyol] {path} стр. {pi}: товаров бренда нет ({strategy}) — листинг закончен")
                break
            before = len(seen)
            for p in mine:
                cid = self.add(p, gender)
                if cid:
                    seen.add(cid)
            idle = idle + 1 if len(seen) == before else 0
            if not prods or idle >= 3 or len(seen) >= total:
                break
            if pi >= last:
                if last < MAX_LISTING_PAGES and not limit:
                    self.truncated = True        # тестовый режим: max_pages меньше потолка сайта
                break
            pi += 1
            offset = data.get("offsetParameters")
            try:
                data, why, _ = self.get_listing(path, pi, None, offset)
            except SourceBlocked:
                raise
            except Exception as e:
                self.errors += 1
                print(f"[trendyol] {path} стр. {pi}: ошибка {e} — листинг прерван")
                break
            if data is None:
                if why != "404" or pi <= MAX_LISTING_PAGES:
                    print(f"[trendyol] {path} стр. {pi}: {why} — листинг прерван")
                    if why != "404":
                        self.errors += 1
                break
        if total > len(seen) and pi >= MAX_LISTING_PAGES and not limit:
            self.capped += total - len(seen)
        print(f"[trendyol] {self.brand} · {label} {path}: всего {total}, стр. {pi}/{need}"
              f"{' (потолок сайта ' + str(MAX_LISTING_PAGES) + ')' if need > MAX_LISTING_PAGES else ''}, "
              f"товаров {len(seen)}, в каталоге уже {len(self.items)} · {time.time() - t0:.0f} с", flush=True)
        return seen

    def crawl_node(self, name: str | None, cid: int | None, gender: str | None, depth: int = 0,
                   label: str = "") -> tuple[bool, int, set[str]]:
        """Листинг (бренд[+пол][+категория]); больше потолка — делим по подкатегориям меню.
        (доступен ли, total, найденные id)."""
        path = self.path(name, cid, gender)
        gid = GENDERS[gender][0] if gender else 0
        if cid and (gid, cid) in self.node_seen:          # тот же листинг уже обойдён из другого раздела
            return True, 0, self.node_seen[(gid, cid)]
        ok, total, seen = self._crawl_node(path, name, cid, gender, gid, depth, label)
        if cid and ok:
            self.node_seen[(gid, cid)] = seen
        return ok, total, seen

    def _crawl_node(self, path: str, name: str | None, cid: int | None, gender: str | None, gid: int, depth: int,
                    label: str) -> tuple[bool, int, set[str]]:
        expect = {"brand": self.brand_id, "cat": cid, "gender": gid or None}
        kids = self.children(gid, cid) if cid and depth < 2 else []
        try:
            data, why, final = self.get_listing(path, 1, expect)
        except SourceBlocked:
            raise
        except Exception as e:
            self.errors += 1
            print(f"[trendyol] {path}: ошибка {e} — пропущен")
            return False, 0, set()
        label = label or (name or "все")
        if data is None:
            if depth == 0 and kids:          # раздел уводит на /sr — пробуем его подкатегории
                print(f"[trendyol] {self.brand} · {path}: {why} — пробую подкатегории ({len(kids)})")
                seen: set[str] = set()
                any_ok = False
                for kname, kcid in kids:
                    ok, _t, s = self.crawl_node(kname, kcid, gender, depth + 1, f"{label}/{kname}")
                    any_ok |= ok
                    seen |= s
                return any_ok, len(seen), seen
            if depth == 0:
                print(f"[trendyol] {self.brand} · {path}: {why} — пропущен")
            return False, 0, set()
        total = int(data.get("total") or 0)
        if total <= SPLIT_ABOVE or not kids:
            return True, total, self.crawl_pages(final, data, gender, label)
        print(f"[trendyol] {self.brand} · {path}: {total} товаров > {SPLIT_ABOVE} (потолок сайта {CAP_ITEMS}) — делю на {len(kids)} подкатегорий")
        seen = set()
        for kname, kcid in kids:
            _ok, _t, s = self.crawl_node(kname, kcid, gender, depth + 1, f"{label}/{kname}")
            seen |= s
        tol = max(GAP_ABS, GAP_PCT * total)
        if total - len(seen) > tol:
            # каких категорий не хватает — по образцу из первых страниц самого раздела (веб-категория — со
            # страницы товара, запоминается в cat_map), потом обходим эти категории
            print(f"[trendyol] {self.brand} · {path}: в подкатегориях меню {len(seen)} из {total} — ищу недостающие категории")
            seen |= self.crawl_pages(final, data, gender, label + " (образец)", limit=SAMPLE_PAGES)
            seen |= self.discover(seen, gender, depth, label)
            if total - len(seen) > tol:
                print(f"[trendyol] {self.brand} · {path}: найдено {len(seen)} из {total} — "
                      f"добираю остаток из самого раздела (до {MAX_LISTING_PAGES} стр.)")
                seen |= self.crawl_pages(final, data, gender, label + " (остаток)")
                seen |= self.discover(seen, gender, depth, label)
            print(f"[trendyol] {self.brand} · {path}: итого найдено {len(seen)} из {total}")
        else:
            print(f"[trendyol] {self.brand} · {path}: подкатегории покрыли {len(seen)} из {total}")
        return True, total, seen

    def web_category(self, pcat: str, url: str) -> list | None:
        """[id, имя] веб-категории товара со страницы товара (product.webCategory); кэшируется в cat_map."""
        if pcat in self.cat_map:
            return self.cat_map[pcat]
        wc = None
        try:
            r = self.client.get(url)
            if r.status_code == 200:
                prod = (_decode_after(r.text, GALLERY_KEY) or {}).get("product") or {}
                w = prod.get("webCategory") or {}
                if w.get("id"):
                    wc = [int(w["id"]), str(w.get("name") or "")]
        except SourceBlocked:
            raise
        except Exception as e:
            print(f"[trendyol] категория со страницы товара не узнана ({e.__class__.__name__})")
            return None                        # не запоминаем — попробуем в другой раз
        self.cat_map[pcat] = wc
        return wc

    def discover(self, seen: set[str], gender: str | None, depth: int, label: str) -> set[str]:
        """Листинг больше потолка и подкатегории меню его не покрыли: какие категории товаров в нём есть
        (по уже собранным карточкам), их веб-категории (одна страница товара на категорию) — и обходим их."""
        cnt = Counter(self.card_cat[c][0] for c in seen if c in self.card_cat)
        sample = {self.card_cat[c][0]: self.card_cat[c][2] for c in seen if c in self.card_cat}
        names = {self.card_cat[c][0]: self.card_cat[c][1] for c in seen if c in self.card_cat}
        gid = GENDERS[gender][0] if gender else 0
        got: set[str] = set()
        for pcat, n in cnt.most_common(60):
            if n < 3:
                break
            wc = self.web_category(pcat, sample[pcat])
            if not wc or (gid, wc[0]) in self.node_seen:
                continue
            self.discovered += 1
            _ok, _t, s = self.crawl_node(_slug(wc[1]) or None, wc[0], gender, max(depth + 1, 1),
                                         f"{label}/{names.get(pcat) or wc[1]}")
            got |= s
        return got

    def crawl_brand(self, brand: str, slug_text: str, brand_id: str) -> dict:
        self.brand, self.slug, self.brand_id = brand, slug_text, brand_id
        self.node_seen = {}
        stat = {"brand_total": None, "genders": {}}
        genders = [g for g in ("women", "men") if not self.query.genders or g in self.query.genders]
        root, why, final = self.get_listing(self.path(None, None, None), 1, {"brand": brand_id})
        if root is not None:
            stat["brand_total"] = int(root.get("total") or 0)
        if root is not None and stat["brand_total"] <= SPLIT_ABOVE:
            # весь бренд влезает в один листинг — самый дешёвый полный обход (пол — со страницы товара)
            self.crawl_pages(final, root, None, "весь бренд")
            return stat
        for g in genders:
            gpath = self.path(None, None, g)
            data, why, gfinal = self.get_listing(gpath, 1, {"brand": brand_id, "gender": GENDERS[g][0]})
            gtotal = int(data.get("total") or 0) if data is not None else None
            stat["genders"][g] = gtotal
            if data is not None and gtotal <= SPLIT_ABOVE:
                self.crawl_pages(gfinal, data, g, g)
                continue
            if data is None:
                print(f"[trendyol] {brand} · {gpath}: {why} — беру только разделы")
            unreachable = False
            for name, cid in GENDER_SECTIONS:
                ok, _t, _s = self.crawl_node(name, cid, g, 0 if data is not None else 2, f"{g}/{name}")
                unreachable |= not ok
            if unreachable and data is not None:
                print(f"[trendyol] {brand}: часть разделов {g} недоступна — добираю из выдачи пола (до потолка)")
                rest = self.crawl_pages(gfinal, data, g, f"{g} (остаток)")
                if gtotal > CAP_ITEMS:          # за потолком что-то осталось — категории со страниц товаров
                    self.discover(rest, g, 0, f"{g} (остаток)")
        name, cid = ACCESSORY_SECTION
        self.crawl_node(name, cid, None, 0, "аксессуары")
        return stat


def _is_brand(p: dict, brand_id: str, brand: str) -> bool:
    ids = {str(w.get("id")) for w in (p.get("webBrands") or []) if isinstance(w, dict)}
    return brand_id in ids or str(p.get("brandId") or "") == brand_id or norm_brand(p.get("brand") or "") == norm_brand(brand)


def _card_attrs(p: dict) -> dict:
    """Свойства из карточки листинга (productCardAttributes — обычно только «Materyal»)."""
    out = {}
    raw = p.get("attributes") or p.get("attributeList") or ((p.get("productCardAttributes") or {}).get("attributes")) or []
    if isinstance(raw, dict):
        raw = [{"name": k, "value": v} for k, v in raw.items()]
    for a in raw if isinstance(raw, list) else []:
        if isinstance(a, dict):
            k = a.get("name") or a.get("key") or a.get("attributeName") or (a.get("attribute") or {}).get("name")
            v = a.get("value") or a.get("unitText") or a.get("attributeValueName") or (a.get("attributeValue") or {}).get("name")
            if isinstance(k, str) and isinstance(v, (str, int, float)) and str(v).strip():
                out[k.strip()] = str(v).strip()
    return out


def _ld_product(html: str) -> dict | None:
    """JSON-LD товара (ProductGroup / Product) со страницы товара."""
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            ld = json.loads(m.group(1))
        except ValueError:
            continue
        for obj in (ld if isinstance(ld, list) else [ld]):
            if isinstance(obj, dict) and obj.get("@type") in ("ProductGroup", "Product"):
                return obj
    return None


def _card_to_product(p: dict, brand: str, gender: str | None, image_size: str) -> Product | None:
    now, old = _prices(p)
    if not now:
        return None
    disc = discount_pct(now, old)
    cid = p.get("contentId") or p.get("id")
    name = re.sub(r"\s+", " ", str(p.get("name") or "")).strip()
    url = str(p.get("url") or "")
    if not cid or not name or not url:
        return None
    category = str((p.get("category") or {}).get("name") or "")

    style_code = None
    m = _STYLE_RE.search(name)
    m2 = None if m else _STYLE2_RE.search(name)
    if m:
        style_code = f"{m.group(1)}-{m.group(2).upper()}"
        name = name[:m.start()].strip() or name
    elif m2:
        style_code = m2.group(1)
        name = name[:m2.start()].strip() or name
    low = _tr_lower(name)  # та же длина, что у name: «İ»/«I» заменяются одной буквой
    cm = _COLOR_RE.match(low) or _COLOR_RE.search(low)  # обычно цвет — первое слово названия
    colors = [name[cm.start():cm.end()].strip().title()] if cm else []

    imgs = [i for i in (p.get("images") or []) if isinstance(i, str)] or ([p["image"]] if p.get("image") else [])
    in_stock = not (p.get("tagStockBar") or {}).get("isSoldOut")

    return Product(
        source="trendyol",
        source_item_id=str(cid),
        brand=brand,
        title=name,
        category=category,
        type=_type_of(category, name),
        gender=gender or _gender_from_text(name),
        price_now=now,
        price_old=old if disc is not None else None,
        currency="TRY",
        discount_pct=disc,
        sizes=[],                      # только со страницы товара (кэш)
        colors=colors,
        images=[_image(i, image_size) for i in imgs[:MAX_IMAGES]],
        url=url if url.startswith("http") else BASE + (url if url.startswith("/") else "/" + url),
        in_stock=in_stock,
        style_code=style_code,
        attrs=_card_attrs(p),
    )


# ---------- страница товара ----------

def _size_label(v: str) -> str:
    v = str(v).strip()
    return "one size" if _tr_lower(v) in ("tek ebat", "std", "standart", "tek beden") else v


def _pdp_details(client: _Client, url: str, info: dict | None = None) -> dict | None:
    """Размеры (в наличии и все), пол, цвет, цена и продавец со страницы товара. info["status"] — HTTP-код."""
    info = info if info is not None else {}
    r = client.get(url)
    info["status"] = r.status_code
    if r.is_redirect:
        loc = r.headers.get("location") or ""
        if "-p-" not in loc:
            info["redirect"] = loc
            return None
        r = client.get(loc if loc.startswith("http") else BASE + loc)
        info["status"] = r.status_code
        if r.is_redirect:
            return None
    if r.status_code != 200:
        return None
    gallery = _decode_after(r.text, GALLERY_KEY) or {}
    prod = gallery.get("product") or {}
    if not prod:
        return None
    ml = prod.get("merchantListing") or {}
    variants = [v for v in (ml.get("variants") or prod.get("variants") or []) if isinstance(v, dict)]
    sizes, all_sizes = [], []
    for v in variants:
        if not v.get("value"):
            continue
        size = _size_label(v["value"])  # beautifiedValue — в нижнем регистре ("xs")
        if size not in all_sizes:
            all_sizes.append(size)
        if v.get("inStock") and size not in sizes:
            sizes.append(size)
    winner = ml.get("winnerVariant") or {}
    prod_in_stock = bool(prod.get("inStock", True))
    if not all_sizes and prod_in_stock and winner.get("inStock", True):
        sizes, all_sizes = ["one size"], ["one size"]       # безразмерный товар (сумка, ремень…)
    if not prod_in_stock:
        sizes = []
    color = None
    attrs: dict[str, str] = {}
    ld = _ld_product(r.text)
    if ld:
        # свойства товара: [{"name": "Kalıp", "unitText": "Slim Fit"}, {"name": "Materyal", "unitText": "%100 Pamuk"} …]
        for a in ld.get("additionalProperty") or []:
            if isinstance(a, dict) and isinstance(a.get("name"), str):
                v = a.get("unitText") or a.get("value")
                if isinstance(v, (str, int, float)) and str(v).strip():
                    attrs[a["name"].strip()] = re.sub(r"\s+", " ", str(v)).strip()
        # "color" бывает кодом ("142331"); тогда берём свойство «Renk» (цветовая группа)
        for c in [ld.get("color"), attrs.get("Renk")]:
            if isinstance(c, str) and re.search(r"[^\W\d_]", c):
                color = re.sub(r"^\d+\s*[-_.]\s*", "", c.strip())  # "01-Siyah" -> "Siyah"
                break
        if color:
            attrs.setdefault("ld_color", color)
    g = _tr_lower((prod.get("gender") or {}).get("name"))
    gender = ("kids" if re.search(r"[çc]ocuk|bebek|kids", g) else "men" if "erkek" in g
              else "women" if re.search(r"kad[ıi]n", g) else None)
    merchant = ml.get("merchant") or {}
    pr = winner.get("price") or {}
    price = _num((pr.get("discountedPrice") or {}).get("value")) or _num((pr.get("sellingPrice") or {}).get("value"))
    return {
        "sizes": sizes,
        "all_sizes": all_sizes,
        "in_stock": bool(sizes),
        "gender": gender,
        "gender_raw": (prod.get("gender") or {}).get("name"),
        "color": str(color).strip() if color else None,
        "category_path": (prod.get("category") or {}).get("hierarchy"),
        "merchant": merchant.get("name"),
        "merchant_id": merchant.get("id"),
        "price": price,
        "attrs": attrs,
        "images": [i for i in (prod.get("images") or []) if isinstance(i, str)],
    }


def _cache_entry(d: dict, official: bool | None, image_size: str) -> dict:
    e = {"sizes": d["sizes"], "all_sizes": d["all_sizes"], "checked_at": _now_iso(), "price": d["price"],
         "merchant": d["merchant"], "official": official, "gender": d["gender"], "color": d["color"],
         "category_path": d["category_path"], "attrs": d["attrs"]}
    if len(d["images"]) > 1:
        e["images"] = [_image(i, image_size) for i in d["images"][:MAX_IMAGES]]
    return e


def _pdp_queue(items: dict[str, dict], cache: dict, brands: list[str], limit: int, max_age_h: float) -> tuple[list[str], int, int]:
    """Очередь проверки: (id по порядку, сколько никогда не проверялись, сколько устарели)."""
    order = {b: i for i, b in enumerate(brands)}
    never_disc: dict[tuple, list] = {}
    never_rest: dict[tuple, list] = {}
    stale = []
    for cid, rec in items.items():
        e = cache.get(cid)
        if not e or not e.get("checked_at"):
            bucket = never_disc if rec.get("discount_pct") else never_rest
            bucket.setdefault((rec["brand"], rec.get("gender") or "?"), []).append(rec)   # бренд × пол по очереди
        elif _age_hours(e.get("checked_at")) > max_age_h:
            stale.append((e.get("checked_at") or "", cid))

    def round_robin(groups: dict[tuple, list]) -> list[str]:
        for lst in groups.values():   # внутри группы: официальный продавец, потом большая скидка
            lst.sort(key=lambda r: (not r.get("_official"), -(r.get("discount_pct") or 0)))
        seqs = [groups[k] for k in sorted(groups, key=lambda k: (order.get(k[0], 99), k[1]))]
        out, i = [], 0
        while any(i < len(s) for s in seqs):
            out += [s[i]["source_item_id"] for s in seqs if i < len(s)]
            i += 1
        return out

    n_never = sum(len(v) for v in never_disc.values()) + sum(len(v) for v in never_rest.values())
    queue = round_robin(never_disc) + round_robin(never_rest) + [cid for _, cid in sorted(stale)]
    return queue[:max(0, limit)], n_never, len(stale)


def _run_pdp(todo: list[str], items: dict[str, dict], cache: dict, cache_path: Path, delay: float,
             concurrency: int, image_size: str) -> dict:
    """Проверяет страницы товаров (1–2 потока), пишет кэш каждые 100 товаров. Останавливается на 403/429."""
    res = {"done": 0, "in_stock": 0, "zero": 0, "gone": 0, "errors": 0, "blocked": None, "gone_ids": []}
    if not todo:
        return res
    lock = threading.Lock()
    stop = threading.Event()
    queue = list(todo)
    t0 = time.time()

    def worker(n: int) -> None:
        client = _Client((delay * 0.85, delay * 1.15))
        if n:
            time.sleep(delay / 2)             # потоки не стартуют одновременно
        while not stop.is_set():
            with lock:
                if not queue:
                    return
                cid = queue.pop(0)
            rec = items[cid]
            info: dict = {}
            try:
                d = _pdp_details(client, rec["url"], info)
            except SourceBlocked as e:
                with lock:
                    res["blocked"] = str(e)
                stop.set()
                return
            except Exception as e:
                with lock:
                    res["errors"] += 1
                    cache.setdefault(cid, {})["error_at"] = _now_iso()
                print(f"[trendyol] страница товара {cid}: {e.__class__.__name__}: {e}")
                continue
            with lock:
                if d is None:
                    if info.get("status") in (404, 410) or (info.get("redirect") is not None and "-p-" not in info["redirect"]):
                        cache[cid] = {"sizes": [], "all_sizes": [], "checked_at": _now_iso(), "gone": True,
                                      "official": rec.get("_official")}
                        res["gone"] += 1
                        res["gone_ids"].append(cid)
                    else:
                        res["errors"] += 1
                        cache.setdefault(cid, {})["error_at"] = _now_iso()
                else:
                    cache[cid] = _cache_entry(d, rec.get("_official"), image_size)
                    if d["sizes"]:
                        res["in_stock"] += 1
                    else:
                        res["zero"] += 1
                        res["gone_ids"].append(cid)
                res["done"] += 1
                done = res["done"]
                if done % 100 == 0 or done == len(todo):
                    _save_json(cache_path, cache)
                if done % 50 == 0 or done == len(todo):
                    rate = done / max(1e-9, time.time() - t0)
                    left = len(todo) - done
                    print(f"[trendyol] PDP {done}/{len(todo)}: в наличии {res['in_stock']}, без размеров {res['zero']}, "
                          f"удалены {res['gone']}, ошибок {res['errors']} · {rate * 60:.0f}/мин, "
                          f"осталось ~{left / max(rate, 1e-9) / 60:.0f} мин", flush=True)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(max(1, min(int(concurrency), 4)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _save_json(cache_path, cache)
    res["seconds"] = round(time.time() - t0)
    return res


def _to_product(rec: dict, e: dict) -> Product:
    """Запись листинга + проверенные размеры из кэша -> Product для run.py."""
    d = {k: v for k, v in rec.items() if not k.startswith("_")}
    p = Product(**d)
    p.sizes = list(e.get("sizes") or [])
    p.in_stock = True
    p.gender = rec.get("gender") or e.get("gender")
    if e.get("color") and not p.colors:  # оттенок из названия точнее цветовой группы
        p.colors = [e["color"]]
    p.attrs = dict(p.attrs or {})
    p.attrs.update(e.get("attrs") or {})
    if e.get("category_path"):
        p.attrs["category_path"] = e["category_path"]
        if not p.type:
            p.type = _type_of(e["category_path"].split("/")[-1], p.title) or classify_type(_tr_lower(e["category_path"]))
    if len(e.get("images") or []) > len(p.images):   # на странице товара фото бывает больше, чем в карточке
        p.images = list(e["images"])
    p.fetched_at = e.get("checked_at") or p.fetched_at
    return p


def fetch(query: Query, **opts) -> list[Product]:
    LAST_RUN.clear()
    so = query.source_opts or {}
    slugs: dict = so.get("brand_slugs") or {}
    official_only = bool(so.get("official_only", False))
    merchant_ids = {str(x) for x in (so.get("merchant_ids") or [])}
    image_size = str(so.get("image_size", "1200/1800") or "")
    pdp_per_run = int(so.get("pdp_per_run", so.get("pdp_limit", 2500)) or 0)
    pdp_max_age = float(so.get("pdp_max_age_hours", 72) or 72)
    pdp_delay = max(0.5, float(so.get("pdp_delay", 1.2) or 1.2))
    pdp_conc = int(so.get("pdp_concurrency", 2) or 1)
    ld = so.get("listing_delay") or [1.0, 2.0]
    listing_delay = (max(1.0, float(ld[0])), max(1.0, float(ld[-1])))
    listing_max_age = float(so.get("listing_max_age_hours", 0) or 0)
    cache_path = Path(so.get("pdp_cache_file") or PDP_CACHE_FILE)
    listing_path = Path(so.get("listing_file") or LISTING_FILE)

    have = {norm_brand(b) for b in slugs}
    for b in query.brands:
        if norm_brand(b) not in have:
            print(f"[trendyol] ВНИМАНИЕ: для бренда {b!r} нет slug в source_opts.trendyol.brand_slugs — пропускаю")
    brands = [b for b in slugs if query.wants_brand(b)]
    if not brands:
        print("[trendyol] нет брендов для обхода")
        LAST_RUN["skipped"] = True
        return []

    # ---- 1. листинги ----
    prev = _load_json(listing_path, {}) or {}
    prev_items: dict = prev.get("items") or {}
    t0 = time.time()
    reuse = (listing_max_age > 0 and prev.get("complete") and _age_hours(prev.get("crawled_at")) <= listing_max_age
             and set(brands) <= set(prev.get("brands") or []))
    complete, reason, blocked = True, "", False
    if reuse:
        items = {k: v for k, v in prev_items.items() if v.get("brand") in brands}
        print(f"[trendyol] листинги не обхожу: полный обход {prev.get('crawled_at')} свежее "
              f"{listing_max_age:g} ч — {len(items)} товаров")
        brand_stats = prev.get("brand_stats") or {}
    else:
        client = _Client(listing_delay)
        crawl = _Crawl(client, query, image_size, official_only, merchant_ids)
        crawl.cat_map = dict(prev.get("cat_map") or {})
        brand_stats = {}
        try:
            for brand in brands:
                m = _SLUG_RE.match(str(slugs[brand]).strip())
                if not m:
                    print(f"[trendyol] ВНИМАНИЕ: slug {slugs[brand]!r} для {brand} не похож на «имя-x-b123» — пропускаю")
                    continue
                before = len(crawl.items)
                tb = time.time()
                try:
                    st = crawl.crawl_brand(brand, m.group(1).lower(), m.group(2))
                except SourceBlocked:
                    raise
                except Exception as e:
                    crawl.errors += 1
                    print(f"[trendyol] бренд {brand}: ошибка обхода {e.__class__.__name__}: {e}")
                    st = {}
                st["listed"] = len(crawl.items) - before
                st["seconds"] = round(time.time() - tb)
                brand_stats[brand] = st
                print(f"[trendyol] === {brand}: в каталоге {st['listed']} товаров (всего у бренда на сайте "
                      f"{st.get('brand_total')}), {st['seconds']} с, запросов всего {client.requests}", flush=True)
        except SourceBlocked as e:
            blocked = True
            complete = False
            reason = f"доступ ограничен посреди обхода: {e}"
            print(f"[trendyol] {e} — останавливаюсь")
            if not crawl.items and not prev_items:
                raise
        items = crawl.items
        if not client.listings_ok and not blocked:
            raise RuntimeError(f"trendyol: ни одна страница листинга не разобрана ({crawl.errors} ошибок) — "
                               "сайт недоступен или сменил формат")
        if crawl.errors and complete:
            complete, reason = False, f"листингов/страниц с ошибкой: {crawl.errors}"
        if crawl.truncated:
            complete = False
            reason = reason or f"обход ограничен max_pages={query.max_pages} (тест)"
        if not complete:   # неполный обход — прошлые записи не выбрасываем
            for k, v in prev_items.items():
                if k not in items and v.get("brand") in brands:
                    items[k] = v
        print(f"[trendyol] листинги: {len(items)} товаров, {client.requests} запросов, {time.time() - t0:.0f} с"
              + (f"; за потолком {MAX_LISTING_PAGES} стр. осталось ~{crawl.capped}" if crawl.capped else "")
              + (f"; категорий найдено со страниц товаров {crawl.discovered}" if crawl.discovered else "")
              + (f"; отсеяно по продавцу {crawl.skipped_seller}" if crawl.skipped_seller else ""))
        if crawl.excluded:
            print("[trendyol] не берём: " + ", ".join(f"{k} — {v}" for k, v in crawl.excluded.most_common(12)))
        untyped = Counter(r.get("category") for r in items.values() if not r.get("type"))
        if untyped:
            print("[trendyol] категории без типа: " + ", ".join(f"{k} {v}" for k, v in untyped.most_common(10)))
        keep_other = {k: v for k, v in prev_items.items() if v.get("brand") not in brands}   # бренды вне этого запуска
        _save_json(listing_path, {"crawled_at": _now_iso(), "complete": complete, "brands": brands,
                                  "brand_stats": brand_stats, "capped": crawl.capped, "cat_map": crawl.cat_map,
                                  "items": {**keep_other, **items}})

    # ---- 2. размеры со страниц товаров ----
    cache: dict = _load_json(cache_path, {}) or {}
    todo, n_never, n_stale = _pdp_queue(items, cache, brands, 0 if blocked else pdp_per_run, pdp_max_age)
    print(f"[trendyol] проверка размеров: никогда не проверялись {n_never}, устарели (> {pdp_max_age:g} ч) {n_stale}; "
          f"в этот запуск {len(todo)} (потоков {pdp_conc}, пауза {pdp_delay:g} с)", flush=True)
    res = _run_pdp(todo, items, cache, cache_path, pdp_delay, pdp_conc, image_size)
    if res["blocked"]:
        print(f"[trendyol] {res['blocked']} — проверка размеров остановлена")
        LAST_RUN.setdefault("reason", f"проверка размеров остановлена: {res['blocked']}")

    # ---- 3. наружу — только проверенные размеры в наличии ----
    out: list[Product] = []
    per = {}
    for cid, rec in items.items():
        b = rec["brand"]
        g = rec.get("gender") or (cache.get(cid) or {}).get("gender") or "?"
        s = per.setdefault((b, g), {"listed": 0, "verified": 0, "zero": 0, "unchecked": 0})
        s["listed"] += 1
        e = cache.get(cid) or {}
        if not e.get("checked_at") or e.get("sizes") is None:
            s["unchecked"] += 1
            continue
        if not e.get("sizes"):
            s["zero"] += 1
            continue
        if e.get("gender") == "kids":
            continue
        gender = rec.get("gender") or e.get("gender")
        if query.genders and gender and gender not in query.genders:
            continue
        try:
            p = _to_product(rec, e)
        except Exception as ex:
            print(f"[trendyol] товар {cid} пропущен: {ex}")
            continue
        if query.types and p.type not in query.types:
            continue
        s["verified"] += 1
        out.append(p)
    gone = set(res["gone_ids"]) | {cid for cid, r in items.items() if r.get("in_stock") is False}
    LAST_RUN["gone_ids"] = sorted(gone)
    if not complete:
        LAST_RUN.update(partial=True, reason=reason)
    total_unchecked = sum(s["unchecked"] for s in per.values())
    runs_left = math.ceil(total_unchecked / pdp_per_run) if pdp_per_run else None
    print("[trendyol] по брендам (в каталоге / с размерами / распродано / ещё не проверено):")
    for (b, g), s in sorted(per.items()):
        print(f"[trendyol]   {b[:18]:<18} {g:<6} {s['listed']:>6} / {s['verified']:>6} / {s['zero']:>5} / {s['unchecked']:>6}")
    LAST_RUN["stats"] = {"listed": len(items), "returned": len(out), "unchecked": total_unchecked,
                         "pdp": {k: v for k, v in res.items() if k != "gone_ids"}, "runs_left": runs_left}
    print(f"[trendyol] итого: в каталоге {len(items)}, отдаю с проверенными размерами {len(out)}, "
          f"ещё не проверено {total_unchecked}"
          + (f" — при {pdp_per_run} за запуск это ещё ~{runs_left} запусков" if runs_left else ""))
    return out


def verify(rows: list[dict], query: Query | None = None, **opts) -> dict[str, dict | None]:
    """Перепроверка товаров по ссылке (страница товара): размеры в наличии сейчас.
    {id: обновлённая строка | None — точно нет (404 / нет в наличии)}; кого нет в ответе — неизвестно.
    Цена здесь не обновляется (её даёт только листинг). Результат пишется и в кэш размеров."""
    so = (query.source_opts if query else None) or {}
    cache_path = Path(so.get("pdp_cache_file") or PDP_CACHE_FILE)
    cache: dict = _load_json(cache_path, {}) or {}
    image_size = str(so.get("image_size", "1200/1800") or "")
    client = _Client((1.0, 1.5))
    out: dict[str, dict | None] = {}
    now_iso = _now_iso()
    for r in rows:
        vid, url = str(r.get("source_item_id")), str(r.get("url") or "")
        if not url.startswith(BASE):
            continue
        info: dict = {}
        try:
            d = _pdp_details(client, url, info)
        except SourceBlocked as e:
            print(f"[trendyol] перепроверка остановлена: {e}")
            break
        except Exception as e:
            print(f"[trendyol] перепроверка {vid}: {e.__class__.__name__}, пропускаю")
            continue
        if d is None:
            if info.get("status") in (404, 410):
                out[vid] = None
                cache[vid] = {"sizes": [], "all_sizes": [], "checked_at": now_iso, "gone": True}
            continue
        cache[vid] = _cache_entry(d, (cache.get(vid) or {}).get("official"), image_size)
        if not d["in_stock"]:
            out[vid] = None
            continue
        row = dict(r)
        row.update(sizes=d["sizes"], in_stock=True, fetched_at=now_iso)
        out[vid] = row
    if out:
        _save_json(cache_path, cache)
    return out
