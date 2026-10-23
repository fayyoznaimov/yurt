"""escarpe.it (sources/escarpe.py): разбор выдачи и страницы товара по сохранённым страницам сайта
(tests/fixtures/escarpe/), robots.txt, ярусы и бюджет проверки страниц товаров, неполный обход, перепроверка по
ссылке, подключение к run.py (--mode, замки, горячие id), файлы сервера, свой хост фото и проверки на утечку
источника. Сети нет: requests.Session подменён, паузы выключены.

    python -m unittest tests.test_escarpe
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import describe  # noqa: E402
import run  # noqa: E402
import sync_state  # noqa: E402
from sources import ADAPTERS, escarpe  # noqa: E402
from sources.base import Query  # noqa: E402

FX = ROOT / "tests" / "fixtures" / "escarpe"
if not (FX / "listing_boss.html").exists():        # страницы источника не хранятся в публичном репозитории
    raise unittest.SkipTest("нет tests/fixtures/escarpe (локальные сохранённые страницы)")
LISTING_HTML = (FX / "listing_boss.html").read_text(encoding="utf-8")
PDP_HTML = (FX / "pdp_boss_tayil.html").read_text(encoding="utf-8")
PDP_ID = "0000306230590"
PDP_URL = "https://escarpe.it/p/stivaletti-boss-tayil-50573316-nero-0000306230590"
LEAK = re.compile(r"escarpe|eobuwie|modivo|€|\bEUR\b|возврат|reso", re.I)


def iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def with_product_ld(html: str, mutate) -> str:
    """Страница товара с изменённым JSON-LD товара (mutate(obj) правит объект на месте)."""
    m = re.search(r'(<script[^>]*data-hid="ldjson-schema-product"[^>]*>)(.*?)(</script>)', html, re.S)
    obj = json.loads(m.group(2))
    mutate(obj)
    return html[:m.start(2)] + json.dumps(obj, ensure_ascii=False) + html[m.end(2):]


def with_regular_price(html: str, regular: str) -> str:
    """Страница товара со скидкой: в блок цены (product-price-new) вставлены строки «Prezzo regolare» — та же разметка
    omnibus, что у карточек со скидкой в выдаче (listing_boss.html, «Chelsea BOSS Tayil»)."""
    snippet = ('<div class="omnibus product-omnibus s left" data-v-139acb9a><div class="line" data-v-139acb9a> Prezzo '
               f'regolare <span class="price" data-v-139acb9a><span class="value" data-v-139acb9a>{regular} €</span>'
               '<span class="percent" data-v-139acb9a> -18% </span></span></div></div>')
    i = html.index('class="product-price-new"')
    j = html.index('class="choose-product-size', i)
    k = html.rindex("<div", i, j)
    return html[:k] + snippet + html[k:]


def mini_listing(n_cards: int, offer_count: int, canonical: str = "https://escarpe.it/c/escarpe/marca:boss") -> str:
    """Маленькая страница выдачи из настоящей: JSON-LD с другим offerCount и первые n карточек."""
    head = LISTING_HTML.replace('"offerCount":1359', f'"offerCount":{offer_count}')
    head = head.replace("https://escarpe.it/c/escarpe/marca:boss", canonical)
    parts = head.split('<li data-product-card="')
    return '<li data-product-card="'.join(parts[:1 + n_cards]) + "</li></ul></body></html>"


class FakeResp:
    def __init__(self, status=200, text="", location=None):
        self.status_code, self.text = status, text
        self.headers = {"location": location} if location else {}
        self.is_redirect = status in (301, 302, 303, 307, 308) and bool(location)
        self.encoding = None


class FakeSession:
    """requests.Session без сети: адрес -> FakeResp (нет в routes — 404). Запросы пишутся в calls."""
    routes: dict = {}
    calls: list = []

    def __init__(self):
        self.headers = {}

    def get(self, url, timeout=None, allow_redirects=True):
        FakeSession.calls.append(url)
        r = FakeSession.routes.get(url)
        if callable(r):
            r = r()
        return r if r is not None else FakeResp(404)


class NetBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        FakeSession.routes, FakeSession.calls = {}, []
        self.listing = self.tmp / "listing.json"
        self.cache = self.tmp / "cache.json"
        # кэши по умолчанию (verify(rows, None) — как вызывает order_api) — тоже во временной папке, не в data/
        self.patches = [mock.patch.object(escarpe.requests, "Session", FakeSession),
                        mock.patch.object(escarpe.time, "sleep", lambda s: None),
                        mock.patch.object(escarpe, "PDP_CACHE_FILE", self.cache),
                        mock.patch.object(escarpe, "LISTING_FILE", self.listing)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def so(self, **kw) -> dict:
        base = {"brands": {"Boss": ["boss"]}, "sections": [{"path": "escarpe"}], "pdp_cache_file": str(self.cache),
                "listing_file": str(self.listing), "root_check": False, "pdp_per_run": 100}
        base.update(kw)
        return base

    def items_from_fixture(self, gender=None, n=None) -> dict:
        crawl = escarpe._Crawl(None, 1)
        for c in escarpe.parse_listing(LISTING_HTML)["cards"][:n]:
            quiet(crawl._add, c, "Boss", "boss", {"path": "escarpe", "gender": gender})
        return crawl.items


# ---------------------------------------------------------------- разбор страниц

class ListingParseTest(unittest.TestCase):
    def setUp(self):
        self.d = escarpe.parse_listing(LISTING_HTML)

    def test_counts_and_pagination(self):
        self.assertEqual(self.d["offer_count"], 1359)
        self.assertEqual((self.d["low"], self.d["high"]), (13.0, 650.0))
        self.assertEqual(len(self.d["cards"]), 72)
        urls = {escarpe.BASE + c["path"] for c in self.d["cards"]}
        self.assertEqual(len(urls), 72)                                   # 72 разных товара на странице
        self.assertTrue(all(re.fullmatch(r"https://escarpe\.it/p/[a-z0-9-]+-\d{13}", u) for u in urls))
        self.assertTrue(set(self.d["urls"]) <= urls)                     # ItemList JSON-LD — те же товары
        pages = math.ceil(self.d["offer_count"] / escarpe.PAGE_SIZE)
        self.assertEqual(pages, 19)
        self.assertEqual(self.d["last_page"], 19)                        # ссылка на последнюю страницу
        self.assertEqual(escarpe.listing_url("escarpe", "boss", 2), "https://escarpe.it/c/escarpe/marca:boss?p=2")
        self.assertIn("marca:boss", self.d["canonical"])

    def test_card_fields_italian_not_polish(self):
        c = next(c for c in self.d["cards"] if c["id"] == PDP_ID)
        self.assertEqual((c["brand"], c["type"], c["color"], c["price"], c["price_old"]),
                         ("BOSS", "Stivaletti", "Nero", 229.99, None))
        self.assertEqual(c["name"], "BOSS Stivaletti BOSS Tayil 50573316 Nero")
        self.assertTrue(c["image"].startswith("https://img.eobuwie.cloud/product("))
        self.assertIn(",jpg)/", c["image"])
        names = " ".join(c["name"] or "" for c in self.d["cards"])
        self.assertNotRegex(names, r"Botki|Kozaki|Śniegowce|Czarny|Brązowy")     # польские имена из ItemList не берём

    def test_discount_cards(self):
        disc = {c["id"]: (c["price"], c["price_old"]) for c in self.d["cards"] if c["price_old"]}
        self.assertEqual(disc, {"0000305486493": (177.99, 209.99), "0000305958556": (34.99, 54.95)})

    def test_listing_record(self):
        crawl = escarpe._Crawl(None, 1)
        c = next(c for c in self.d["cards"] if c["id"] == "0000305486493")
        quiet(crawl._add, c, "Boss", "boss", {"path": "uomo", "gender": "men"})
        rec = crawl.items["0000305486493"]
        self.assertEqual((rec["source"], rec["brand"], rec["type"], rec["gender"], rec["currency"]),
                         ("escarpe_it", "Boss", "обувь", "men", "EUR"))
        self.assertEqual((rec["price_now"], rec["price_old"], rec["discount_pct"]), (177.99, 209.99, 15.2))
        self.assertEqual(rec["title"], "Chelsea BOSS Tayil 50552489 Nero")       # alt фото без бренда в начале
        self.assertEqual(rec["url"], "https://escarpe.it/p/chelsea-boss-tayil-50552489-nero-0000305486493")


class PdpParseTest(unittest.TestCase):
    def test_fixture(self):
        d = escarpe.parse_pdp(PDP_HTML)
        self.assertEqual(d["id"], PDP_ID)
        self.assertEqual(d["sizes"], ["40", "41", "42", "43", "44", "45", "46"])
        self.assertEqual(d["all_sizes"], d["sizes"])
        self.assertEqual((d["price"], d["price_old"]), (229.99, None))
        self.assertEqual(d["brand_raw"], "BOSS")
        self.assertEqual(d["name"], "Stivaletti BOSS Tayil 50573316 Nero")
        self.assertEqual((d["color"], d["material"]), ("Nero", "Pelle pieno fiore"))   # без невидимого
        self.assertEqual(d["category"], "Polacchine e chelsea boots")
        self.assertEqual(d["category_path"], "Uomo/Polacchine e chelsea boots")
        self.assertEqual((d["gender"], d["unisex"]), ("men", False))
        self.assertEqual(len(d["images"]), 7)
        self.assertTrue(all(u.startswith("https://img.eobuwie.cloud/product(") and ",jpg)/" in u and u.endswith(".jpg")
                            for u in d["images"]))
        self.assertEqual(d["attrs"]["Sistema di chiusura"], "Cerniera")
        self.assertEqual(d["attrs"]["Materiale esterno"], "Pelle fiore")
        for k in ("Marca", "Index", "Nome prodotto", "Produttore", "Indirizzo"):   # служебное и адреса производителя
            self.assertNotIn(k, d["attrs"])

    def test_availability_per_size(self):
        def sold_46(obj):
            for v in obj["hasVariant"]:
                if v["size"] == "46":
                    v["offers"]["availability"] = "https://schema.org/OutOfStock"
        d = escarpe.parse_pdp(with_product_ld(PDP_HTML, sold_46))
        self.assertNotIn("46", d["sizes"])
        self.assertIn("46", d["all_sizes"])

        def all_sold(obj):
            for v in obj["hasVariant"]:
                v["offers"]["availability"] = "https://schema.org/OutOfStock"
        d = escarpe.parse_pdp(with_product_ld(PDP_HTML, all_sold))
        self.assertEqual(d["sizes"], [])
        self.assertFalse(d["in_stock"])

    def test_one_size_and_fractions(self):
        def bag(obj):
            obj["offers"] = obj["hasVariant"][0]["offers"]
            del obj["hasVariant"]
        self.assertEqual(escarpe.parse_pdp(with_product_ld(PDP_HTML, bag))["sizes"], ["one size"])
        self.assertEqual(escarpe._size_label("41 1/3"), "41 ⅓")
        self.assertEqual(escarpe._size_label("Taglia unica"), "one size")

    def test_discount(self):
        d = escarpe.parse_pdp(with_regular_price(PDP_HTML, "279,99"))
        self.assertEqual((d["price"], d["price_old"]), (229.99, 279.99))

        def regular_in_ld(obj):                         # JSON-LD с обычной ценой, а на странице видна скидка
            for v in obj["hasVariant"]:
                v["offers"]["price"] = 279.99
        d = escarpe.parse_pdp(with_product_ld(with_regular_price(PDP_HTML, "279,99"), regular_in_ld))
        self.assertEqual((d["price"], d["price_old"]), (229.99, 279.99))

    def test_gender(self):
        self.assertEqual(escarpe.pdp_gender("female", ["Borse", "Borse a tracolla"], None), ("women", False))
        self.assertEqual(escarpe.pdp_gender("unisex", ["Accessori"], None), (None, True))
        self.assertEqual(escarpe.pdp_gender(None, ["Uomo", "Sneakers"], None), ("men", False))
        self.assertEqual(escarpe.pdp_gender("male", ["Bambini", "Bambino"], None)[0], "kids")
        self.assertEqual(escarpe.pdp_gender(None, ["Accessori"], "escarpe/Accessori"), (None, False))

    def test_type_mapping(self):
        cases = {"Stivaletti": "обувь", "Chelsea": "обувь", "Polacchine e chelsea boots": "обувь", "Sneakers": "обувь",
                 "Décolleté": "обувь", "Stivali da neve": "обувь", "Borsetta": "сумки", "Pochette per cosmetici": "сумки",
                 "Porta PC": "сумки", "Zaino": "сумки", "Porta carte": "аксессуары", "Portafoglio": "аксессуары",
                 "Cintura": "аксессуары", "Sciarpa": "аксессуары", "Pantaloncini": "шорты", "Pantaloni della tuta": "брюки",
                 "Jeans": "джинсы", "Felpa": "толстовки", "T-shirt": "футболки и поло", "Polo": "футболки и поло",
                 "Camicia di jeans": "рубашки", "Giacca di jeans": "куртки и пальто", "Piumino": "куртки и пальто",
                 "Cappotto": "куртки и пальто", "Maglione": "свитеры и кардиганы", "Tuta": None,
                 # категории из пробного обхода 23.10.2026
                 "Set di boxer": "нижнее бельё", "Set di t-shirt": "футболки и поло", "Set regalo": None,
                 "Set orologio e braccialetto": "аксессуары", "Custodie per carte di credito": "аксессуары",
                 "Custodia per PC": "сумки", "Custodia per documenti": "сумки", "Camicia da notte": "нижнее бельё",
                 "Camicia del pigiama": "нижнее бельё", "Completo intimo": "нижнее бельё", "Perizoma": "нижнее бельё",
                 "Culotte classiche": "нижнее бельё", "Pedulini": "нижнее бельё", "Longsleeve": "футболки и поло",
                 "Borsellino": "аксессуары", "Borsello": "сумки", "Basco": "аксессуары", "Wellington": "обувь",
                 "Body": None}
        for text, want in cases.items():
            self.assertEqual(escarpe.type_of(text), want, text)


class DescribeTest(unittest.TestCase):
    def product(self, genders=("men",)):
        crawl = escarpe._Crawl(None, 1)
        for c in escarpe.parse_listing(LISTING_HTML)["cards"]:
            quiet(crawl._add, c, "Boss", "boss", {"path": "uomo", "gender": None})
        rec = crawl.items[PDP_ID]
        rec["_genders"] = list(genders)
        e = escarpe._cache_entry(escarpe.parse_pdp(PDP_HTML))
        return dict(escarpe.to_product(rec, e).to_dict(), country="IT")

    def test_russian_card_from_real_data(self):
        p = self.product()
        self.assertEqual((p["brand"], p["type"], p["gender"], p["price_now"], p["currency"]),
                         ("Boss", "обувь", "men", 229.99, "EUR"))
        d = describe.describe(p)
        self.assertEqual(d["title"], "Ботинки, чёрные, натуральная кожа")
        self.assertEqual((d["color"], d["composition"], d["size_system"]), ("чёрный", "натуральная кожа", "EU"))
        self.assertEqual(d["details"][:3], ["Застёжка на молнию", "Круглый носок", "Без каблука"])
        self.assertTrue(d["description"].startswith("Ботинки Boss чёрного цвета из натуральной кожи."))
        for text in [d["title"], d["description"], d["composition"], *d["details"]]:
            self.assertIsNone(LEAK.search(text), text)
        self.assertEqual(p["url"], PDP_URL)                       # ссылка — только в закрытых полях (ADMIN_FIELDS)
        self.assertIn("url", run.ADMIN_FIELDS)
        self.assertNotIn("url", run.PUBLIC_FIELDS)

    def test_unisex_and_colors(self):
        self.assertIsNone(self.product(("men", "women"))["gender"])
        self.assertEqual(describe.color_ru("Beige scuro"), "тёмно-бежевый")
        self.assertEqual(describe.color_ru("Marrone scuro"), "тёмно-коричневый")
        self.assertEqual(describe.color_ru("Bianco sporco"), "молочный")
        for it, group in (("Nero", "чёрный"), ("Bianco", "белый"), ("Blu scuro", "синий"), ("Beige", "бежевый"),
                          ("Grigio", "серый"), ("Rosso", "красный"), ("Verde", "зелёный"), ("Rosa", "розовый"),
                          ("Cammello", "коричневый"), ("Marrone scuro", "коричневый")):
            self.assertEqual(describe.COLOR_GROUPS[describe.color_group(describe.color_ru(it))][0], group, it)

    def test_kinds(self):
        def title(cat, path=None, gender="men", typ=None):
            q = {"source": "escarpe_it", "brand": "Boss", "title": cat, "category": cat, "type": typ or escarpe.type_of(cat, path),
                 "gender": gender, "sizes": ["M"], "country": "IT", "colors": [], "attrs": {"category_path": path} if path else {}}
            return describe.describe(q)["title"]
        self.assertEqual(title("Stivaletti"), "Ботинки")
        self.assertEqual(title("Stivali da neve"), "Сапоги")
        self.assertEqual(title("Sneakers"), "Кроссовки")
        self.assertEqual(title("Mocassini"), "Мокасины")
        self.assertEqual(title("Francesine"), "Туфли на шнурках")
        self.assertEqual(title("Décolleté", gender="women"), "Женские туфли на каблуке")
        self.assertEqual(title("Pochette per cosmetici", gender="women"), "Женская косметичка")
        self.assertEqual(title("Borsetta", "Borse/Borse a tracolla", gender="women"), "Женская сумка через плечо")
        self.assertEqual(title("Zaino"), "Рюкзак")
        self.assertEqual(title("Portafoglio"), "Кошелёк")
        self.assertEqual(title("Cintura"), "Ремень")
        self.assertEqual(title("Felpa"), "Толстовка")
        self.assertEqual(title("Pantaloni della tuta"), "Спортивные брюки")
        self.assertEqual(title("Piumino", gender="women"), "Женский пуховик")
        self.assertEqual(title("Tuta"), "Boss")                    # тип не угадываем — заголовок = бренд
        # в середине крошек «T-shirt e polo» — у футболки вид по последнему узлу, не «поло»
        tee = "Abbigliamento/Abbigliamento donna/T-shirt e polo/T-shirt"
        self.assertEqual(title("T-shirt", tee, gender="women"), "Женская футболка")
        self.assertEqual(title("Polo", "Abbigliamento/Abbigliamento uomo/T-shirt e polo/Magliette polo"), "Поло")
        self.assertEqual(title("Custodie per carte di credito"), "Картхолдер")
        self.assertEqual(title("Perizoma", gender="women"), "Женские трусы")

    def test_regular_price_from_listing_card(self):
        # страница товара не показала «Prezzo regolare», карточка выдачи при той же цене показала — скидка есть
        rec = {"_card_price": 53.99, "_card_price_old": 99.95, "_seen_at": iso_ago(2)}
        e = {"price": 53.99, "price_old": None, "checked_at": iso_ago(1)}
        self.assertEqual(escarpe._fresher_price(rec, e), (53.99, 99.95))
        self.assertEqual(escarpe._fresher_price(rec, dict(e, price=49.99)), (49.99, None))     # цена другая — нет
        self.assertEqual(escarpe._fresher_price(dict(rec, _seen_at=iso_ago(0.5)), dict(e, price=49.99)), (53.99, 99.95))
        self.assertEqual(escarpe._fresher_price(rec, dict(e, price_old=120.0)), (53.99, 120.0))   # страница важнее


# ---------------------------------------------------------------- сеть: robots, обход, страницы товаров

class RobotsAndClientTest(NetBase):
    def test_robots(self):
        ok = ["/c/escarpe/marca:boss", "/c/uomo/marca:boss?p=3", "/c/donna/marca:boss", "/c/borse/marca:karl_lagerfeld",
              "/c/abbigliamento/abbigliamento-uomo/marca:ea7_emporio_armani?p=2", "/c/accessori/marca:michael_michael_kors",
              "/p/stivaletti-boss-tayil-50573316-nero-0000306230590", "/"]
        bad = ["/s/boss?q=boss", "/m-api/products", "/t-api/x", "/n-api/y", "/c/escarpe/marca:boss%26colore:nero",
               "/c/escarpe/marca:boss/colore:nero/taglia:40", "/c/escarpe/marca:boss/colore:nero-bianco-blu",
               "/c/escarpe/prezzo:10-50", "/c/escarpe/marca:boss?orderDir=asc", "https://eobuwie.com.pl/c/eobuwie"]
        for u in ok:
            self.assertTrue(escarpe.robots_ok(u), u)
        for u in bad:
            self.assertFalse(escarpe.robots_ok(u), u)

    def test_client_never_requests_disallowed_and_stops_on_block(self):
        c = escarpe._Client()
        with self.assertRaises(ValueError):
            c.get("https://escarpe.it/s/boss?q=boss")
        self.assertEqual(FakeSession.calls, [])                    # запрос даже не отправлен
        for code in (403, 429, 500, 503):
            FakeSession.routes[PDP_URL] = FakeResp(code)
            with self.assertRaises(escarpe.SourceBlocked):
                c.get(PDP_URL)
        self.assertEqual(FakeSession.calls.count(PDP_URL), 4)      # по одному разу на код — без повторов


class CrawlTest(NetBase):
    def test_listing_mode_truncated_is_incomplete(self):
        FakeSession.routes["https://escarpe.it/c/escarpe/marca:boss"] = FakeResp(200, LISTING_HTML)
        q = Query(max_pages=1, source_opts=self.so(_mode="listing"))
        out = quiet(escarpe.fetch, q)
        self.assertEqual(out, [])
        self.assertTrue(escarpe.LAST_RUN["no_new_data"])          # run.py оставит прошлые строки
        st = escarpe.LAST_RUN["sections"]["boss@escarpe"]
        self.assertEqual((st["offer_count"], st["pages_total"], st["pages"], st["seen"], st["complete"]),
                         (1359, 19, 1, 72, False))
        saved = json.loads(self.listing.read_text(encoding="utf-8"))
        self.assertFalse(saved["complete"])
        self.assertEqual(len(saved["items"]), 72)
        self.assertEqual(FakeSession.calls, ["https://escarpe.it/c/escarpe/marca:boss"])   # ?p=2 не запрашивалась
        self.assertFalse(self.cache.exists())                     # страницы товаров не проверялись

    def test_pagination_requests_all_pages(self):
        FakeSession.routes["https://escarpe.it/c/escarpe/marca:boss"] = FakeResp(200, mini_listing(72, 100))
        page2 = mini_listing(28, 100).replace("-00003", "-99993")          # другие товары (другие 13-значные id)
        FakeSession.routes["https://escarpe.it/c/escarpe/marca:boss?p=2"] = FakeResp(200, page2)
        quiet(escarpe.fetch, Query(max_pages=10, source_opts=self.so(_mode="listing")))
        self.assertEqual(FakeSession.calls, ["https://escarpe.it/c/escarpe/marca:boss",
                                             "https://escarpe.it/c/escarpe/marca:boss?p=2"])
        self.assertTrue(escarpe.LAST_RUN["sections"]["boss@escarpe"]["complete"])
        self.assertEqual(escarpe.LAST_RUN["sections"]["boss@escarpe"]["seen"], 100)
        self.assertTrue(escarpe.LAST_RUN["listing"]["complete"])
        # та же вторая страница, что и первая (сайт отдал повтор) — не «конец», а ошибка: раздел не полный
        FakeSession.calls.clear()
        FakeSession.routes["https://escarpe.it/c/escarpe/marca:boss?p=2"] = FakeResp(200, mini_listing(28, 100))
        quiet(escarpe.fetch, Query(max_pages=10, source_opts=self.so(_mode="listing")))
        self.assertFalse(escarpe.LAST_RUN["sections"]["boss@escarpe"]["complete"])

    def test_absence_only_after_complete_section(self):
        so = self.so(_mode="listing", sections=[{"path": "uomo", "gender": "men"}, {"path": "borse"}])
        old = self.items_from_fixture(n=3)
        gone_id, kept_id, both_id = list(old)
        old[gone_id]["_sections"] = ["boss@uomo"]                 # раздел обойдён полностью, товара нет — выбыл
        old[kept_id]["_sections"] = ["boss@borse"]                # раздел не дообошли — остаётся
        old[both_id]["_sections"] = ["boss@uomo", "boss@borse"]
        self.listing.write_text(json.dumps({"crawled_at": iso_ago(30), "complete": True, "brands": ["Boss"],
                                            "items": old}), encoding="utf-8")
        page = mini_listing(5, 2, canonical="https://escarpe.it/c/uomo/marca:boss")
        page = page.split('<li data-product-card="')
        page = '<li data-product-card="'.join(page[:1] + page[4:6]) + "</li>"   # 2 других товара, без трёх старых
        FakeSession.routes["https://escarpe.it/c/uomo/marca:boss"] = FakeResp(200, page)
        FakeSession.routes["https://escarpe.it/c/borse/marca:boss"] = FakeResp(302, "", location="https://escarpe.it/c/borse")
        quiet(escarpe.fetch, Query(max_pages=5, source_opts=so))
        saved = json.loads(self.listing.read_text(encoding="utf-8"))
        self.assertNotIn(gone_id, saved["items"])
        self.assertIn(kept_id, saved["items"])
        self.assertEqual(saved["items"][both_id]["_sections"], ["boss@borse"])
        self.assertFalse(saved["complete"])
        self.assertNotIn("https://escarpe.it/c/borse", FakeSession.calls)        # перенаправление не выполняется

    def test_empty_section_404_is_complete(self):
        FakeSession.routes["https://escarpe.it/c/escarpe/marca:boss"] = FakeResp(200, mini_listing(3, 3))
        so = self.so(_mode="listing", sections=[{"path": "escarpe"}, {"path": "borse"}])    # borse -> 404
        quiet(escarpe.fetch, Query(max_pages=5, source_opts=so))
        self.assertTrue(escarpe.LAST_RUN["listing"]["complete"])
        self.assertEqual(escarpe.LAST_RUN["sections"]["boss@borse"]["offer_count"], 0)

    def test_block_mid_crawl_stops_without_retry(self):
        FakeSession.routes["https://escarpe.it/c/escarpe/marca:boss"] = FakeResp(200, mini_listing(72, 200))
        FakeSession.routes["https://escarpe.it/c/escarpe/marca:boss?p=2"] = FakeResp(429)
        out = quiet(escarpe.fetch, Query(max_pages=10, source_opts=self.so()))
        self.assertEqual(out, [])
        self.assertTrue(escarpe.LAST_RUN["partial"])
        self.assertEqual(FakeSession.calls.count("https://escarpe.it/c/escarpe/marca:boss?p=2"), 1)
        self.assertNotIn(PDP_URL, FakeSession.calls)               # после блокировки страницы товаров не открываются
        self.assertEqual(len(json.loads(self.listing.read_text(encoding="utf-8"))["items"]), 72)

    def test_extra_page_past_counter_and_sponsored(self):
        # 72 карточки при счётчике 72, но одна — чужого бренда: товаров меньше счётчика — заглядываем на стр. 2
        parts = mini_listing(72, 72).split('<li data-product-card="')
        parts[1] = parts[1].replace('<div class="title" data-v-dca8462a>BOSS</div>',
                                    '<div class="title" data-v-dca8462a>Nike</div>')
        parts[2] = parts[2].replace('class="product-card-small"', 'class="product-card-small sponsored"')   # реклама
        page = '<li data-product-card="'.join(parts)
        FakeSession.routes["https://escarpe.it/c/escarpe/marca:boss"] = FakeResp(200, page)
        quiet(escarpe.fetch, Query(max_pages=10, source_opts=self.so(_mode="listing",
                                                                     sections=[{"path": "escarpe", "gender": "men"}])))
        self.assertEqual(FakeSession.calls[-1], "https://escarpe.it/c/escarpe/marca:boss?p=2")   # 404 — конец выдачи
        st = escarpe.LAST_RUN["sections"]["boss@escarpe"]
        self.assertTrue(st["complete"])
        self.assertEqual(st["seen"], 71)
        items = json.loads(self.listing.read_text(encoding="utf-8"))["items"]
        genders = Counter(r["gender"] for r in items.values())
        self.assertEqual(genders, Counter({"men": 70, None: 1}))   # рекламная карточка: пол — со страницы товара

    def test_kids_section_refused(self):
        secs = quiet(escarpe.parse_sections, {"sections": [{"path": "bambini"}, "abbigliamento/abbigliamento-bambini",
                                                           {"path": "uomo", "gender": "men"}]})
        self.assertEqual(secs, [{"path": "uomo", "gender": "men"}])


class TiersTest(NetBase):
    def setUp(self):
        super().setUp()
        items = self.items_from_fixture(gender="men")
        self.ids = list(items)[:6]
        self.items = {i: items[i] for i in self.ids}
        self.listing.write_text(json.dumps({"crawled_at": iso_ago(2), "complete": True, "brands": ["Boss"],
                                            "section_keys": ["boss@escarpe"], "items": self.items}), encoding="utf-8")
        self.zero = with_product_ld(PDP_HTML, lambda o: [v["offers"].update(availability="https://schema.org/OutOfStock")
                                                         for v in o["hasVariant"]])
        for i, rec in self.items.items():
            FakeSession.routes[rec["url"]] = FakeResp(200, PDP_HTML)

    def test_pdp_mode_tiers_budget_and_output(self):
        h1, h2, w1, w2, c1, n1 = self.ids
        FakeSession.routes[self.items[w2]["url"]] = FakeResp(200, self.zero)     # товар сайта распродан
        entry = escarpe._cache_entry(escarpe.parse_pdp(PDP_HTML))
        cache = {h1: dict(entry, checked_at=iso_ago(5)), h2: dict(entry, checked_at=iso_ago(1)),
                 w1: dict(entry, checked_at=iso_ago(2)), w2: dict(entry, checked_at=iso_ago(30)),
                 c1: dict(entry, checked_at=iso_ago(200))}
        self.cache.write_text(json.dumps(cache), encoding="utf-8")
        q = Query(max_pages=60, source_opts=self.so(_mode="pdp", _hot=[h1, h2], _warm=[w1, w2], pdp_per_run=2))
        out = quiet(escarpe.fetch, q)
        checked = [u for u in FakeSession.calls if "/p/" in u]
        self.assertEqual(checked, [self.items[h1]["url"], self.items[w2]["url"]])   # горячий, потом устаревший с сайта
        self.assertNotIn("https://escarpe.it/c/escarpe/marca:boss", FakeSession.calls)   # выдача не обходилась
        got = {p.source_item_id for p in out}
        self.assertEqual(got, {h1, h2, w1, c1})                    # только проверенные, с размерами в наличии
        self.assertNotIn(n1, got)                                  # ни разу не проверен — ждёт очереди
        self.assertIn(w2, escarpe.LAST_RUN["gone_ids"])           # проверен — размеров нет → распродан
        self.assertFalse(escarpe.LAST_RUN.get("partial"))
        self.assertEqual(escarpe.LAST_RUN["stats"]["tiers"]["hot"], {"total": 2, "fresh": 2, "due": 0})
        p = next(p for p in out if p.source_item_id == h1)
        self.assertEqual((p.price_now, p.currency, p.sizes[0], p.gender, p.brand), (229.99, "EUR", "40", "men", "Boss"))

    def test_budget_and_errors(self):
        todo = self.ids[:3]
        with mock.patch.object(escarpe.time, "time", side_effect=[0, 0] + [10_000] * 50):
            res = quiet(escarpe._run_pdp, escarpe._Client(), todo, self.items, {}, self.cache, 60)
        self.assertTrue(res["budget_hit"])
        self.assertLess(res["done"], 3)
        FakeSession.routes[self.items[todo[1]]["url"]] = FakeResp(403)
        res = quiet(escarpe._run_pdp, escarpe._Client(), todo, self.items, {}, self.cache, 0)
        self.assertEqual(res["done"], 1)
        self.assertIn("403", res["blocked"])

    def test_gone_and_foreign(self):
        a, b, c = self.ids[:3]
        FakeSession.routes[self.items[a]["url"]] = FakeResp(404)
        FakeSession.routes[self.items[b]["url"]] = FakeResp(301, location="https://escarpe.it/c/uomo")
        FakeSession.routes[self.items[c]["url"]] = FakeResp(200, with_product_ld(PDP_HTML, lambda o: o.update(
            brand={"@type": "Brand", "name": "Nike"})))
        cache: dict = {}
        res = quiet(escarpe._run_pdp, escarpe._Client(), [a, b, c], self.items, cache, self.cache, 0, {"Boss": ["boss"]})
        self.assertEqual(sorted(res["gone_ids"]), sorted([a, b, c]))
        self.assertTrue(cache[a]["gone"] and cache[b]["gone"])
        self.assertTrue(cache[c]["foreign"])
        self.assertNotIn("https://escarpe.it/c/uomo", FakeSession.calls)

    def test_pdp_mode_without_listing_fails(self):
        self.listing.unlink()
        with self.assertRaises(RuntimeError):
            quiet(escarpe.fetch, Query(source_opts=self.so(_mode="pdp")))

    def test_verify(self):
        a, b = self.ids[:2]
        FakeSession.routes[self.items[b]["url"]] = FakeResp(404)
        rows = [dict(self.items[a], sizes=[]), dict(self.items[b]), {"source_item_id": "x", "url": "https://www.trendyol.com/x-p-1"}]
        res = quiet(escarpe.verify, rows, None)                     # order_api вызывает без query
        self.assertTrue(json.loads(self.cache.read_text(encoding="utf-8"))[b]["gone"])   # кэш по умолчанию (подменён)
        self.assertEqual(res[a]["sizes"][:2], ["40", "41"])
        self.assertEqual(res[a]["price_now"], 229.99)
        self.assertIsNone(res[b])
        self.assertNotIn("x", res)
        # та же цена, страница без «Prezzo regolare» — прежняя зачёркнутая цена строки остаётся
        res = quiet(escarpe.verify, [dict(self.items[a], price_now=229.99, price_old=279.99)], None)
        self.assertEqual((res[a]["price_now"], res[a]["price_old"], res[a]["discount_pct"]), (229.99, 279.99, 17.9))
        FakeSession.routes[self.items[a]["url"]] = FakeResp(429)
        self.assertEqual(quiet(escarpe.verify, rows[:2], None), {})    # блокировка — ничего не известно


class MergeTest(unittest.TestCase):
    def test_merge_listing(self):
        prev = {"1": {"brand": "Boss", "_sections": ["boss@uomo"]}, "2": {"brand": "Boss", "_sections": ["boss@borse"]},
                "3": {"brand": "Boss", "_sections": ["boss@old"]}, "4": {"brand": "Lacoste", "_sections": ["lacoste@uomo"]}}
        crawled = {"5": {"brand": "Boss", "_sections": ["boss@uomo"]}}
        stats = {"boss@uomo": {"complete": True}, "boss@borse": {"complete": False}}
        out = escarpe.merge_listing(prev, crawled, stats, {"boss@uomo", "boss@borse"}, ["Boss"])
        self.assertEqual(set(out), {"2", "5"})          # 1 — полный раздел без него; 3 — раздела больше нет; 4 — не тот бренд

    def test_resolve_gender(self):
        self.assertEqual(escarpe.resolve_gender({"_genders": ["men"]}, {"gender": "women"}), "men")
        self.assertIsNone(escarpe.resolve_gender({"_genders": ["men", "women"]}, {}))
        self.assertEqual(escarpe.resolve_gender({"_genders": []}, {"gender": "women"}), "women")
        self.assertIsNone(escarpe.resolve_gender({"_genders": []}, {"gender": None, "unisex": True}))


# ---------------------------------------------------------------- run.py, config, сервер, фото, утечки

class RunIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (run.DATA, run.SITE)
        run.DATA, run.SITE = self.tmp / "data", self.tmp / "site"
        run.DATA.mkdir()
        self.env = mock.patch.dict(os.environ, {"ORDERS_DB_PATH": str(self.tmp / "nope.sqlite"), "IMG_BASE": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        run.DATA, run.SITE = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_registry_and_config(self):
        self.assertEqual(ADAPTERS["escarpe_it"], ("sources.escarpe", {}, "IT"))
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertIn("escarpe_it", cfg["sources"])
        so = cfg["source_opts"]["escarpe_it"]
        brands = escarpe.parse_brands(so)
        self.assertEqual(brands["Michael Kors"], ["michael_kors", "michael_michael_kors"])
        self.assertEqual(brands["EA7"], ["ea7_emporio_armani"])
        self.assertEqual(set(cfg["source_filters"]["escarpe_it"]["brands"]), set(brands))
        self.assertEqual(cfg["source_filters"]["escarpe_it"]["discount_min"], 0)
        self.assertTrue(cfg["source_filters"]["escarpe_it"]["only_with_sizes"])
        for b in brands:                                           # вес бренда в «Рекомендуем» есть у всех
            self.assertTrue(any(run.brand_key(b) == run.brand_key(x) for k, v in cfg["brand_tier"].items()
                                if isinstance(v, list) for x in v), b)
        for sec in escarpe.parse_sections(so):
            self.assertTrue(escarpe.robots_ok(escarpe.listing_url(sec["path"], "michael_michael_kors", 2)), sec)

    def test_modes_locks_and_extras(self):
        self.assertEqual(run.collect_lock_name("escarpe_it", {"_mode": "listing"}), "escarpe_it-listing")
        self.assertEqual(run.collect_lock_name("escarpe_it", {"_mode": "pdp"}), "escarpe_it")
        self.assertEqual(run.collect_lock_name("trendyol", {"_mode": "listing"}), "trendyol-listing")
        args = SimpleNamespace(mode="pdp", trendyol_mode=None)
        self.assertEqual((run.source_mode("escarpe_it", args), run.source_mode("trendyol", args)), ("pdp", "pdp"))
        args = SimpleNamespace(mode=None, trendyol_mode="listing")
        self.assertEqual((run.source_mode("escarpe_it", args), run.source_mode("trendyol", args)), ("auto", "listing"))
        self.assertEqual(run.source_mode("escarpe_it", SimpleNamespace(trendyol_mode="auto")), "auto")   # старые вызовы
        state = {"items": {"AAAAAAA": {"source": "escarpe_it", "item_id": PDP_ID, "on_site": True}}, "sources": {}}
        cfg = {"source_opts": {"escarpe_it": {"pdp_hot_top": 0}}}
        ex = quiet(run.source_extras, "escarpe_it", cfg, state, "auto")
        self.assertEqual((ex["_mode"], ex["_warm"]), ("auto", {PDP_ID}))
        lk = sync_state.Lock(run.DATA / "collect_escarpe_it-listing.lock", "тест")
        self.assertTrue(lk.try_acquire())
        try:
            ex = quiet(run.source_extras, "escarpe_it", cfg, state, "auto")
            self.assertEqual(ex["_mode"], "pdp")                   # выдачу обходит другое задание — берём сохранённую
            self.assertTrue(ex["_listing_running"])
        finally:
            lk.release()
        self.assertEqual(quiet(run.source_extras, "escarpe_it", cfg, state, "listing"), {"_mode": "listing"})

    def test_collect_with_verify(self):
        from sources.base import Product
        keep = Product(source="escarpe_it", source_item_id="1", brand="Boss", title="Sneakers BOSS", category="Sneakers",
                       type="обувь", gender="men", price_now=100.0, price_old=None, currency="EUR", discount_pct=None,
                       sizes=["42"], colors=["Nero"], images=[], url="https://escarpe.it/p/sneakers-boss-x-0000000000001")
        prev = [dict(keep.to_dict()), dict(keep.to_dict(), source_item_id="2", url="https://escarpe.it/p/x-0000000000002")]

        def fake_fetch(query, **opts):
            escarpe.LAST_RUN.update(gone_ids=[], absence_means_gone=True)
            return [keep]

        def fake_verify(rows, query=None, **opts):
            return {str(r["source_item_id"]): None for r in rows}

        cfg = {"source_opts": {"escarpe_it": {}}, "source_filters": {"escarpe_it": {"brands": ["Boss"]}}, "sync": {}}
        with mock.patch.object(escarpe, "fetch", fake_fetch), mock.patch.object(escarpe, "verify", fake_verify):
            rows, info = quiet(run.collect, "escarpe_it", cfg, prev, {"1", "2"}, False, {"_mode": "pdp"})
        self.assertEqual([r["source_item_id"] for r in rows], ["1"])
        self.assertEqual(info["gone_ids"], ["2"])                  # пропал из выдачи и подтверждён по ссылке
        self.assertFalse(info["absence_means_gone"])
        self.assertTrue(run.raw_path("escarpe_it").is_file())


class BuildLeakTest(unittest.TestCase):
    """Сборка сайта из data/raw_escarpe_it.json со своим хостом фото: в публичных файлах ни escarpe, ни eobuwie/modivo,
    ни ссылок на магазин; ссылка и исходное название — только в закрытом site/admin/."""

    def setUp(self):
        import content
        import pricing
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (run.DATA, run.SITE, pricing.FX_CACHE, content.build, run.ROOT)
        run.DATA, run.SITE, run.ROOT = self.tmp / "data", self.tmp / "site", self.tmp
        run.DATA.mkdir()
        pricing.FX_CACHE = self.tmp / "data" / "fx.json"
        content.build = lambda *a, **k: None
        run._TOKMAP = None
        self.env = mock.patch.dict(os.environ, {"IMG_BASE": "https://img.example.uz", "IMG_SECRET": "s3cret-for-test",
                                                "IMG_MAP": "", "ORDERS_DB_PATH": str(self.tmp / "none.sqlite")})
        self.env.start()

    def tearDown(self):
        import content
        import pricing
        self.env.stop()
        run.DATA, run.SITE, pricing.FX_CACHE, content.build, run.ROOT = self.saved
        run._TOKMAP = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_public_files_do_not_reveal_source(self):
        import catalog_files as cf
        import deploy
        crawl = escarpe._Crawl(None, 1)
        for c in escarpe.parse_listing(LISTING_HTML)["cards"][:3]:
            quiet(crawl._add, c, "Boss", "boss", {"path": "uomo", "gender": "men"})
        e = escarpe._cache_entry(escarpe.parse_pdp(PDP_HTML))
        rows = [escarpe.to_product(rec, e).to_dict() for rec in crawl.items.values()]
        run.write_raw("escarpe_it", rows)
        cfg = {"sources": ["escarpe_it"], "filters": {"brands": [], "discount_min": 0},
               "source_filters": {"escarpe_it": {"brands": ["Boss"], "only_with_sizes": True}},
               "pricing": {"fx_manual": {"EUR": 14000.0}, "margin_pct": 25, "round_to_uzs": 10000,
                           "cargo_uzs_per_item": {"IT": 160000}},
               "source_opts": {}, "sync": {}, "site": {"name": "t"}}
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        args = SimpleNamespace(offline=True, if_changed=False, config=str(cfg_path), accept_drop=False, mode=None,
                               trendyol_mode=None)
        quiet(run.build, cfg, run.sync_cfg(cfg), set(), args)
        cat = cf.PublicCatalog(run.SITE)
        self.assertEqual(len(cat), 3)
        blob = "".join((run.SITE / f).read_text(encoding="utf-8") for f in cf.published_files(run.SITE))
        self.assertIsNone(re.search(r"escarpe|eobuwie|modivo|Stivaletti|50573316", blob, re.I))
        self.assertIsNone(deploy.SHOP_HOST_RE.search(blob.encode("utf-8")))
        self.assertIn("Ботинки", blob)
        admin = "".join(p.read_text(encoding="utf-8") for p in (run.SITE / "admin").glob("*.js"))
        self.assertIn("https://escarpe.it/p/", admin)               # ссылка на товар — только в закрытых данных


class ServerAndHostsTest(unittest.TestCase):
    SRV = ROOT / "server"

    def test_server_job(self):
        script = (self.SRV / "yurt-run.sh").read_text(encoding="utf-8")
        jobs = set(re.search(r"\n\s+(turkey-akinon\|[^)]*)\)", script).group(1).split("|"))
        self.assertIn("escarpe", jobs)
        body = script[script.index("\n  escarpe)"):]
        body = body[:body.index(";;")]
        self.assertIn("run.py --collect-only --only escarpe_it", body)
        self.assertIn("build_and_deploy", body)
        svc = (self.SRV / "systemd" / "yurt-escarpe.service").read_text(encoding="utf-8")
        self.assertRegex(svc, r"(?m)^ExecStart=.*yurt-run\.sh escarpe$")
        timer = (self.SRV / "systemd" / "yurt-escarpe.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* *:35:00", timer)                 # каждый час
        self.assertIn("Unit=yurt-escarpe.service", timer)
        setup = (self.SRV / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("yurt-escarpe.timer", re.search(r'^TIMERS="([^"]+)"', setup, re.M).group(1).split())
        self.assertIn("yurt-run.sh escarpe", (self.SRV / "crontab.example").read_text(encoding="utf-8"))

    def test_image_host_and_leak_markers(self):
        import channel
        import deploy
        import img_api
        self.assertEqual(img_api.host_rule("img.eobuwie.cloud"), ("https://escarpe.it/", 2.0))
        self.assertIsNone(img_api.host_rule("img.eobuwie.cloud.evil.example"))
        self.assertTrue(deploy.SHOP_HOST_RE.search(b'"https://img.eobuwie.cloud/product(x.jpg,jpg)/a.jpg"'))
        for word in ("escarpe.it", "eobuwie", "MODIVO"):
            self.assertTrue(deploy.UNSAFE_TEXT.search(word), word)
            self.assertTrue(channel.find_leak(f"Ботинки Boss — {word}"), word)
        self.assertIsNone(channel.find_leak("Ботинки, чёрные, натуральная кожа"))


if __name__ == "__main__":
    unittest.main()
