"""Файлы каталога на сайте: раскладка по частям (site/data/), закрытые данные (site/admin/) и старый формат.

Пишет run.py (write_site), читают run.py (прошлые карточки для «Нет в наличии»), order_api.py / order_lookup.py
(цены заказа), brands.py, datastore.py. Страница (site/index.html) читает те же файлы сама.

    site/data/manifest.json        {layout, version, generated_at, summary, site, count, facets, dict, ..., shards, detail}
    site/data/i/<k>.<hash>.js      индекс: компактные записи для витрины/фильтров/корзины (по столбцам, со словарями)
    site/data/d/<xx>.<hash>.js     подробности: описание, пункты, состав, все фото, fetched_at — по корзинам id
    site/products.js               ≤ LEGACY_MAX товаров — весь каталог как раньше (window.DEALS, для file://);
                                   больше — маленький window.DEALS_MANIFEST (манифест), части грузятся по одной
    site/products.json             только при ≤ LEGACY_MAX (старый формат целиком)
    site/admin/<xx>.js, meta.js    закупка/маржа/ссылки по корзинам id — НИКОГДА не публикуются (deploy.py)
    site/products-admin.js         всё закрытое одним файлом — только при ≤ ADMIN_COMBINED_MAX

Файлы частей — JSON в обёртке: первая строка «__DS("i/0",», последняя «);». Страница по http берёт текст и
разбирает JSON между ними (JSON.parse в фоновом потоке), по file:// — подключает как <script> (window.__DS).
Имена индекса и подробностей содержат хэш содержимого: браузер и CDN кэшируют их сколько угодно, новая сборка —
новые имена (манифест всегда перечитывается). Неизменившиеся части подробностей между сборками не перекачиваются.

Индекс (одна часть): {"v":1, "k":номер, "n":строк, "id":[...], "b":[бренд], "t":[название или номер в "td"],
"td":[словарь названий части]?, "ty":[тип], "g":[пол], "o":[страна], "c":[цвет], "ss":[система размеров],
"p":[цена/price_unit], "d":[скидка*disc_scale или null], "s":[[размер,...]], "soi":[строки], "sov":[[размеры]],
"out":[строки «нет в наличии»], "f":[first_seen: минут от seen_epoch (секунды) или null], "ip":[префикс фото],
"im":[середина адреса первого фото], "is":[хвост адреса], "ni":[сколько всего фото],
"r":[место в порядке «Рекомендуем»: целое, больше — показывать раньше], "cg":[группа цвета], "zk":[ключи размеров],
"kw":[основы слов для поиска]}. Номера словарей — из manifest.dict (0 = null).
cg — номер группы цвета в manifest.dict.cgroup ([{name, hex}], 12 групп, describe.COLOR_GROUPS) или -1 (нет группы).
zk — ключи фильтра размеров (sizes_norm.filter_keys) номерами manifest.dict.zk (номер = порядок показа): список
номеров или 0 — «как у размеров строки»: объединение manifest.dict.szk[x] по номерам размеров x из "s"
(szk[x] — ключи размера dict.size[x] без учёта товара, sizes_norm.default_keys). Список пишется только там, где
ключи зависят от товара (низ по талии «W30», рубашки по вороту «ворот 40», ремни «см 90» …) — так столбец в 5 раз
меньше. kw — основы слов (describe.keywords) номерами manifest.dict.kw без основ цвета; основы цвета строки —
manifest.dict.ckw[номер цвета "c"]. Полный набор строки = ckw[c] + kw (до describe.KW_MAX).
manifest.facets.colors = {"all"|тип: [[всего, в наличии] × 12 групп]}, manifest.facets.zk = {"all"|тип:
[[номер ключа, всего, в наличии], ...]} в порядке показа.
r = N − место в итоговом порядке ranking.py (оценка + перемешивание брендов), N — товаров в каталоге: r уникальны
(1..N), сортировка по r по убыванию повторяет этот порядок; у распроданных r самые маленькие. Части без "r"
(старые сборки) читаются как раньше. Порядок витрины по умолчанию (_default_key) — по r.
Часть 0 («голова») — HEAD_TOP первых по r, по HEAD_PER_GROUP первых по r в каждой паре тип × пол, HEAD_NEW самых
новых, по HEAD_PRICE самых дешёвых и дорогих и HEAD_DISC с самой большой скидкой (без повторов): первые страницы
каталога («Рекомендуем», категории, новинки, по цене, по скидке) не перестраиваются, когда приходят остальные части.
Остальные части — по корзинам fnv1a32(id) & 255 (manifest.shards[k].b = [от, до]): товар из прямой ссылки
?p=ID страница находит, загрузив сначала его часть.
Строки головы идут в порядке витрины (r ↓); в остальных частях порядок строк НЕ означает порядок показа — они
сгруппированы по бренду, типу и названию (_pack_key: так части сжимаются на ~15% лучше). Страница всегда сортирует сама.

Подробности: {"v":1, "pre":[...], "suf":[...], "items": {id: [description, details, composition,
[префикс, середина, хвост, ...все фото], fetched_at]}}; корзина = fnv1a32(id) & (detail.shards - 1).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import describe
import sizes_norm

LAYOUT = 1
LEGACY_MAX = 30_000            # до стольких товаров пишутся и старые products.js / products.json целиком
ADMIN_COMBINED_MAX = 30_000    # до стольких — и products-admin.js одним файлом
HEAD_TOP = 1500                # «голова»: первые товары витрины по r («Рекомендуем») …
HEAD_PER_GROUP = 60            # … первые по r в каждой паре тип × пол (первые страницы категорий) …
HEAD_NEW = 500                 # … самые новые …
HEAD_PRICE = 200               # … самые дешёвые / дорогие (первые страницы сортировок по цене) …
HEAD_DISC = 200                # … и с самой большой скидкой (первая страница «По скидке»)
HASH_BUCKETS = 256             # виртуальные корзины для частей индекса после головы
SHARD_TARGET = 2_500_000       # байт (без сжатия) в одной части индекса
DETAIL_PER_SHARD = 150         # товаров в файле подробностей (число файлов — степень двойки, 16…1024)
SEEN_UNIT = 60                 # first_seen в индексе — с точностью до минуты (секунды витрине не нужны)
ADMIN_PER_SHARD = 600          # товаров в закрытом файле (16…256)
DATA_DIR = "data"
ADMIN_DIR = "admin"
WRAP_HEAD = '__DS("{key}",\n'
WRAP_TAIL = "\n);\n"
PUBLIC_FIELDS = ("id", "brand", "title", "type", "gender", "origin", "price_uzs", "discount_pct", "sizes",
                 "sizes_out", "size_system", "color", "composition", "details", "description", "images",
                 "in_stock", "fetched_at", "first_seen")
LETTER_SIZES = ["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "XXXL", "3XL", "4XL", "5XL"]
# поля для витрины сверх карточки (не входят в PUBLIC_FIELDS): r — место в «Рекомендуем», zk — ключи фильтра
# размеров (sizes_norm.py), kw — основы слов для поиска (describe.keywords), cg — группа цвета (describe.color_group)
EXTRA_FIELDS = ("r", "zk", "kw", "cg")
ADMIN_LEAK_MARKERS = (b'"cost_uzs"', b'"margin_uzs"', b'"source_item_id"', b'"price_now"')


# ---------------------------------------------------------------- общие мелочи

def fnv1a(s: str) -> int:
    """FNV-1a 32 бита по кодам UTF-16 — та же hash32(), что в index.html."""
    h = 2166136261
    for unit in _utf16_units(s):
        h ^= unit
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _utf16_units(s: str):
    for ch in s:
        o = ord(ch)
        if o < 0x10000:
            yield o
        else:
            o -= 0x10000
            yield 0xD800 + (o >> 10)
            yield 0xDC00 + (o & 0x3FF)


def _pow2(n: float, lo: int, hi: int) -> int:
    k = lo
    while k < n and k < hi:
        k *= 2
    return k


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def wrap(key: str, obj) -> bytes:
    return (WRAP_HEAD.format(key=key) + _dumps(obj) + WRAP_TAIL).encode("utf-8")


def unwrap(data: bytes | str):
    """Текст части (обёртка __DS(...)) -> объект. Голый JSON тоже принимается."""
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    if text.startswith("__DS("):
        text = text[text.index("\n") + 1: text.rindex("\n)")]
    return json.loads(text)


def _hash8(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()[:8]


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _size_key(s: str):
    """Порядок размеров как cmpSize() в index.html: буквенные, числовые, остальные."""
    u = re.sub(r"\s+", "", s.upper())
    if u in LETTER_SIZES:
        return (0, LETTER_SIZES.index(u), u)
    m = re.match(r"\d+(\.\d*)?([eE][+-]?\d+)?", u.replace(",", ".", 1))
    if m:
        return (1, float(m.group(0)), u)
    return (2, 0.0, u)


def _ts(s) -> int | None:
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _url_parts(urls, min_count: int) -> dict[str, tuple[str, str, str]]:
    """Адреса -> (общий префикс, середина, общий ?хвост). Префикс — самая длинная «папка» адреса, которая
    встречается хотя бы min_count раз (у CDN вида .../2026/06/05/<товар>/<uuid>.jpg — .../2026/06/05/)."""
    urls = [u for u in set(urls) if isinstance(u, str) and u]
    cnt, scnt = Counter(), Counter()
    cands = {}
    for u in urls:
        q = u.find("?")
        base, suf = (u[:q], u[q:]) if q >= 0 else (u, "")
        start = base.find("://")
        start = base.find("/", start + 3) if start >= 0 else 0
        cs = [base[:j + 1] for j in range(len(base)) if base[j] == "/" and j >= start >= 0]
        cands[u] = (base, suf, cs)
        cnt.update(cs)
        scnt[suf] += 1
    out = {}
    for u, (base, suf, cs) in cands.items():
        pre = next((c for c in reversed(cs) if cnt[c] >= min_count), "")
        tail = suf if suf and scnt[suf] >= min_count else ""
        out[u] = (pre, base[len(pre):] + (suf if not tail else ""), tail)
    return out


class _Dict:
    """Словарь строк: 0 — null/пусто, дальше по частоте."""

    def __init__(self, values, key=None):
        cnt = values if isinstance(values, Counter) else Counter(v for v in values if v not in (None, ""))
        cnt = Counter({k: v for k, v in cnt.items() if k not in (None, "")})
        order = sorted(cnt, key=key) if key else sorted(cnt, key=lambda v: (-cnt[v], v))
        self.list = [None] + order
        self.idx = {v: i for i, v in enumerate(self.list) if i}

    def __call__(self, v) -> int:
        return self.idx.get(v, 0) if v not in (None, "") else 0


# ---------------------------------------------------------------- запись

def _default_key(i: int, p: dict):
    """Порядок витрины по умолчанию: «Рекомендуем» — r по убыванию (при равенстве скидка ↓, цена ↑),
    распроданные в конце."""
    return (1 if p.get("in_stock") is False else 0, -(p.get("r") or 0), -(p.get("discount_pct") or 0),
            p.get("price_uzs") or 0, i)


def _pack_key(i: int, p: dict):
    """Порядок строк в частях после головы: похожие рядом (бренд, тип, название, цена) — лучше сжатие.
    На показ не влияет: страница сортирует по r / скидке / цене сама."""
    return (p.get("brand") or "", p.get("type") or "", p.get("title") or "", p.get("price_uzs") or 0, p.get("id") or "", i)


def _ranks(products: list[dict]) -> list[int]:
    """r каждой карточки: поле "r" (run.py → ranking.py), а если его нет ни у кого — порядок списка
    (products приходят в порядке витрины: первый получает r = N)."""
    have = [p.get("r") for p in products]
    if any(isinstance(x, (int, float)) and not isinstance(x, bool) for x in have):
        return [int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0 for x in have]
    n = len(products)
    return [n - i for i in range(n)]


def _encode_index(k: int, rows: list[dict], D: dict, epoch: int, unit: int, scale: int) -> dict:
    titles = [r.get("title") or "" for r in rows]
    uniq = Counter(titles)
    use_td = len(uniq) < 0.6 * max(1, len(rows))
    out = {"v": 1, "k": k, "n": len(rows), "id": [r["id"] for r in rows]}
    out["b"] = [D["brand"](r.get("brand")) for r in rows]
    if use_td:
        td = sorted(uniq)                   # по алфавиту: «Рубашка, белая, …» рядом — словарь сжимается лучше
        ti = {t: i for i, t in enumerate(td)}
        out["td"] = td
        out["t"] = [ti[t] for t in titles]
    else:
        out["t"] = titles
    out["ty"] = [D["type"](r.get("type")) for r in rows]
    out["g"] = [D["gender"](r.get("gender")) for r in rows]
    out["o"] = [D["origin"](r.get("origin")) for r in rows]
    out["c"] = [D["color"](r.get("color")) for r in rows]
    out["ss"] = [D["sys"](r.get("size_system")) for r in rows]
    out["p"] = [int(r.get("price_uzs") or 0) // unit for r in rows]
    out["d"] = [None if r.get("discount_pct") is None else round(r["discount_pct"] * scale) for r in rows]
    sz = D["size"]
    out["s"] = [list(dict.fromkeys(sz(s) for s in (r.get("sizes") or []) if sz(s))) for r in rows]
    soi, sov = [], []
    for i, r in enumerate(rows):
        so = [sz(s) for s in (r.get("sizes_out") or []) if sz(s)]
        if so:
            soi.append(i)
            sov.append(so)
    out["soi"], out["sov"] = soi, sov
    out["out"] = [i for i, r in enumerate(rows) if r.get("in_stock") is False]
    f = []
    for r in rows:
        t = _ts(r.get("first_seen"))
        f.append(None if t is None else (t - epoch) // SEEN_UNIT)
    out["f"] = f
    # первое фото: общая «папка» адреса — из словаря этой части (ipd), ?хвост — из словаря манифеста
    firsts = [next((u for u in (r.get("images") or []) if isinstance(u, str) and u), "") for r in rows]
    parts = _url_parts([u for u in firsts if u], 3)
    pre = _Dict(Counter(parts[u][0] for u in firsts if u))
    ip, im, is_, ni = [], [], [], []
    for r, u in zip(rows, firsts):
        ni.append(sum(1 for x in (r.get("images") or []) if isinstance(x, str) and x))
        if u:
            a, b, c = parts[u]
            sc = D["imgsuf"](c)
            ip.append(pre(a))
            im.append(b + (c if c and not sc else ""))
            is_.append(sc)
        else:
            ip.append(0), im.append(""), is_.append(0)
    out["ipd"] = pre.list
    out["ip"], out["im"], out["is"], out["ni"] = ip, im, is_, ni
    out["r"] = [int(r.get("r") or 0) for r in rows]
    out["cg"] = [r["cg"] for r in rows]
    out["zk"] = [r["_zke"] for r in rows]
    out["kw"] = [r["_kwe"] for r in rows]
    return out


def _encode_detail(rows: list[dict]) -> dict:
    parts = _url_parts((u for r in rows for u in r.get("images") or []), 2)
    pre_c = Counter(a for a, _, _ in parts.values() if a)
    suf_c = Counter(c for _, _, c in parts.values() if c)
    pre = [""] + sorted(pre_c, key=lambda a: (-pre_c[a], a))
    suf = [""] + sorted(suf_c, key=lambda c: (-suf_c[c], c))
    pi, si = {a: i for i, a in enumerate(pre)}, {c: i for i, c in enumerate(suf)}
    items = {}
    for r in sorted(rows, key=lambda r: r["id"]):
        flat = []
        for u in r.get("images") or []:
            if not (isinstance(u, str) and u):
                continue
            a, b, c = parts[u]
            flat += [pi[a], b, si[c]]
        items[r["id"]] = [r.get("description"), r.get("details") or [], r.get("composition"), flat, r.get("fetched_at")]
    return {"v": 1, "pre": pre, "suf": suf, "items": items}


def _facets(products: list[dict], zk_dict: "_Dict | None" = None) -> dict:
    """Счётчики для фильтров: [всего, в наличии]. colors — по группам цвета (номер = индекс manifest.dict.cgroup),
    zk — ключи размеров [номер в manifest.dict.zk, всего, в наличии] в порядке sizes_norm.sort_key;
    у colors и zk — "all" и по каждому типу (имя типа как в types)."""
    brands: dict[str, list[int]] = {}
    types: dict[str, list[int]] = {}
    sizes, genders = set(), set()
    ng = len(describe.COLOR_GROUPS)
    colors: dict[str, list[list[int]]] = {"all": [[0, 0] for _ in range(ng)]}
    zk: dict[str, dict[str, list[int]]] = {"all": {}}
    sold = 0
    for p in products:
        out = p.get("in_stock") is False
        sold += out
        b = (p.get("brand") or "").strip() or "Без бренда"
        t = (p.get("type") or "").strip() or "другое"
        for m, k in ((brands, b), (types, t)):
            c = m.setdefault(k, [0, 0])
            c[0] += 1
            c[1] += 0 if out else 1
        if p.get("gender") in ("women", "men", "kids"):
            genders.add(p["gender"])
        if not out:
            sizes.update(str(s).strip() for s in p.get("sizes") or [] if str(s).strip())
        g = p.get("cg")
        if isinstance(g, int) and 0 <= g < ng:
            for key in ("all", t):
                c = colors.setdefault(key, [[0, 0] for _ in range(ng)])[g]
                c[0] += 1
                c[1] += 0 if out else 1
        for k in p.get("zk") or []:
            for key in ("all", t):
                c = zk.setdefault(key, {}).setdefault(k, [0, 0])
                c[0] += 1
                c[1] += 0 if out else 1
    zk_out = {t: [[zk_dict(k) if zk_dict else k, *n] for k, n in sorted(m.items(), key=lambda x: sizes_norm.sort_key(x[0]))]
              for t, m in zk.items()}
    return {"brands": brands, "types": types, "sizes": sorted(sizes, key=_size_key),
            "genders": sorted(genders), "sold": sold, "count": len(products), "avail": len(products) - sold,
            "colors": colors, "zk": zk_out}


def _extras(p: dict) -> dict:
    """zk / kw / cg карточки: готовые из run.py (поля "zk", "kw") или посчитанные по самой карточке."""
    zk = p.get("zk")
    if not isinstance(zk, list):
        zk = sizes_norm.filter_keys(p.get("sizes") or [], p.get("type"), p.get("gender"))
    kw = p.get("kw")
    if not isinstance(kw, list):
        kw = describe.keywords(p)
    ckw = describe.color_keywords(p.get("color"))            # основы цвета всегда первые (в файле — через dict.ckw)
    kw = ckw + [str(k) for k in kw if k and str(k) not in ckw]
    return {"zk": [str(k) for k in zk if k], "kw": kw[:describe.KW_MAX], "cg": describe.color_group(p.get("color"))}


def _remove_stale(folder: Path, keep: set[str]) -> int:
    n = 0
    for f in folder.glob("*") if folder.is_dir() else []:
        if f.is_file() and f.name not in keep:
            f.unlink()
            n += 1
    return n


def write_site(site: Path, summary: dict, site_cfg: dict, products: list[dict], admin: dict, *,
               legacy_max: int = LEGACY_MAX, admin_combined_max: int = ADMIN_COMBINED_MAX) -> dict:
    """Пишет каталог в site/: data/ (части + манифест), admin/ (закрытое), products.js и при малом каталоге —
    products.json / products-admin.js. products — публичные карточки (PUBLIC_FIELDS) и, по желанию, "r"
    (место в «Рекомендуем», ranking.py; без него r — по порядку списка), admin — {id: закупка, "_meta": {...}}.
    Возвращает сводку (размеры, число файлов)."""
    site = Path(site)
    data_dir = site / DATA_DIR
    products = [dict({k: p.get(k) for k in PUBLIC_FIELDS}, r=rk, **_extras(p)) for p, rk in zip(products, _ranks(products))]
    n = len(products)

    # словари (общие для всех частей индекса)
    firsts = [p["images"][0] for p in products if p.get("images") and isinstance(p["images"][0], str) and p["images"][0]]
    fparts = _url_parts(firsts, 3)
    suf_c = Counter(fparts[u][2] for u in firsts)
    D = {
        "brand": _Dict(p.get("brand") for p in products),
        "type": _Dict(p.get("type") for p in products),
        "gender": _Dict(p.get("gender") for p in products),
        "origin": _Dict(p.get("origin") for p in products),
        "color": _Dict(p.get("color") for p in products),
        "sys": _Dict(p.get("size_system") for p in products),
        "size": _Dict((s for p in products for s in (p.get("sizes") or []) + (p.get("sizes_out") or [])), key=_size_key),
        "imgsuf": _Dict(suf_c),
    }
    # ключи размеров: номер в словаре = порядок показа (sizes_norm.sort_key); szk — ключи каждого размера словаря
    # size без учёта товара (sizes_norm.default_keys): строка, чьи ключи совпадают с ними, пишет в zk 0
    size_keys = [sizes_norm.default_keys(x) if x else [] for x in D["size"].list]
    D["zk"] = _Dict(Counter(k for ks in [p["zk"] for p in products] + size_keys for k in ks), key=sizes_norm.sort_key)
    # основы для поиска: основы цвета — в словаре цветов (ckw), в строке kw — остальные
    color_kw = [describe.color_keywords(c) if c else [] for c in D["color"].list]
    D["kw"] = _Dict(Counter(k for ks in [p["kw"] for p in products] + color_kw for k in ks))
    szk = [[D["zk"](k) for k in ks] for ks in size_keys]
    ckw = [[D["kw"](k) for k in ks] for ks in color_kw]
    for p in products:
        zk = [D["zk"](k) for k in p["zk"]]
        own = set(color_kw[D["color"](p.get("color"))])
        p["_kwe"] = [D["kw"](k) for k in p["kw"] if k not in own]
        derived = {x for s in p.get("sizes") or [] for x in szk[D["size"](s)]}
        p["_zke"] = 0 if set(zk) == derived else zk
    stamps = [t for t in (_ts(p.get("first_seen")) for p in products) if t is not None]
    epoch = min(stamps) if stamps else 0
    prices = [int(p.get("price_uzs") or 0) for p in products]
    unit = 1000 if prices and all(x % 1000 == 0 for x in prices) else 1
    discs = [p["discount_pct"] for p in products if p.get("discount_pct") is not None]
    scale = 10 if all(abs(d * 10 - round(d * 10)) < 1e-6 for d in discs) else 1000

    # голова: первые по r, первые по r в каждой паре тип × пол, самые новые, дешёвые, дорогие, с большой скидкой;
    # остальные — по корзинам id
    order = sorted(range(n), key=lambda i: _default_key(i, products[i]))
    head = set(order[:HEAD_TOP])
    instock = [i for i in order if products[i].get("in_stock") is not False]
    per_group: Counter = Counter()
    for i in instock:
        g = (products[i].get("type") or "", products[i].get("gender") or "")
        if per_group[g] < HEAD_PER_GROUP:
            per_group[g] += 1
            head.add(i)
    newest = sorted(instock, key=lambda i: (-(_ts(products[i].get("first_seen")) or 0), i))
    head.update(newest[:HEAD_NEW])
    by_price = sorted(instock, key=lambda i: (products[i].get("price_uzs") or 0, -(products[i].get("discount_pct") or 0), i))
    head.update(by_price[:HEAD_PRICE])
    head.update(sorted(instock, key=lambda i: (-(products[i].get("price_uzs") or 0), -(products[i].get("discount_pct") or 0), i))[:HEAD_PRICE])
    head.update(sorted(instock, key=lambda i: (-(products[i].get("discount_pct") or 0), products[i].get("price_uzs") or 0, i))[:HEAD_DISC])
    head_rows = [products[i] for i in order if i in head]
    rest = [i for i in order if i not in head]
    by_bucket: dict[int, list[int]] = {}
    for i in rest:
        by_bucket.setdefault(fnv1a(products[i]["id"]) & (HASH_BUCKETS - 1), []).append(i)
    est = {b: sum(150 + len(products[i].get("title") or "") * 2 for i in ids) for b, ids in by_bucket.items()}
    total_est = sum(est.values())
    n_parts = max(1, round(total_est / SHARD_TARGET)) if rest else 0
    groups: list[tuple[int, int, list[int]]] = []
    if n_parts:
        per = total_est / n_parts
        lo, acc, cur = 0, 0, []
        for b in range(HASH_BUCKETS):
            ids = by_bucket.get(b, [])
            cur += ids
            acc += est.get(b, 0)
            if (acc >= per * (len(groups) + 1) and len(groups) < n_parts - 1) or b == HASH_BUCKETS - 1:
                groups.append((lo, b, sorted(cur, key=lambda i: _pack_key(i, products[i]))))
                lo, cur = b + 1, []

    shards, keep_i = [], set()
    parts = [(None, head_rows)] + [((lo, hi), [products[i] for i in ids]) for lo, hi, ids in groups]
    for k, (rng, rows) in enumerate(parts):
        blob = wrap(f"i/{k}", _encode_index(k, rows, D, epoch, unit, scale))
        name = f"{k}.{_hash8(blob)}.js"
        if not (data_dir / "i" / name).is_file():
            _write_atomic(data_dir / "i" / name, blob)
        keep_i.add(name)
        ent = {"file": f"i/{name}", "count": len(rows), "bytes": len(blob), "gz": len(gzip.compress(blob, 6))}
        if rng is None:
            ent["head"] = True
        else:
            ent["b"] = list(rng)
        shards.append(ent)

    # подробности по корзинам id
    nd = _pow2(n / DETAIL_PER_SHARD, 16, 1024)
    hexw = len(f"{nd - 1:x}")
    buckets: dict[int, list[dict]] = {}
    for p in products:
        buckets.setdefault(fnv1a(p["id"]) & (nd - 1), []).append(p)
    dfiles, keep_d, dbytes, dgz = [], set(), 0, 0
    for b in range(nd):
        key = f"{b:0{hexw}x}"
        blob = wrap(f"d/{key}", _encode_detail(buckets.get(b, [])))
        name = f"{key}.{_hash8(blob)}.js"
        if not (data_dir / "d" / name).is_file():
            _write_atomic(data_dir / "d" / name, blob)
        keep_d.add(name)
        dfiles.append(f"d/{name}")
        dbytes += len(blob)
        dgz += len(gzip.compress(blob, 6))

    # сводка по брендам (сотни-тысячи строк) нужна только режиму ?admin=1 — в закрытые файлы, не в манифест
    full_summary = summary
    by_brand = (summary or {}).get("by_brand")
    summary = {k: v for k, v in (summary or {}).items() if k != "by_brand"}
    if by_brand is not None:
        admin = dict(admin)
        admin["_meta"] = dict(admin.get("_meta") or {}, by_brand=by_brand)
    version = _hash8(("|".join(s["file"] for s in shards) + "|" + "|".join(dfiles)).encode())
    manifest = {
        "layout": LAYOUT, "version": version, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": summary, "site": site_cfg, "count": n, "facets": _facets(products, D["zk"]),
        "dict": dict({k: d.list for k, d in D.items()}, szk=szk, ckw=ckw,
                     cgroup=[{"name": name, "hex": hx} for name, hx in describe.COLOR_GROUPS]),
        "price_unit": unit, "disc_scale": scale, "seen_epoch": epoch, "seen_unit": SEEN_UNIT,
        "hash": "fnv1a32", "buckets": HASH_BUCKETS, "shards": shards,
        "detail": {"shards": nd, "prefix_len": hexw, "files": dfiles, "bytes": dbytes, "gz": dgz},
        "legacy": n <= legacy_max,
    }
    mtext = _dumps(manifest)
    _write_atomic(data_dir / "manifest.json", mtext.encode("utf-8"))
    stale = _remove_stale(data_dir / "i", keep_i) + _remove_stale(data_dir / "d", keep_d)

    # старый формат целиком (file:// и старые инструменты) — только для небольшого каталога
    if n <= legacy_max:
        legacy = [{k: v for k, v in p.items() if not k.startswith("_")} for p in products]   # + r, zk, kw, cg
        payload = _dumps({"summary": full_summary, "site": site_cfg, "products": legacy})
        _write_atomic(site / "products.json", payload.encode("utf-8"))
        _write_atomic(site / "products.js", ("window.DEALS = " + payload + ";").encode("utf-8"))
    else:
        (site / "products.json").unlink(missing_ok=True)
        _write_atomic(site / "products.js", stub_js(mtext).encode("utf-8"))

    astats = write_admin(site, admin, combined=n <= admin_combined_max)
    return {"count": n, "index_files": len(shards), "index_bytes": sum(s["bytes"] for s in shards),
            "index_gz": sum(s["gz"] for s in shards), "head_count": shards[0]["count"] if shards else 0,
            "detail_files": nd, "detail_bytes": dbytes, "detail_gz": dgz, "stale_removed": stale,
            "legacy": n <= legacy_max, "manifest_bytes": len(mtext.encode("utf-8")), **astats}


def stub_js(manifest_text: str) -> str:
    """products.js для большого каталога: только манифест — страница по file:// подключит части сама."""
    return ("// Каталог разбит на части (site/data/). Этот файл — только оглавление для открытия index.html\n"
            "// двойным щелчком (file://); на сайте страница читает data/manifest.json.\n"
            "window.DEALS_MANIFEST = " + manifest_text + ";\n")


