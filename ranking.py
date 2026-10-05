"""Порядок витрины «Рекомендуем»: оценка товара 0..1 и перемешивание брендов.

Пишет run.py (столбец r в индексе каталога, см. catalog_files.py), позже — channel.py (подборки для Telegram).

    import ranking
    rk = ranking.Ranker.from_config(cfg)          # cfg — config.json (dict); без него читается config.json
    s = rk.score(row)                             # 0..1, распроданный — 0
    pos = rk.positions(rows)                      # pos[i] — место rows[i] в итоговом порядке (0 — первый)
    r = rk.ranks(rows)                            # r[i] = len(rows) - pos[i]: чем больше, тем раньше показывать

row — публичная карточка (как в site/data): brand, title, type, gender, price_uzs, discount_pct, sizes,
images (или _n_img — число фото), in_stock, id.

Оценка = 0.30·скидка + 0.25·бренд + 0.20·размеры + 0.10·цена + 0.10·сезон + 0.05·фото:
  скидка   clamp((скидка − 20) / 50, 0, 1): 20% и меньше — 0, 70% и больше — 1;
  бренд    вес из config.json → brand_tier (1.0 люкс … 0.35 масс-маркет, не из списка — default 0.5);
  размеры  min(размеров в наличии, 4) / 4 × (1, если есть ходовой размер для пола и типа, иначе 0.5);
           «Единый размер» (сумки, ремни, шарфы) подходит всем — 1;
  цена     до 700 тыс. 0.3; 0.7–1.5 млн 0.7; 1.5–6 млн 1.0; 6–12 млн 0.8; дороже 12 млн 0.5;
  сезон    season_weight(месяц, тип) — климат Ташкента (летом футболки и шорты, зимой куртки и свитеры);
  фото     3 и больше — 1, иначе 0.5.

Перемешивание (diversify): товары по убыванию оценки, но в любом окне из 48 подряд не больше 4 одного бренда
и не больше 2 с одинаковыми брендом и названием (тридцать «Рубашка Pierre Cardin» подряд больше не выйдут).
Если ограничение выполнить нечем (в хвосте остался один бренд), сначала снимается ограничение по бренду,
потом — по названию. Распроданные — в самом конце.
"""
from __future__ import annotations

import heapq
import json
import re
import unicodedata
from collections import deque
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent

WEIGHTS = {"disc": 0.30, "brand": 0.25, "size": 0.20, "price": 0.10, "season": 0.10, "photos": 0.05}
DEFAULT_TIER = 0.5
WINDOW = 48           # окно перемешивания
MAX_BRAND = 4         # не больше стольких товаров одного бренда в окне
MAX_TITLE = 2         # и не больше стольких с одинаковыми брендом и названием
ONE_SIZE = "единый размер"

# ходовые размеры (буквенные — точное совпадение; числа — диапазоны включительно)
POPULAR = {
    "men": {"letters": {"M", "L", "XL", "XXL", "2XL"}, "it": (48, 54), "shoes": (41, 44), "jeans_w": (31, 36)},
    "women": {"letters": {"S", "M", "L"}, "it": (40, 46), "shoes": (37, 39), "jeans_w": (26, 30)},
}

