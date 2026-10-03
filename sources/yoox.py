"""YOOX, итальянская витрина yoox.com/it — через настоящий браузер (Playwright).

Сайт стоит за Akamai Bot Manager: обычный HTTP-запрос получает 403, поэтому
страницы открываются в Chromium. Данные берём ТОЛЬКО из того, что сайт сам
встроил в загруженную страницу:

    <script id="__NEXT_DATA__"> → props.pageProps.algoliaServerState.initialResults

Ключи Algolia со страницы не читаем и Algolia напрямую не вызываем. Защиту сайта
не обходим (никаких stealth-плагинов, подмены отпечатков, прокси): если YOOX не
пускает браузер, источник честно падает с понятным сообщением.

Проверено на сайте 02.10.2026:
  - страница бренда: /it/{donna|uomo}/shoponline/{slug}_d — так ссылается меню самого
    сайта («emporio armani_d»). Суффикс «_md» — группа брендов («dolce&gabbana_md»);
    «emporio armani_md» сайт НЕ узнаёт и показывает весь отдел без фильтра по бренду,
    поэтому каждая выдача проверяется на наличие фильтра model.brand.
  - пагинация: ?page=2 (на сайте с 1, в данных results.page с 0), 60 товаров на странице.
  - сортировка (меню «Ordina per»): sortBy=BestDeals («Migliori Occasioni», по убыванию
    скидки от retailPrice), sortBy=Latest («Nuovi arrivi»), пусто — «Consigliati».
  - товар: https://www.yoox.com/it/{variantId}/item
  - фото: https://www.yoox.com/images/items/{2 первые цифры}/{variantId в нижнем регистре}_14_{ракурс}.jpg
    ?impolicy=crop&width=464&height=591&gravity=Center (ширины из srcset: 165…1008),
    ракурсы — images.shotTypes (F — спереди, R — сзади, D — деталь, A, E …).
  - robots.txt: Disallow */*textsearch*, /item?*, */itemDetails*, /pg/*, /TellAFriend* …
    Наши адреса (…/shoponline/…, /it/<id>/item, /images/items/…) под запреты не попадают.

Цена: price_old = fullPrice (цена на YOOX до текущей уценки), price_now = currentPrice.
retailPrice (рекомендованная цена производителя) и markdownPercentageFromRetailPrice
НЕ используем для скидки: они завышают «скидку» относительно реальной цены на сайте.

Настройки (config.json → source_opts.yoox):
  brand_slugs      {"Emporio Armani": "emporio armani"} — без суффикса добавляется "_d";
                   можно указать явно: "dolce&gabbana_md"
  headless         true (по умолчанию) | false — обычное окно браузера на этом ПК
  browser_channel  null (Chromium из Playwright) | "chrome" | "msedge" — установленный браузер
  sort             "BestDeals" (по умолчанию) | "Latest" | ""
  max_images       60 — скольким товарам за запуск скачивать фото в site/img/yoox/
"""
from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import quote, unquote

from .base import (BROWSER_UA, MAX_IMAGES, Product, Query, classify_type, discount_pct, norm_brand,
                   polite_sleep)

SOURCE = "yoox"
BASE = "https://www.yoox.com"
DEPTS = {"women": "donna", "men": "uomo"}          # gender -> раздел сайта
SITE_DIR = Path(__file__).resolve().parent.parent / "site"
IMG_DIR = SITE_DIR / "img" / "yoox"
IMG_REL = "img/yoox"
IMG_PARAMS = "impolicy=crop&width=464&height=591&gravity=Center"
SHOT_ORDER = "frdaeb"                              # какие ракурсы брать первыми
SLUG_SAFE = "&'!*(),;=:@+"                          # символы, которые сайт оставляет в пути как есть

