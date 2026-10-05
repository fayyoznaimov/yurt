"""Тексты карточки (describe.py): заголовок «вид, цвет, свойство» без бренда, группы цветов, слова для поиска,
размерная сетка.

    python -m unittest tests.test_describe
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import describe as ds  # noqa: E402

FORBIDDEN = re.compile(r"yoox|trendyol|akinon|dsmcdn|€|₺|\bTL\b|EUR|TRY|возврат", re.I)


def pc(title, ptype, color, fibre=None, gender="men", **attrs):
    """Товар Pierre Cardin (как в data/raw_pcardin_tr.json)."""
    a = {"filterable_detail_color": color, "filterable_product_base_type": attrs.pop("base", "")}
    if fibre:
        a["integration_fabric_fiber"] = fibre
    a.update(attrs)
    return {"source": "pcardin_tr", "brand": "Pierre Cardin", "title": title, "category": "", "type": ptype,
            "gender": gender, "sizes": ["S", "M", "L"], "country": "TR", "attrs": a}


def ty(title, ptype, attrs, gender="men", sizes=("M",)):
    """Товар Trendyol (свойства страницы товара)."""
    return {"source": "trendyol", "brand": "Cacharel", "title": title, "category": "", "type": ptype,
            "gender": gender, "sizes": list(sizes), "country": "TR", "attrs": attrs}


class TitleTest(unittest.TestCase):
    def test_examples_from_task(self):
        shirt = pc("Beyaz Slim Fit Uzun Kollu Gömlek", "рубашки", "Beyaz", "%100 Pamuk", base="Gömlek")
        self.assertEqual(ds.describe(shirt)["title"], "Рубашка, белая, slim fit")
        puffer = ty("Lacivert Şişme Mont", "куртки и пальто", {"Renk": "Lacivert", "Kapüşon": "Var"})
        self.assertEqual(ds.describe(puffer)["title"], "Пуховик, тёмно-синий, с капюшоном")
        loafer = pc("Kahverengi Deri Loafer Ayakkabı", "обувь", "Kahverengi", "%100 Deri (Leather)",
                    filterable_product_type="Loafer")
        self.assertEqual(ds.describe(loafer)["title"], "Лоферы, коричневые, натуральная кожа")

    def test_women_adjective_and_agreement(self):
        p = pc("Siyah Slim Fit Pantolon", "брюки", "Siyah", gender="women", base="Pantolon")
        self.assertEqual(ds.describe(p)["title"], "Женские брюки, чёрные, slim fit")
        bag = pc("Bordo Omuz Çantası", "сумки", "Bordo", gender="women", base="Omuz Çantası")
        self.assertEqual(ds.describe(bag)["title"], "Женская сумка через плечо, бордовая")
        hoodie = ty("Gri Kapüşonlu Sweatshirt", "толстовки", {"Renk": "Gri", "Kapüşon": "Var"})
        self.assertEqual(ds.describe(hoodie)["title"], "Худи, серое")      # «с капюшоном» уже в слове «Худи»

    def test_no_brand_no_invented_attrs(self):
        bare = pc("Lacivert Gömlek", "рубашки", None, base="Gömlek")
        self.assertEqual(ds.describe(bare)["title"], "Рубашка")             # нет данных — нет свойств
        only_color = pc("Lacivert Gömlek", "рубашки", "Lacivert", base="Gömlek")
        d = ds.describe(only_color)
        self.assertEqual(d["title"], "Рубашка, тёмно-синяя")
        self.assertNotIn("Pierre Cardin", d["title"])
        # описание — как раньше: вид + бренд + цвет
        self.assertTrue(d["description"].startswith("Рубашка Pierre Cardin тёмно-синего цвета"), d["description"])
        # смесь волокон в заголовок не идёт (только один материал)
        blend = pc("Beyaz Gömlek", "рубашки", "Beyaz", "%59 Pamuk %41 Poliester", base="Gömlek")
        self.assertEqual(ds.describe(blend)["title"], "Рубашка, белая")
        # синтетика из одного волокна — тоже нет (только натуральные материалы)
        poly = pc("Siyah Mont", "куртки и пальто", "Siyah", "%100 Polyester", base="Mont")
        self.assertEqual(ds.describe(poly)["title"], "Куртка, чёрная")

    def test_two_attrs_without_color_and_weak_pattern(self):
        p = ty("Gömlek", "рубашки", {"Kalıp": "Slim Fit", "Desen": "Çizgili"})
        self.assertEqual(ds.describe(p)["title"], "Рубашка, slim fit, в полоску")
        t = ty("Tişört", "футболки и поло", {"Renk": "Siyah", "Desen": "Desenli", "Kalıp": "Slim Fit"})
        self.assertEqual(ds.describe(t)["title"], "Футболка, чёрная, slim fit")  # «с узором» — только если нечего больше
        t2 = ty("Tişört", "футболки и поло", {"Renk": "Siyah", "Desen": "Desenli"})
        self.assertEqual(ds.describe(t2)["title"], "Футболка, чёрная, с узором")

    def test_color_agree(self):
        self.assertEqual(ds.color_agree("тёмно-синий", "f"), "тёмно-синяя")
        self.assertEqual(ds.color_agree("тёмно-синий", "pl"), "тёмно-синие")
        self.assertEqual(ds.color_agree("голубой", "n"), "голубое")
        self.assertEqual(ds.color_agree("голубой", "pl"), "голубые")
        self.assertEqual(ds.color_agree("белый", "f"), "белая")
        self.assertEqual(ds.color_agree("хаки", "f"), "цвета хаки")
        self.assertEqual(ds.color_agree("фуксия", "m"), "цвета фуксии")
        self.assertEqual(ds.color_agree("серый меланж", "f"), "серый меланж")
        self.assertIsNone(ds.color_agree(None, "m"))

    def test_titles_have_no_source_words(self):
        p = ty("Lacivert Şişme Mont", "куртки и пальто", {"Renk": "Lacivert", "Kapüşon": "Var", "Kalıp": "Slim"})
        d = ds.describe(p)
        for k in ("title", "description"):
            self.assertIsNone(FORBIDDEN.search(d[k]), d[k])


class ColorGroupTest(unittest.TestCase):
    def test_groups_by_last_word(self):
        g = {name: i for i, (name, _) in enumerate(ds.COLOR_GROUPS)}
        self.assertEqual(len(ds.COLOR_GROUPS), 12)
        cases = {"чёрный": "чёрный", "тёмно-синий": "синий", "серо-бежевый": "бежевый", "сине-зелёный": "зелёный",
                 "молочный": "белый", "хаки": "зелёный", "кэмел меланж": "коричневый", "бордовый": "красный",
                 "фуксия": "розовый", "горчичный": "жёлтый/оранжевый", "сиреневый": "фиолетовый",
                 "разноцветный": "разноцветный", "голубой": "синий", "серебристый": "серый",
                 "светло-зелëный": "зелёный"}
        for color, group in cases.items():
            self.assertEqual(ds.color_group(color), g[group], color)
        self.assertEqual(ds.color_group("прозрачный"), -1)
        self.assertEqual(ds.color_group(None), -1)
        self.assertEqual(ds.color_group("неведомый"), -1)

    def test_every_known_color_has_group(self):
        colors = set(ds.COLORS.values())
        no_group = {c for c in colors if ds.color_group(c) < 0}
        self.assertEqual(no_group, {"прозрачный"})


class KeywordsTest(unittest.TestCase):
    def test_keywords_from_card(self):
        card = {"color": "молочный", "composition": "95% хлопок, 5% эластан",
                "details": ["Приталенный крой (slim fit)", "В полоску", "С капюшоном", "Застёжка на молнию",
                            "Длинный рукав"]}
        kw = ds.keywords(card)
        self.assertEqual(kw, ["молочн", "бел", "хлоп", "slim", "полос", "капюшон"])   # не больше KW_MAX
        self.assertLessEqual(len(kw), ds.KW_MAX)
        self.assertTrue(all(k == k.lower() and "ё" not in k for k in kw))

    def test_keywords_fibres_and_minor_parts(self):
        kw = ds.keywords({"color": "чёрный", "composition": "70% шерсть, 25% кашемир, 5% полиамид", "details": []})
        self.assertEqual(kw, ["черн", "шерст", "кашемир"])          # 5% полиамида — не основа поиска
        kw = ds.keywords({"color": "белый", "composition": "лён", "details": []})
        self.assertEqual(kw, ["бел", "лен", "льн"])
        self.assertEqual(ds.keywords({"color": None, "composition": None, "details": None}), [])

    def test_color_keywords(self):
        self.assertEqual(ds.color_keywords("тёмно-синий"), ["син"])
        self.assertEqual(ds.color_keywords("хаки"), ["хаки", "зелен"])
        self.assertEqual(ds.color_keywords("абрикосовый"), ["абрикосов", "оранжев"])
        self.assertEqual(ds.color_keywords("горчичный"), ["горчичн", "желт"])
        self.assertEqual(ds.color_keywords("прозрачный"), [])


class SizeSystemTest(unittest.TestCase):
    def test_waist_vs_italian(self):
        self.assertEqual(ds.size_system({"type": "брюки", "gender": "women", "sizes": ["38", "40"], "country": "IT"}), "IT")
        self.assertEqual(ds.size_system({"type": "брюки", "gender": "men", "sizes": ["30", "32"], "country": "IT"}), "W")
        self.assertEqual(ds.size_system({"type": "джинсы", "gender": "women", "sizes": ["28W-32L"], "country": "IT"}), "W")
        self.assertEqual(ds.size_system({"type": "джинсы", "gender": "women", "sizes": ["25", "27"], "country": "IT"}), "W")
        self.assertEqual(ds.size_system({"type": "брюки", "gender": "men", "sizes": ["46", "48"], "country": "TR"}), "EU")
        self.assertEqual(ds.size_system({"type": "обувь", "sizes": ["40"]}), "EU")
        self.assertEqual(ds.size_system({"type": "рубашки", "sizes": ["S", "M"]}), "INT")


if __name__ == "__main__":
    unittest.main()
