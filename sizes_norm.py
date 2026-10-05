"""Ключи фильтра размеров: разные записи одного размера -> один ключ (для фильтра витрины).

    from sizes_norm import filter_keys, sort_keys
    filter_keys(["EU40/IT39", "40.5", "41 ⅓"], "обувь")          # -> ["40", "40½", "41⅓"]
    filter_keys(["28W-32L", "29", "30/32"], "джинсы", "men")      # -> ["W28", "W29", "W30"]
    filter_keys(["39", "40", "41"], "рубашки", "men")             # -> ["ворот 39", "ворот 40", "ворот 41"]

Показываемый список размеров (sizes) НЕ меняется — заказ проверяется по нему (order_api.py). Ключи — только
для фильтра: товар подходит под выбранный размер, если ключ есть в его списке (столбец "zk" индекса,
см. catalog_files.py). Ничего не придумывается: ключ строится только из размера, который есть у товара.

Правила по типу товара (base.TYPE_ORDER):
  обувь                       «EU40/IT39» -> «40» (берётся EU), «40 ⅓» -> «40⅓», «40.5» / «40,5» -> «40½»,
                              «35-37» -> «35–37»
  джинсы, брюки, шорты, юбки  по талии (дюймы): «28W-32L», «28W», «28/32», «29-32» -> «W28»; простые числа —
                              по талии или итальянский размер, решается по товару целиком (waist_mode):
                              есть запись с W/L или нечётное число 23–41 -> талия; женское, все чётные и
                              от 34 -> итальянский; иначе талия, если все числа до 42. Итальянский — само число
                              («46»); «46-6 Drop» / «50R» -> «46» / «50»
  пиджаки и костюмы, куртки   «48-6 Drop», «50R», «48 ⅔», «48.5» -> «48»
  рубашки                     размер ворота -> «ворот 40» (collar_mode: нечётные 35–47, у мужских — и все
                              числа 35–46); иначе итальянский размер («44»)
  аксессуары                  ремни в сантиметрах (числа 65–140) -> «см 90»
  все типы                    буквенные: XXL = 2XL -> «XXL», XXXL -> «3XL», 2XS -> «XXS», «S/M» -> «S» и «M»;
                              носки «39_42» / «39-42» -> «39–42»; «21.5» -> «21½»; «Единый размер» (и «--»,
                              ONESIZE …) — ключ «Единый размер»; бельё «1 B», «II C», платки «90x90» — как есть

default_keys(размер) — ключи одного размера без учёта товара (для сжатия столбца zk, см. catalog_files.py).

Порядок ключей (sort_key): буквенные XXXS … 6XL; числа по значению (40 < 40⅓ < 40½ < 40⅔ < 41; диапазоны
«39–42» — по первому числу); «W24» … по числу; «ворот 37» … по числу; «см 70» … по числу; прочее
(«1 B», «II C», «90x90») по алфавиту; «Единый размер» — последним.
"""
from __future__ import annotations

import re

ONE_SIZE = "Единый размер"
LETTERS = ["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL", "6XL"]
LETTER_ALIAS = {"3XS": "XXXS", "2XS": "XXS", "2XL": "XXL", "XXXL": "3XL", "XXXXL": "4XL", "XXXXXL": "5XL",
                "XXXXXXL": "6XL"}
_ONE = {"--", "-", "ONESIZE", "ONE SIZE", "OS", "TU", "UNI", "UNICA", "STD", "STANDART", "TEK EBAT",
        ONE_SIZE.upper()}
BOTTOMS = ("джинсы", "брюки", "шорты", "юбки")
TAILORED = ("пиджаки и костюмы", "куртки и пальто")
FRACTIONS = {"⅓": 1 / 3, "½": 0.5, "⅔": 2 / 3}
DASH = "–"          # en dash в диапазонах: «39–42»

_W_RE = re.compile(r"(\d{2})\s*W(?:\s*[-/]?\s*L?\s*\d{2}\s*L?)?", re.I)      # 28W, 28W-32L, 25W-L33
_WL_RE = re.compile(r"(\d{2})\s*[/-]\s*(\d{2})")                               # 28/32, 29-32 (талия/длина)
_DROP_RE = re.compile(r"(\d{2})\s*(?:-\s*\d+\s*DROP|R|L|S|C)", re.I)           # 48-6 Drop, 50R
_FRAC_RE = re.compile(r"(\d{1,2})\s*([⅓½⅔])")
_DEC_RE = re.compile(r"(\d{1,2})[.,](\d)")
_EUIT_RE = re.compile(r"EU\s*(\d{1,2}(?:[.,]\d)?)\s*/\s*IT\s*\d{1,2}(?:[.,]\d)?", re.I)
_RANGE_RE = re.compile(r"(\d{1,3})\s*[-_–]\s*(\d{1,3})")