# Категории YOOX (it) -> ключи base.TYPE_ORDER. Проверяются ДО classify_type и
# сверху вниз, порядок важен: «Camicie di jeans» — рубашка, «Gonne jeans» — юбка,
# «Pantaloni jeans» — джинсы, «Pantaloni felpa» — брюки, «Maglie Gilet» — трикотаж,
# «Vestiti in Maglia» — платье, «Cappelli» — аксессуар (не «Cappe»). «Costumi» у YOOX — купальники (типа нет).
IT_TYPES = [
    ("обувь", r"scarp|sneaker|stringat|\bmule\b|zoccol|d[ée]collet|mocassin|stival|sandal|infradito|"
              r"ciabatt|espadrill|ballerin|slip-on|polacchin|francesin|calzature"),
    ("сумки", r"\bbors|pochette|zain|marsupi|valig|trolley|beauty case|shopper|bauletto"),
    ("нижнее бельё", r"intimo|reggisen|\bslip\b|pigiam|boxer|sottovest|vestagli|calze|calzini|"
                     r"guêpière|giarrettier|perizoma|culotte"),
    ("платья", r"vestit|tubin|chemisier"),
    ("футболки и поло", r"t-?shirt|\bpolo\b|\btop\b|canott|camisole|bustier|^body$"),
    ("юбки", r"gonn"),
    ("шорты", r"short|bermuda"),
    ("рубашки", r"camici|\bblus"),
    ("свитеры и кардиганы", r"magli|pullover|cardigan|dolcevita|lupetto|girocollo"),
    ("куртки и пальто", r"giubb|giacc|cappott|piumin|parka|bomber|\bcapp[ae]\b|trench|impermeab|"
                        r"soprabit|shearling|teddy|gilet|montgomery|caban|mantell|poncho|capispalla"),
    ("джинсы", r"jeans|denim"),
    ("брюки", r"pantalon|leggings|\bcargo\b|chino|jogger"),
    ("толстовки", r"felp"),
    ("пиджаки и костюмы", r"complet|tailleur|blazer|smoking|\babiti\b"),
    ("аксессуары", r"cappell|guant|sciarp|foulard|cintur|portafogl|portacart|portachiav|occhial|"
                   r"cravatt|papillon|gioiell|orolog|braccial|collan|orecchin|anell|berrett|accessori|"
                   r"piccola pelletteria"),
]
_IT_RE = [(t, re.compile(p, re.I)) for t, p in IT_TYPES]

# Текст из __NEXT_DATA__: только встроенная выдача, без настроек/ключей Algolia.
_READ_JS = """() => {
  const el = document.getElementById('__NEXT_DATA__');
  if (!el) return null;
  let d;
  try { d = JSON.parse(el.textContent); } catch (e) { return {results: null, error: 'bad json'}; }
  const st = ((d && d.props && d.props.pageProps) || {}).algoliaServerState;
  const ir = st && st.initialResults;
  if (!ir) return {results: null};
  const out = [];
  for (const k of Object.keys(ir)) {
    for (const r of (ir[k].results || [])) {
      out.push({index: k, page: r.page, nbPages: r.nbPages, nbHits: r.nbHits, params: r.params || '',
                hits: (r.hits || []).map(h => { const c = Object.assign({}, h);
                  delete c._highlightResult; delete c._rankingInfo; return c; })});
    }
  }
  return {results: out};
}"""

# Скачать картинку тем же браузером (те же cookies и сессия), вернуть base64.
_FETCH_IMG_JS = """async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include'});
    if (!r.ok) return {status: r.status};
    const buf = new Uint8Array(await r.arrayBuffer());
    let s = '';
    for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return {status: r.status, type: r.headers.get('content-type') || '', b64: btoa(s)};
  } catch (e) { return {status: 0, error: String(e)}; }
}"""


class Blocked(RuntimeError):
    """YOOX не отдал страницу браузеру (Akamai / заглушка)."""


class PageError(RuntimeError):
    """Страница открылась, но это не выдача (404, другой формат) — пропускаем раздел."""


# ---------------------------------------------------------------- разбор данных

