"""Порядок «Рекомендуем» (ranking.py): части оценки, ходовые размеры, сезон, перемешивание брендов, r.

    python -m unittest tests.test_ranking
"""
from __future__ import annotations

import sys
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ranking as rk  # noqa: E402

CFG = {"brand_tier": {"_help": "x", "default": 0.5, "1.0": ["Gucci", "Dolce&Gabbana"], "0.8": ["Jacob Cohën"],
                      "0.35": ["Pierre Cardin"]}}


def row(i=0, **kw):
    r = {"id": f"P{i:05d}", "brand": "Boss", "title": f"Рубашка Boss {i}", "type": "рубашки", "gender": "men",
         "price_uzs": 2_000_000, "discount_pct": 50.0, "sizes": ["M", "L", "XL", "XXL"],
         "images": ["a.jpg", "b.jpg", "c.jpg"], "in_stock": True}
    r.update(kw)
    return r


def windows_ok(rows, order, window=rk.WINDOW, max_brand=rk.MAX_BRAND, max_title=rk.MAX_TITLE) -> bool:
    for s in range(0, max(1, len(order) - window + 1)):
        win = [rows[i] for i in order[s:s + window]]
        b = Counter(rk.norm_key(r["brand"]) for r in win)
        t = Counter((rk.norm_key(r["brand"]), r["title"].lower()) for r in win)
        if (b and max(b.values()) > max_brand) or (t and max(t.values()) > max_title):
            return False
    return True


class PartsTest(unittest.TestCase):
    def test_norm_key(self):
        self.assertEqual(rk.norm_key("Dolce & Gabbana"), rk.norm_key("Dolce&Gabbana"))
        self.assertEqual(rk.norm_key("Jacob Cohën"), rk.norm_key("JACOB COHEN"))
        self.assertEqual(rk.norm_key("U.S. Polo Assn."), "uspoloassn")
        self.assertEqual(rk.norm_key("EA7"), "ea7")

    def test_tiers_from_config(self):
        r = rk.Ranker.from_config(CFG)
        self.assertEqual(r.tier("Gucci"), 1.0)
        self.assertEqual(r.tier("dolce & gabbana"), 1.0)
        self.assertEqual(r.tier("Jacob Cohen"), 0.8)
        self.assertEqual(r.tier("Pierre Cardin"), 0.35)
        self.assertEqual(r.tier("Неизвестный"), 0.5)
        tiers, default = rk.parse_tiers({"Gucci": 0.9, "default": 0.4})       # плоский вид тоже понимается
        self.assertEqual((tiers["gucci"], default), (0.9, 0.4))

    def test_real_config_has_all_tiers(self):
        r = rk.Ranker.from_config()                                          # config.json проекта
        self.assertEqual(r.tier("Gucci"), 1.0)
        self.assertEqual(r.tier("Boss"), 0.8)
        self.assertEqual(r.tier("EA7"), 0.6)
        self.assertEqual(r.tier("U.S. Polo Assn."), 0.45)
        self.assertEqual(r.tier("Pierre Cardin"), 0.35)

    def test_disc_and_price(self):
        self.assertEqual(rk.disc_n(None), 0)
        self.assertEqual(rk.disc_n(20), 0)
        self.assertAlmostEqual(rk.disc_n(45), 0.5)
        self.assertEqual(rk.disc_n(90), 1)
        self.assertEqual(rk.price_band(699_999), 0.3)
        self.assertEqual(rk.price_band(700_000), 0.7)
        self.assertEqual(rk.price_band(1_500_000), 1.0)
        self.assertEqual(rk.price_band(6_000_000), 1.0)
        self.assertEqual(rk.price_band(8_000_000), 0.8)
        self.assertEqual(rk.price_band(13_000_000), 0.5)

    def test_season(self):
        self.assertEqual(rk.season_weight(10, "куртки и пальто"), 1.0)
        self.assertEqual(rk.season_weight(11, "шорты"), 0.1)
        self.assertEqual(rk.season_weight(7, "шорты"), 1.0)
        self.assertLess(rk.season_weight(7, "куртки и пальто"), 0.3)
        self.assertEqual(rk.season_weight(1, "куртки и пальто"), 1.0)
        self.assertEqual(rk.season_weight(datetime(2026, 10, 5), "футболки и поло"), 0.3)
        self.assertEqual(rk.season_weight(4, None), rk.SEASON_DEFAULT)
        for m in range(1, 13):                                               # таблица на каждый месяц
            self.assertIn(m, rk.SEASON)

    def test_popular_sizes(self):
        P = rk.popular_size
        self.assertTrue(P("M", "men", "рубашки"))
        self.assertTrue(P("2XL", "men", "рубашки"))
        self.assertFalse(P("S", "men", "рубашки"))
        self.assertTrue(P("S", "women", "платья"))
        self.assertFalse(P("XL", "women", "платья"))
        self.assertTrue(P("L/XL", "men", "футболки и поло"))
        self.assertTrue(P("50", "men", "пиджаки и костюмы"))
        self.assertFalse(P("56", "men", "пиджаки и костюмы"))
        self.assertTrue(P("50-6 Drop", "men", "пиджаки и костюмы"))
        self.assertTrue(P("42", "women", "платья"))
        self.assertFalse(P("38", "women", "платья"))
        self.assertTrue(P("42.5", "men", "обувь"))
        self.assertTrue(P("EU41/IT40", "men", "обувь"))
        self.assertFalse(P("45", "men", "обувь"))
        self.assertTrue(P("38 ⅔", "women", "обувь"))
        self.assertFalse(P("36", "women", "обувь"))
        self.assertTrue(P("32W-34L", "men", "джинсы"))
        self.assertTrue(P("W33", "men", "джинсы"))
        self.assertTrue(P("33/32", "men", "джинсы"))
        self.assertTrue(P("31", "men", "джинсы"))                            # просто 31 у джинсов — тоже W
        self.assertFalse(P("W30", "men", "джинсы"))
        self.assertTrue(P("28", "women", "джинсы"))
        self.assertFalse(P("32", "women", "джинсы"))
        self.assertTrue(P("50", "men", "джинсы"))                            # итальянский размер джинсов
        self.assertTrue(P("S", None, "рубашки"))                             # пол неизвестен — мужской или женский
        self.assertFalse(P("M", "kids", "рубашки"))

    def test_size_n(self):
        self.assertEqual(rk.size_n(row(sizes=[])), 0)
        self.assertEqual(rk.size_n(row(sizes=["Единый размер"], type="сумки")), 1.0)
        self.assertEqual(rk.size_n(row(sizes=["M", "S"])), 0.5)              # 2/4 × ходовой
        self.assertEqual(rk.size_n(row(sizes=["XS", "S", "3XL", "4XL"])), 0.5)  # 4/4 × не ходовой
        self.assertEqual(rk.size_n(row(sizes=["S", "M", "L", "XL", "XXL"])), 1.0)

    def test_score(self):
        r = rk.Ranker.from_config(CFG)
        self.assertEqual(r.score(row(in_stock=False)), 0)
        best = row(brand="Gucci", type="куртки и пальто", discount_pct=80)
        self.assertAlmostEqual(r.score(best, 10), 1.0)
        x = row(brand="Pierre Cardin", discount_pct=45, price_uzs=500_000, sizes=["S", "XS"], images=["a"])
        want = 0.30 * 0.5 + 0.25 * 0.35 + 0.20 * (2 / 4 * 0.5) + 0.10 * 0.3 + 0.10 * 0.7 + 0.05 * 0.5
        self.assertAlmostEqual(r.score(x, 10), want, places=5)
        self.assertEqual(rk.photos_n({"_n_img": 4}), 1.0)                   # число фото из индекса каталога
        for v in (r.score(row(i, discount_pct=d), m) for i, d, m in ((1, 0, 1), (2, 99, 7), (3, None, 12))):
            self.assertTrue(0 <= v <= 1)