def _letters(s: str) -> list[str] | None:
    """«xxl» -> ["XXL"], «S/M» -> ["S", "M"], «XXL/XXXL» -> ["XXL", "3XL"]; не буквенный -> None."""
    u = re.sub(r"\s+", "", s.upper())
    parts = u.split("/") if "/" in u else [u]
    out = []
    for x in parts:
        x = LETTER_ALIAS.get(x, x)
        if x not in LETTERS:
            return None
        out.append(x)
    return out


def _half(n: float) -> str:
    """40.5 -> «40½», 40.333 -> «40⅓», 40 -> «40»."""
    whole = int(n)
    rest = n - whole
    for sym, val in FRACTIONS.items():
        if abs(rest - val) < 0.05:
            return f"{whole}{sym}"
    return str(whole)


def _plain_numbers(sizes) -> list[int]:
    return [int(s) for s in (str(x).strip() for x in sizes or []) if re.fullmatch(r"\d{2}", s)]


def waist_mode(sizes, ptype: str | None = None, gender: str | None = None) -> bool:
    """Числа у низа (джинсы/брюки/шорты/юбки) — размер по талии в дюймах (W), а не итальянский?
    Решается по всем размерам товара: запись с W/L («28W-32L», «28/32») -> да; «46-6 Drop» / «50R» -> нет;
    нечётное число 23–41 -> да (итальянские размеры только чётные); женское, все чётные и от 34 -> нет
    (женский итальянский 34–50); иначе да, если все числа не больше 42 (мужской итальянский — от 44)."""
    if ptype is not None and ptype not in BOTTOMS:
        return False
    vals = [str(x).strip() for x in sizes or [] if str(x).strip()]
    if any(_W_RE.fullmatch(s) or _WL_RE.fullmatch(s) for s in vals):
        return True
    if any(_DROP_RE.fullmatch(s) for s in vals):
        return False
    nums = [n for n in _plain_numbers(vals) if n >= 20]
    if not nums:
        return False
    if any(n % 2 and 23 <= n <= 41 for n in nums):
        return True
    if gender == "women":
        return min(nums) < 34 and max(nums) <= 42
    return max(nums) <= 42


def collar_mode(sizes, gender: str | None = None) -> bool:
    """Числа у рубашки — размер ворота (37–46 см), а не итальянский размер одежды?
    Нечётное число 35–47 -> да; у мужских (и без пола) — да и тогда, когда все числа в 35–46
    (мужской итальянский размер одежды — от 44 и шире: 48, 50 …)."""
    nums = [n for n in _plain_numbers(sizes) if n >= 20]
    if not nums:
        return False
    if any(n % 2 and 35 <= n <= 47 for n in nums):
        return True
    if gender == "women":
        return False
    return 35 <= min(nums) and max(nums) <= 46


def _shoe_key(s: str) -> str | None:
    m = _EUIT_RE.fullmatch(s)
    if m:
        s = m.group(1)
    m = _FRAC_RE.fullmatch(s)
    if m:
        return m.group(1) + m.group(2)
    m = _DEC_RE.fullmatch(s)
    if m:
        return _half(int(m.group(1)) + int(m.group(2)) / 10)
    if re.fullmatch(r"\d{1,2}", s):
        return str(int(s))
    m = _RANGE_RE.fullmatch(s)
    if m:
        return f"{int(m.group(1))}{DASH}{int(m.group(2))}"
    return None


def _num_part(s: str) -> int | None:
    """«48-6 Drop» / «50R» / «48 ⅔» / «48.5» / «48» -> 48."""
    for rx in (_DROP_RE, _FRAC_RE, _DEC_RE):
        m = rx.fullmatch(s)
        if m:
            return int(m.group(1))
    return int(s) if re.fullmatch(r"\d{1,3}", s) else None


