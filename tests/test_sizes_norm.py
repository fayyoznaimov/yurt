"""Ключи фильтра размеров (sizes_norm.py).

    python -m unittest tests.test_sizes_norm
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sizes_norm as sn  # noqa: E402


class FilterKeysTest(unittest.TestCase):
    def test_shoes(self):
        self.assertEqual(sn.filter_keys(["EU40/IT39", "40 ⅓", "40.5", "41,5", "EU38.5/IT38"], "обувь"),
                         ["38½", "40", "40⅓", "40½", "41½"])
        self.assertEqual(sn.filter_keys(["40", "EU40/IT39"], "обувь"), ["40"])       # один ключ без повторов
        self.assertEqual(sn.filter_keys(["35-37"], "обувь"), ["35–37"])

    def test_bottoms_waist(self):
        self.assertEqual(sn.filter_keys(["28W-32L", "28/32", "28W", "29"], "джинсы", "men"), ["W28", "W29"])
        self.assertEqual(sn.filter_keys(["25W-L33"], "джинсы", "women"), ["W25"])
        self.assertEqual(sn.filter_keys(["30", "31", "32"], "брюки", "men"), ["W30", "W31", "W32"])
        self.assertEqual(sn.filter_keys(["38", "40"], "брюки", "men"), ["W38", "W40"])       # мужской IT — от 44
        self.assertEqual(sn.filter_keys(["46", "48", "50"], "брюки", "men"), ["46", "48", "50"])
        self.assertEqual(sn.filter_keys(["38", "40", "42"], "брюки", "women"), ["38", "40", "42"])  # женский IT
        self.assertEqual(sn.filter_keys(["26", "28"], "брюки", "women"), ["W26", "W28"])
        self.assertEqual(sn.filter_keys(["27"], "юбки", "women"), ["W27"])
        self.assertEqual(sn.filter_keys(["46-6 Drop", "50R"], "брюки", "men"), ["46", "50"])
        self.assertEqual(sn.filter_keys(["S", "M", "XXL"], "шорты", "men"), ["S", "M", "XXL"])

    def test_waist_mode(self):
        self.assertTrue(sn.waist_mode(["29", "30"], "брюки", "women"))        # нечётное — талия
        self.assertFalse(sn.waist_mode(["40"], "брюки", "women"))
        self.assertTrue(sn.waist_mode(["34"], "брюки", "men"))
        self.assertFalse(sn.waist_mode(["44"], "брюки", "men"))
        self.assertFalse(sn.waist_mode(["30"], "рубашки", "men"))             # не низ

    def test_tailored(self):
        self.assertEqual(sn.filter_keys(["48-6 Drop", "50R", "48 ⅔", "52.5", "54"], "пиджаки и костюмы"),
                         ["48", "50", "52", "54"])
        self.assertEqual(sn.filter_keys(["XL", "52"], "куртки и пальто"), ["XL", "52"])

    def test_shirts_collar(self):
        self.assertEqual(sn.filter_keys(["39", "40", "41"], "рубашки", "men"), ["ворот 39", "ворот 40", "ворот 41"])
        self.assertEqual(sn.filter_keys(["38", "40", "42"], "рубашки", "men"), ["ворот 38", "ворот 40", "ворот 42"])
        self.assertEqual(sn.filter_keys(["38", "40", "42"], "рубашки", "women"), ["38", "40", "42"])
        self.assertEqual(sn.filter_keys(["46", "48", "50"], "рубашки", "men"), ["46", "48", "50"])
        self.assertTrue(sn.collar_mode(["37"], "women"))

    def test_underwear_socks_belts_letters(self):
        self.assertEqual(sn.filter_keys(["39_42", "35-38", "1 B", "II C", "5"], "нижнее бельё"),
                         ["5", "35–38", "39–42", "1 B", "II C"])
        self.assertEqual(sn.filter_keys(["90", "95", "Единый размер"], "аксессуары"), ["см 90", "см 95", "Единый размер"])
        self.assertEqual(sn.filter_keys(["57", "58"], "аксессуары"), ["57", "58"])           # не ремень — как есть
        self.assertEqual(sn.filter_keys(["XXL", "2XL", "XXXL", "3XL", "2XS", "xs", "S/M"], "футболки и поло"),
                         ["XXS", "XS", "S", "M", "XXL", "3XL"])
        self.assertEqual(sn.filter_keys(["--", "ONESIZE"], "сумки"), ["Единый размер"])
        self.assertEqual(sn.filter_keys([], "обувь"), [])
        self.assertEqual(sn.filter_keys(None, None), [])

    def test_order(self):
        keys = ["Единый размер", "см 90", "ворот 40", "W30", "40½", "39–42", "40", "XL", "S", "1 B", "3XL", "40⅓"]
        self.assertEqual(sn.sort_keys(keys),
                         ["S", "XL", "3XL", "39–42", "40", "40⅓", "40½", "W30", "ворот 40", "см 90", "1 B", "Единый размер"])

    def test_default_keys(self):
        self.assertEqual(sn.default_keys("EU40/IT39"), ["40"])
        self.assertEqual(sn.default_keys("28W-32L"), ["W28"])
        self.assertEqual(sn.default_keys("30/32"), ["W30"])
        self.assertEqual(sn.default_keys("48-6 Drop"), ["48"])
        self.assertEqual(sn.default_keys("XXXL"), ["3XL"])
        self.assertEqual(sn.default_keys("S/M"), ["S", "M"])
        self.assertEqual(sn.default_keys("39_42"), ["39–42"])
        self.assertEqual(sn.default_keys("--"), ["Единый размер"])
        self.assertEqual(sn.default_keys(""), [])

    def test_display_sizes_untouched(self):
        sizes = ["EU40/IT39", "40.5"]
        sn.filter_keys(sizes, "обувь")
        self.assertEqual(sizes, ["EU40/IT39", "40.5"])


if __name__ == "__main__":
    unittest.main()