class DiversifyTest(unittest.TestCase):
    def make(self):
        rows = [row(i, brand="Pierre Cardin", title="Рубашка Pierre Cardin", discount_pct=90) for i in range(60)]
        rows += [row(100 + i, brand=f"Brand{i % 20}", title=f"Вещь {i % 7}", discount_pct=30 + i % 40)
                 for i in range(240)]
        rows += [row(900 + i, in_stock=False) for i in range(5)]
        return rows

    def test_window_limits_and_positions(self):
        rows = self.make()
        r = rk.Ranker.from_config(CFG)
        pos = r.positions(rows)
        self.assertEqual(sorted(pos), list(range(len(rows))))                # перестановка
        order = rk.order_from_positions(pos)
        live = [i for i in order if rows[i]["in_stock"]]
        self.assertTrue(windows_ok(rows, live[:200]))                       # пока есть из чего выбирать — строго
        first = [rows[i] for i in order[:48]]
        self.assertLessEqual(sum(p["brand"] == "Pierre Cardin" for p in first), 4)
        self.assertLessEqual(max(Counter((p["brand"], p["title"]) for p in first).values()), 2)
        self.assertTrue(all(not rows[i]["in_stock"] for i in order[-5:]))   # распроданные — в конце
        self.assertEqual(pos, r.positions(rows))                            # детерминированно

    def test_identical_titles_spread(self):
        # 30 одинаковых «Рубашка Pierre Cardin» с лучшей скидкой: в окне 48 — не больше 2 одинаковых
        rows = [row(i, brand="Pierre Cardin", title="Рубашка Pierre Cardin", discount_pct=90) for i in range(30)]
        rows += [row(100 + i, brand=f"Brand{i % 20}", title=f"Вещь {i}", discount_pct=40) for i in range(200)]
        order = rk.order_from_positions(rk.diversify(rows, [0.9] * 30 + [0.5] * 200))
        first = [rows[i] for i in order[:48]]
        self.assertEqual(sum(p["title"] == "Рубашка Pierre Cardin" for p in first), 2)
        self.assertTrue(windows_ok(rows, order[:200]))

    def test_tail_relaxes(self):
        rows = [row(i, brand="Pierre Cardin", title=f"Рубашка {i % 3}") for i in range(20)]
        scores = [1 - i / 100 for i in range(20)]
        pos = rk.diversify(rows, scores)
        self.assertEqual(sorted(pos), list(range(20)))                     # все на месте, хоть ограничение и не выполнить

    def test_ranks(self):
        rows = self.make()
        r = rk.Ranker.from_config(CFG)
        ranks = r.ranks(rows)
        self.assertEqual(sorted(ranks), list(range(1, len(rows) + 1)))
        by_r = sorted(range(len(rows)), key=lambda i: -ranks[i])
        self.assertEqual(by_r, rk.order_from_positions(r.positions(rows)))
        self.assertEqual(rk.ranks(rows, cfg=CFG), ranks)


if __name__ == "__main__":
    unittest.main()
