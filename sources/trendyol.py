"""Trendyol (витрина TR): товары со скидкой со страниц брендов.

Как устроено:
  * Листинг бренда — обычная HTML-страница https://www.trendyol.com/<путь>?pi=N
    (36 карточек на страницу); товары лежат в JSON
    window["__single-search-result__PROPS"] = {...}.data.products[].
  * Пол и категория задаются ПУТЁМ, а не параметрами: <slug>-x-b<бренд>-g<пол>-c<категория>
    (g1 = kadın, g2 = erkek; c82 = giyim, c114 = ayakkabı, c117 = çanta, c27 = aksesuar).
    Параметр ?wg= (пол) запрещён в robots.txt, поэтому его не используем.
  * ?lpd=30 — «самая низкая цена за 30 дней». По умолчанию ВЫКЛЮЧЕН: он оставляет
    товары с мелкими скидками и выкидывает долгие акции «Sepette %40» (проверено:
    Pierre Cardin erkek giyim, стр. 1 без lpd — 17 товаров со скидкой ≥40%, с lpd — 1).
    Если включить и поиск «сдастся» и покажет чужой бренд
    (appliedSearchStrategy = RECOMMENDED_BRAND), раздел переобходится без lpd.
  * ds=true на страницах брендов ломает фильтр бренда (выдаёт другие марки) — не используем.
  * Не используем /sr, ?q=, ?sst=, ?prc=, ?wg= (robots.txt) и apigw.trendyol.com.
  * Из-за IP вне Турции сайт перекидывает на /en/select-country — передаём те же куки,
    что ставит выбор страны на самом сайте: storefrontId=1; countryCode=TR; language=tr.
  * Цена — та, что видна на карточке (singlePrice.salePrice, часто это цена «в корзине»,
    Sepette), старая цена — зачёркнутая (strikethroughPrice).
  * Размеры есть только на странице товара: для лучших N товаров (source_opts.pdp_limit)
    читаем window["__envoy_product-image-gallery__PROPS"].product.

Настройки (config.json → source_opts.trendyol):
  brand_slugs        {"Pierre Cardin": "pierre-cardin-x-b122", ...} — обязательно
  pdp_limit          сколько страниц товаров открыть за запуск ради размеров (20)
  lowest_price_days  значение lpd (по умолчанию 0 — не фильтровать на стороне сайта)
  categories         какие разделы обходить: ["giyim", "ayakkabi", "canta", "aksesuar"]
  official_only      брать только карточки с бейджем официального продавца (false)
  merchant_ids       белый список id продавцов (необязательно)
  image_size         "1200/1800" — размер картинок CDN; "" — оригинал
"""
from __future__ import annotations

import json
import re
import sys

import requests

from .base import BROWSER_UA, TYPE_ORDER, Product, Query, classify_type, discount_pct, norm_brand, polite_sleep

if hasattr(sys.stdout, "reconfigure") and (sys.stdout.encoding or "").lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")  # турецкие буквы в выводе не роняют cp1251-консоль

BASE = "https://www.trendyol.com"
CDN = "https://cdn.dsmcdn.com"
PAGE_SIZE = 36
STOREFRONT_COOKIES = {"storefrontId": "1", "countryCode": "TR", "language": "tr", "platform": "web"}
LISTING_KEY = 'window["__single-search-result__PROPS"]='
GALLERY_KEY = 'window["__envoy_product-image-gallery__PROPS"]='

GENDERS = {"women": (1, "kadin"), "men": (2, "erkek")}
CLOTHING_TYPES = set(TYPE_ORDER) - {"обувь", "сумки", "аксессуары"}
# раздел -> (id категории, делить по полу?, какие типы там лежат)
SECTIONS = {
    "giyim": (82, True, CLOTHING_TYPES),
    "ayakkabi": (114, True, {"обувь"}),
    "canta": (117, True, {"сумки"}),
    # бренд+пол+аксессуары сайт уводит на /sr (запрещено), поэтому аксессуары без пола
    "aksesuar": (27, False, {"аксессуары"}),
}

