"""Общий формат товара и запроса для всех источников.

Каждый адаптер (sources/<имя>.py) реализует одну функцию:

    def fetch(query: Query, **opts) -> list[Product]

и возвращает товары СО СКИДКОЙ в едином формате. Фильтр по цене в сумах,
скидке и типу окончательно применяет run.py, но адаптер может использовать
query, чтобы не ходить по лишним страницам.
"""
from __future__ import annotations

import re
import time
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# Нормализованные типы товаров. Порядок важен: проверяется сверху вниз,
# первое совпадение побеждает (например, «толстовки» раньше «футболок»:
# «polo yaka sweatshirt»; «футболки» раньше «рубашек»;
# «костюмы» раньше «платьев», потому что «takım elbise» = костюм).
TYPE_ORDER = [
    "обувь", "сумки", "нижнее бельё", "джинсы", "толстовки", "футболки и поло",
    "пиджаки и костюмы", "платья", "юбки", "шорты", "рубашки",
    "свитеры и кардиганы", "куртки и пальто", "брюки", "аксессуары",
]

TYPE_KEYWORDS = {
    "обувь": r"shoe|sneaker|\bboots?\b|stivalett|loafer|sandal|mocassin|stival|scarpe|d[ée]collet|ballerin|espadrill|ciabatt|infradito|slipper|slip-on|ayakkab|[çc]izme|\bbot\b|terlik|sandalet|babet|обув|кроссов|ботин|туфл|сапог|лофер|кед",
    "сумки": r"\bbags?\b|borsa|borse|zaino|backpack|tote|clutch|[çc]anta|сумк|рюкзак",
    "нижнее бельё": r"underwear|boxer|\bbriefs?\b|intimo|i[çc] giyim|k[üu]lot|\bbra\b|reggiseno|бель[её]|трус",
    "джинсы": r"\bjeans?\b|denim|kot pantolon|\bkot\b|джинс",
    "футболки и поло": r"\bt-?shirt|\btee\b|polo|ti[şs][öo]rt|футболк|поло",
    "толстовки": r"sweatshirt|hoodie|felpa|толстовк|худи|свитшот",
    "пиджаки и костюмы": r"blazer|\bsuits?\b|completo|tak[ıi]m elbise|tak[ıi]m|пиджак|костюм",
    "платья": r"\bdress|abito|vestit|elbise|плать",
    "юбки": r"skirt|gonna|gonne|\betek|юбк",
    "шорты": r"\bshorts\b|bermuda|[şs]ort\b|шорт",
    "рубашки": r"\bshirts?\b|camici|g[öo]mlek|рубаш|блуз|blouse|bluz",
    "свитеры и кардиганы": r"sweater|pullover|cardigan|maglia|maglion|knit|kazak|h[ıi]rka|triko|свитер|кардиган|джемпер",
    "куртки и пальто": r"jacket|coat|parka|piumin|giubbott|cappott|giacca|\bmont\b|kaban|ceket|tren[çc]kot|yelek|gilet|куртк|пальто|пуховик|парка|жилет",
    "брюки": r"trouser|\bpants?\b|pantalon|pantol|chino|jogger|e[şs]ofman alt|брюк",
    "аксессуары": r"\bbelts?\b|cintur|wallet|portafogl|scarf|sciarp|\bhats?\b|cappell|\bties?\b|cravatt|kemer|c[üu]zdan|atk[ıi]|[şs]apka|kravat|sunglass|occhial|ремень|кошел|шарф|галстук|очки|перчат|guant|eldiven",
}
_TYPE_RE = {t: re.compile(p, re.I) for t, p in TYPE_KEYWORDS.items()}


def classify_type(*texts: str | None) -> str | None:
    """Определяет нормализованный тип по названию категории / товара (ru/en/it/tr)."""
    text = " ".join(t for t in texts if t).lower()
    for t in TYPE_ORDER:
        if _TYPE_RE[t].search(text):
            return t
    return None


def norm_brand(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def discount_pct(now: float | None, old: float | None) -> float | None:
    if not now or not old or old <= now:
        return None
    return round((1 - now / old) * 100, 1)


def polite_sleep(lo: float = 1.5, hi: float = 3.5) -> None:
    """Пауза между запросами к одному сайту — не нагружаем источник."""
    time.sleep(random.uniform(lo, hi))


MAX_IMAGES = 8           # сколько фото товара хранить (все ракурсы, но не больше)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@dataclass
class Query:
    brands: list[str] = field(default_factory=list)   # пусто = все бренды источника
    types: list[str] = field(default_factory=list)    # ключи TYPE_KEYWORDS; пусто = все
    genders: list[str] = field(default_factory=list)  # "men" | "women"; пусто = все
    discount_min: float = 0                           # минимальная скидка, %
    max_pages: int = 3                                # максимум страниц выдачи на один раздел
    source_opts: dict = field(default_factory=dict)   # настройки источника из config.json

    def wants_brand(self, brand: str) -> bool:
        return not self.brands or norm_brand(brand) in {norm_brand(b) for b in self.brands}


@dataclass
class Product:
    source: str               # "pcardin_tr" | "cacharel_tr" | "trendyol" | "yoox"
    source_item_id: str       # id товара на сайте-источнике
    brand: str
    title: str
    category: str             # категория как на сайте-источнике
    type: str | None          # нормализованный тип (classify_type)
    gender: str | None        # "men" | "women" | "kids" | None
    price_now: float          # цена со скидкой, в валюте источника
    price_old: float | None   # цена до скидки
    currency: str             # "EUR" | "TRY"
    discount_pct: float | None
    sizes: list[str]          # размеры в наличии
    colors: list[str]
    images: list[str]         # абсолютные URL или пути относительно site/ (например "img/yoox/123.jpg"), до 8 шт.
    url: str                  # ссылка на товар на сайте-источнике
    in_stock: bool = True
    style_code: str | None = None
    # сырые описательные свойства как на сайте-источнике (состав, цвет, крой, воротник, рукав…);
    # describe.py переводит их в русский текст для карточки. Наружу (products.js) не выкладываются.
    attrs: dict = field(default_factory=dict)
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)