def write_admin(site: Path, admin: dict, combined: bool) -> dict:
    """site/admin/<корзина>.js + meta.js (и products-admin.js целиком, если combined). Не публикуется."""
    folder = Path(site) / ADMIN_DIR
    items = {k: v for k, v in admin.items() if not k.startswith("_") and isinstance(v, dict)}
    na = _pow2(len(items) / ADMIN_PER_SHARD, 16, 256)
    hexw = len(f"{na - 1:x}")
    buckets: dict[int, dict] = {}
    for pid, rec in items.items():
        buckets.setdefault(fnv1a(pid) & (na - 1), {})[pid] = rec
    keep = set()
    for b in range(na):
        key = f"{b:0{hexw}x}"
        _write_atomic(folder / f"{key}.js", wrap(f"a/{key}", {"v": 1, "items": buckets.get(b, {})}))
        keep.add(f"{key}.js")
    meta = {"v": 1, "shards": na, "prefix_len": hexw, "count": len(items), "_meta": admin.get("_meta") or {}}
    _write_atomic(folder / "meta.js", wrap("a/meta", meta))
    keep.add("meta.js")
    keep.add("admin.js")          # модуль страницы ?admin=1 (site/index.html), не данные
    _remove_stale(folder, keep)
    combined_path = Path(site) / "products-admin.js"
    if combined:
        _write_atomic(combined_path, ("window.DEALS_ADMIN = " + _dumps(admin) + ";").encode("utf-8"))
    else:
        combined_path.unlink(missing_ok=True)
    return {"admin_files": na, "admin_combined": combined}