# Названия категорий Trendyol, которые classify_type не знает или путает
# («Pijama Takımı» иначе станет костюмом). Проверяются по названию категории до classify_type.
TR_CATEGORY_TYPES = [
    (r"pijama|gecelik|sabahl[ıi]k|i[çc] [çc]ama[şs][ıi]r|i[çc] giyim|\batlet|s[üu]tyen|[çc]orap|termal|i[çc]lik|fanila|b[üu]stiyer|korse", "нижнее бельё"),
    (r"^ceket$|blazer", "пиджаки и костюмы"),
    (r"(kot|deri|denim|s[üu]et|bomber|[şs]i[şs]me|kapitone)\s+ceket", "куртки и пальто"),
    (r"pantolon|\btayt|e[şs]ofman alt|kapri|salopet", "брюки"),
    (r"tunik", "рубашки"),
    (r"\btulum", "платья"),
    (r"s[üu]veter", "свитеры и кардиганы"),
    (r"\bpolar\b|e[şs]ofman [üu]st", "толстовки"),
    (r"ya[ğg]murluk|palto|k[üu]rk\b", "куртки и пальто"),
    (r"espadril|panduf|mokasen|oxford|ayakkab", "обувь"),
    (r"valiz|bavul|portf[öo]y|evrak", "сумки"),
    (r"\bsaat|g[öo]zl[üu][ğk]|kartl[ıi]k|kol d[üu][ğg]me|\bbere\b|[şs]al\b|e[şs]arp|fular|bandana|\btak[ıi]\b|kolye|bileklik|k[üu]pe|y[üu]z[üu]k|anahtarl[ıi]k|[şs]emsiye|boyunluk|papyon", "аксессуары"),
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


class SourceBlocked(RuntimeError):
    pass


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

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        })
        for k, v in STOREFRONT_COOKIES.items():
            self.s.cookies.set(k, v, domain=".trendyol.com", path="/")
        self.requests = 0
        self.listings_ok = 0   # сколько страниц листинга удалось разобрать

    def get(self, url: str) -> requests.Response:
        if self.requests:
            polite_sleep()
        self.requests += 1
        try:
            r = self.s.get(url, timeout=30, allow_redirects=False)
        except requests.RequestException as e:  # одна повторная попытка при сетевой ошибке
            print(f"[trendyol] сеть: {e.__class__.__name__}, повтор через паузу")
            polite_sleep(5, 8)
            r = self.s.get(url, timeout=30, allow_redirects=False)
        if r.status_code in (403, 429):
            raise SourceBlocked(f"trendyol ответил {r.status_code} на {url} — похоже, доступ ограничен")
        loc = r.headers.get("location") or ""
        if r.is_redirect and "select-country" in loc:
            raise SourceBlocked("trendyol перекидывает на выбор страны — куки витрины TR не приняты")
        return r


def _sections(query: Query) -> list[tuple[str, int, str | None]]:
    """[(раздел, id категории, пол|None)] с учётом query.types/genders и source_opts.categories."""
    opts = query.source_opts or {}
    names = opts.get("categories") or list(SECTIONS)
    genders = [g for g in ("men", "women") if not query.genders or g in query.genders]
    out = []
    for name in names:
        if name not in SECTIONS:
            print(f"[trendyol] неизвестный раздел {name!r}, есть: {list(SECTIONS)}")
            continue
        cat_id, by_gender, types = SECTIONS[name]
        if query.types and not types & set(query.types):
            continue
        if by_gender:
            out += [(name, cat_id, g) for g in genders]
        else:
            out.append((name, cat_id, None))
    return out


def _section_path(slug_text: str, brand_id: str, cat_name: str, cat_id: int, gender: str | None) -> str:
    if gender:
        g_id, g_slug = GENDERS[gender]
        return f"/{slug_text}-{g_slug}-{cat_name}-x-b{brand_id}-g{g_id}-c{cat_id}"
    return f"/{slug_text}-{cat_name}-x-b{brand_id}-c{cat_id}"