# Сезонный вес типа по месяцам (Ташкент: жаркое лето до +40, зима около нуля, короткая весна и осень).
_SEASONS = {
    # октябрь–ноябрь: похолодание — верхняя одежда и трикотаж
    (10, 11): {"куртки и пальто": 1.0, "свитеры и кардиганы": 1.0, "обувь": 0.9, "пиджаки и костюмы": 0.8,
               "брюки": 0.8, "джинсы": 0.8, "толстовки": 0.8, "рубашки": 0.7, "сумки": 0.7, "аксессуары": 0.7,
               "нижнее бельё": 0.6, "юбки": 0.5, "платья": 0.5, "футболки и поло": 0.3, "шорты": 0.1},
    # декабрь–февраль: зима и праздники (подарки — сумки и аксессуары)
    (12, 1, 2): {"куртки и пальто": 1.0, "свитеры и кардиганы": 1.0, "обувь": 0.9, "пиджаки и костюмы": 0.8,
                 "брюки": 0.8, "джинсы": 0.8, "толстовки": 0.8, "сумки": 0.8, "аксессуары": 0.8,
                 "рубашки": 0.6, "нижнее бельё": 0.6, "платья": 0.5, "юбки": 0.4, "футболки и поло": 0.2,
                 "шорты": 0.05},
    # март–апрель: весна — лёгкие куртки, рубашки, обувь
    (3, 4): {"обувь": 1.0, "пиджаки и костюмы": 0.9, "брюки": 0.9, "джинсы": 0.9, "рубашки": 0.9,
             "куртки и пальто": 0.7, "толстовки": 0.7, "свитеры и кардиганы": 0.6, "футболки и поло": 0.7,
             "платья": 0.8, "юбки": 0.8, "сумки": 0.8, "аксессуары": 0.8, "нижнее бельё": 0.6, "шорты": 0.4},
    # май–август: жара — футболки, поло, шорты, платья, лёгкие рубашки
    (5, 6, 7, 8): {"футболки и поло": 1.0, "шорты": 1.0, "платья": 1.0, "рубашки": 0.9, "юбки": 0.9,
                   "обувь": 0.8, "сумки": 0.8, "аксессуары": 0.8, "брюки": 0.7, "нижнее бельё": 0.7,
                   "джинсы": 0.6, "пиджаки и костюмы": 0.5, "толстовки": 0.3, "свитеры и кардиганы": 0.2,
                   "куртки и пальто": 0.1},
    # сентябрь: ещё тепло, но уже к осени — рубашки, брюки, обувь
    (9,): {"рубашки": 0.9, "брюки": 0.9, "джинсы": 0.9, "обувь": 0.9, "пиджаки и костюмы": 0.8,
           "сумки": 0.8, "аксессуары": 0.8, "футболки и поло": 0.7, "платья": 0.7, "толстовки": 0.7,
           "свитеры и кардиганы": 0.7, "юбки": 0.7, "нижнее бельё": 0.6, "куртки и пальто": 0.6, "шорты": 0.4},
}
SEASON = {m: table for months, table in _SEASONS.items() for m in months}
SEASON_DEFAULT = 0.6      # тип неизвестен или не в таблице

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_W = re.compile(r"(?:^W\s*(\d{2})\b|^(\d{2})\s*W\b)")


# ---------------------------------------------------------------- мелочи

def norm_key(s) -> str:
    """Ключ бренда/названия: нижний регистр, без диакритики и без знаков («Jacob Cohën» = «jacob cohen» = «JacobCohen»,
    «Dolce & Gabbana» = «Dolce&Gabbana», «U.S. Polo Assn.» = «us polo assn»). Цифры остаются (EA7)."""
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(ch for ch in s if ch.isalnum() and not unicodedata.combining(ch))