def _one_key(s: str, ptype: str | None, waist: bool, collar: bool) -> list[str]:
    if s.upper() in _ONE:
        return [ONE_SIZE]
    let = _letters(s)
    if let:
        return let
    if ptype == "обувь":
        k = _shoe_key(s)
        return [k] if k else [s]
    if ptype in BOTTOMS:
        m = _W_RE.fullmatch(s) or _WL_RE.fullmatch(s)
        if m:
            n = int(m.group(1))
            return [f"W{n}"]
        n = _num_part(s)
        if n is not None:
            return [f"W{n}"] if waist and 23 <= n <= 44 else [str(n)]
        return [s]
    if ptype in TAILORED:
        n = _num_part(s)
        return [str(n)] if n is not None else [s]
    if ptype == "рубашки":
        n = _num_part(s)
        if n is not None:
            return [f"ворот {n}"] if collar and 35 <= n <= 48 else [str(n)]
        return [s]
    if ptype == "аксессуары" and re.fullmatch(r"\d{2,3}", s) and 65 <= int(s) <= 140:
        return [f"см {int(s)}"]                      # ремни: длина в сантиметрах
    m = _RANGE_RE.fullmatch(s)                       # носки «39_42», «35-38» -> «39–42»
    if m and int(m.group(1)) < int(m.group(2)):
        return [f"{int(m.group(1))}{DASH}{int(m.group(2))}"]
    if re.fullmatch(r"\d{1,3}", s):
        return [str(int(s))]
    m = _DEC_RE.fullmatch(s) or _FRAC_RE.fullmatch(s)
    if m:                                            # «21.5» -> «21½»
        return [_half(int(m.group(1)) + (int(m.group(2)) / 10 if m.group(2).isdigit() else FRACTIONS[m.group(2)]))]
    return [s]                                       # бельё «1 B», «II C», платки «90x90» — как есть


def filter_keys(sizes, ptype: str | None = None, gender: str | None = None) -> list[str]:
    """Размеры товара (как на карточке) -> ключи фильтра без повторов, в порядке sort_key."""
    vals = []
    for x in sizes or []:
        s = re.sub(r"\s+", " ", str(x)).strip()
        if s and s not in vals:
            vals.append(s)
    waist = waist_mode(vals, ptype, gender) if ptype in BOTTOMS else False
    collar = collar_mode(vals, gender) if ptype == "рубашки" else False
    out: list[str] = []
    for s in vals:
        for k in _one_key(s, ptype, waist, collar):
            if k and k not in out:
                out.append(k)
    return sort_keys(out)


def default_keys(size) -> list[str]:
    """Ключи одного размера без учёта товара: буквенные, «EU40/IT39», дроби, «48-6 Drop», диапазоны, числа.
    По ним catalog_files сжимает столбец zk: у строки, чьи ключи совпадают с ключами её размеров по этой таблице
    (manifest.dict.szk), в zk стоит 0. Отличаются (и пишутся списком) низ по талии («W30»), рубашки по вороту
    («ворот 40»), ремни («см 90»), пиджаки с «48 ⅔» и т.п."""
    s = re.sub(r"\s+", " ", str(size or "")).strip()
    if not s:
        return []
    if s.upper() in _ONE:
        return [ONE_SIZE]
    let = _letters(s)
    if let:
        return let
    m = _W_RE.fullmatch(s) or re.fullmatch(r"(\d{2})\s*/\s*\d{2}", s)       # «28W-32L», «30/32» — всегда талия
    if m:
        return [f"W{int(m.group(1))}"]
    m = _DROP_RE.fullmatch(s)
    if m:
        return [str(int(m.group(1)))]
    k = _shoe_key(s)
    return [k] if k else [s]


def _num_value(s: str) -> float | None:
    m = re.fullmatch(r"(\d{1,3})([⅓½⅔])?", s)
    if not m:
        return None
    return int(m.group(1)) + (FRACTIONS[m.group(2)] if m.group(2) else 0)


def sort_key(k: str):
    """Порядок ключей: буквенные, числа (и диапазоны), W, ворот, см, прочее, «Единый размер»."""
    k = str(k or "")
    if k in LETTERS:
        return (0, LETTERS.index(k), "")
    v = _num_value(k)
    if v is not None:
        return (1, v, "")
    m = re.fullmatch(rf"(\d{{1,3}}){DASH}(\d{{1,3}})", k)
    if m:
        return (1, int(m.group(1)) + 0.01, k)
    for grp, prefix in ((2, "W"), (3, "ворот "), (4, "см ")):
        if k.startswith(prefix) and k[len(prefix):].isdigit():
            return (grp, int(k[len(prefix):]), "")
    if k == ONE_SIZE:
        return (6, 0, "")
    return (5, 0, k)


def sort_keys(keys) -> list[str]:
    return sorted(dict.fromkeys(k for k in keys if k), key=sort_key)
