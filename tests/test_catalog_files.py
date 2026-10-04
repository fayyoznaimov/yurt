"""Проверка раскладки каталога частями (catalog_files.py): запись → чтение без потерь, порог старого формата,
уборка устаревших частей, закрытые данные отдельно.

    python -m unittest tests.test_catalog_files
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import catalog_files as cf  # noqa: E402


def make_products(n: int) -> tuple[list[dict], dict]:
    prods, admin = [], {"_meta": {"rates": {"EUR": 13000.0}}}
    for i in range(n):
        pid = f"T{i:06d}"
        prods.append({
            "id": pid, "brand": ["Boss", "Tod's", "Paul & Shark"][i % 3], "title": f"Рубашка {i % 7}",
            "type": "рубашки" if i % 4 else None, "gender": ["men", "women", None][i % 3], "origin": "IT" if i % 2 else "TR",
            "price_uzs": 100000 + (i % 50) * 10000, "discount_pct": None if i % 5 == 0 else round(10 + i % 70 + 0.5 * (i % 2), 1),
            "sizes": ["M", "S", "XL"] if i % 6 else [], "sizes_out": ["L"] if i % 9 == 0 else [], "size_system": "INT",
            "color": "белый" if i % 3 else None, "composition": "100% хлопок", "details": ["Прямой крой"] if i % 2 else [],
            "description": f"Описание {i}", "images": [f"https://cdn.example.com/a/{i % 13}/{pid}_{k}.jpg?w=400" for k in range(i % 4)],
            "in_stock": i % 50 != 7, "fetched_at": "2026-10-03T09:44:14Z", "first_seen": f"2026-10-0{1 + i % 3}T1{i % 10}:2{i % 6}:00Z",
        })
        admin[pid] = {"source": "yoox", "source_item_id": str(i), "url": f"https://shop/{i}", "cost_uzs": 50000, "margin_uzs": 50000}
    return prods, admin


class CatalogFilesTest(unittest.TestCase):
    def setUp(self):
        self.site = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.site, ignore_errors=True)

    def test_fnv_matches_js_hash32(self):
        self.assertEqual(cf.fnv1a("a"), 0xE40C292C)          # FNV-1a 32 — то же, что hash32() в index.html
        self.assertEqual(cf.fnv1a(""), 2166136261)

    def test_roundtrip_and_legacy(self):
        prods, admin = make_products(1200)
        st = cf.write_site(self.site, {"generated_at": "x", "by_brand": {"Boss": {"count": 1}}}, {"name": "t"}, prods, admin)
        self.assertTrue(st["legacy"])
        self.assertTrue((self.site / "products.json").is_file())
        c = cf.PublicCatalog(self.site)
        self.assertEqual(len(c), 1200)
        self.assertNotIn("by_brand", c.summary)                 # сводка по брендам — только в закрытых файлах
        for p in prods:
            self.assertEqual(c[p["id"]], {k: p.get(k) for k in cf.PUBLIC_FIELDS}, p["id"])
        a = cf.AdminCatalog(self.site)
        self.assertEqual(a.get("T000005"), admin["T000005"])
        self.assertIn("by_brand", a.info)
        # в публичных частях нет закупочных полей
        for f in cf.published_files(self.site):
            data = (self.site / f).read_bytes()
            self.assertFalse(any(m in data for m in cf.ADMIN_LEAK_MARKERS), f)

    def test_big_catalog_stub_and_stale_cleanup(self):
        prods, admin = make_products(600)
        cf.write_site(self.site, {}, {}, prods, admin, legacy_max=100, admin_combined_max=100)
        old = set(cf.published_files(self.site))
        self.assertFalse((self.site / "products.json").exists())
        self.assertFalse((self.site / "products-admin.js").exists())
        stub = (self.site / "products.js").read_text(encoding="utf-8")
        self.assertIn("window.DEALS_MANIFEST", stub)
        self.assertTrue((self.site / "admin" / "meta.js").is_file())
        prods[0]["price_uzs"] += 10000                          # цена поменялась — новая часть индекса
        cf.write_site(self.site, {}, {}, prods, admin, legacy_max=100, admin_combined_max=100)
        new = set(cf.published_files(self.site))
        on_disk = {p.relative_to(self.site).as_posix() for p in (self.site / "data").rglob("*") if p.is_file()}
        self.assertEqual(on_disk, new)                          # устаревших частей на диске нет
        self.assertTrue(old - new)
        self.assertEqual(cf.PublicCatalog(self.site)[prods[0]["id"]]["price_uzs"], prods[0]["price_uzs"])

    def test_head_has_top_discounts(self):
        prods, admin = make_products(5000)
        prods.sort(key=lambda p: (p["in_stock"] is False, -(p["discount_pct"] or 0), p["price_uzs"]))
        cf.write_site(self.site, {}, {}, prods, admin)
        m = cf.load_manifest(self.site)
        head = cf.unwrap((self.site / "data" / m["shards"][0]["file"]).read_bytes())
        top = {p["id"] for p in prods[:cf.HEAD_DISC]}
        self.assertTrue(top <= set(head["id"]))
        self.assertTrue(all(s.get("b") for s in m["shards"][1:]))


if __name__ == "__main__":
    unittest.main()