# ---------------------------------------------------------------- чтение

def load_manifest(site: Path) -> dict | None:
    p = Path(site) / DATA_DIR / "manifest.json"
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
        return m if isinstance(m, dict) and m.get("layout") == LAYOUT else None
    except (OSError, ValueError):
        return None


def public_signature(site: Path):
    """Отпечаток публичных данных (меняется при каждой сборке): манифест, иначе старый products.json."""
    for p in (Path(site) / DATA_DIR / "manifest.json", Path(site) / "products.json"):
        try:
            st = p.stat()
            return (p.name, st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return None


def admin_signature(site: Path):
    for p in (Path(site) / ADMIN_DIR / "meta.js", Path(site) / "products-admin.js"):
        try:
            st = p.stat()
            return (p.name, st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return None


def decode_index(sh: dict, m: dict) -> list[dict]:
    """Часть индекса -> карточки (без подробностей: description/details/composition/fetched_at = None,
    images — только первое фото). "_r" — место в «Рекомендуем» (None у частей старых сборок без "r")."""
    D, unit, scale, epoch = m["dict"], m.get("price_unit", 1), m.get("disc_scale", 10), m.get("seen_epoch", 0)
    su = m.get("seen_unit", 1)
    n = sh["n"]
    td = sh.get("td")
    rr = sh.get("r") if isinstance(sh.get("r"), list) and len(sh["r"]) == n else None
    cg = sh.get("cg") if isinstance(sh.get("cg"), list) and len(sh["cg"]) == n else None
    zk = sh.get("zk") if isinstance(sh.get("zk"), list) and len(sh["zk"]) == n else None
    kw = sh.get("kw") if isinstance(sh.get("kw"), list) and len(sh["kw"]) == n else None
    dzk, dkw, szk, ckw = D.get("zk") or [None], D.get("kw") or [None], D.get("szk") or [], D.get("ckw") or []

    def zk_of(i):
        z = zk[i]
        if z != 0:
            return [dzk[x] for x in z]
        keys = {x for s in sh["s"][i] for x in (szk[s] if s < len(szk) else [])}
        return [dzk[x] for x in sorted(keys)]

    def kw_of(i):
        c = sh["c"][i]
        return [dkw[x] for x in (ckw[c] if c < len(ckw) else []) + kw[i]]
    sov = dict(zip(sh.get("soi") or [], sh.get("sov") or []))
    out_rows = set(sh.get("out") or [])
    pre, suf = sh.get("ipd") or D.get("imgpre") or [None], D.get("imgsuf") or [None]
    rows = []
    for i in range(n):
        t = sh["t"][i]
        d = sh["d"][i]
        f = sh["f"][i]
        img = []
        if sh["ni"][i]:
            img = [(pre[sh["ip"][i]] or "") + sh["im"][i] + (suf[sh["is"][i]] or "")]
        rows.append({
            "id": sh["id"][i], "brand": D["brand"][sh["b"][i]], "title": td[t] if td is not None else t,
            "type": D["type"][sh["ty"][i]], "gender": D["gender"][sh["g"][i]], "origin": D["origin"][sh["o"][i]],
            "price_uzs": sh["p"][i] * unit, "discount_pct": None if d is None else (d / scale if d % scale else d // scale),
            "sizes": [D["size"][x] for x in sh["s"][i]], "sizes_out": [D["size"][x] for x in sov.get(i, [])],
            "size_system": D["sys"][sh["ss"][i]], "color": D["color"][sh["c"][i]],
            "composition": None, "details": None, "description": None, "images": img,
            "in_stock": i not in out_rows, "fetched_at": None,
            "first_seen": None if f is None else _iso(epoch + f * su), "_n_img": sh["ni"][i],
            "_r": rr[i] if rr is not None else None,
            "_cg": cg[i] if cg is not None else None,
            "_zk": zk_of(i) if zk is not None else None,
            "_kw": kw_of(i) if kw is not None else None,
        })
    return rows


def decode_detail(obj: dict) -> dict[str, dict]:
    pre, suf = obj.get("pre") or [""], obj.get("suf") or [""]
    out = {}
    for pid, (desc, details, compo, flat, fetched) in (obj.get("items") or {}).items():
        imgs = [(pre[flat[j]] or "") + flat[j + 1] + (suf[flat[j + 2]] or "") for j in range(0, len(flat), 3)]
        out[pid] = {"description": desc, "details": details, "composition": compo, "images": imgs, "fetched_at": fetched}
    return out


class PublicCatalog(Mapping):
    """Публичные карточки по коду товара, полные (с подробностями). Индекс читается целиком (он компактный),
    файлы подробностей — по мере обращения. Старый products.json тоже понимает."""

    def __init__(self, site: Path):
        self.site = Path(site)
        self.manifest = load_manifest(self.site)
        self._rows: dict[str, dict] = {}
        self._rank: dict[str, int] = {}
        self._extra: dict[str, dict] = {}
        self._details: dict[int, dict] = {}
        self.summary, self.site_cfg, self.legacy = {}, {}, False
        if self.manifest:
            m = self.manifest
            self.summary, self.site_cfg = m.get("summary") or {}, m.get("site") or {}
            for s in m["shards"]:
                for r in decode_index(unwrap((self.site / DATA_DIR / s["file"]).read_bytes()), m):
                    if r["id"] not in self._rows:
                        self._rows[r["id"]] = r
                        if r.get("_r") is not None:
                            self._rank[r["id"]] = r["_r"]
                        if r.get("_zk") is not None:
                            self._extra[r["id"]] = {"cg": r["_cg"], "zk": r["_zk"], "kw": r["_kw"]}
        else:
            path = self.site / "products.json"
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                self.summary, self.site_cfg, self.legacy = data.get("summary") or {}, data.get("site") or {}, True
                for p in data.get("products") or []:
                    if isinstance(p, dict) and p.get("id") and p["id"] not in self._rows:
                        if isinstance(p.get("r"), int):
                            self._rank[p["id"]] = p["r"]
                        if isinstance(p.get("zk"), list):
                            self._extra[p["id"]] = {k: p.get(k) for k in ("cg", "zk", "kw")}
                        self._rows[p["id"]] = {k: v for k, v in p.items() if k not in EXTRA_FIELDS}

    def rank(self, pid: str) -> int | None:
        """Место в «Рекомендуем» (столбец r: больше — раньше); None — сборка без r."""
        return self._rank.get(pid)

    def ranks(self) -> dict[str, int]:
        return dict(self._rank)

    def extras(self, pid: str) -> dict | None:
        """{"cg": группа цвета или -1, "zk": [ключи размеров], "kw": [основы для поиска]}; None — старая сборка."""
        e = self._extra.get(pid)
        return None if e is None else {"cg": e["cg"], "zk": list(e["zk"]), "kw": list(e["kw"])}

    def _full(self, r: dict) -> dict:
        if self.legacy or "_n_img" not in r:
            return r
        m = self.manifest["detail"]
        b = fnv1a(r["id"]) & (m["shards"] - 1)
        if b not in self._details:
            try:
                self._details[b] = decode_detail(unwrap((self.site / DATA_DIR / m["files"][b]).read_bytes()))
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                self._details[b] = {}           # файла нет / битый — карточка без подробностей, но не падаем
        d = self._details[b].get(r["id"]) or {}
        out = {k: r.get(k) for k in PUBLIC_FIELDS}
        out.update({k: d.get(k) for k in ("description", "details", "composition", "fetched_at")})
        out["images"] = d.get("images") or r.get("images") or []
        r.clear()
        r.update(out)
        return r

    def index_row(self, pid: str) -> dict | None:
        """Карточка без подробностей (быстро: только индекс) — для цен, размеров, наличия."""
        r = self._rows.get(pid)
        return None if r is None else {k: v for k, v in r.items() if not k.startswith("_")}

    def index_rows(self) -> dict[str, dict]:
        """Все карточки без подробностей (цены, размеры, наличие) — быстро, без файлов data/d/."""
        return {pid: {k: v for k, v in r.items() if not k.startswith("_")} for pid, r in self._rows.items()}

    def __getitem__(self, pid: str) -> dict:
        return self._full(self._rows[pid])

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, pid) -> bool:
        return pid in self._rows

    def products(self) -> list[dict]:
        return [self[pid] for pid in self._rows]


class AdminCatalog(Mapping):
    """Закрытые данные по коду: site/admin/<корзина>.js по мере обращения, иначе products-admin.js целиком."""

    def __init__(self, site: Path):
        self.site = Path(site)
        self.meta: dict = {}
        self._combined: dict | None = None
        self._buckets: dict[int, dict] = {}
        mp = self.site / ADMIN_DIR / "meta.js"
        if mp.is_file():
            self.meta = unwrap(mp.read_bytes())
        else:
            cp = self.site / "products-admin.js"
            self._combined = {}
            if cp.is_file():
                text = cp.read_text(encoding="utf-8").strip()
                i = text.find("=")
                data = json.loads(text[i + 1:].strip().rstrip(";").strip()) if i >= 0 else {}
                self.meta = {"_meta": data.get("_meta") or {}}
                self._combined = {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}

    def _bucket(self, pid: str) -> dict:
        n = int(self.meta.get("shards") or 16)
        b = fnv1a(pid) & (n - 1)
        if b not in self._buckets:
            p = self.site / ADMIN_DIR / f"{b:0{int(self.meta.get('prefix_len') or 1)}x}.js"
            try:
                self._buckets[b] = unwrap(p.read_bytes()).get("items") or {}
            except (OSError, ValueError):
                self._buckets[b] = {}
        return self._buckets[b]

    def _all(self) -> dict:
        if self._combined is not None:
            return self._combined
        out = {}
        for b in range(int(self.meta.get("shards") or 0)):
            p = self.site / ADMIN_DIR / f"{b:0{int(self.meta.get('prefix_len') or 1)}x}.js"
            if p.is_file():
                out.update(unwrap(p.read_bytes()).get("items") or {})
        self._combined = out
        return out

    @property
    def info(self) -> dict:
        return self.meta.get("_meta") or {}

    def __getitem__(self, pid: str) -> dict:
        src = self._combined if self._combined is not None else self._bucket(pid)
        return src[pid]

    def get(self, pid, default=None):
        try:
            return self[pid]
        except KeyError:
            return default

    def __iter__(self):
        return iter(self._all())

    def __len__(self) -> int:
        return len(self._all())


def read_public(site: Path) -> tuple[dict, dict, list[dict]]:
    c = PublicCatalog(site)
    return c.summary, c.site_cfg, c.products()


def read_admin(site: Path) -> dict:
    a = AdminCatalog(site)
    out = dict(a._all())
    out["_meta"] = a.info
    return out


def published_files(site: Path) -> list[str]:
    """Публичные файлы раскладки по манифесту (пути относительно site/): manifest + все части."""
    m = load_manifest(site)
    if not m:
        return []
    return [f"{DATA_DIR}/manifest.json"] + [f"{DATA_DIR}/{s['file']}" for s in m["shards"]] + \
           [f"{DATA_DIR}/{f}" for f in m["detail"]["files"]]
