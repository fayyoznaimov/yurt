"""Турецкие магазины на платформе Akinon «Project Zero» (Next.js App Router).

    fetch(query, site="pcardin_tr")   # pierrecardin.com.tr, раздел Garage Sale
    fetch(query, site="cacharel_tr")  # cacharel.com.tr, Outlet + «Online'a Özel» (только мужское)

Каталог отдаётся обычным HTML; данные лежат в RSC-потоке Next.js —
куски self.__next_f.push([1,"..."]) с экранированным JSON. Склеиваем куски,
находим блок {"pagination":{"current_page":..}, ..., "products":[...]} и
разбираем товары. Нужен браузерный User-Agent (на «инструментальные» UA сайт
отвечает 403). Защиты от ботов нет, ничего не обходим.

robots.txt запрещает /c/*, ?search_text=, sorter=, ps=, price=,
attributes_filterable_* — их не используем. Разрешены page= и category_ids=.

Дополнительно в config.json → source_opts.<источник> можно задать:
    "sections": ["/garage-sale/"]   # свои разделы (пути) вместо стандартных
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urlsplit

import requests

from .base import BROWSER_UA, Product, Query, classify_type, discount_pct, polite_sleep

SITES = {
    "pcardin_tr": {
        "base": "https://www.pierrecardin.com.tr",
        "brand": "Pierre Cardin",
        "sections": ["/garage-sale/"],
        # фасет category_ids по полу (проверено: сочетается с page=)
        "gender_facet": {"men": 2, "women": 27},
        "only_gender": None,
        "type_facet": None,
    },
    "cacharel_tr": {
        "base": "https://www.cacharel.com.tr",
        "brand": "Cacharel",
        "sections": ["/outlet/", "/online-ozel-urunler/"],
        "gender_facet": None,
        "only_gender": "men",  # на сайте только мужская одежда
        # category_ids: 14 = Koleksiyon (одежда), 2 = Ayakkabı & Aksesuar
        "type_facet": {"clothes": 14, "shoes_acc": 2},
    },
}

SHOES_ACC_TYPES = {"обувь", "аксессуары", "сумки"}

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
    return out[:4]


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
        return None

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
                if attempt == 2:
                    raise
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


def fetch(query: Query, **opts) -> list[Product]:
    site = opts.get("site")
    if site not in SITES:
        raise ValueError(f"akinon: неизвестный site={site!r}, ожидается один из {list(SITES)}")
    cfg = dict(SITES[site])
    so = query.source_opts or {}
    if so.get("sections"):
        cfg["sections"] = list(so["sections"])

    if not query.wants_brand(cfg["brand"]):
        print(f"[{site}] бренд {cfg['brand']} не в фильтре — пропускаю")
        return []
    if query.genders and cfg["only_gender"] and cfg["only_gender"] not in query.genders:
        print(f"[{site}] на сайте только {cfg['only_gender']}, а нужно {query.genders} — пропускаю")
        return []
    if query.genders and not {"men", "women"} & set(query.genders):
        print(f"[{site}] нужного пола {query.genders} на сайте нет — пропускаю")
        return []

    client = _Client(site, cfg)
    found: dict[str, Product] = {}
    pages_ok, errors = 0, []
    max_pages = max(1, int(query.max_pages or 1))

    try:
        for path in cfg["sections"]:
            for params, facet_gender in _section_params(cfg, query):
                gender_fallback = facet_gender or cfg["only_gender"]
                page, num_pages = 1, None
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
                    except Exception as e:  # одна плохая страница не роняет весь источник
                        errors.append(f"{label}: {e}")
                        print(f"[{site}] {label}: ошибка, страница пропущена: {e}")
                        break
                    cur = int(pagination.get("current_page") or page)
                    num_pages = int(pagination.get("num_pages") or 1)
                    if cur != page:  # сайт вернул другую страницу — дальше выдачи нет
                        print(f"[{site}] {label}: сайт отдал страницу {cur} вместо {page}, раздел закончен")
                        break
                    pages_ok += 1

                    kept = skipped_brand = 0
                    for it in items:
                        try:
                            brand_attr = (it.get("attributes") or {}).get("filterable_brand")
                            if brand_attr and _tr_lower(brand_attr) != _tr_lower(cfg["brand"]):
                                skipped_brand += 1
                                continue
                            prod = to_product(it, site, cfg, gender_fallback)
                        except Exception as e:  # битый товар — пропускаем
                            print(f"[{site}] товар {it.get('pk')} пропущен: {e}")
                            continue
                        if prod is None or (prod.discount_pct or 0) < query.discount_min:
                            continue
                        if query.genders and prod.gender and prod.gender not in query.genders:
                            continue
                        found.setdefault(prod.source_item_id, prod)
                        kept += 1
                    if items and skipped_brand == len(items):
                        print(f"[{site}] {label}: все товары чужого бренда — похоже на чужую страницу")
                    print(f"[{site}] {label} стр. {page}/{num_pages} (всего {pagination.get('total_count')}): "
                          f"товаров {len(items)}, со скидкой ≥{query.discount_min:g}%: {kept}")
                    page += 1
    except SiteBlocked as e:
        if not pages_ok:
            raise RuntimeError(f"[{site}] источник недоступен: {e}") from e
        print(f"[{site}] {e}; останавливаюсь, отдаю уже собранное")

    if not pages_ok:
        raise RuntimeError(f"[{site}] не удалось разобрать ни одной страницы: " + "; ".join(errors or ["нет данных"]))
    print(f"[{site}] итого: {len(found)} товаров со скидкой, запросов: {client.requests_made}")
    return list(found.values())