def _listing(client: _Client, url: str) -> dict | None:
    r = client.get(url)
    if r.is_redirect:
        loc = r.headers.get("location") or ""
        why = "на /sr (запрещено robots.txt)" if re.search(r"(^|/)sr(/|\?|$)", loc) else f"на {loc}"
        print(f"[trendyol] {url} перенаправляет {why} — раздел пропущен")
        return None
    if r.status_code == 404:
        print(f"[trendyol] {url}: 404 — раздел пропущен")
        return None
    r.raise_for_status()
    props = _decode_after(r.text, LISTING_KEY)
    if not props or not isinstance(props.get("data"), dict):
        raise ValueError(f"на {url} нет __single-search-result__PROPS — формат сайта изменился?")
    client.listings_ok += 1
    return props["data"]


def _is_brand(p: dict, brand_id: str, brand: str) -> bool:
    ids = {str(w.get("id")) for w in (p.get("webBrands") or []) if isinstance(w, dict)}
    return brand_id in ids or norm_brand(p.get("brand") or "") == norm_brand(brand)


def _gender_path(slug_text: str, brand_id: str, gender: str) -> str:
    """Вся выдача бренда для пола, без категории: /ramsey-erkek-x-b185-g2."""
    g_id, g_slug = GENDERS[gender]
    return f"/{slug_text}-{g_slug}-x-b{brand_id}-g{g_id}"


def _crawl_section(client: _Client, query: Query, brand: str, slug_text: str, brand_id: str,
                   section: tuple[str, int, str | None], lpd: int, state: dict | None = None,
                   path: str | None = None):
    """Отдаёт (карточка, пол раздела) по страницам одного раздела бренда.
    state["redirected"] = True, если раздел сразу увёл на /sr или 404 (тогда пробуем общую выдачу пола)."""
    cat_name, cat_id, gender = section
    path = path or _section_path(slug_text, brand_id, cat_name, cat_id, gender)
    use_lpd = bool(lpd)
    pi = 1
    while pi <= max(1, query.max_pages):
        params = ([f"lpd={lpd}"] if use_lpd else []) + ([f"pi={pi}"] if pi > 1 else [])
        url = BASE + path + ("?" + "&".join(params) if params else "")
        data = _listing(client, url)
        if data is None:
            if pi == 1 and state is not None:
                state["redirected"] = True
            return
        prods = [p for p in (data.get("products") or []) if isinstance(p, dict)]
        mine = [p for p in prods if _is_brand(p, brand_id, brand)]
        strategy = data.get("appliedSearchStrategy") or "DEFAULT"
        if strategy != "DEFAULT" or (prods and not mine):
            # поиск ничего не нашёл и подсунул «похожий бренд»
            if use_lpd and pi == 1:
                print(f"[trendyol] {path}: с lpd={lpd} нет товаров бренда ({strategy}) — обхожу без lpd")
                use_lpd = False
                continue
            print(f"[trendyol] {path}: товаров бренда нет ({strategy})")
            return
        total = int(data.get("total") or 0)
        print(f"[trendyol] {brand} · {path}{'?lpd=' + str(lpd) if use_lpd else ''} · стр. {pi}: "
              f"{len(mine)} карточек (всего в разделе {total})")
        for p in mine:
            yield p, gender
        if not prods or pi * PAGE_SIZE >= total:
            return
        pi += 1


def _card_to_product(p: dict, brand: str, gender: str | None, image_size: str) -> Product | None:
    now, old = _prices(p)
    disc = discount_pct(now, old)
    if disc is None:
        return None
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
    one_size = bool(p.get("isOneSize")) or _tr_lower(p.get("variantValue")) in ("tek ebat", "std", "standart")
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
        price_old=old,
        currency="TRY",
        discount_pct=disc,
        sizes=["one size"] if one_size and in_stock else [],
        colors=colors,
        images=[_image(i, image_size) for i in imgs[:8]],
        url=url if url.startswith("http") else BASE + (url if url.startswith("/") else "/" + url),
        in_stock=in_stock,
        style_code=style_code,
    )