def _title_key(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _month(now) -> int:
    if isinstance(now, int):
        return now if 1 <= now <= 12 else datetime.now().month
    if isinstance(now, (datetime, date)):
        return now.month
    return datetime.now().month


def _num(s: str) -> float | None:
    m = _NUM.search(s)
    return float(m.group(0).replace(",", ".")) if m else None


def load_config(path: Path | None = None) -> dict:
    try:
        return json.loads(Path(path or ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def parse_tiers(section: dict | None) -> tuple[dict[str, float], float]:
    """config.json → brand_tier: {"default": 0.5, "1.0": ["Gucci", ...], "0.8": [...]} (или плоско {"Gucci": 1.0}).
    Возвращает ({ключ бренда: вес}, вес по умолчанию)."""
    tiers: dict[str, float] = {}
    default = DEFAULT_TIER
    for k, v in (section or {}).items():
        if str(k).startswith("_"):
            continue
        try:
            if k == "default":
                default = float(v)
            elif isinstance(v, (list, tuple)):
                w = float(k)
                for b in v:
                    tiers[norm_key(b)] = w
            elif isinstance(v, (int, float)):
                tiers[norm_key(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return tiers, default


# ---------------------------------------------------------------- части оценки

def disc_n(discount) -> float:
    try:
        return _clamp((float(discount or 0) - 20) / 50)
    except (TypeError, ValueError):
        return 0.0


def price_band(price_uzs) -> float:
    p = float(price_uzs or 0)
    if p < 700_000:
        return 0.3
    if p < 1_500_000:
        return 0.7
    if p <= 6_000_000:
        return 1.0
    if p <= 12_000_000:
        return 0.8
    return 0.5


def season_weight(month, type_: str | None) -> float:
    """Вес типа вещи в этом месяце (0..1). month — 1..12, datetime/date или None (сейчас)."""
    return SEASON.get(_month(month), {}).get(type_ or "", SEASON_DEFAULT)


def popular_size(size, gender: str | None, type_: str | None) -> bool:
    """Ходовой ли размер для пола и типа. Пол неизвестен — ходовой для мужчин или для женщин; детское — нет."""
    if gender not in POPULAR:
        if gender:                       # kids и прочее — ходовых нет
            return False
        return any(popular_size(size, g, type_) for g in POPULAR)
    P = POPULAR[gender]
    u = re.sub(r"\s+", "", str(size or "").upper())
    if not u:
        return False
    # буквенные, в том числе «L/XL», «S-M»
    parts = [x for x in re.split(r"[/\-]", u) if x]
    if any(x in P["letters"] for x in parts):
        return True
    if type_ == "обувь":
        n = _num(u.replace("EU", ""))
        lo, hi = P["shoes"]
        return n is not None and lo <= n < hi + 1          # 44.5 — ещё 44-й, 45 — уже нет
    if type_ == "джинсы":
        m = _W.search(u)
        n = float(m.group(1) or m.group(2)) if m else _num(u)
        if n is None:
            return False
        lo, hi = P["jeans_w"]
        if lo <= n <= hi:                # W31 / 31W-32L / 31/32 / просто 31
            return True
        lo, hi = P["it"]
        return n >= 38 and lo <= n <= hi  # джинсы в итальянских размерах (48, 50…)
    n = _num(u)
    if n is None:
        return False
    lo, hi = P["it"]
    return lo <= n <= hi


def has_popular_size(sizes, gender: str | None, type_: str | None) -> bool:
    return any(popular_size(s, gender, type_) for s in sizes or [])


def size_n(row: dict) -> float:
    sizes = [str(s).strip() for s in row.get("sizes") or [] if str(s).strip()]
    if not sizes:
        return 0.0
    if all(s.lower() == ONE_SIZE for s in sizes):
        return 1.0                       # единый размер подходит всем
    pop = has_popular_size(sizes, row.get("gender"), row.get("type"))
    return min(len(sizes), 4) / 4 * (1.0 if pop else 0.5)


def photos_n(row: dict) -> float:
    n = row.get("_n_img")
    if n is None:
        n = sum(1 for u in row.get("images") or [] if isinstance(u, str) and u)
    return 1.0 if n >= 3 else 0.5


# ---------------------------------------------------------------- оценка и порядок

class Ranker:
    def __init__(self, tiers: dict[str, float] | None = None, default_tier: float = DEFAULT_TIER,
                 window: int = WINDOW, max_brand: int = MAX_BRAND, max_title: int = MAX_TITLE):
        self.tiers = dict(tiers or {})
        self.default_tier = float(default_tier)
        self.window, self.max_brand, self.max_title = int(window), int(max_brand), int(max_title)

    @classmethod
    def from_config(cls, cfg: dict | None = None, **kw) -> "Ranker":
        cfg = load_config() if cfg is None else cfg
        tiers, default = parse_tiers(cfg.get("brand_tier"))
        return cls(tiers, default, **kw)

    def tier(self, brand) -> float:
        return self.tiers.get(norm_key(brand), self.default_tier)

    def parts(self, row: dict, now=None) -> dict[str, float]:
        """Составляющие оценки (0..1 каждая) — для отладки и подписи «почему этот товар выше»."""
        return {"disc": disc_n(row.get("discount_pct")), "brand": self.tier(row.get("brand")),
                "size": size_n(row), "price": price_band(row.get("price_uzs")),
                "season": season_weight(now, row.get("type")), "photos": photos_n(row)}

    def score(self, row: dict, now=None) -> float:
        if row.get("in_stock") is False:
            return 0.0
        p = self.parts(row, now)
        return round(_clamp(sum(WEIGHTS[k] * p[k] for k in WEIGHTS)), 6)

    def scores(self, rows: list[dict], now=None) -> list[float]:
        month = _month(now)
        return [self.score(r, month) for r in rows]

    def positions(self, rows: list[dict], scores: list[float] | None = None, now=None) -> list[int]:
        """pos[i] — место rows[i] в итоговом порядке (0 — первый)."""
        return diversify(rows, self.scores(rows, now) if scores is None else scores,
                         window=self.window, max_brand=self.max_brand, max_title=self.max_title)

    def ranks(self, rows: list[dict], scores: list[float] | None = None, now=None) -> list[int]:
        """r[i] = len(rows) - pos[i]: сортировка по r по убыванию даёт итоговый порядок; r уникальны, 1..N."""
        n = len(rows)
        return [n - p for p in self.positions(rows, scores, now)]


def score(row: dict, now=None, tiers: dict[str, float] | None = None, default_tier: float = DEFAULT_TIER) -> float:
    """Оценка 0..1 без объекта Ranker; tiers — {ключ бренда: вес} (parse_tiers), без них — из config.json."""
    if tiers is None:
        return Ranker.from_config().score(row, now)
    return Ranker(tiers, default_tier).score(row, now)


def diversify(rows: list[dict], scores: list[float] | None = None, *, window: int = WINDOW,
              max_brand: int = MAX_BRAND, max_title: int = MAX_TITLE) -> list[int]:
    """Итоговый порядок: по убыванию оценки (при равенстве — скидка ↓, цена ↑, код), но в любом окне из window
    подряд не больше max_brand товаров одного бренда и не больше max_title с теми же брендом и названием.
    Возвращает места: pos[i] — номер rows[i] в итоговом порядке (0 — первый). Распроданные — в конце."""
    n = len(rows)
    if scores is None:
        rk = Ranker.from_config()
        scores = rk.scores(rows)

    def base_key(i):
        r = rows[i]
        return (-scores[i], -(r.get("discount_pct") or 0), r.get("price_uzs") or 0, str(r.get("id") or ""), i)

    order0 = sorted(range(n), key=base_key)
    live = [i for i in order0 if rows[i].get("in_stock") is not False]
    sold = [i for i in order0 if rows[i].get("in_stock") is False]
    rank0 = {i: k for k, i in enumerate(live)}
    bk = [norm_key(r.get("brand")) for r in rows]
    tk = [_title_key(r.get("title")) for r in rows]

    # по бренду: очереди по названию (в порядке оценки) и куча голов этих очередей (rank0, название)
    queues: dict[str, dict[str, deque]] = {}
    heaps: dict[str, list] = {}
    for i in live:
        b, t = bk[i], tk[i]
        qs = queues.setdefault(b, {})
        q = qs.get(t)
        if q is None:
            q = qs[t] = deque()
            heapq.heappush(heaps.setdefault(b, []), (rank0[i], t))
        q.append(i)

    bc: dict[str, int] = {}               # в окне (последние window-1): товаров бренда
    tc: dict[tuple, int] = {}             # … и с тем же брендом+названием
    recent: deque = deque()
    best: dict[str, tuple | None] = {}    # бренд -> (rank0, название) лучшей доступной головы (кэш)

    def best_of(b):
        if b in best:
            return best[b]
        h = heaps[b]
        popped, found = [], None
        while h:
            e = heapq.heappop(h)
            popped.append(e)
            if tc.get((b, e[1]), 0) < max_title:
                found = e
                break
        for e in popped:
            heapq.heappush(h, e)
        best[b] = found
        return found

    out: list[int] = []
    active = set(heaps)
    while active:
        pick = None
        for b in active:                  # 1) оба ограничения
            if bc.get(b, 0) >= max_brand:
                continue
            e = best_of(b)
            if e is not None and (pick is None or e[0] < pick[0][0]):
                pick = (e, b)
        if pick is None:                  # 2) без ограничения по бренду
            for b in active:
                e = best_of(b)
                if e is not None and (pick is None or e[0] < pick[0][0]):
                    pick = (e, b)
        if pick is None:                  # 3) без ограничений — просто лучший
            for b in active:
                e = heaps[b][0]
                if pick is None or e[0] < pick[0][0]:
                    pick = (e, b)
        (r0, t), b = pick
        # снять выбранную голову с кучи бренда и поставить следующую из очереди названия
        h = heaps[b]
        popped = []
        while True:
            e = heapq.heappop(h)
            if e == (r0, t):
                break
            popped.append(e)
        for e in popped:
            heapq.heappush(h, e)
        q = queues[b][t]
        i = q.popleft()
        if q:
            heapq.heappush(h, (rank0[q[0]], t))
        else:
            del queues[b][t]
        if not h:
            active.discard(b)
        out.append(i)
        # окно
        bc[b] = bc.get(b, 0) + 1
        tc[(b, t)] = tc.get((b, t), 0) + 1
        recent.append((b, t))
        best.pop(b, None)
        if len(recent) > window - 1:
            ob, ot = recent.popleft()
            bc[ob] -= 1
            tc[(ob, ot)] -= 1
            best.pop(ob, None)
    out += sold
    pos = [0] * n
    for k, i in enumerate(out):
        pos[i] = k
    return pos


def order_from_positions(pos: list[int]) -> list[int]:
    """pos[i] -> индексы строк в итоговом порядке."""
    out = [0] * len(pos)
    for i, p in enumerate(pos):
        out[p] = i
    return out


def ranks(rows: list[dict], now=None, cfg: dict | None = None) -> list[int]:
    """r для каждой строки (см. Ranker.ranks); cfg — config.json, без него читается с диска."""
    return Ranker.from_config(cfg).ranks(rows, now=now)