def _num(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _pretty(name: str) -> str:
    return re.sub(r"'S\b", "'s", name.title())


def _brand(model_brand, names: dict[str, str]) -> str | None:
    """["GIORGIO ARMANI-150", "GIORGIO ARMANI-150 > EMPORIO ARMANI-128"] -> "Emporio Armani"."""
    if isinstance(model_brand, str):
        model_brand = [model_brand]
    if not model_brand:
        return None
    raw = str(model_brand[-1]).split(">")[-1].strip()
    raw = re.sub(r"-\d+$", "", raw).strip()
    if not raw:
        return None
    return names.get(norm_brand(raw)) or _pretty(raw)


def yoox_type(micro: str | None, macro: str | None, title: str | None = None) -> str | None:
    """Тип по категориям YOOX: сначала микро-категория, потом макро, потом название."""
    for text in (micro, macro):
        if not text:
            continue
        for t, rx in _IT_RE:
            if rx.search(text):
                return t
        t = classify_type(text)
        if t:
            return t
    return classify_type(title)


def _shots(h: dict) -> list[str]:
    img = h.get("images") or {}
    shots = [str(s).lower() for s in (img.get("shotTypes") or []) if s]
    if not shots:
        m = re.search(r"_([a-z])\.jpg", str(img.get("url") or ""), re.I)
        shots = [m.group(1).lower()] if m else ["f"]
    return sorted(dict.fromkeys(shots), key=lambda s: SHOT_ORDER.find(s) if s in SHOT_ORDER else 99)


def image_url(variant_id: str, shot: str) -> str:
    return f"{BASE}/images/items/{variant_id[:2]}/{variant_id.lower()}_14_{shot}.jpg?{IMG_PARAMS}"


def hit_attrs(h: dict) -> dict:
    """Описательные свойства товара из выдачи — как есть, по-итальянски (переводит describe.py).

    dynamicAttributes — словарь списков {"Collo-nck": ["Girocollo-grcll"], "Categorie-ctgr": ["Scarpe-clztr",
    "Scarpe-clztr > Stringate-strngt"]}; суффиксы «-код» и иерархию «A > B» describe.py разбирает сам."""
    model = h.get("model") or {}
    cats = model.get("categories") or {}
    out = {
        "composition": h.get("composition"),
        "colorLabel": h.get("colorLabel"),
        "color": h.get("color"),                       # ["Marrone-836D5C", "Marrone-836D5C > Testa di moro-7A485E"]
        "mainMaterial": model.get("mainMaterial"),
        "seasonality": model.get("seasonality"),
        "macro": cats.get("macro"),
        "micro": cats.get("micro"),
        "modelGender": model.get("gender"),
        "modelName": (model.get("modelName") or "").strip() or None,
        "sizeCodes": h.get("refinementSizeCodes"),      # ["Footwear", "Footwear > 1"] / ["International", …]
        "dynamicAttributes": h.get("dynamicAttributes") or None,
    }
    # всё прочее описательное, что может появиться в выдаче (на случай расширения формата YOOX)
    for k in ("description", "details", "fit", "pattern", "neckline", "sleeves", "closure"):
        if h.get(k):
            out[k] = h[k]
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def parse_hit(h: dict, gender: str, query: Query, names: dict[str, str]) -> Product | None:
    """Один товар из выдачи -> Product или None (б/у / не тот бренд / не тот тип / скидка меньше
    query.discount_min). При discount_min = 0 товар без уценки тоже берётся: price_old и discount_pct = None."""
    vid = str(h.get("variantId") or h.get("objectID") or "").strip()
    if not vid or h.get("preowned") or not h.get("purchasable", False):
        return None
    offer = h.get("modelBestOfferByPrice") or {}
    now, old = _num(offer.get("currentPrice")), _num(offer.get("fullPrice"))
    disc = discount_pct(now, old)
    if not now:
        return None
    if disc is None:
        # без уценки (fullPrice == currentPrice): берём, только если скидка не требуется
        if (query.discount_min or 0) > 0:
            return None
        old = None
    elif disc < (query.discount_min or 0):
        return None
    model = h.get("model") or {}
    brand = _brand(model.get("brand"), names)
    if not brand or not query.wants_brand(brand):
        return None
    cats = model.get("categories") or {}
    macro, micro = str(cats.get("macro") or "").strip(), str(cats.get("micro") or "").strip()
    color = str(h.get("colorLabel") or "").strip()
    title = " ".join(x for x in (brand, micro) if x) + (f", {color.lower()}" if color else "")
    ptype = yoox_type(micro, macro, title)
    if query.types and ptype not in query.types:
        return None
    sizes = [str(s).strip() for s in (h.get("availableSizes") or []) if str(s).strip()]
    return Product(
        source=SOURCE,
        source_item_id=vid,
        brand=brand,
        title=title,
        category=" > ".join(x for x in (macro.capitalize(), micro) if x),
        type=ptype,
        gender=gender,
        price_now=now,
        price_old=old,
        currency=str(offer.get("currency") or "EUR").upper(),
        discount_pct=disc,
        sizes=sizes,
        colors=[color] if color else [c.get("name") for c in h.get("availableColors") or [] if c.get("name")],
        # все ракурсы (до MAX_IMAGES); часть потом заменяется на скачанные файлы
        images=[image_url(vid, s) for s in _shots(h)[:MAX_IMAGES]],
        url=f"{BASE}/it/{vid}/item",
        in_stock=True,
        style_code=str(model.get("id") or "") or None,
        attrs=hit_attrs(h),
    )


def main_result(data: dict | None) -> dict | None:
    """Из всех результатов страницы — основной (тот, где есть товары)."""
    res = [r for r in ((data or {}).get("results") or []) if r.get("hits")]
    return max(res, key=lambda r: len(r["hits"])) if res else None


def page_max_markdown(hits: list[dict]) -> float:
    """Максимальная скидка на странице (по любой базе) — для ранней остановки при BestDeals."""
    best = 0.0
    for h in hits:
        o = h.get("modelBestOfferByPrice") or {}
        vals = [_num(o.get("markdownPercentageFromRetailPrice")), _num(o.get("markdownPercentageFromFullPrice")),
                discount_pct(_num(o.get("currentPrice")), _num(o.get("fullPrice"))),
                discount_pct(_num(o.get("currentPrice")), _num(o.get("retailPrice")))]
        best = max([best] + [v for v in vals if v is not None])
    return best


# ---------------------------------------------------------------- браузер

def _slug(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip().lower())
    return s if re.search(r"_m?d$", s) else s + "_d"


def _targets(query: Query, slugs: dict) -> list[tuple[str, str]]:
    by_norm = {norm_brand(k): (k, v) for k, v in slugs.items() if v}
    if not query.brands:
        return [(k, _slug(v)) for k, v in by_norm.values()]
    out = []
    for b in query.brands:
        hit = by_norm.get(norm_brand(b))
        if hit:
            out.append((b, _slug(hit[1])))
        else:
            print(f"[yoox] для бренда «{b}» нет slug в source_opts.yoox.brand_slugs — пропускаю")
    return out


def _url(dept: str, slug: str, page_no: int, sort: str) -> str:
    qs = []
    if page_no > 1:
        qs.append(f"page={page_no}")
    if sort:
        qs.append(f"sortBy={quote(sort)}")
    return f"{BASE}/it/{dept}/shoponline/{quote(slug, safe=SLUG_SAFE)}" + ("?" + "&".join(qs) if qs else "")


def _launch(pw, headless: bool, channel: str | None, PWError):
    tried = []
    for ch in ([channel] if channel else [None, "chrome", "msedge"]):
        try:
            b = pw.chromium.launch(headless=headless, channel=ch)
            print(f"[yoox] браузер: {ch or 'Chromium из Playwright'} {b.version}, "
                  f"{'headless' if headless else 'видимое окно'}")
            return b
        except PWError as e:
            tried.append(f"{ch or 'chromium'}: {str(e).strip().splitlines()[0]}")
    raise RuntimeError("yoox: не удалось запустить браузер (" + "; ".join(tried) + "). "
                       "Выполните: .venv/Scripts/python -m playwright install chromium "
                       "или укажите source_opts.yoox.browser_channel = \"chrome\" / \"msedge\"")


def _load(page, url: str, PWError, PWTimeout) -> dict:
    """Открыть страницу и достать встроенную выдачу. Blocked — если сайт не пустил."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        raise
    except PWError as e:
        msg = str(e).strip().splitlines()[0]
        if re.search(r"ERR_HTTP2_PROTOCOL_ERROR|ERR_CONNECTION_RESET|ERR_EMPTY_RESPONSE|ERR_CONNECTION_CLOSED", msg):
            raise Blocked(f"соединение сброшено сервером ({msg})") from None
        raise
    status = resp.status if resp else None
    if not status or status < 400:
        try:
            page.wait_for_selector("#__NEXT_DATA__", state="attached", timeout=10000)
        except PWTimeout:
            pass
    data = page.evaluate(_READ_JS)
    if data is not None and (not status or status < 400):
        return data
    html = page.content()
    if "courtesy" in html or status in (403, 429):
        why = "страница-заглушка «We're making YOOX even better»" if "courtesy" in html else "доступ запрещён"
        raise Blocked(f"HTTP {status}, {why}")
    if status and status >= 400:
        raise PageError(f"HTTP {status}")
    # 200 без данных: челлендж/антибот-страница или сайт сменил формат
    raise Blocked(f"HTTP {status}, на странице нет __NEXT_DATA__ («{page.title()[:60]}»)")


def _blocked_msg(headless: bool, reason: Exception) -> str:
    if headless:
        return (f"YOOX (Akamai Bot Manager) не пускает headless-браузер: {reason}. "
                "Поставьте \"headless\": false в config.json → source_opts.yoox — на этом ПК откроется "
                "обычное окно браузера (не закрывайте его до конца сбора).")
    return (f"YOOX (Akamai Bot Manager) не отдал страницу автоматизированному браузеру: {reason}. "
            "Защиту не обходим — источник пропущен. Можно попробовать позже или с другой сети; "
            "надёжный вариант — официальный партнёрский товарный фид YOOX.")


# ---------------------------------------------------------------- фото

def _save_images(page, products: list[Product], limit: int) -> None:
    """Первые 2 ракурса для `limit` товаров с наибольшей скидкой -> site/img/yoox/<id>_<n>.jpg.

    Качаем тем же браузером (fetch внутри страницы yoox.com); если не вышло — обычным
    HTTP (фото на 02.10.2026 отдаются и так); если и это не вышло — оставляем URL YOOX.
    """
    if limit <= 0 or not products:
        return
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    use_browser = page is not None
    session = None
    got = cached = failed = 0
    for p in sorted(products, key=lambda x: -(x.discount_pct or 0))[:limit]:
        local = []
        for n, url in enumerate(p.images[:2], 1):
            name = f"{p.source_item_id}_{n}.jpg"
            path = IMG_DIR / name
            if path.exists() and path.stat().st_size > 0:
                local.append(f"{IMG_REL}/{name}")
                cached += 1
                continue
            data = None
            if use_browser:
                try:
                    r = page.evaluate(_FETCH_IMG_JS, url)
                    if r.get("status") == 200 and r.get("b64") and "image" in (r.get("type") or "image"):
                        data = base64.b64decode(r["b64"])
                    elif r.get("status") in (401, 403):
                        use_browser = False
                except Exception:                                  # noqa: BLE001 — одна картинка не важна
                    use_browser = False
            if data is None:
                try:
                    import requests
                    session = session or requests.Session()
                    resp = session.get(url, headers={"User-Agent": BROWSER_UA, "Referer": BASE + "/it"}, timeout=30)
                    if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                        data = resp.content
                except Exception:                                  # noqa: BLE001
                    data = None
            if data:
                path.write_bytes(data)
                local.append(f"{IMG_REL}/{name}")
                got += 1
            else:
                local.append(url)
                failed += 1
            polite_sleep(0.3, 0.8)
        if local:
            p.images = local + p.images[len(local):]      # остальные ракурсы — по ссылке
    print(f"[yoox] фото: скачано {got}, уже были {cached}, не удалось {failed} (остальные — ссылки на yoox.com)")


# ---------------------------------------------------------------- точка входа

def fetch(query: Query, **opts) -> list[Product]:
    try:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Для источника yoox нужен Playwright: .venv/Scripts/python -m pip install -r requirements.txt, "
            "затем .venv/Scripts/python -m playwright install chromium") from e

    so = query.source_opts or {}
    slugs = so.get("brand_slugs") or {}
    targets = _targets(query, slugs)
    if not targets:
        raise RuntimeError("yoox: нет ни одного бренда со slug — заполните source_opts.yoox.brand_slugs в config.json")
    headless = bool(so.get("headless", True))
    sort = so.get("sort", "BestDeals") or ""
    max_pages = max(1, int(query.max_pages or 1))
    genders = [g for g in DEPTS if not query.genders or g in query.genders]
    if not genders:
        print(f"[yoox] в фильтре genders={query.genders} нет women/men — нечего собирать")
        return []
    names = {norm_brand(b): b for b in list(slugs) + list(query.brands)}

    products: dict[str, Product] = {}
    pages_ok = 0
    first = True
    stop_all = False
    last_error = None
    with sync_playwright() as pw:
        browser = _launch(pw, headless, so.get("browser_channel"), PWError)
        try:
            ctx = browser.new_context(locale="it-IT")
            page = ctx.new_page()
            # картинки/шрифты/видео листинга не грузим — меньше нагрузка на сайт
            page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font")
                       else r.continue_())
            for label, slug in targets:
                for g in genders:
                    dept = DEPTS[g]
                    for n in range(1, max_pages + 1):
                        if not first:
                            polite_sleep(3, 8)
                        first = False
                        url = _url(dept, slug, n, sort)
                        try:
                            data = _load(page, url, PWError, PWTimeout)
                        except Blocked as e:
                            if pages_ok == 0:
                                raise RuntimeError(_blocked_msg(headless, e)) from None
                            print(f"[yoox] {url}: сайт перестал отдавать страницы ({e}) — останавливаюсь")
                            stop_all = True
                            break
                        except (PWError, PageError) as e:        # таймаут / сеть / 404 — пропускаем раздел
                            last_error = f"{url}: {str(e).strip().splitlines()[0]}"
                            print(f"[yoox] ошибка загрузки {last_error} — раздел пропущен")
                            break
                        pages_ok += 1
                        res = main_result(data)
                        if res is None:
                            print(f"[yoox] {label} / {dept} стр. {n}: товаров нет")
                            break
                        if "model.brand" not in unquote(res.get("params") or ""):
                            print(f"[yoox] «{slug}»: сайт не узнал бренд (выдача без фильтра по бренду) — "
                                  f"проверьте brand_slugs, ссылку бренда можно взять из меню Designer на yoox.com")
                            break
                        hits = res["hits"]
                        kept = 0
                        for h in hits:
                            try:
                                p = parse_hit(h, g, query, names)
                            except Exception as e:                # noqa: BLE001 — один плохой товар не валит источник
                                print(f"[yoox] пропущен товар {h.get('variantId')}: {e}")
                                continue
                            if p and p.source_item_id not in products:
                                products[p.source_item_id] = p
                                kept += 1
                        nb_pages = int(res.get("nbPages") or 1)
                        print(f"[yoox] {label} / {dept} стр. {n}/{nb_pages} (всего {res.get('nbHits')}): "
                              f"{len(hits)} товаров, подходит {kept}")
                        if n >= nb_pages:
                            break
                        if sort == "BestDeals" and page_max_markdown(hits) < (query.discount_min or 0):
                            print(f"[yoox] {label} / {dept}: дальше скидки меньше {query.discount_min}% — хватит")
                            break
                    if stop_all:
                        break
                if stop_all:
                    break
            _save_images(page, list(products.values()), int(so.get("max_images", 60)))
        finally:
            browser.close()
    if pages_ok == 0 and last_error:
        raise RuntimeError(f"yoox: не загрузилась ни одна страница, последняя ошибка — {last_error}")
    print(f"[yoox] итого: {len(products)} товаров со скидкой, страниц загружено {pages_ok}")
    return list(products.values())