def _pdp_details(client: _Client, url: str) -> dict | None:
    """Размеры в наличии, пол, цвет и продавец со страницы товара."""
    r = client.get(url)
    if r.is_redirect:
        loc = r.headers.get("location") or ""
        if "-p-" not in loc:
            return None
        r = client.get(loc if loc.startswith("http") else BASE + loc)
        if r.is_redirect:
            return None
    if r.status_code != 200:
        return None
    gallery = _decode_after(r.text, GALLERY_KEY) or {}
    prod = gallery.get("product") or {}
    if not prod:
        return None
    ml = prod.get("merchantListing") or {}
    variants = ml.get("variants") or prod.get("variants") or []
    sizes = []
    for v in variants:
        if isinstance(v, dict) and v.get("inStock") and v.get("value"):
            size = str(v["value"]).strip()  # beautifiedValue — в нижнем регистре ("xs")
            size = "one size" if _tr_lower(size) in ("tek ebat", "std", "standart") else size
            if size not in sizes:
                sizes.append(size)
    color = None
    m = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S)
    if m:
        try:
            ld = json.loads(m.group(1))
        except ValueError:
            ld = None
        if isinstance(ld, dict):
            # "color" бывает кодом ("142331"); тогда берём свойство «Renk» (цветовая группа)
            renk = [a.get("unitText") or a.get("value") for a in ld.get("additionalProperty") or []
                    if isinstance(a, dict) and a.get("name") == "Renk"]
            for c in [ld.get("color")] + renk:
                if isinstance(c, str) and re.search(r"[^\W\d_]", c):
                    color = re.sub(r"^\d+\s*[-_.]\s*", "", c.strip())  # "01-Siyah" -> "Siyah"
                    break
    g = _tr_lower((prod.get("gender") or {}).get("name"))
    merchant = ml.get("merchant") or {}
    return {
        "sizes": sizes,
        "in_stock": bool(prod.get("inStock", True)) and bool(sizes),
        "gender": {"erkek": "men", "kadın": "women", "kadin": "women", "çocuk": "kids", "cocuk": "kids"}.get(g),
        "color": str(color).strip() if color else None,
        "category_path": (prod.get("category") or {}).get("hierarchy"),
        "merchant": merchant.get("name"),
    }


