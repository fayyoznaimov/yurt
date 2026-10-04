"""Турецкие магазины на платформе Akinon «Project Zero» (Next.js App Router).

    fetch(query, site="pcardin_tr")   # pierrecardin.com.tr — весь каталог (мужское + женское)
    fetch(query, site="cacharel_tr")  # cacharel.com.tr — весь каталог (на сайте только мужское)

Весь каталог: сначала широкие разделы (sections: «/erkek-1/», «/kadin-1/», «/garage-sale/»…) целиком,
потом все категории из карты сайта (sitemap.xml → sitemap/categories-1) — они почти всегда подмножество
широких, поэтому категория бросается, как только две страницы подряд не дали новых товаров. Товары
сводятся по pk. В конце — сверка с картой товаров (sitemap/products-1): сколько из неё нашлось в листингах.
Скидка не обязательна (price_old = None, если её нет). Детское и парфюмерия не берутся.

Каталог отдаётся обычным HTML; данные лежат в RSC-потоке Next.js —
куски self.__next_f.push([1,"..."]) с экранированным JSON. Склеиваем куски,
находим блок {"pagination":{"current_page":..}, ..., "products":[...]} и
разбираем товары. Нужен браузерный User-Agent (на «инструментальные» UA сайт
отвечает 403). Защиты от ботов нет, ничего не обходим.

robots.txt запрещает /c/*, ?search_text=, sorter=, ps=, price=,
attributes_filterable_* — их не используем. Разрешены page= и category_ids=.

Дополнительно в config.json → source_opts.<источник> можно задать:
    "sections": ["/erkek-1/", ...]  # свои широкие разделы (пути) вместо стандартных — обходятся целиком
    "discover": true                # добавить категории из sitemap.xml (по умолчанию да)
    "max_pages": 200                # потолок страниц на один раздел
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import requests

from .base import BROWSER_UA, MAX_IMAGES, Product, Query, classify_type, discount_pct, polite_sleep

SITES = {
    "pcardin_tr": {
        "base": "https://www.pierrecardin.com.tr",
        "brand": "Pierre Cardin",
        "sections": ["/erkek-1/", "/kadin-1/", "/garage-sale/"],
        # фасет category_ids по полу (проверено: сочетается с page=)
        "gender_facet": {"men": 2, "women": 27},
        "only_gender": None,
        "type_facet": None,
    },
    "cacharel_tr": {
        "base": "https://www.cacharel.com.tr",
        "brand": "Cacharel",
        "sections": ["/tum-erkek-urunleri-1/", "/outlet/", "/online-ozel-urunler/", "/erkek-aksesuar/",
                     "/erkek-ayakkabi/"],
        "gender_facet": None,
        "only_gender": "men",  # на сайте только мужская одежда
        # category_ids: 14 = Koleksiyon (одежда), 2 = Ayakkabı & Aksesuar
        "type_facet": {"clothes": 14, "shoes_acc": 2},
    },
}

SHOES_ACC_TYPES = {"обувь", "аксессуары", "сумки"}
# не одежда — в каталог не берём (по filterable_product_base_type / названию)
EXCLUDE_RE = re.compile(r"parf[üu]m|kozmetik|deodorant|kolonya|\bedt\b|\bedp\b", re.I)

# filterable_product_base_type (значение и подпись) -> ключ base.TYPE_ORDER.
# classify_type путает часть турецких названий («Ceket» = пиджак, а не куртка;
# «Pijama Takım» — не костюм; «Deniz Şortu», «Palto» не ловит), поэтому сначала словарь.
BASE_TYPE_MAP = {
    "ayakkabı": "обувь", "terlik": "обувь", "bot": "обувь", "sneaker": "обувь", "sandalet": "обувь",
    "çanta": "сумки", "sırt çantası": "сумки", "evrak çantası": "сумки",
    "pijama takım": "нижнее бельё", "boxer": "нижнее бельё", "atlet": "нижнее бельё",
    "iç giyim": "нижнее бельё", "çorap": "нижнее бельё", "külot": "нижнее бельё",
    "jean pantolon": "джинсы", "pantolon (jean)": "джинсы", "jean": "джинсы",
    "t-shirt": "футболки и поло", "tişört": "футболки и поло", "polo": "футболки и поло",
    "polo yaka tişört": "футболки и поло",
    "sweatshirt": "толстовки",
    "ceket": "пиджаки и костюмы", "takım elbise": "пиджаки и костюмы", "smokin": "пиджаки и костюмы",
    "blazer": "пиджаки и костюмы",
    "elbise": "платья", "örme elbise": "платья", "dokuma elbise": "платья", "tulum": "платья",
    "etek": "юбки", "dokuma etek": "юбки", "örme etek": "юбки",
    "şort": "шорты", "deniz şortu": "шорты", "şort / bermuda": "шорты", "bermuda": "шорты",
    "gömlek": "рубашки", "bluz": "рубашки", "tunik": "рубашки",
    "triko kazak": "свитеры и кардиганы", "kazak /triko": "свитеры и кардиганы", "kazak": "свитеры и кардиганы",
    "triko hırka": "свитеры и кардиганы", "hırka": "свитеры и кардиганы", "triko": "свитеры и кардиганы",
    "mont": "куртки и пальто", "deri mont": "куртки и пальто", "deri mont-kaban": "куртки и пальто",
    "kaban": "куртки и пальто", "palto": "куртки и пальто", "trenchcoat": "куртки и пальто",
    "trençkot": "куртки и пальто", "yelek": "куртки и пальто", "dokuma yelek": "куртки и пальто",
    "pardesü": "куртки и пальто", "yağmurluk": "куртки и пальто", "şişme mont": "куртки и пальто",
    "pantolon": "брюки", "pantolon (klasik)": "брюки", "chinos": "брюки", "pantolon (kanvas-chino)": "брюки",
    "örme pantolon": "брюки", "eşofman altı": "брюки", "triko pantolon": "брюки",
    "pantolon (triko)": "брюки", "tayt": "брюки",
    "kemer": "аксессуары", "kol düğmesi": "аксессуары", "şapka": "аксессуары", "kravat": "аксессуары",
    "papyon": "аксессуары", "mendil": "аксессуары", "atkı": "аксессуары", "bere": "аксессуары",
    "eldiven": "аксессуары", "cüzdan": "аксессуары", "kartlık": "аксессуары", "havlu": "аксессуары",
}

GENDER_MAP = {
    "erkek": "men", "kadın": "women", "kadin": "women",
    "erkek çocuk": "kids", "kız çocuk": "kids", "çocuk": "kids", "unisex": None,
}

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]+|\\.)*)"\]\)')
_CANON_RE = re.compile(
    r'<link[^>]*rel="canonical"[^>]*href="([^"]+)"|<link[^>]*href="([^"]+)"[^>]*rel="canonical"'
    r'|<meta[^>]*property="og:url"[^>]*content="([^"]+)"', re.I)
_STYLE_RE = re.compile(r"-(\d{6,10})-([a-z0-9]{2,8})/?$", re.I)
_DEC = json.JSONDecoder()

TIMEOUT = 60
# Итог последнего fetch() для run.py: partial — собрано не всё (блокировка / пропущенные страницы),
# тогда отсутствие товара в выдаче не значит, что он распродан; skipped — источник сознательно пропущен.
LAST_RUN: dict = {}


class SiteBlocked(RuntimeError):
    """Сайт отвечает 403/429 — дальше не ходим."""


def _tr_lower(s: str | None) -> str:
    """lower() с учётом турецких İ/I."""
    return re.sub(r"\s+", " ", (s or "").replace("İ", "i").replace("I", "ı").lower()).strip()


def _headers() -> dict:
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    }


# ---------- разбор страницы ----------

def rsc_text(html: str) -> str:
    """Склеивает куски self.__next_f.push([1,"..."]) в один RSC-поток."""
    parts = _PUSH_RE.findall(html)
    if not parts:
        raise ValueError("на странице нет RSC-данных (self.__next_f) — формат сайта изменился?")
    return "".join(json.loads('"' + p + '"', strict=False) for p in parts)


def canonical_url(html: str) -> str | None:
    m = _CANON_RE.search(html)
    return next((g for g in m.groups() if g), None) if m else None


def parse_listing(html: str) -> tuple[dict, list[dict]]:
    """Возвращает (pagination, products) из HTML страницы раздела."""
    s = rsc_text(html)
    i = s.find('"pagination":{"current_page"')
    if i < 0:
        raise ValueError("в RSC-потоке нет блока pagination/products — формат сайта изменился?")
    pagination, _ = _DEC.raw_decode(s, i + len('"pagination":'))
    j = s.find('"products":[', i)
    if j < 0:
        raise ValueError("в RSC-потоке нет списка products — формат сайта изменился?")
    products, _ = _DEC.raw_decode(s, j + len('"products":'))
    if not isinstance(products, list):
        raise ValueError("products — не список (ссылка RSC?) — формат сайта изменился?")
    return pagination, [p for p in products if isinstance(p, dict)]


def _kw_label(p: dict, key: str) -> str | None:
    kw = (p.get("attributes_kwargs") or {}).get(key)
    return kw.get("label") if isinstance(kw, dict) else None


def _type_of(base_value: str | None, base_label: str | None, product_type: str | None, title: str) -> str | None:
    for key in (base_value, base_label):
        t = BASE_TYPE_MAP.get(_tr_lower(key))
        if t:
            return t
    return classify_type(base_label, base_value, product_type, title)


def _gender_of(attrs: dict, p: dict, fallback: str | None) -> str | None:
    for raw in (attrs.get("filterable_gender"), _kw_label(p, "integration_gender")):
        key = _tr_lower(raw)
        if key in GENDER_MAP:
            return GENDER_MAP[key]
    return fallback


def _images(p: dict) -> list[str]:
    imgs = [im for im in (p.get("productimage_set") or []) if isinstance(im, dict) and im.get("image")]
    imgs = [im for im in imgs if im.get("status", "active") == "active"]
    imgs.sort(key=lambda im: im.get("order") or 0)
    out = []
    for im in imgs:
        url = im["image"]
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http") and url not in out:
            out.append(url)
    return out[:MAX_IMAGES]


# Какие свойства товара сохранять в Product.attrs (переводит describe.py): все filterable_*, кроме
# служебных, и несколько integration_* / текстовых полей с составом и сезоном.
ATTR_SKIP = {"filterable_brand", "filterable_size", "filterable_secondary_size", "filterable_gender"}
ATTR_EXTRA = ("integration_fabric_fiber", "integration_fabric_blends", "integration_season",
              "deri_bilgi", "elyaf_bilgi", "product_name_new", "mensei")


def _attrs(p: dict) -> dict:
    """{"filterable_fit": "Slim Fit", "filterable_neck_type": "Polo Yaka", "integration_fabric_fiber": "%100 Pamuk", …}.
    Для filterable_* берём подпись (label) — она понятнее значения: «Slim Fit» вместо «Slim», «Tişört» вместо «T-Shirt»."""
    raw = p.get("attributes") or {}
    out = {}
    for k, v in raw.items():
        if not ((k.startswith("filterable_") and k not in ATTR_SKIP) or k in ATTR_EXTRA):
            continue
        label = _kw_label(p, k) if k.startswith("filterable_") else None
        val = label or v
        if isinstance(val, (str, int, float)) and str(val).strip() and not str(val).startswith("$"):
            out[k] = re.sub(r"\s+", " ", str(val)).strip()
    return out


def _sizes(p: dict) -> tuple[list[str], bool]:
    """(размеры в наличии, есть ли вообще вариант по размеру)."""
    sizes, has_size_variant = [], False
    for v in ((p.get("extra_data") or {}).get("variants") or []):
        if not isinstance(v, dict) or v.get("attribute_key") != "integration_size":
            continue
        has_size_variant = True
        for o in v.get("options") or []:
            if isinstance(o, dict) and o.get("in_stock") and o.get("label"):
                label = str(o["label"]).strip()
                if label not in sizes:
                    sizes.append(label)
    return sizes, has_size_variant


def to_product(p: dict, site: str, cfg: dict, gender_fallback: str | None) -> Product | None:
    """Один товар из листинга -> Product. None, если товар не подходит/битый."""
    attrs = p.get("attributes") or {}
    now = float(p["price"])
    old = float(p["retail_price"]) if p.get("retail_price") not in (None, "") else None
    disc = discount_pct(now, old)
    if disc is None:
        old = None            # без скидки: цена одна

    title = re.sub(r"\s+", " ", str(p.get("name") or "")).strip()
    if not title:
        return None
    base_value = attrs.get("filterable_product_base_type")
    base_label = _kw_label(p, "filterable_product_base_type")
    product_type = attrs.get("filterable_product_type")
    category = base_label or base_value or product_type or ""

    path = str(p.get("absolute_url") or "")
    m = _STYLE_RE.search(path)
    if m:
        style_code = f"{m.group(1)}-{m.group(2).upper()}"
    elif p.get("base_code") and attrs.get("integration_color"):
        style_code = f"{p['base_code']}-{attrs['integration_color']}"
    else:
        style_code = None

    color = attrs.get("filterable_color") or (_kw_label(p, "integration_color") or "").title()
    sizes, has_size_variant = _sizes(p)
    if has_size_variant:
        in_stock = bool(sizes)
    else:  # безразмерные (сумки и т.п.)
        in_stock = bool(p.get("in_stock"))
        sizes = ["one size"] if in_stock else []

    return Product(
        source=site,
        source_item_id=str(p["pk"]),
        brand=cfg["brand"],
        title=title,
        category=category,
        type=_type_of(base_value, base_label, product_type, title),
        gender=_gender_of(attrs, p, gender_fallback),
        price_now=now,
        price_old=old,
        currency=str(p.get("currency_type") or "try").upper(),
        discount_pct=disc,
        sizes=sizes,
        colors=[color] if color else [],
        images=_images(p),
        url=cfg["base"] + (path if path.startswith("/") else "/" + path),
        in_stock=in_stock,
        style_code=style_code,
        attrs=_attrs(p),
    )


# ---------- сеть ----------

class _Client:
    def __init__(self, site: str, cfg: dict):
        self.site, self.cfg = site, cfg
        self.host = urlsplit(cfg["base"]).netloc.lower()
        self.session = requests.Session()
        self.session.headers.update(_headers())
        self.requests_made = 0

    def _get(self, url: str) -> str:
        if self.requests_made:
            polite_sleep()
        self.requests_made += 1
        r = self.session.get(url, timeout=TIMEOUT)
        if r.status_code in (403, 429):
            raise SiteBlocked(f"{self.host} ответил {r.status_code} на {url} — похоже, доступ закрыт")
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text

    def page(self, path: str, params: dict) -> str | None:
        """HTML страницы раздела; None, если сайт дважды вернул чужую страницу (общий кэш CDN)."""
        url = self.cfg["base"] + path + (("?" + urlencode(params)) if params else "")
        for attempt in (1, 2):
            try:
                html = self._get(url)
            except SiteBlocked:
                raise
            except requests.RequestException as e:
                resp = getattr(e, "response", None)
                if attempt == 2 or (resp is not None and resp.status_code in (404, 410)):
                    raise                      # 404 — раздела нет, повторять незачем
                print(f"[{self.site}] сетевая ошибка ({e}), повтор через паузу")
                polite_sleep(5, 8)
                continue
            canon = canonical_url(html)
            canon_host = urlsplit(canon).netloc.lower() if canon else self.host
            if canon_host == self.host:
                return html
            print(f"[{self.site}] {url}: пришла страница другого сайта ({canon_host}) — общий кэш, "
                  + ("повторю после паузы" if attempt == 1 else "страницу пропускаю"))
            if attempt == 1:
                polite_sleep(5, 8)
        return None


def _section_params(cfg: dict, query: Query) -> list[tuple[dict, str | None]]:
    """Набор фасетов category_ids для обхода: [(params, пол по фасету)]."""
    gf = cfg.get("gender_facet")
    wanted = [g for g in query.genders if g in ("men", "women")]
    if gf and len(set(wanted)) == 1:
        g = wanted[0]
        return [({"category_ids": gf[g]}, g)]

    tf = cfg.get("type_facet")
    if tf and query.types:
        types = set(query.types)
        if types <= SHOES_ACC_TYPES:
            return [({"category_ids": tf["shoes_acc"]}, None)]
        if not types & SHOES_ACC_TYPES:
            return [({"category_ids": tf["clothes"]}, None)]
    return [({}, None)]


_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


def _sitemap_locs(client: "_Client", name: str) -> list[str]:
    """Адреса из карты сайта: name = "categories" / "products" (sitemap.xml → sitemap/<name>-N)."""
    try:
        index = client._get(client.cfg["base"] + "/sitemap.xml")
    except SiteBlocked:
        raise
    except Exception as e:
        print(f"[{client.site}] sitemap.xml не прочитан: {e}")
        return []
    out: list[str] = []
    for sm in _LOC_RE.findall(index):
        if f"/sitemap/{name}-" not in sm:
            continue
        try:
            out += _LOC_RE.findall(client._get(sm))
        except SiteBlocked:
            raise
        except Exception as e:
            print(f"[{client.site}] {sm} не прочитан: {e}")
    return out


def _category_paths(client: "_Client", seeds: list[str]) -> list[str]:
    """Категории из карты сайта, которые можно обходить (robots.txt: не /c/*, без фильтров и параметров)."""
    out = []
    for loc in _sitemap_locs(client, "categories"):
        u = urlsplit(loc)
        path = u.path if u.path.endswith("/") else u.path + "/"
        if (u.netloc.lower() != client.host or u.query or path.startswith("/c/") or "attributes" in path
                or path == "/" or path in seeds or path in out):
            continue
        out.append(path)
    return out


def _path_gender(path: str) -> str | None:
    p = path.lower()
    if re.search(r"(^|[/-])(kadin|women)", p):
        return "women"
    if re.search(r"(^|[/-])(erkek|men)([/-]|$)", p):
        return "men"
    return None


def fetch(query: Query, **opts) -> list[Product]:
    site = opts.get("site")
    LAST_RUN.clear()
    if site not in SITES:
        raise ValueError(f"akinon: неизвестный site={site!r}, ожидается один из {list(SITES)}")
    cfg = dict(SITES[site])
    so = query.source_opts or {}
    if so.get("sections"):
        cfg["sections"] = list(so["sections"])
    discover = bool(so.get("discover", True))

    if not query.wants_brand(cfg["brand"]):
        print(f"[{site}] бренд {cfg['brand']} не в фильтре — пропускаю")
        LAST_RUN["skipped"] = True
        return []
    if query.genders and cfg["only_gender"] and cfg["only_gender"] not in query.genders:
        print(f"[{site}] на сайте только {cfg['only_gender']}, а нужно {query.genders} — пропускаю")
        LAST_RUN["skipped"] = True
        return []
    if query.genders and not {"men", "women"} & set(query.genders):
        print(f"[{site}] нужного пола {query.genders} на сайте нет — пропускаю")
        LAST_RUN["skipped"] = True
        return []

    client = _Client(site, cfg)
    found: dict[str, Product] = {}
    seen_pk: set[str] = set()          # все pk из листингов (и отброшенные) — для «новых товаров нет»
    seen_url: set[str] = set()
    pages_ok, errors = 0, []
    skipped = {"детское": 0, "не одежда": 0, "чужой бренд": 0}
    max_pages = max(1, int(query.max_pages or 1))
    t0 = time.time()

    def crawl(path: str, params: dict, facet_gender: str | None, seed: bool) -> None:
        nonlocal pages_ok
        gender_fallback = facet_gender or cfg["only_gender"] or _path_gender(path)
        page, num_pages, idle = 1, None, 0
        while page <= max_pages and (num_pages is None or page <= num_pages):
            q = dict(params)
            if page > 1:
                q["page"] = page
            label = f"{path}{'?' + urlencode(q) if q else ''}"
            try:
                html = client.page(path, q)
                if html is None:
                    errors.append(f"{label}: дважды пришла страница другого магазина (общий кэш)")
                    page += 1
                    continue
                pagination, items = parse_listing(html)
            except SiteBlocked:
                raise
            except requests.HTTPError as e:
                if not seed and page == 1 and e.response is not None and e.response.status_code in (404, 410):
                    return                       # категория из карты сайта больше не существует
                errors.append(f"{label}: {e}")
                print(f"[{site}] {label}: ошибка, раздел прерван: {e}")
                return
            except Exception as e:  # одна плохая страница не роняет весь источник
                if not seed and page == 1:
                    return                       # не листинг (посадочная страница вроде /erkek/)
                errors.append(f"{label}: {e}")
                print(f"[{site}] {label}: ошибка, раздел прерван: {e}")
                return
            cur = int(pagination.get("current_page") or page)
            num_pages = int(pagination.get("num_pages") or 1)
            if cur != page:  # сайт вернул другую страницу — дальше выдачи нет
                break
            pages_ok += 1
            new = 0
            for it in items:
                pk = str(it.get("pk"))
                if pk in seen_pk:
                    continue
                new += 1
                seen_pk.add(pk)
                seen_url.add(str(it.get("absolute_url") or "").strip("/"))
                try:
                    attrs = it.get("attributes") or {}
                    brand_attr = attrs.get("filterable_brand")
                    if brand_attr and _tr_lower(brand_attr) != _tr_lower(cfg["brand"]):
                        skipped["чужой бренд"] += 1
                        continue
                    base = f"{attrs.get('filterable_product_base_type') or ''} {_kw_label(it, 'filterable_product_base_type') or ''}"
                    if EXCLUDE_RE.search(_tr_lower(base)) or EXCLUDE_RE.search(_tr_lower(it.get("name"))):
                        skipped["не одежда"] += 1
                        continue
                    prod = to_product(it, site, cfg, gender_fallback)
                except Exception as e:  # битый товар — пропускаем
                    print(f"[{site}] товар {it.get('pk')} пропущен: {e}")
                    continue
                if prod is None:
                    continue
                if prod.gender == "kids":
                    skipped["детское"] += 1
                    continue
                if (prod.discount_pct or 0) < query.discount_min:
                    continue
                if query.genders and prod.gender and prod.gender not in query.genders:
                    continue
                found.setdefault(prod.source_item_id, prod)
            idle = 0 if new else idle + 1
            if seed or page == 1 or new:
                print(f"[{site}] {label} стр. {page}/{num_pages} (всего {pagination.get('total_count')}): "
                      f"товаров {len(items)}, новых {new}; в каталоге {len(found)}", flush=True)
            if not seed and idle >= 2:            # категория — подмножество уже обойдённого
                break
            page += 1

    try:
        for path in cfg["sections"]:
            for params, facet_gender in _section_params(cfg, query):
                crawl(path, params, facet_gender, True)
        extra = _category_paths(client, cfg["sections"]) if discover else []
        if extra:
            print(f"[{site}] категорий из карты сайта: {len(extra)} — проверяю, нет ли в них новых товаров")
        for path in extra:
            crawl(path, {}, None, False)
        if discover:
            urls = {urlsplit(u).path.strip("/") for u in _sitemap_locs(client, "products")}
            if urls:
                hit = len(urls & seen_url)
                print(f"[{site}] сверка с картой товаров: {len(urls)} в sitemap, из них в листингах {hit} "
                      f"({hit * 100 // max(1, len(urls))}%; в карте сайта в основном старые/распроданные товары); "
                      f"всего в листингах {len(seen_pk)} товаров")
    except SiteBlocked as e:
        if not pages_ok:
            raise RuntimeError(f"[{site}] источник недоступен: {e}") from e
        print(f"[{site}] {e}; останавливаюсь, отдаю уже собранное")
        LAST_RUN.update(partial=True, reason=f"доступ закрыт посреди обхода: {e}")

    if not pages_ok:
        raise RuntimeError(f"[{site}] не удалось разобрать ни одной страницы: " + "; ".join(errors or ["нет данных"]))
    if errors and not LAST_RUN.get("partial"):
        LAST_RUN.update(partial=True, reason=f"пропущено страниц с ошибкой: {len(errors)}")
    n_disc = sum(1 for p in found.values() if p.discount_pct)
    n_stock = sum(1 for p in found.values() if p.in_stock)
    print(f"[{site}] итого: {len(found)} товаров (в наличии {n_stock}, со скидкой {n_disc}), "
          f"страниц {pages_ok}, запросов {client.requests_made}, {time.time() - t0:.0f} с"
          + "".join(f"; {k}: {v}" for k, v in skipped.items() if v))
    return list(found.values())


# ---------- перепроверка товаров по ссылке (для run.py) ----------

def parse_pdp(rsc: str, pk: str) -> dict | None:
    """Цена и размеры в наличии со страницы товара (RSC-поток). None — страница не разобрана."""
    i = rsc.find('{"product":{"pk":%s,' % pk)
    if i < 0:
        return None
    try:
        p = _DEC.raw_decode(rsc, i)[0]["product"]
        now = float(p["price"])
        old = float(p["retail_price"]) if p.get("retail_price") not in (None, "") else None
    except (ValueError, KeyError, TypeError):
        return None
    key = '"variants":[{"attribute_key":"integration_size"'
    variants = None
    j = rsc.find(key)
    while j >= 0:                  # блок размеров именно этого товара (тот же base_code), а не рекомендаций
        try:
            cand = _DEC.raw_decode(rsc, j + len('"variants":'))[0]
        except ValueError:
            cand = None
        codes = {str(((o or {}).get("product") or {}).get("base_code") or "")
                 for v in (cand or []) if isinstance(v, dict) for o in (v.get("options") or []) if isinstance(o, dict)}
        if cand and str(p.get("base_code") or "") in codes:
            variants = cand
            break
        j = rsc.find(key, j + 1)
    sizes, has_size_variant = _sizes({"extra_data": {"variants": variants or []}})
    if has_size_variant:
        in_stock = bool(sizes)
    else:
        in_stock = bool(p.get("in_stock"))
        sizes = ["one size"] if in_stock else []
    return {"price_now": now, "price_old": old, "discount_pct": discount_pct(now, old),
            "sizes": sizes, "in_stock": in_stock}


def verify(rows: list[dict], query: Query | None = None, **opts) -> dict[str, dict | None]:
    """Перепроверка товаров, которых нет в выдаче распродажи (или у которых пропали размеры), по их ссылкам.
    Возвращает {id: обновлённая строка | None — точно нет (404 / нет в наличии)}; кого нет в ответе — неизвестно."""
    site = opts.get("site")
    cfg = SITES.get(site)
    if not cfg:
        return {}
    client = _Client(site, cfg)
    out: dict[str, dict | None] = {}
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in rows:
        vid, url = str(r.get("source_item_id")), str(r.get("url") or "")
        if not url.startswith(cfg["base"]):
            continue
        if client.requests_made:
            polite_sleep()
        client.requests_made += 1
        try:
            resp = client.session.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"[{site}] перепроверка {vid}: сеть ({e.__class__.__name__}), пропускаю")
            continue
        if resp.status_code in (403, 429):
            print(f"[{site}] перепроверка остановлена: сайт ответил {resp.status_code}")
            break
        if resp.status_code in (404, 410):
            if resp.history:                           # 404 после перенаправления (гео / другая витрина) —
                continue                               # не доказательство распродажи
            out[vid] = None
            continue
        if resp.status_code != 200:
            continue
        resp.encoding = "utf-8"
        canon = canonical_url(resp.text)
        if canon and urlsplit(canon).netloc.lower() != client.host:
            continue                                   # чужая страница из общего кэша
        try:
            info = parse_pdp(rsc_text(resp.text), vid)
        except ValueError:
            info = None
        if info is None:
            continue
        if not info["in_stock"]:
            out[vid] = None
            continue
        row = dict(r)
        row.update(info, fetched_at=now_iso)
        out[vid] = row
    return out
