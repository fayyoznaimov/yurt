"""Русский текст для карточки товара — без намёков на магазин-источник.

    from describe import describe
    d = describe(product_dict)   # {"title", "color", "composition", "details", "description", "size_system"}

На входе — товар из data/raw_*.json (Product.to_dict() + country из run.py). Используются только
бренд, тип, категория, исходное название и сырые свойства product["attrs"] (итальянские у YOOX,
турецкие у Pierre Cardin / Cacharel / Trendyol). Названия магазинов, цены и ссылки в текст не попадают.

Словари ниже — обычные dict на уровне модуля; ключи пишутся как на сайте (с турецкими/итальянскими
буквами и в любом регистре): при загрузке они приводятся к «сложенному» виду (см. fold), так что
«Kısa Kol», «KISA KOL» и «kisa kol» — один ключ. Чего нет в словаре, то не показывается (никакого
сырого итальянского/турецкого текста на сайте), а run.py печатает самые частые непереведённые
значения — их и стоит добавлять сюда.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

# ---------------------------------------------------------------- нормализация


_FOLD = str.maketrans({
    "ı": "i", "ş": "s", "ç": "c", "ğ": "g", "ö": "o", "ü": "u", "â": "a", "î": "i", "û": "u",
    "à": "a", "á": "a", "è": "e", "é": "e", "ì": "i", "í": "i", "ò": "o", "ó": "o", "ù": "u", "ú": "u",
    "’": "'", "`": "'", "–": "-", "—": "-", "®": "", "™": "",
})


def fold(s) -> str:
    """«Kısa Kol» / «KISA KOL» / «Maniche corte» -> «kisa kol» / «maniche corte»: нижний регистр без диакритики."""
    s = str(s or "").replace("İ", "i").replace("I", "i").lower().translate(_FOLD)
    return re.sub(r"\s+", " ", s).strip(" .,;:")


def _fk(d: dict) -> dict:
    """Словарь с ключами в сложенном виде (fold)."""
    return {fold(k): v for k, v in d.items()}


# ---------------------------------------------------------------- непереведённое

MISSING: dict[str, Counter] = defaultdict(Counter)   # категория -> Counter(значение)


def _miss(kind: str, value) -> None:
    v = re.sub(r"\s+", " ", str(value or "")).strip()
    if v and len(v) <= 60:
        MISSING[kind][v] += 1


def missing_report(top: int = 15) -> list[str]:
    """Строки для консоли: самые частые непереведённые значения по категориям."""
    out = []
    for kind in sorted(MISSING):
        c = MISSING[kind]
        items = ", ".join(f"{v} ({n})" for v, n in c.most_common(top))
        more = f" …ещё {len(c) - top}" if len(c) > top else ""
        out.append(f"  {kind}: {items}{more}")
    return out


# ---------------------------------------------------------------- цвета (IT + TR -> RU)
# Значение — прилагательное в мужском роде или несклоняемое слово («хаки»). Для фразы «…цвета»
# родительный падеж строится автоматически (-ый/-ий/-ой -> -ого/-его), исключения — в COLOR_GEN.

COLORS = _fk({
    # итальянский (colorLabel YOOX)
    "Nero": "чёрный", "Bianco": "белый", "Bianco ottico": "белый", "Blu notte": "тёмно-синий", "Blu": "синий",
    "Blu navy": "тёмно-синий", "Blu scuro": "тёмно-синий", "Blu chiaro": "голубой", "Blu china": "тёмно-синий",
    "Grigio": "серый", "Grigio chiaro": "светло-серый", "Grigio scuro": "тёмно-серый", "Grigio perla": "жемчужно-серый",
    "Antracite": "тёмно-серый", "Piombo": "тёмно-серый", "Canna di fucile": "тёмно-серый",
    "Beige": "бежевый", "Sabbia": "песочный", "Tortora": "серо-бежевый", "Cammello": "кэмел",
    "Avorio": "молочный", "Off white": "молочный", "Panna": "кремовый", "Ecru": "молочный",
    "Testa di moro": "тёмно-коричневый", "Marrone": "коричневый", "Marrone scuro": "тёмно-коричневый",
    "Marrone chiaro": "светло-коричневый", "Cioccolato": "шоколадный", "Nocciola": "светло-коричневый",
    "Cuoio": "коньячный", "Ruggine": "рыжий", "Mattone": "кирпичный",
    "Verde militare": "хаки", "Khaki": "хаки", "Kaki": "хаки", "Verde": "зелёный", "Verde scuro": "тёмно-зелёный",
    "Verde chiaro": "светло-зелёный", "Verde salvia": "серо-зелёный", "Verde petrolio": "сине-зелёный",
    "Verde lime": "лаймовый", "Verde smeraldo": "изумрудный", "Verde oliva": "оливковый", "Ottanio": "сине-зелёный",
    "Celeste": "голубой", "Azzurro": "голубой", "Avio": "серо-голубой", "Carta da zucchero": "пыльно-голубой",
    "Turchese": "бирюзовый",
    "Rosso": "красный", "Rosso scuro": "тёмно-красный", "Rosso pomodoro": "ярко-красный", "Bordeaux": "бордовый",
    "Rosa": "розовый", "Rosa chiaro": "светло-розовый", "Rosa antico": "пыльно-розовый", "Cipria": "пудровый",
    "Corallo": "коралловый", "Salmone": "лососевый", "Fucsia": "фуксия", "Magenta": "пурпурный",
    "Porpora": "пурпурный", "Prugna": "сливовый", "Viola": "фиолетовый", "Viola scuro": "тёмно-фиолетовый",
    "Malva": "лиловый", "Lilla": "сиреневый",
    "Arancione": "оранжевый", "Mandarino": "оранжевый", "Albicocca": "абрикосовый",
    "Giallo": "жёлтый", "Giallo ocra": "охристый", "Giallo pastello": "пастельно-жёлтый", "Giallo chiaro": "светло-жёлтый",
    "Senape": "горчичный",
    "Argento": "серебристый", "Oro": "золотистый", "Oro rosa": "розово-золотой", "Bronzo": "бронзовый",
    "Trasparente": "прозрачный", "Multicolore": "разноцветный",
    # турецкий (Pierre Cardin, Cacharel, Trendyol)
    "Siyah": "чёрный", "Beyaz": "белый", "Kırık Beyaz": "молочный", "Lacivert": "тёмно-синий",
    "Koyu Lacivert": "тёмно-синий", "Açık Lacivert": "синий", "Gece Mavisi": "тёмно-синий",
    "Gri": "серый", "Açık Gri": "светло-серый", "Koyu Gri": "тёмно-серый", "Antrasit": "тёмно-серый",
    "Füme": "тёмно-серый", "Duman": "дымчато-серый",
    "Bej": "бежевый", "Açık Bej": "светло-бежевый", "Koyu Bej": "тёмно-бежевый", "Kum": "песочный",
    "Stone": "серо-бежевый", "Taş": "серо-бежевый", "Vizon": "серо-коричневый", "Camel": "кэмел",
    "Ekru": "молочный", "Krem": "кремовый", "Taba": "коньячный", "Tarçın": "рыжевато-коричневый",
    "Kahverengi": "коричневый", "Kahve": "коричневый", "Açık Kahverengi": "светло-коричневый",
    "Açık Kahve": "светло-коричневый", "Koyu Kahverengi": "тёмно-коричневый", "Koyu Kahve": "тёмно-коричневый",
    "Toprak": "терракотовый", "Kiremit": "кирпичный",
    "Haki": "хаки", "Yeşil": "зелёный", "Koyu Yeşil": "тёмно-зелёный", "Açık Yeşil": "светло-зелёный",
    "Yağ Yeşili": "оливковый", "Zümrüt": "изумрудный", "Zümrüt Yeşili": "изумрудный", "Mint": "мятный",
    "Mint Yeşili": "мятный", "Su Yeşili": "мятный", "Çağla": "фисташковый", "Petrol": "сине-зелёный",
    "Petrol Mavisi": "сине-зелёный",
    "Mavi": "синий", "Açık Mavi": "голубой", "Koyu Mavi": "тёмно-синий", "Buz Mavisi": "светло-голубой",
    "Bebe Mavisi": "нежно-голубой", "Saks": "ярко-синий", "Saks Mavi": "ярко-синий", "Saks Mavisi": "ярко-синий",
    "İndigo": "индиго", "Turkuaz": "бирюзовый",
    "Kırmızı": "красный", "Bordo": "бордовый", "Vişne": "вишнёвый", "Nar Çiçeği": "коралловый",
    "Pembe": "розовый", "Açık Pembe": "светло-розовый", "Pudra": "пудровый", "Somon": "лососевый",
    "Fuşya": "фуксия", "Mor": "фиолетовый", "Lila": "сиреневый", "Mürdüm": "сливовый",
    "Turuncu": "оранжевый", "Sarı": "жёлтый", "Hardal": "горчичный",
    "Gümüş": "серебристый", "Altın": "золотистый", "Bronz": "бронзовый", "Nude": "телесный", "Ten": "телесный",
    "Koyu İndigo": "тёмно-синий", "Açık İndigo": "синий", "Koyu Haki": "тёмно-оливковый",
    "Açık Haki": "светло-оливковый", "Koyu Vizon": "серо-коричневый", "Mercan": "коралловый",
    "Oranj": "оранжевый", "Aqua": "бирюзовый", "Deve Tüyü": "кэмел",
    "Çok Renkli": "разноцветный", "Renkli": "разноцветный", "Karışık": "разноцветный",
    # английский (встречается в турецких магазинах)
    "Black": "чёрный", "White": "белый", "Navy": "тёмно-синий", "Grey": "серый", "Gray": "серый",
    "Blue": "синий", "Brown": "коричневый", "Green": "зелёный", "Red": "красный", "Beige": "бежевый",
    "Cream": "кремовый", "Multicolor": "разноцветный", "Gold": "золотистый", "Silver": "серебристый",
})
# Несклоняемые / особые формы для фразы «…цвета»: «цвета хаки», «цвета фуксии».
COLOR_GEN = {"хаки": "цвета хаки", "кэмел": "цвета кэмел", "индиго": "цвета индиго", "фуксия": "цвета фуксии"}
# Приставки и суффиксы оттенка, если точного сочетания нет в COLORS: «Açık Bej», «Gri Melanj».
COLOR_PREFIX = _fk({"Açık": "светло-", "Koyu": "тёмно-", "Chiaro": "светло-", "Scuro": "тёмно-"})
COLOR_SUFFIX = _fk({"Melanj": " меланж", "Mélange": " меланж", "Melange": " меланж"})


def color_ru(raw) -> str | None:
    """«Testa di moro» -> «тёмно-коричневый», «Antrasit Melanj» -> «тёмно-серый меланж». Неизвестный -> None."""
    if not raw:
        return None
    s = fold(str(raw).replace("-", " ") if re.fullmatch(r"[a-z-]+", str(raw)) else raw)   # «koyu-yesil» (slug)
    s = re.sub(r"^\d+\s*[-_.]?\s*", "", s)              # «01-Siyah»
    if not s or re.fullmatch(r"[a-z]{0,3}\d+[a-z]?", s):  # код цвета «VR249» — не цвет
        return None
    if s in COLORS:
        return COLORS[s]
    suffix = ""
    for k, v in COLOR_SUFFIX.items():
        if s.endswith(" " + k):
            s, suffix = s[: -len(k) - 1].strip(), v
            break
    if s in COLORS:
        return COLORS[s] + suffix
    head, _, rest = s.partition(" ")
    if head in COLOR_PREFIX and rest in COLORS and re.fullmatch(r"[а-яё]+(ый|ий|ой)", COLORS[rest]):
        return COLOR_PREFIX[head] + COLORS[rest] + suffix
    _miss("цвет", raw)
    return None


def color_phrase(color: str | None) -> str | None:
    """«тёмно-синий» -> «тёмно-синего цвета»; «серый меланж» -> None (не склоняем составные)."""
    if not color:
        return None
    if color in COLOR_GEN:
        return COLOR_GEN[color]
    m = re.fullmatch(r"([а-яё-]+?)(ый|ий|ой)", color)
    if not m:
        return None
    stem, end = m.groups()
    gen = "его" if end == "ий" and not re.search(r"[гкхжшчщ]$", stem) else "ого"
    return f"{stem}{gen} цвета"


# ---------------------------------------------------------------- состав (волокна IT + TR + EN -> RU)
# Значение — (именительный, родительный): «100% хлопок», «из 100% хлопка».

FIBRES = _fk({
    "Cotone": ("хлопок", "хлопка"), "Pamuk": ("хлопок", "хлопка"), "Cotton": ("хлопок", "хлопка"),
    "Pamuklu": ("хлопок", "хлопка"),
    "Cotone organico": ("органический хлопок", "органического хлопка"),
    "Organik Pamuk": ("органический хлопок", "органического хлопка"),
    "Organic Cotton": ("органический хлопок", "органического хлопка"),
    "Cotone riciclato": ("переработанный хлопок", "переработанного хлопка"),
    "Geri Dönüştürülmüş Pamuk": ("переработанный хлопок", "переработанного хлопка"),
    "Lana": ("шерсть", "шерсти"), "Yün": ("шерсть", "шерсти"), "Wool": ("шерсть", "шерсти"),
    "Lana Vergine": ("натуральная шерсть", "натуральной шерсти"), "Virgin Wool": ("натуральная шерсть", "натуральной шерсти"),
    "Lana Merino": ("шерсть мериноса", "шерсти мериноса"), "Merino": ("шерсть мериноса", "шерсти мериноса"),
    "Merinos": ("шерсть мериноса", "шерсти мериноса"), "Merinos Yünü": ("шерсть мериноса", "шерсти мериноса"),
    "Lana d'agnello": ("шерсть ягнёнка", "шерсти ягнёнка"), "Kuzu Yünü": ("шерсть ягнёнка", "шерсти ягнёнка"),
    "Lambswool": ("шерсть ягнёнка", "шерсти ягнёнка"),
    "Lana di alpaca": ("шерсть альпаки", "шерсти альпаки"), "Alpaca": ("шерсть альпаки", "шерсти альпаки"),
    "Alpaka": ("шерсть альпаки", "шерсти альпаки"), "Mohair": ("мохер", "мохера"), "Moher": ("мохер", "мохера"),
    "Lana riciclata": ("переработанная шерсть", "переработанной шерсти"),
    "Cachemire": ("кашемир", "кашемира"), "Cashmere": ("кашемир", "кашемира"), "Kaşmir": ("кашемир", "кашемира"),
    "Seta": ("шёлк", "шёлка"), "İpek": ("шёлк", "шёлка"), "Silk": ("шёлк", "шёлка"),
    "Lino": ("лён", "льна"), "Keten": ("лён", "льна"), "Linen": ("лён", "льна"),
    "Canapa": ("конопляное волокно", "конопляного волокна"), "Kenevir": ("конопляное волокно", "конопляного волокна"),
    "Poliestere": ("полиэстер", "полиэстера"), "Poliester": ("полиэстер", "полиэстера"),
    "Polyester": ("полиэстер", "полиэстера"),
    "Poliestere riciclato": ("переработанный полиэстер", "переработанного полиэстера"),
    "Geri Dönüştürülmüş Polyester": ("переработанный полиэстер", "переработанного полиэстера"),
    "Geri Dönüştürülmüş Poliester": ("переработанный полиэстер", "переработанного полиэстера"),
    "Viscosa": ("вискоза", "вискозы"), "Viskon": ("вискоза", "вискозы"), "Viskoz": ("вискоза", "вискозы"),
    "Viscose": ("вискоза", "вискозы"), "Rayon": ("вискоза", "вискозы"),
    "Poliviskon": ("поливискоза", "поливискозы"),
    "Elastan": ("эластан", "эластана"), "Elastane": ("эластан", "эластана"), "Elestan": ("эластан", "эластана"),
    "Spandex": ("эластан", "эластана"), "Spandeks": ("эластан", "эластана"), "Elastan - Spandeks": ("эластан", "эластана"),
    "Likra": ("эластан", "эластана"), "Lycra": ("эластан", "эластана"), "Elastomero": ("эластан", "эластана"),
    "Elastomultiestere": ("эластомультиэстер", "эластомультиэстера"),
    "Poliammide": ("полиамид", "полиамида"), "Poliamid": ("полиамид", "полиамида"), "Polyamide": ("полиамид", "полиамида"),
    "Nylon": ("полиамид", "полиамида"), "Naylon": ("полиамид", "полиамида"),
    "Poliammide riciclato": ("переработанный полиамид", "переработанного полиамида"),
    "Acrilico": ("акрил", "акрила"), "Akrilik": ("акрил", "акрила"), "Acrylic": ("акрил", "акрила"),
    "Modal": ("модал", "модала"), "Lyocell": ("лиоцелл", "лиоцелла"), "Liyosel": ("лиоцелл", "лиоцелла"),
    "Tencel": ("лиоцелл", "лиоцелла"), "Cupro": ("купра", "купры"), "Acetato": ("ацетат", "ацетата"),
    "Asetat": ("ацетат", "ацетата"), "Poliuretano": ("полиуретан", "полиуретана"),
    "Poliüretan": ("полиуретан", "полиуретана"), "Polyurethane": ("полиуретан", "полиуретана"),
    "PU": ("полиуретан", "полиуретана"), "PVC - Polivinilcloruro": ("ПВХ", "ПВХ"), "PVC": ("ПВХ", "ПВХ"),
    "Polipropilene": ("полипропилен", "полипропилена"), "Polipropilen": ("полипропилен", "полипропилена"),
    "Fibra T-400": ("волокно T-400", "волокна T-400"), "Metallo": ("металл", "металла"), "Metal": ("металл", "металла"),
    "Fibra metallica": ("металлизированная нить", "металлизированной нити"),
    "Metalik İplik": ("металлизированная нить", "металлизированной нити"),
    "Pelle": ("натуральная кожа", "натуральной кожи"), "Deri": ("натуральная кожа", "натуральной кожи"),
    "Leather": ("натуральная кожа", "натуральной кожи"), "Hakiki Deri": ("натуральная кожа", "натуральной кожи"),
    "Gerçek Deri": ("натуральная кожа", "натуральной кожи"),
    "Pelle di vitello": ("телячья кожа", "телячьей кожи"), "Dana Derisi": ("телячья кожа", "телячьей кожи"),
    "Pelle di bovino": ("натуральная кожа", "натуральной кожи"), "Sığır Derisi": ("натуральная кожа", "натуральной кожи"),
    "Pelle di cervo": ("оленья кожа", "оленьей кожи"), "Pelle di capra": ("козья кожа", "козьей кожи"),
    "Keçi Derisi": ("козья кожа", "козьей кожи"), "Pelle di agnello": ("кожа ягнёнка", "кожи ягнёнка"),
    "Kuzu Derisi": ("кожа ягнёнка", "кожи ягнёнка"), "Pelle di cavallo": ("конская кожа", "конской кожи"),
    "Pelle rigenerata": ("прессованная кожа", "прессованной кожи"),
    "Pelo di vitello": ("телячья кожа с ворсом", "телячьей кожи с ворсом"),
    "Camoscio": ("замша", "замши"), "Süet": ("замша", "замши"), "Suede": ("замша", "замши"),
    "Süet Deri": ("замша", "замши"), "Nabuk": ("нубук", "нубука"), "Nubuck": ("нубук", "нубука"),
    "Vernice": ("лакированная кожа", "лакированной кожи"), "Rugan": ("лакированная кожа", "лакированной кожи"),
    "Suni Deri": ("искусственная кожа", "искусственной кожи"), "Ecopelle": ("искусственная кожа", "искусственной кожи"),
    "Pelle sintetica": ("искусственная кожа", "искусственной кожи"), "Similpelle": ("искусственная кожа", "искусственной кожи"),
    "Gomma": ("резина", "резины"), "Kauçuk": ("резина", "резины"), "Rubber": ("резина", "резины"),
    "Plastica": ("пластик", "пластика"), "Plastik": ("пластик", "пластика"),
    "Fibre tessili": ("текстиль", "текстиля"), "Tekstil": ("текстиль", "текстиля"), "Textile": ("текстиль", "текстиля"),
    "Materiale sintetico": ("синтетический материал", "синтетического материала"),
    "Fibre sintetiche": ("синтетический материал", "синтетического материала"),
    "Sentetik": ("синтетический материал", "синтетического материала"),
    "Juta": ("джут", "джута"), "Jüt": ("джут", "джута"), "Rafia": ("рафия", "рафии"),
    "Argento": ("серебро", "серебра"), "Gümüş": ("серебро", "серебра"), "Acciaio": ("сталь", "стали"),
    "Çelik": ("сталь", "стали"), "Ottone": ("латунь", "латуни"), "Legno": ("дерево", "дерева"),
    "Hakiki": ("натуральная кожа", "натуральной кожи"),          # «%100 Hakiki Deri» в названии
    "Altre Fibre": ("другие волокна", "других волокон"), "Poliacrilico": ("акрил", "акрила"),
    "Polietilene": ("полиэтилен", "полиэтилена"), "Zama": ("цинковый сплав", "цинкового сплава"),
    "Lana mohair": ("мохер", "мохера"), "Lana di yak": ("шерсть яка", "шерсти яка"),
    "Fibre elastiche": ("эластичное волокно", "эластичного волокна"),
    "Acetato riciclato": ("переработанный ацетат", "переработанного ацетата"),
    "Cotone Pima": ("хлопок пима", "хлопка пима"), "PES": ("полиэстер", "полиэстера"),
    "PES riciclato": ("переработанный полиэстер", "переработанного полиэстера"),
    "Eco Poliestere": ("переработанный полиэстер", "переработанного полиэстера"),
    "Karışık Lifler": ("смешанные волокна", "смешанных волокон"),
    "Lana di cammello": ("верблюжья шерсть", "верблюжьей шерсти"), "Montone": ("овчина", "овчины"),
    "Pelle ovina": ("овечья кожа", "овечьей кожи"), "Diacetato di cellulosa": ("ацетат целлюлозы", "ацетата целлюлозы"),
    "Bio-acetato": ("биоацетат", "биоацетата"), "Pelle riciclata": ("переработанная кожа", "переработанной кожи"),
    "Rigenerato di fibre di cuoio": ("прессованная кожа", "прессованной кожи"),
    "Lana di baby alpaca": ("шерсть альпаки", "шерсти альпаки"), "Lana Merinos": ("шерсть мериноса", "шерсти мериноса"),
    "Escorial": ("шерсть эскориал", "шерсти эскориал"), "EVA": ("ЭВА", "ЭВА"),
    "Cotone riciclato pre-consumer": ("переработанный хлопок", "переработанного хлопка"),
    "Cotone riciclato post-consumer": ("переработанный хлопок", "переработанного хлопка"),
    "Lycra T400": ("эластан T400", "эластана T400"), "Elastam": ("эластан", "эластана"),
    "Elastan riciclato": ("переработанный эластан", "переработанного эластана"),
    "Elastomultiester": ("эластомультиэстер", "эластомультиэстера"), "Elastolefina": ("эластолефин", "эластолефина"),
    "Lyocell Tencel": ("лиоцелл", "лиоцелла"), "Metal Lif": ("металлизированная нить", "металлизированной нити"),
    "Fibra di metallo": ("металлизированная нить", "металлизированной нити"),
    "Poliestere metallizzato": ("металлизированный полиэстер", "металлизированного полиэстера"),
    "Poliuretano termoplastico": ("термопластичный полиуретан", "термопластичного полиуретана"),
    "Spalmato poliuretano": ("полиуретановое покрытие", "полиуретанового покрытия"),
    "Madreperla": ("перламутр", "перламутра"), "Vetro": ("стекло", "стекла"), "Cam": ("стекло", "стекла"),
    "Abaca": ("абака", "абаки"), "Palladio": ("палладий", "палладия"),
    "PES - Polietersolfoni": ("полиэфирсульфон", "полиэфирсульфона"), "Polibutilene": ("полибутилен", "полибутилена"),
    "Policloruro": ("ПВХ", "ПВХ"), "Hakiki Dana Deri": ("телячья кожа", "телячьей кожи"),
    "Lateks": ("латекс", "латекса"), "Elastan Spandeks": ("эластан", "эластана"),
    "Deri ve Tekstil": ("натуральная кожа и текстиль", "натуральной кожи и текстиля"), "Lattice": ("латекс", "латекса"),
})
# Не волокна — «состав» с такими значениями просто не показываем (и не считаем непереведённым).
NOT_FIBRES = {fold(x) for x in ("Belirtilmemiş", "Triko", "Kumaş", "Örgü", "Dokuma", "Diğer", "Karışık")}
_SUPER_RE = re.compile(r"super (\d+) ?'?s (lana|wool|yun)")


def _fibre(name: str) -> tuple[str, str] | None:
    k = fold(name)
    if k in FIBRES:
        return FIBRES[k]
    m = _SUPER_RE.fullmatch(k)
    if m:
        return f"шерсть Super {m.group(1)}s", f"шерсти Super {m.group(1)}s"
    m = re.fullmatch(r"(.+?) karisimli", k)               # «Keten Karışımlı» — смесовая ткань
    if m and m.group(1) in FIBRES:
        nom, gen = FIBRES[m.group(1)]
        return f"{nom} (смесовая ткань)", f"смесовой ткани с добавлением {gen}"
    return None


def _pct(s: str) -> str:
    s = s.replace(",", ".")
    return s[:-2] if s.endswith(".0") else s.replace(".", ",")


def parse_composition(raw) -> list[tuple[str | None, str, str]] | None:
    """«95% Cotone, 5% Elastan» / «%95 Pamuk %5 Elastan - Spandeks» / «Pamuk - Elastan»
    -> [("95", "хлопок", "хлопка"), ("5", "эластан", "эластана")]. Неизвестное волокно -> None."""
    if not raw or not isinstance(raw, str) or fold(raw) in NOT_FIBRES:
        return None
    s = re.sub(r"\([^)]*\)", " ", raw)                                    # «%100 Deri (Leather)»
    s = re.sub(r"%\s*(\d+(?:[.,]\d+)?)", r"\1%", s)                        # «%100» -> «100%»
    s = re.sub(r"\s+(?=\d+(?:[.,]\d+)?\s*%)", ", ", s.strip())             # новая доля — новый кусок
    out, unknown = [], []
    for part in re.split(r"[,;/+]", s):
        part = part.strip(" -.:")
        if not part:
            continue
        m = re.match(r"(\d+(?:[.,]\d+)?)\s*%\s*(.+)", part)
        pct, name = (m.group(1), m.group(2).strip(" -.")) if m else (None, part)
        f = _fibre(name)
        if f:
            out.append((pct, *f))
            continue
        # «Pamuk - Elastan», «Pamuk Keten», «Poliviskon Elestan» — смесь без долей
        pieces = [x for x in re.split(r"\s+-\s+|\s+ve\s+|\s+e\s+|\s+", name) if x] if pct is None else []
        if len(pieces) > 1 and all(_fibre(x) for x in pieces):
            out += [(None, *_fibre(x)) for x in pieces]
            continue
        unknown.append(name)
    if unknown:
        for u in unknown:
            _miss("состав (волокно)", u)
        return None
    # «100% натуральная кожа» -> «натуральная кожа»: у кожи и замши доля не нужна
    if len(out) == 1 and out[0][0] == "100" and re.search(r"кож|замш|нубук", out[0][1]):
        out = [(None, out[0][1], out[0][2])]
    return out or None


def composition_text(parts) -> str | None:
    if not parts:
        return None
    return ", ".join((f"{_pct(p)}% " if p else "") + nom for p, nom, _ in parts)


# ---------------------------------------------------------------- вид товара (заголовок)
# Для каждого нормализованного типа (base.TYPE_ORDER) — правила (regex по сложенному тексту
# категории/названия, существительное). Первое совпадение побеждает, иначе — слово по умолчанию.

KIND_RULES = {
    "обувь": [
        (r"sneaker|spor ayakkab|running|kosu ayakkab|trainer", "Кроссовки"),
        (r"loafer", "Лоферы"),
        (r"mocassin|mokasen|makosen|moccasin", "Мокасины"),
        (r"oxford", "Оксфорды"), (r"\bderby", "Дерби"),
        (r"stringat|francesin|lace-?up", "Туфли на шнурках"),
        (r"stivalett|polacchin|\bbot\b|\bbotlar|chelsea|ankle boot|postal|desert boot", "Ботинки"),
        (r"stival|cizme|\bboots?\b", "Сапоги"),
        (r"espadril", "Эспадрильи"),
        (r"infradito|parmak arasi|flip-?flop", "Вьетнамки"),
        (r"pantofol|panduf|ev terlig", "Домашние тапочки"),
        (r"ciabatt|terlik|slides?\b", "Шлёпанцы"),
        (r"sandal", "Сандалии"),
        (r"\bmules?\b|zoccol|\bsabo", "Мюли"),
        (r"slip-?on", "Слипоны"),
        (r"klasik ayakkab|scarpe eleganti", "Классические туфли"),
        (r"decollet|stiletto|topuklu ayakkab", "Туфли на каблуке"),
        (r"ballerin|babet", "Балетки"),
        (r"ayakkab", "Туфли"),
    ],
    "сумки": [
        (r"zain|sirt canta|backpack|rucksack", "Рюкзак"),
        (r"marsupi|bel canta|bum bag|waist bag", "Поясная сумка"),
        (r"tracolla|postaci|capraz|messenger|omuz", "Сумка через плечо"),
        (r"beauty case|makyaj|necessaire|kozmetik|tuvalet canta", "Косметичка"),
        (r"portadocument|evrak|portfoy|laptop|briefcase", "Портфель"),
        (r"borson|valig|trolley|bavul|valiz|seyahat|duffle|weekender", "Дорожная сумка"),
        (r"shopper|\btote", "Сумка-шопер"),
        (r"clutch|pochette|el cantasi", "Клатч"),
    ],
    "нижнее бельё": [
        (r"mare|deniz sort|swim|mayo", "Плавательные шорты"),
        (r"pigiam|pijama", "Пижама"),
        (r"calz|corap|\bsocks?\b", "Носки"),
        (r"boxer", "Трусы-боксеры"),
        (r"\bslip|kulot|brief", "Трусы"),
        (r"canott|atlet|fanila|\btank", "Майка"),
        (r"t-?shirt|tisort|topwear|top & t", "Нательная футболка"),
        (r"accappatoi|bornoz|sabahlik|vestaglia", "Халат"),
        (r"termal|thermal", "Термобельё"),
    ],
    "джинсы": [(r"short|sort\b", "Джинсовые шорты")],
    "толстовки": [
        (r"cappuccio|kapusonlu|hoodie|hooded", "Худи"),
        (r"\bzip|fermuarli|cerniera", "Толстовка на молнии"),
        (r"sweatshirt", "Свитшот"),
    ],
    "футболки и поло": [
        (r"\bpolo\b", "Поло"),
        (r"canott|atlet|\btank|sleeveless|kolsuz", "Майка"),
        (r"\bcrop", "Кроп-топ"),
        (r"\btop\b|bustier|camisole|^body$", "Топ"),
        (r"long ?sleeve|uzun kollu", "Лонгслив"),
    ],
    "пиджаки и костюмы": [
        (r"smok", "Смокинг"),
        (r"gilet|panciott|yelek", "Жилет"),
        (r"complet|takim elbise|\babiti?\b|\bsuits?\b|tailleur", "Костюм"),
    ],
    "платья": [(r"tulum|jumpsuit|\btuta\b", "Комбинезон")],
    "юбки": [],
    "шорты": [
        (r"mare|deniz|swim|mayo", "Плавательные шорты"),
        (r"(shorts?|sort) ?(e|&|/) ?bermuda", "Шорты"),
        (r"bermuda", "Бермуды"),
        (r"jean|denim", "Джинсовые шорты"),
    ],
    "рубашки": [
        (r"\bblus|bluz|blouse", "Блузка"),
        (r"tunik|tunic", "Туника"),
        (r"overshirt|gomlek ceket|shacket", "Рубашка-куртка"),
        (r"jean|denim", "Джинсовая рубашка"),
    ],
    "свитеры и кардиганы": [
        (r"polo yaka.*(t-?shirt|tisort)|triko polo", "Трикотажное поло"),     # «Polo Yaka Triko Tişört»
        (r"t-?shirt|tisort", "Трикотажная футболка"),
        (r"cardigan|hirka", "Кардиган"),
        (r"dolcevita|lupetto|balikci|turtleneck|collo alto", "Водолазка"),
        (r"gilet|smanicat|yelek|kolsuz|\bvest\b", "Жилет"),
        (r"\bzip|fermuar", "Джемпер на молнии"),
        (r"pullover", "Джемпер"),
    ],
    "куртки и пальто": [
        (r"piumin|imbottit|sisme|puffer|\bdown\b", "Пуховик"),
        (r"trench|trenckot", "Тренч"),
        (r"parka", "Парка"),
        (r"bomber", "Бомбер"),
        (r"shearling|teddy|montone", "Дублёнка"),
        (r"smanicat|gilet|yelek|\bvest\b", "Жилет"),
        (r"cappott|palto|kaban|\bcoat\b|montgomery|caban", "Пальто"),
        (r"soprabit|pardesu|impermeab|yagmurluk|raincoat", "Плащ"),
        (r"jean|denim|kot ceket", "Джинсовая куртка"),
        (r"\bderi\b|leather", "Кожаная куртка"),
    ],
    "брюки": [
        (r"chino", "Чиносы"),
        (r"cargo|kargo", "Брюки карго"),
        (r"jogger|pantaloni felpa|esofman|sweatpant|track", "Спортивные брюки"),
        (r"legging|tayt", "Леггинсы"),
    ],
    None: [(r"teli mare|havlu|towel", "Пляжное полотенце")],     # тип не определился
    "аксессуары": [
        (r"cintur|kemer|\bbelts?\b", "Ремень"),
        (r"cravatt|kravat|\bties?\b", "Галстук"),
        (r"papillon|papyon|bow tie", "Галстук-бабочка"),
        (r"fazzolett|mendil|pocket square", "Нагрудный платок"),
        (r"sciarp|\batki|scarf|foulard|fular|\bsal\b|esarp|boyunluk", "Шарф"),
        (r"guant|eldiven|glove", "Перчатки"),
        (r"portacart|kartlik|card ?holder", "Картхолдер"),
        (r"portamonet|bozuk para", "Монетница"),
        (r"portafogl|cuzdan|wallet", "Кошелёк"),
        (r"portachiav|anahtarlik|key", "Брелок для ключей"),
        (r"montatur|\bframes?\b|optik", "Оправа для очков"),
        (r"occhial|gozluk|sunglass", "Солнцезащитные очки"),
        (r"berrett|\bbere\b|beanie", "Шапка"),
        (r"cappell|sapka|\bhats?\b|\bcaps?\b|kasket", "Головной убор"),
        (r"kol dugme|gemell|cufflink", "Запонки"),
        (r"calz|corap|\bsocks?\b", "Носки"),
        (r"orolog|\bsaat|watch", "Часы"),
        (r"braccial|bileklik", "Браслет"),
        (r"collan|kolye", "Колье"),
        (r"anell|yuzuk", "Кольцо"),
        (r"orecchin|kupe", "Серьги"),
        (r"ombrell|semsiye", "Зонт"),
        (r"teli mare|havlu|towel", "Пляжное полотенце"),
    ],
}
KIND_DEFAULT = {
    "обувь": "Обувь", "сумки": "Сумка", "нижнее бельё": "Бельё", "джинсы": "Джинсы", "толстовки": "Толстовка",
    "футболки и поло": "Футболка", "пиджаки и костюмы": "Пиджак", "платья": "Платье", "юбки": "Юбка",
    "шорты": "Шорты", "рубашки": "Рубашка", "свитеры и кардиганы": "Свитер", "куртки и пальто": "Куртка",
    "брюки": "Брюки", "аксессуары": "Аксессуар",
}
_KIND_RE = {t: [(re.compile(p), n) for p, n in rules] for t, rules in KIND_RULES.items()}
# род существительного для «Женская/Женский/Женское/Женские …», если не угадывается по окончанию
NOUN_GENDER = {"Худи": "n", "Поло": "n", "Бельё": "n", "Термобельё": "n"}
WOMEN = {"m": "Женский", "f": "Женская", "n": "Женское", "pl": "Женские"}


def _noun_gender(noun: str) -> str:
    if noun in NOUN_GENDER:
        return NOUN_GENDER[noun]
    w = noun.split()[0].split("-")[0].lower()
    for ends, g in ((("ые", "ие"), "pl"), (("ая", "яя"), "f"), (("ый", "ий", "ой"), "m"), (("ое", "ее"), "n"),
                    (("ы", "и"), "pl"), (("а", "я"), "f"), (("о", "е", "ё"), "n")):
        if w.endswith(ends):
            return g
    return "m"


def _yoox_leaf_cats(attrs: dict) -> list[str]:
    """dynamicAttributes["Categorie-ctgr"] -> листовые категории без кодов: ["Camicie regular fit"]."""
    out = []
    for k, vals in (attrs.get("dynamicAttributes") or {}).items():
        if fold(_strip_code(k)) != "categorie":
            continue
        for v in vals if isinstance(vals, list) else [vals]:
            if ">" in str(v):
                out.append(_strip_code(str(v).split(">")[-1].strip()))
    return out


def _kind_text(p: dict) -> str:
    a = p.get("attrs") or {}
    src = p.get("source")
    if src == "yoox":
        bits = [a.get("micro")] + _yoox_leaf_cats(a)
    elif src in ("pcardin_tr", "cacharel_tr"):
        bits = [a.get("filterable_product_type"), a.get("filterable_product_base_type"), p.get("title")]
    else:
        bits = [p.get("category"), a.get("category_path"), p.get("title")]
    if src != "yoox":       # у YOOX категория = macro > micro, а macro («Completi e coordinati») сбивает
        bits.append(p.get("category"))
    return fold(" | ".join(str(b) for b in bits if b))


def title_ru(p: dict) -> str:
    brand = (p.get("brand") or "").strip()
    ptype = p.get("type")
    text = _kind_text(p)
    noun = None
    for rx, n in _KIND_RE.get(ptype, []):
        if rx.search(text):
            noun = n
            break
    if not noun:
        noun = KIND_DEFAULT.get(ptype)
        if ptype == "обувь":
            _miss("вид обуви (слово «Обувь»)", p.get("category"))
    if not noun:
        return brand
    if p.get("gender") == "women" and ptype not in ("платья", "юбки") and noun not in ("Блузка", "Туника"):
        noun = WOMEN[_noun_gender(noun)] + " " + noun[0].lower() + noun[1:]
    return f"{noun} {brand}".strip()


# ---------------------------------------------------------------- свойства -> пункты «details»
# Ключ свойства (как на сайте) -> тема; тема + значение -> русский пункт.

TOPIC_KEYS = _fk({
    # Trendyol (JSON-LD страницы товара)
    "Kalıp": "fit", "Yaka Tipi": "collar", "Yaka": "collar", "Kol Tipi": "sleeve", "Kol Boyu": "sleeve",
    "Desen": "pattern", "Kumaş Tipi": "fabric", "Dokuma Tipi": "fabric", "Kapama Şekli": "closure",
    "Kapama Tipi": "closure", "Cep": "pocket", "Cep Tipi": "pocket", "Ürün Detayı": "detail", "Boy": "length",
    "Boy / Uzunluk": "length", "Bel": "rise", "Bel Tipi": "rise", "Bel Yüksekliği": "rise", "Paça Tipi": "leg",
    "Paça": "leg", "Sezon": "season", "Topuk Tipi": "heel_type", "Topuk Boyu": "heel", "Burun Tipi": "toe",
    "Burun": "toe", "Taban": "sole", "Taban Malzemesi": "sole", "Taban Tipi": "sole", "İç Astar": "lining",
    "Astar": "lining", "Astar Durumu": "lining", "Ortam": "style", "Siluet": "silhouette", "Kapüşon": "hood",
    "Bağlama Şekli": "closure", "Alt Taban Materyali": "sole", "İç Astar & İç Taban Materyali": "lining",
    "Astar Materyali": "lining",
    # Pierre Cardin / Cacharel (Akinon)
    "filterable_fit": "fit", "filterable_neck_type": "collar", "filterable_arm_lenght": "sleeve",
    "filterable_figure": "pattern", "filterable_lining": "lining", "filterable_pocket": "pocket",
    "filterable_season": "season", "integration_season": "season", "filterable_product_lenght": "length",
    # YOOX (ключи dynamicAttributes без кода «-nck» и т.п.; seasonality)
    "Collo": "collar", "Colletto": "collar", "Scollo": "collar", "Maniche": "sleeve", "Chiusura": "closure",
    "Fantasia": "pattern", "Stampa": "pattern", "Vestibilità": "fit", "Lunghezza": "length", "Vita": "rise",
    "Punta": "toe", "Tacco": "heel", "Altezza tacco": "heel", "Suola": "sole", "Tasche": "pocket",
    "Cappuccio": "hood", "Fodera": "lining", "Fondo": "leg", "Gamba": "leg", "seasonality": "season",
})
# Свойства, которые сознательно не показываем (цвет и состав выводятся отдельно, остальное — служебное).
SKIP_KEYS = {fold(k) for k in (
    "Renk", "ld_color", "Materyal", "Materyal Bileşeni", "Menşei", "mensei", "Yıkama Talimatı", "Paket İçeriği",
    "Persona", "Sürdürülebilirlik Detayı", "Ek Özellik", "Koleksiyon", "category_path", "Cinsiyet", "Yaş Grubu",
    "Garanti Süresi", "Ürün Tipi", "Model", "Stil", "Kumaş Ağırlığı", "Gramaj", "Kalınlık", "Ürün Ölçüsü",
    "Dış Materyal", "Saya Materyali", "Kutu Durumu", "Kemer/Kuşak Durumu", "Baskı/Nakış Tekniği", "Parça Sayısı",
    "Çivi Tipi", "Çeşit", "Bilek Stili", "Kumaş Teknolojisi",
    "filterable_color", "filterable_detail_color", "filterable_sub_color", "filterable_product_type",
    "filterable_product_base_type", "filterable_fabric_blends", "integration_fabric_fiber",
    "integration_fabric_blends", "deri_bilgi", "elyaf_bilgi", "product_name_new",
    "composition", "colorLabel", "color", "mainMaterial", "macro", "micro", "modelGender", "modelName",
    "sizeCodes", "dynamicAttributes", "Categorie", "Lavaggio",
)}
# Значения, которые ничего не говорят покупателю — пропускаем в любой теме.
IGNORE_VALUES = {fold(v) for v in (
    "", "Yok", "Standart", "Basic", "Belirtilmemiş", "Diğer", "Other", "None", "Ek Özellik Mevcut Değil",
    "Hayır", "Evet", "Klasik", "Blazer", "Essentials", "Bel", "Normal", "Regular", "Dokuma", "Tekli", "BASIC",
    "Bulunmamaktadır", "Smart/Office", "Spor", "ILKB", "Overshirt", "Smokin-Damatlık", "Evrak Çantası",
    "Bot", "Orijinal Boy", "Standart Kol", "Düz Dokuma", "Comfort", "Modern", "Günlük", "Detaysız", "Suni Deri",
    "Hakiki Deri", "Fake Triko", "PVC",
)}

TOPIC_VALUES = {t: _fk(d) for t, d in {
    "fit": {
        "Slim Fit": "Приталенный крой (slim fit)", "Slim": "Приталенный крой (slim fit)",
        "Extra Slim": "Узкий крой (extra slim)", "Extra Slim Fit": "Узкий крой (extra slim)",
        "Super Slim": "Узкий крой (extra slim)", "Dar Kalıp": "Узкий крой (extra slim)",
        "Skinny": "Облегающий крой (skinny)", "Skinny Fit": "Облегающий крой (skinny)",
        "Super Skinny": "Облегающий крой (skinny)",
        "Regular Fit": "Прямой крой (regular fit)", "Normal Kalıp": "Прямой крой (regular fit)",
        "Standart Kalıp": "Прямой крой (regular fit)", "Classic Fit": "Прямой крой (regular fit)",
        "Comfort": "Свободный крой", "Comfort Fit": "Свободный крой", "Rahat Kalıp": "Свободный крой",
        "Relaxed": "Свободный крой", "Relaxed Fit": "Свободный крой", "Loose Fit": "Свободный крой",
        "Bol Kalıp": "Свободный крой", "Geniş Kalıp": "Свободный крой",
        "Oversize": "Оверсайз", "Oversized": "Оверсайз", "Oversize Fit": "Оверсайз", "Salaş": "Оверсайз",
        "Straight": "Прямой крой", "Straight Fit": "Прямой крой", "Düz Kesim": "Прямой крой",
        "Carrot": "Крой «морковка» (carrot fit)", "Carrot Fit": "Крой «морковка» (carrot fit)",
        "Tapered": "Зауженный книзу крой", "Tapered Fit": "Зауженный книзу крой",
        "Modern Fit": "Полуприталенный крой (modern fit)", "Semi Slim": "Полуприталенный крой (modern fit)",
        "Boxy": "Свободный прямой крой (boxy)", "Boxy Fit": "Свободный прямой крой (boxy)",
        "Mom Fit": "Свободный крой (mom fit)", "Dad Fit": "Свободный крой (dad fit)", "Relax": "Свободный крой",
        "Flare Fit": "Расклешённый крой", "A-Form Fit": "А-силуэт",
        "Wide Leg": "Широкие штанины", "Bootcut": "Крой bootcut (расклешённые от колена)",
    },
    "collar": {
        "Polo Yaka": "Воротник-поло", "Collo a polo": "Воротник-поло", "Polo": "Воротник-поло",
        "Bisiklet Yaka": "Круглый вырез", "Girocollo": "Круглый вырез", "Crew Neck": "Круглый вырез",
        "Yuvarlak Yaka": "Круглый вырез",
        "V Yaka": "V-образный вырез", "Scollo a V": "V-образный вырез", "Collo a V": "V-образный вырез",
        "V Neck": "V-образный вырез",
        "Balıkçı Yaka": "Высокое горло", "Balıkçı": "Высокое горло", "Dolcevita": "Высокое горло",
        "Collo alto": "Высокое горло", "Turtleneck": "Высокое горло", "Boğazlı": "Высокое горло",
        "Yarım Balıkçı": "Невысокое горло", "Lupetto": "Невысокое горло", "Mock Neck": "Невысокое горло",
        "Dik Yaka": "Воротник-стойка", "Hakim Yaka": "Воротник-стойка", "Collo alla coreana": "Воротник-стойка",
        "Coreana": "Воротник-стойка", "Mandarin": "Воротник-стойка",
        "Kapüşonlu": "С капюшоном", "Kapüşonlu Yaka": "С капюшоном", "Cappuccio": "С капюшоном",
        "Gömlek Yaka": "Рубашечный воротник", "Klasik Yaka": "Классический воротник",
        "Collo classico": "Классический воротник", "Collo francese": "Классический воротник",
        "İtalyan Yaka": "Итальянский воротник", "Yarım İtalyan Yaka": "Итальянский воротник",
        "Collo all'italiana": "Итальянский воротник",
        "Düğmeli Yaka": "Воротник на пуговицах (button-down)", "Button Down": "Воротник на пуговицах (button-down)",
        "Button-down": "Воротник на пуговицах (button-down)",
        "Apaş Yaka": "Воротник апаш", "Fermuarlı Yaka": "Воротник на молнии", "Collo con zip": "Воротник на молнии",
        "Alttan Britli Yaka": "Воротник на пуговицах (button-down)",
        "Alttan Biritli Yaka": "Воротник на пуговицах (button-down)", "Kırlangıç Yaka": "Классический воротник",
        "Fermuarlı Polo Yaka": "Воротник-поло на молнии",
        "Şal Yaka": "Воротник-шалька", "Collo sciallato": "Воротник-шалька",
        "Ceket Yaka": "Воротник с лацканами", "Çentik Yaka": "Воротник с лацканами", "Yarım Balıkçı Yaka": "Невысокое горло",
        "Kare Yaka": "Квадратный вырез", "Kayık Yaka": "Вырез-лодочка", "Collo a barca": "Вырез-лодочка",
        "U Yaka": "Глубокий круглый вырез", "Bebe Yaka": "Отложной воротник",
    },
    "sleeve": {
        "Kısa Kol": "Короткий рукав", "Kısa": "Короткий рукав", "Kısa Kollu": "Короткий рукав",
        "Yarım Kol": "Короткий рукав", "Maniche corte": "Короткий рукав", "Short Sleeve": "Короткий рукав",
        "Uzun Kol": "Длинный рукав", "Uzun": "Длинный рукав", "Uzun Kollu": "Длинный рукав",
        "Maniche lunghe": "Длинный рукав", "Long Sleeve": "Длинный рукав",
        "Kolsuz": "Без рукавов", "Senza maniche": "Без рукавов", "Sleeveless": "Без рукавов",
        "Truvakar Kol": "Рукав 3/4", "Truvakar": "Рукав 3/4", "3/4 Kol": "Рукав 3/4", "Maniche a 3/4": "Рукав 3/4",
        "Reglan Kol": "Рукав реглан", "Raglan": "Рукав реглан", "Maniche raglan": "Рукав реглан",
        "Düşük Omuz": "Спущенное плечо", "Düşük Kol": "Спущенное плечо", "Balon Kol": "Объёмный рукав",
    },
    "pattern": {
        "Düz": "Однотонная расцветка", "Düz Renk": "Однотонная расцветка", "Tinta unita": "Однотонная расцветка",
        "Plain": "Однотонная расцветка", "Solid": "Однотонная расцветка",
        "Çizgili": "В полоску", "A righe": "В полоску", "Righe": "В полоску", "Rigato": "В полоску",
        "Striped": "В полоску",
        "Kareli": "В клетку", "Ekose": "В клетку", "A quadri": "В клетку", "Quadri": "В клетку",
        "Checked": "В клетку", "Plaid": "В клетку", "Tartan": "В клетку",
        "Desenli": "С узором", "Fantasia": "С узором", "Patterned": "С узором",
        "Baskılı": "С принтом", "Stampa": "С принтом", "Stampato": "С принтом", "Printed": "С принтом",
        "Logolu": "С логотипом", "Logo": "С логотипом",
        "Puantiyeli": "В горошек", "Pois": "В горошек", "A pois": "В горошек",
        "Kamuflaj": "Камуфляжная расцветка", "Mimetico": "Камуфляжная расцветка",
        "Çiçekli": "Цветочный принт", "Floreale": "Цветочный принт", "Fiorato": "Цветочный принт",
        "Geometrik": "Геометрический узор", "Geometrico": "Геометрический узор",
        "Mikro Desen": "Мелкий узор", "Micro fantasia": "Мелкий узор",
        "Jakarlı": "Жаккардовый узор", "Jacquard": "Жаккардовый узор",
        "Yazılı": "С надписью", "Lettering": "С надписью",
        "Kazayağı": "Узор «гусиная лапка»", "Pied de poule": "Узор «гусиная лапка»",
        "Balıksırtı": "Узор «ёлочка»", "Balık Sırtı": "Узор «ёлочка»", "Spina di pesce": "Узор «ёлочка»",
        "Herringbone": "Узор «ёлочка»", "Renk Bloklu": "Колор-блок", "Color Block": "Колор-блок",
        "Batik": "Тай-дай", "Tie Dye": "Тай-дай", "Paisley": "Узор пейсли", "Şal Desen": "Узор пейсли",
        "Nakışlı": "С вышивкой", "Ricamo": "С вышивкой", "Etnik": "Этнический узор",
        "Tropikal": "Тропический принт", "Fitilli": "В рубчик", "A coste": "В рубчик",
        "Baklava Desen": "Узор ромбами", "Argyle": "Узор ромбами", "Ekose/Kareli": "В клетку", "Ekose / Kareli": "В клетку",
        "Renkli": "Многоцветный узор",
        "Oduncu": "В клетку", "Armürlü": "Фактурное плетение (добби)", "Gofreli": "Вафельная фактура",
        "Oxford": "Ткань оксфорд", "Flanel": "Фланель", "Kadife": "Вельвет",
    },
    "fabric": {
        "Örme": "Трикотаж", "Triko": "Трикотаж", "Denim": "Деним", "Pike": "Ткань пике",
        "Penye": "Мягкий хлопковый трикотаж (пенье)", "Oxford": "Ткань оксфорд", "Poplin": "Поплин",
        "Saten": "Сатин", "Üç İplik": "Футер-трёхнитка", "İki İplik": "Футер-двухнитка", "Şardonlu": "С начёсом",
        "Gabardin": "Габардин", "Twill": "Твил", "Süprem": "Трикотаж супрем", "Ribana": "Трикотаж в рубчик",
        "Interlok": "Трикотаж интерлок", "Kanvas": "Канвас", "Şambre": "Шамбре", "Chambray": "Шамбре",
        "Flanel": "Фланель", "Fitilli Kadife": "Вельвет", "Kadife": "Вельвет", "Örgü": "Трикотаж",
        "Keten Görünümlü": "Ткань с фактурой льна", "Polar": "Флис", "3 İplik Şardonlu": "Футер-трёхнитка с начёсом",
        "3 İplik": "Футер-трёхнитка", "2 İplik": "Футер-двухнитка", "Dokulu": "Фактурная ткань",
    },
    "closure": {
        "Düğmeli": "Застёжка на пуговицы", "Düğme": "Застёжка на пуговицы", "Bottoni": "Застёжка на пуговицы",
        "Yarım Pat Düğme": "Планка на пуговицах", "Yarım Pat": "Планка на пуговицах", "Pat Düğme": "Планка на пуговицах",
        "Fermuarlı": "Застёжка на молнию", "Fermuar": "Застёжка на молнию", "Zip": "Застёжка на молнию",
        "Cerniera": "Застёжка на молнию", "Tam Fermuar": "Застёжка на молнию",
        "Yarım Fermuar": "Короткая молния у горловины", "Mezza zip": "Короткая молния у горловины",
        "Bağcıklı": "На шнурках", "Lacci": "На шнурках", "Bağcıksız": "Без шнурков", "Senza lacci": "Без шнурков",
        "Cırt Cırtlı": "На липучке", "Cırtlı": "На липучке", "Velcro": "На липучке", "Strappo": "На липучке",
        "Çıtçıtlı": "На кнопках", "Automatici": "На кнопках", "Lastikli": "На резинке", "Elastico": "На резинке",
        "İpli": "На кулиске", "Coulisse": "На кулиске", "Kapamasız": "Без застёжки", "Senza chiusura": "Без застёжки",
        "Tokalı": "Застёжка на пряжку", "Fibbia": "Застёжка на пряжку",
        "Düğme ve Fermuar": "Молния и пуговицы", "Fermuar ve Düğme": "Молния и пуговицы",
        "Kruvaze": "Двубортная модель",
    },
    "pocket": {
        "Cepsiz": "Без карманов", "Senza tasche": "Без карманов", "Cepli": "С карманами", "Con tasche": "С карманами",
        "Tek Cep": "Нагрудный карман", "Göğüs Cepli": "Нагрудный карман", "Göğüs Cep": "Нагрудный карман",
        "Taschino": "Нагрудный карман", "Çift Cep": "Два кармана", "Çift Cepli": "Два кармана",
        "4 Cep": "Четыре кармана",
        "Kanguru Cep": "Карман-кенгуру", "Kanguru": "Карман-кенгуру",
        "Yan Cep": "Боковые карманы", "Tasche laterali": "Боковые карманы",
        "Kapaklı": "Карманы с клапанами", "Kapaklı Cep": "Карманы с клапанами",
        "5 Cep": "Пять карманов", "Beş Cep": "Пять карманов", "5 Tasche": "Пять карманов",
        "Torba Cep": "Накладные карманы", "Kargo Cep": "Карманы карго", "Fileto Cep": "Прорезные карманы", "Gizli Cep": "Потайной карман",
    },
    "length": {
        # «Boy: Uzun» у Trendyol стоит и у обычных футболок/брюк — ничего не значит (None = молча пропустить)
        "Uzun": None, "Kısa": "Укороченная модель", "Crop": "Укороченная модель",
        "Cropped": "Укороченная модель", "Corto": "Укороченная модель", "Midi": "Длина миди",
        "Mini": "Длина мини", "Maxi": "Длина макси", "Maksi": "Длина макси",
    },
    "rise": {
        "Normal Bel": "Средняя посадка", "Orta Bel": "Средняя посадка", "Vita normale": "Средняя посадка",
        "Vita media": "Средняя посадка", "Regular Rise": "Средняя посадка",
        "Yüksek Bel": "Высокая посадка", "Vita alta": "Высокая посадка", "High Rise": "Высокая посадка",
        "Düşük Bel": "Низкая посадка", "Vita bassa": "Низкая посадка", "Low Rise": "Низкая посадка",
    },
    "leg": {
        "Dar Paça": "Зауженные книзу штанины", "Dar": "Зауженные книзу штанины",
        "Bol Paça": "Широкие штанины", "Geniş Paça": "Широкие штанины", "Gamba larga": "Широкие штанины",
        "Düz Paça": "Прямые штанины", "Gamba dritta": "Прямые штанины",
        "Lastikli Paça": "Манжеты на штанинах", "Polsino": "Манжеты на штанинах",
        "Duble Paça": "С отворотами", "Risvolto": "С отворотами",
        "İspanyol Paça": "Расклешённые штанины", "Zampa": "Расклешённые штанины",
    },
    "season": {
        "Summer": "Сезон: весна–лето", "Winter": "Сезон: осень–зима", "Mid-season": "Демисезонная модель",
        "Yaz": "Сезон: весна–лето", "İlkbahar/Yaz": "Сезон: весна–лето", "İlkbahar-Yaz": "Сезон: весна–лето",
        "İlkbahar Yaz": "Сезон: весна–лето", "Kış": "Сезон: осень–зима", "Sonbahar/Kış": "Сезон: осень–зима",
        "Sonbahar-Kış": "Сезон: осень–зима", "Sonbahar Kış": "Сезон: осень–зима",
        "İlkbahar": "Демисезонная модель", "Sonbahar": "Демисезонная модель",
        "İlkbahar/Sonbahar": "Демисезонная модель", "4 Mevsim": "Всесезонная модель",
        "Dört Mevsim": "Всесезонная модель", "Tüm Sezonlar": "Всесезонная модель", "All Season": "Всесезонная модель",
    },
    "heel": {
        "Düz": "Без каблука", "Topuksuz": "Без каблука", "Flat": "Без каблука", "Kısa Topuklu": "Низкий каблук",
        "Orta Topuklu": "Средний каблук", "Yüksek Topuklu": "Высокий каблук", "Basso": "Низкий каблук",
    },
    "heel_type": {
        "Düz Topuklu": "Плоская подошва", "Düz Taban": "Плоская подошва", "Düz": "Плоская подошва",
        "Dolgu Topuk": "Танкетка", "Dolgu": "Танкетка", "Zeppa": "Танкетка", "Kalın Topuk": "Устойчивый каблук",
        "Kalın Topuklu": "Устойчивый каблук", "İnce Topuk": "Тонкий каблук", "Platform": "На платформе",
    },
    "toe": {
        "Yuvarlak Burun": "Круглый носок", "Yuvarlak": "Круглый носок", "Punta tonda": "Круглый носок",
        "Sivri Burun": "Острый носок", "Sivri": "Острый носок", "Punta a punta": "Острый носок",
        "Kare Burun": "Квадратный носок", "Kare": "Квадратный носок", "Punta quadrata": "Квадратный носок",
        "Açık Burun": "Открытый носок", "Badem Burun": "Миндалевидный носок",
    },
    "sole": {
        "Kauçuk": "Резиновая подошва", "Kauçuk Taban": "Резиновая подошва", "Gomma": "Резиновая подошва",
        "EVA": "Лёгкая подошва из EVA", "Eva Taban": "Лёгкая подошва из EVA", "Termo": "Подошва из термопластичной резины",
        "Termo Taban": "Подошва из термопластичной резины", "TPR": "Подошва из термопластичной резины",
        "Poliüretan": "Полиуретановая подошва", "PU": "Полиуретановая подошва", "Poliuretano": "Полиуретановая подошва",
        "Deri": "Кожаная подошва", "Deri Taban": "Кожаная подошва", "Cuoio": "Кожаная подошва",
        "TPU": "Подошва из термополиуретана", "Düz Taban": "Плоская подошва", "Faylon": "Лёгкая подошва (филон)",
    },
    "lining": {
        "Astarsız": "Без подкладки", "Senza fodera": "Без подкладки", "Yarım Astarlı": "Полуподкладка",
        "Mezza fodera": "Полуподкладка", "Astarlı": "На подкладке", "Tam Astarlı": "На подкладке",
        "Foderato": "На подкладке", "Deri": "Кожаная подкладка", "Deri Astar": "Кожаная подкладка",
        "Tekstil": "Текстильная подкладка", "Tekstil Astar": "Текстильная подкладка",
        "Deri + Tekstil": "Подкладка из кожи и текстиля",
        "Kürklü": "Утеплённая подкладка", "Suni Kürk": "Утеплённая подкладка", "Polar": "Флисовая подкладка",
        "Polar Astarlı": "Флисовая подкладка",
    },
    "hood": {"Con cappuccio": "С капюшоном", "Kapüşonlu": "С капюшоном", "Var": "С капюшоном"},
    "style": {
        "Casual/Günlük": "Повседневный стиль", "Casual / Günlük": "Повседневный стиль", "Günlük": "Повседневный стиль",
        "Casual": "Повседневный стиль", "Spor": "Спортивный стиль", "Sport": "Спортивный стиль",
        "Klasik/Ofis": "Классический стиль", "Ofis": "Классический стиль", "İş": "Классический стиль",
        "Davet": "Для особых случаев", "Özel Gün": "Для особых случаев", "Plaj": "Пляжный стиль",
        "Sportswear": "Спортивный стиль", "Şık/Gece": "Для особых случаев",
    },
    "detail": {
        "Logolu": "С логотипом", "Nakışlı": "С вышивкой", "Nakış": "С вышивкой", "Kapüşonlu": "С капюшоном",
        "Fermuarlı": "Застёжка на молнию", "Cepli": "С карманами", "Yırtık Detaylı": "С потёртостями",
        "Aplikeli": "С аппликацией", "Baskılı": "С принтом", "Pileli": "Со складками", "Yırtmaçlı": "С разрезом",
        "Biyeli": "С контрастным кантом", "Dikiş Detaylı": "Декоративная строчка", "Düğmeli": "Застёжка на пуговицы",
        "Kemerli": "С ремнём в комплекте",
    },
    "silhouette": {"Oversize": "Оверсайз", "Slim": "Приталенный силуэт", "Fitted": "Приталенный силуэт",
                   "A Kesim": "А-силуэт", "Jogger": "Модель-джоггеры", "Jegging": "Джеггинсы",
                   "Asimetrik": "Асимметричный крой", "Uzun": None},
}.items()}

# Подсказки из листовой категории YOOX («Camicie slim fit», «Felpe con cappuccio»…): (regex, тема, пункт).
YOOX_CATEGORY_HINTS = [
    (r"regular fit", "fit", "Прямой крой (regular fit)"),
    (r"slim fit", "fit", "Приталенный крой (slim fit)"),
    (r"skinny", "fit", "Облегающий крой (skinny)"),
    (r"straight", "fit", "Прямой крой"),
    (r"oversize", "fit", "Оверсайз"),
    (r"a righe", "pattern", "В полоску"),
    (r"a quadri", "pattern", "В клетку"),
    (r"fantasia", "pattern", "С узором"),
    (r"tinta unita", "pattern", "Однотонная расцветка"),
    (r"con cappuccio", "hood", "С капюшоном"),
    (r"con zip", "closure", "Застёжка на молнию"),
    (r"dolcevita", "collar", "Высокое горло"),
    (r"crop", "length", "Укороченная модель"),
    (r"5 tasche", "pocket", "Пять карманов"),
]
# Подсказки из турецкого названия товара (если на странице товара таких свойств не было).
TITLE_HINTS = [
    (r"\b(extra|super) slim\b", "fit", "Узкий крой (extra slim)"),
    (r"\bslim fit\b|\bslim\b", "fit", "Приталенный крой (slim fit)"),
    (r"\bregular fit\b|\bregular\b", "fit", "Прямой крой (regular fit)"),
    (r"\boversize", "fit", "Оверсайз"),
    (r"\bcomfort\b|\brelax", "fit", "Свободный крой"),
    (r"\bcarrot\b", "fit", "Крой «морковка» (carrot fit)"),
    (r"\bskinny\b", "fit", "Облегающий крой (skinny)"),
    (r"\bstraight\b", "fit", "Прямой крой"),
    (r"\bmodern fit\b", "fit", "Полуприталенный крой (modern fit)"),
    (r"polo yaka", "collar", "Воротник-поло"),
    (r"bisiklet yaka", "collar", "Круглый вырез"),
    (r"\bv yaka", "collar", "V-образный вырез"),
    (r"yarim balikci", "collar", "Невысокое горло"),
    (r"balikci", "collar", "Высокое горло"),
    (r"dik yaka|hakim yaka", "collar", "Воротник-стойка"),
    (r"dugmeli yaka|button ?down", "collar", "Воротник на пуговицах (button-down)"),
    (r"apas yaka", "collar", "Воротник апаш"),
    (r"italyan yaka", "collar", "Итальянский воротник"),
    (r"kapusonlu", "hood", "С капюшоном"),
    (r"uzun kol", "sleeve", "Длинный рукав"),
    (r"kisa kol", "sleeve", "Короткий рукав"),
    (r"kolsuz", "sleeve", "Без рукавов"),
    (r"yarim fermuar", "closure", "Короткая молния у горловины"),
    (r"fermuarli", "closure", "Застёжка на молнию"),
    (r"cizgili", "pattern", "В полоску"),
    (r"kareli|ekose", "pattern", "В клетку"),
    (r"kazayagi", "pattern", "Узор «гусиная лапка»"),
    (r"jakarli", "pattern", "Жаккардовый узор"),
    (r"puantiyeli", "pattern", "В горошек"),
    (r"kamuflaj", "pattern", "Камуфляжная расцветка"),
    (r"baskili", "pattern", "С принтом"),
    (r"desenli", "pattern", "С узором"),
    (r"\bpike\b", "fabric", "Ткань пике"),
    (r"uc iplik", "fabric", "Футер-трёхнитка"),
    (r"sardonlu", "fabric", "С начёсом"),
    (r"kanguru cep", "pocket", "Карман-кенгуру"),
    (r"kargo cep", "pocket", "Карманы карго"),
    (r"utu (gerektirmeyen|istemeyen)|non ?iron|kolay utulenir", "detail", "Не требует глажки"),
    (r"su itici|water ?repellent", "detail", "Водоотталкивающая ткань"),
    (r"kapitone", "detail", "Стёганая модель"),
]
_YOOX_HINT_RE = [(re.compile(p), t, b) for p, t, b in YOOX_CATEGORY_HINTS]
_TITLE_HINT_RE = [(re.compile(p), t, b) for p, t, b in TITLE_HINTS]
# порядок пунктов в карточке
TOPIC_ORDER = ["fit", "collar", "hood", "sleeve", "closure", "pattern", "fabric", "pocket", "rise", "leg",
               "length", "toe", "heel_type", "heel", "sole", "lining", "detail", "silhouette", "style", "season"]
MAX_DETAILS = 8


def _strip_code(s: str) -> str:
    """YOOX: «Girocollo-grcll» -> «Girocollo», «T-shirt-tshrt» -> «T-shirt», «Collo-nck» -> «Collo»."""
    return re.sub(r"-[a-z0-9_]+$", "", str(s or "").strip())


def _topic_value(topic: str, value) -> str | None:
    v = fold(value)
    if not v or v in IGNORE_VALUES:
        return None
    table = TOPIC_VALUES.get(topic, {})
    if v in table:
        return table[v]                                       # None в словаре — «знаем, но не показываем»
    v2 = fold(re.sub(r"\([^)]*\)", "", str(value)))          # «Kısa Topuklu (1- 4 cm)» -> «kisa topuklu»
    if v2 in table:
        return table[v2]
    if v2 in IGNORE_VALUES:
        return None
    _miss(f"свойство «{topic}»", value)
    return None


def _raw_pairs(p: dict) -> list[tuple[str, str]]:
    """(ключ, значение) из attrs любого источника; у YOOX — ещё и dynamicAttributes."""
    a = p.get("attrs") or {}
    pairs = []
    for k, v in a.items():
        if k == "dynamicAttributes" and isinstance(v, dict):
            for dk, dv in v.items():
                key = _strip_code(dk)
                for x in dv if isinstance(dv, list) else [dv]:
                    if ">" not in str(x):          # иерархию («Scarpe > Stringate») берёт title/категории
                        pairs.append((key, _strip_code(x)))
        elif isinstance(v, (str, int, float)):
            pairs.append((k, str(v)))
    return pairs


def details_ru(p: dict) -> list[str]:
    by_topic: dict[str, list[str]] = {}

    def add(topic: str, bullet: str | None) -> None:
        if bullet and bullet not in by_topic.setdefault(topic, []):
            by_topic[topic].append(bullet)

    for k, v in _raw_pairs(p):
        fk = fold(k)
        if fk in SKIP_KEYS:
            continue
        topic = TOPIC_KEYS.get(fk)
        if not topic:
            if p.get("source") != "yoox" or fk not in ("categorie",):
                _miss("свойство (ключ)", k)
            continue
        add(topic, _topic_value(topic, v))

    src = p.get("source")
    if src == "yoox":
        for cat in _yoox_leaf_cats(p.get("attrs") or {}):
            for rx, topic, bullet in _YOOX_HINT_RE:
                if rx.search(fold(cat)) and topic not in by_topic:
                    add(topic, bullet)
    else:
        title = fold(p.get("title"))
        for rx, topic, bullet in _TITLE_HINT_RE:
            if topic == "fabric" and p.get("type") == "обувь":
                continue
            if rx.search(title) and not by_topic.get(topic):
                add(topic, bullet)
    # «С капюшоном» из воротника и из отдельного свойства — один пункт
    if "hood" in by_topic and "С капюшоном" in by_topic.get("collar", []):
        by_topic["collar"].remove("С капюшоном")
    out = []
    for t in TOPIC_ORDER:
        for b in by_topic.get(t, []):
            if b not in out:
                out.append(b)
    return out[:MAX_DETAILS]


# ---------------------------------------------------------------- состав и цвет по источнику

def _composition_raw(p: dict) -> list:
    a = p.get("attrs") or {}
    src = p.get("source")
    if src == "yoox":
        return [a.get("composition"), a.get("mainMaterial")]
    if src in ("pcardin_tr", "cacharel_tr"):
        return [a.get("integration_fabric_fiber"), a.get("integration_fabric_blends"),
                a.get("filterable_fabric_blends"), _title_composition(p.get("title"))]
    return [a.get("Materyal Bileşeni"), a.get("Dış Materyal"), a.get("Saya Materyali"), a.get("Materyal"),
            _title_composition(p.get("title"))]


def _title_composition(title) -> str | None:
    """«… %100 Pamuk …» в турецком названии."""
    m = re.search(r"%\s?100\s+[^\W\d_]+", str(title or ""))
    return m.group(0) if m else None


def _color_raw(p: dict) -> list:
    a = p.get("attrs") or {}
    src = p.get("source")
    colors = list(p.get("colors") or [])
    if src == "yoox":
        return [a.get("colorLabel")] + colors
    if src in ("pcardin_tr", "cacharel_tr"):
        return [a.get("filterable_detail_color")] + colors
    return colors + [a.get("Renk"), a.get("ld_color")]


# ---------------------------------------------------------------- размерная сетка

_LETTER = re.compile(r"(\d?X{0,4}[SL]|M|XXS|XS|S/M|M/L|L/XL)", re.I)
_ONE = {"one size", "onesize", "--", "tu", "std", "standart", "tek ebat", "unica"}


def size_system(p: dict) -> str | None:
    """"EU" — обувь, "INT" — S/M/L, "W" — джинсы/брюки по талии (дюймы), "IT"/"EU" — итальянские / турецкие
    числовые размеры одежды. None — если неясно или безразмерный товар."""
    sizes = [str(s).strip() for s in p.get("sizes") or [] if str(s).strip().lower() not in _ONE]
    if not sizes:
        return None
    ptype = p.get("type")
    if ptype == "обувь":
        return "EU"
    if all(_LETTER.fullmatch(s) for s in sizes):
        return "INT"
    nums = []
    for s in sizes:
        m = re.fullmatch(r"(\d{2,3})(?:[.,]5)?(?:\s*W.*|/\d{2}|-\d{2})?", s, re.I)
        if not m:
            return None
        nums.append(int(m.group(1)))
    if ptype in ("джинсы", "брюки", "шорты") and all(24 <= n <= 42 for n in nums):
        return "W"
    if ptype in ("аксессуары", "сумки") or min(nums) < 34:
        return None
    return "IT" if p.get("country") == "IT" else "EU"


# ---------------------------------------------------------------- описание

ORIGIN_RU = {"IT": "Италии", "TR": "Турции"}
_DESC_TOPICS = ["fit", "collar", "hood", "sleeve", "closure", "pattern", "fabric", "toe", "sole", "lining"]


def _lower_first(s: str) -> str:
    """«В полоску» -> «в полоску», но «V-образный вырез» остаётся как есть."""
    return s[0].lower() + s[1:] if len(s) > 1 and not s[1].isupper() and s[1] != "-" else s


def description_ru(title: str, color: str | None, comp_parts, details: list[str], origin: str | None) -> str:
    s1 = title
    cp = color_phrase(color)
    if cp:
        s1 += " " + cp
    sentences = []
    if comp_parts and len(comp_parts) == 1:
        pct, _, gen = comp_parts[0]
        s1 += " из " + (f"{_pct(pct)}% " if pct else "") + gen        # «из 100% хлопка», «из натуральной кожи»
        sentences.append(s1 + ".")
    else:
        sentences.append(s1 + ".")
        if comp_parts:
            sentences.append(f"Состав: {composition_text(comp_parts)}.")
    picked = [d for d in details if not d.startswith(("Сезон", "Демисезон", "Всесезон"))
              and "стиль" not in d][:3]
    if picked:
        sentences.append(picked[0] + "".join(", " + _lower_first(d) for d in picked[1:]) + ".")
    where = ORIGIN_RU.get(origin or "")
    sentences.append(f"Оригинал. Привезём из {where}." if where else "Оригинал.")
    return " ".join(sentences)


def describe(p: dict) -> dict:
    """Всё, что видит покупатель в карточке: заголовок, цвет, состав, пункты, описание, размерная сетка."""
    title = title_ru(p)
    color = None
    for raw in _color_raw(p):
        if raw:
            color = color_ru(raw)
            if color:
                break
    comp_parts = None
    for raw in _composition_raw(p):
        if raw:
            comp_parts = parse_composition(raw)
            if comp_parts:
                break
    # «из текстиля» у одежды ничего не говорит (так Trendyol пишет «Materyal» у футболок) — не показываем
    if comp_parts and [c[1] for c in comp_parts] == ["текстиль"] and p.get("type") not in ("обувь", "сумки", "аксессуары"):
        comp_parts = None
    details = details_ru(p)
    origin = p.get("country")
    return {
        "title": title,
        "color": color,
        "composition": composition_text(comp_parts),
        "details": details,
        "description": description_ru(title, color, comp_parts, details, origin),
        "size_system": size_system(p),
    }