def fetch(query: Query, **opts) -> list[Product]:
    so = query.source_opts or {}
    slugs: dict = so.get("brand_slugs") or {}
    lpd = int(so.get("lowest_price_days", 0) or 0)
    pdp_limit = int(so.get("pdp_limit", 20) or 0)
    official_only = bool(so.get("official_only", False))
    merchant_ids = {str(x) for x in (so.get("merchant_ids") or [])}
    image_size = str(so.get("image_size", "1200/1800") or "")

    have = {norm_brand(b) for b in slugs}
    for b in query.brands:
        if norm_brand(b) not in have:
            print(f"[trendyol] ВНИМАНИЕ: для бренда {b!r} нет slug в source_opts.trendyol.brand_slugs — пропускаю")
    brands = [b for b in slugs if query.wants_brand(b)]
    if not brands:
        print("[trendyol] нет брендов для обхода")
        return []
    sections = _sections(query)
    if not sections:
        print(f"[trendyol] нет разделов под типы {query.types} / пол {query.genders}")
        return []

    client = _Client()
    found: dict[str, tuple[Product, bool]] = {}   # contentId -> (товар, официальный продавец)
    seen = skipped_seller = failed_sections = 0
    try:
        for brand in brands:
            m = _SLUG_RE.match(str(slugs[brand]).strip())
            if not m:
                print(f"[trendyol] ВНИМАНИЕ: slug {slugs[brand]!r} для {brand} не похож на «имя-x-b123» — пропускаю")
                continue
            slug_text, brand_id = m.group(1).lower(), m.group(2)
            # Если раздел «бренд + пол + категория» уводит на /sr (так у Ramsey), после основных разделов
            # обходим общую выдачу бренда для этого пола (/ramsey-erkek-x-b185-g2) — она открыта.
            jobs = [(section, None) for section in sections]
            fallback_queued = set()
            while jobs:
                section, path = jobs.pop(0)
                state = {"redirected": False}
                try:
                    for card, gender in _crawl_section(client, query, brand, slug_text, brand_id, section, lpd,
                                                       state, path):
                        seen += 1
                        official = bool(card.get("hasOfficialSellerBadge"))
                        if (official_only and not official) or (merchant_ids and str(card.get("merchantId")) not in merchant_ids):
                            skipped_seller += 1
                            continue
                        try:
                            prod = _card_to_product(card, brand, gender, image_size)
                        except Exception as e:  # одна битая карточка не мешает остальным
                            print(f"[trendyol] пропущена карточка {card.get('contentId')}: {e}")
                            continue
                        if not prod or not prod.in_stock or (prod.discount_pct or 0) < query.discount_min:
                            continue
                        if query.types and prod.type not in query.types:
                            continue
                        if query.genders and prod.gender and prod.gender not in query.genders:
                            continue
                        if prod.source_item_id not in found:
                            found[prod.source_item_id] = (prod, official)
                except SourceBlocked:
                    raise
                except Exception as e:
                    failed_sections += 1
                    print(f"[trendyol] раздел {section} бренда {brand} пропущен: {e}")
                g = section[2]
                if state["redirected"] and path is None and g and g not in fallback_queued:
                    fallback_queued.add(g)
                    gp = _gender_path(slug_text, brand_id, g)
                    print(f"[trendyol] {brand}: раздел недоступен — дальше беру общую выдачу {gp}")
                    jobs.append((("все", 0, g), gp))
    except SourceBlocked as e:
        if not found:
            raise
        print(f"[trendyol] {e} — останавливаюсь, отдаю то, что успел собрать")
        pdp_limit = 0

    if not client.listings_ok and failed_sections:
        raise RuntimeError(f"trendyol: ни одна страница листинга не разобрана ({failed_sections} разделов с ошибкой) — "
                           "сайт недоступен или сменил формат")
    print(f"[trendyol] карточек бренда: {seen}, подходят по скидке ≥{query.discount_min}%: {len(found)}"
          + (f", отсеяно по продавцу: {skipped_seller}" if skipped_seller else ""))

    # Размеры: страницы товаров для лучших позиций (официальный продавец, затем большая скидка)
    items = sorted(found.values(), key=lambda x: (not x[1], -(x[0].discount_pct or 0)))
    todo = [p for p, _ in items if p.sizes != ["one size"]][:pdp_limit]
    for n, prod in enumerate(todo, 1):
        try:
            d = _pdp_details(client, prod.url)
        except SourceBlocked as e:
            print(f"[trendyol] {e} — размеры для остальных не загружены")
            break
        except Exception as e:
            print(f"[trendyol] страница товара {prod.source_item_id} не прочитана: {e}")
            continue
        if not d:
            print(f"[trendyol] страница товара {prod.source_item_id}: нет данных о размерах")
            continue
        prod.sizes = d["sizes"]
        prod.in_stock = d["in_stock"]
        prod.gender = prod.gender or d["gender"]
        if d["color"] and not prod.colors:  # оттенок из названия точнее цветовой группы
            prod.colors = [d["color"]]
        if not prod.type and d["category_path"]:
            prod.type = _type_of(d["category_path"].split("/")[-1], prod.title) or classify_type(_tr_lower(d["category_path"]))
        print(f"[trendyol] PDP {n}/{len(todo)} {prod.source_item_id}: продавец {d['merchant'] or '?'}, "
              f"размеры {', '.join(prod.sizes) or 'нет в наличии'}")

    out = [p for p, _ in items if p.in_stock and not (query.genders and p.gender not in query.genders)]
    no_sizes = sum(1 for p in out if not p.sizes)
    if no_sizes:
        print(f"[trendyol] без размеров (страница товара не открывалась, pdp_limit={pdp_limit}): {no_sizes}")
    return out
