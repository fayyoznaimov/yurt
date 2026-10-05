"""Проверка раскладки каталога частями (catalog_files.py): запись → чтение без потерь, порог старого формата,
уборка устаревших частей, закрытые данные отдельно.

    python -m unittest tests.test_catalog_files
"""
from __future__ import annotations

import json
import re
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

    def _shards(self):
        m = cf.load_manifest(self.site)
        return m, [cf.unwrap((self.site / "data" / s["file"]).read_bytes()) for s in m["shards"]]

    def test_head_composition(self):
        prods, admin = make_products(9000)
        types = ["рубашки", "брюки", "обувь", "куртки и пальто", None]
        for i, p in enumerate(prods):
            p["type"] = types[i % 5]
            p["r"] = (i * 7919) % 9000 + 1                      # уникальные r вразнобой
        cf.write_site(self.site, {}, {}, prods, admin)
        m, shards = self._shards()
        head = set(shards[0]["id"])
        self.assertTrue(m["shards"][0].get("head"))
        self.assertTrue(all(s.get("b") for s in m["shards"][1:]))
        live = [p for p in prods if p["in_stock"] is not False]
        by_r = sorted(prods, key=lambda p: (p["in_stock"] is False, -p["r"]))
        self.assertTrue({p["id"] for p in by_r[:cf.HEAD_TOP]} <= head)
        groups = {}
        for p in sorted(live, key=lambda p: -p["r"]):
            groups.setdefault((p["type"] or "", p["gender"] or ""), []).append(p["id"])
        for g, ids in groups.items():
            self.assertTrue(set(ids[:cf.HEAD_PER_GROUP]) <= head, g)
        newest = sorted(live, key=lambda p: p["first_seen"], reverse=True)
        cutoff = newest[cf.HEAD_NEW - 1]["first_seen"]
        self.assertTrue({p["id"] for p in live if p["first_seen"] > cutoff} <= head)
        cheap = sorted(live, key=lambda p: p["price_uzs"])
        self.assertTrue({p["id"] for p in live if p["price_uzs"] < cheap[cf.HEAD_PRICE - 1]["price_uzs"]} <= head)
        dear = sorted(live, key=lambda p: -p["price_uzs"])
        self.assertTrue({p["id"] for p in live if p["price_uzs"] > dear[cf.HEAD_PRICE - 1]["price_uzs"]} <= head)
        disc = sorted(live, key=lambda p: -(p["discount_pct"] or 0))
        self.assertTrue({p["id"] for p in live if (p["discount_pct"] or 0) > (disc[cf.HEAD_DISC - 1]["discount_pct"] or 0)} <= head)
        self.assertLess(len(head), 9000)

    def test_r_column_order_and_decode(self):
        prods, admin = make_products(3000)
        for i, p in enumerate(prods):
            p["r"] = 3000 - ((i * 37) % 3000)                    # r = N - место, места вразнобой
        cf.write_site(self.site, {}, {}, prods, admin)
        m, shards = self._shards()
        for k, sh in enumerate(shards):
            self.assertEqual(len(sh["r"]), sh["n"])
            if k == 0:                                          # голова — в порядке витрины: r ↓, распроданные в конце
                out = set(sh["out"])
                keys = [(i in out, -sh["r"][i]) for i in range(sh["n"])]
                self.assertEqual(keys, sorted(keys))
            else:                                               # остальные — по бренду/типу/названию (сжатие)
                bt = [(m["dict"]["brand"][b], m["dict"]["type"][ty], sh["td"][t] if "td" in sh else t)
                      for b, ty, t in zip(sh["b"], sh["ty"], sh["t"])]
                self.assertEqual(bt, sorted(bt, key=lambda x: tuple(v or "" for v in x)))
        c = cf.PublicCatalog(self.site)
        want = {p["id"]: p["r"] for p in prods}
        self.assertEqual(c.ranks(), want)
        self.assertEqual(c.rank(prods[5]["id"]), prods[5]["r"])
        self.assertNotIn("r", c[prods[5]["id"]])                # карточка — только PUBLIC_FIELDS
        self.assertNotIn("_r", c.index_row(prods[5]["id"]))
        # сортировка по r по убыванию воспроизводит порядок ranking (здесь — заданные места)
        allr = sorted(((r, pid) for sh in shards for pid, r in zip(sh["id"], sh["r"])), reverse=True)
        self.assertEqual([pid for _, pid in allr], [p["id"] for p in sorted(prods, key=lambda p: -p["r"])])

    def test_r_from_list_order_without_field(self):
        prods, admin = make_products(500)
        cf.write_site(self.site, {}, {}, prods, admin)
        c = cf.PublicCatalog(self.site)
        self.assertEqual(c.rank(prods[0]["id"]), 500)           # первый в списке — самый большой r
        self.assertEqual(c.rank(prods[-1]["id"]), 1)
        legacy = json.loads((self.site / "products.json").read_text(encoding="utf-8"))
        self.assertEqual(legacy["products"][0]["r"], 500)

    def test_decode_old_shards_without_r(self):
        prods, admin = make_products(400)
        cf.write_site(self.site, {}, {}, prods, admin)
        m = cf.load_manifest(self.site)
        for s in m["shards"]:                                   # часть старой сборки — без столбца r
            path = self.site / "data" / s["file"]
            obj = cf.unwrap(path.read_bytes())
            obj.pop("r", None)
            path.write_bytes(cf.wrap(f"i/{obj['k']}", obj))
        c = cf.PublicCatalog(self.site)
        self.assertEqual(len(c), 400)
        self.assertEqual(c.ranks(), {})
        self.assertIsNone(c.rank(prods[0]["id"]))
        self.assertEqual(c[prods[3]["id"]], {k: prods[3].get(k) for k in cf.PUBLIC_FIELDS})

    def test_cg_zk_kw_columns(self):
        prods, admin = make_products(700)
        sizes = [(["EU40/IT39", "40.5", "41 ⅓"], "обувь", "women"), (["28W-32L", "29", "30/32"], "джинсы", "men"),
                 (["39", "40", "41"], "рубашки", "men"), (["XXL", "2XL", "S/M"], "футболки и поло", "men"),
                 (["90", "95"], "аксессуары", None), (["38", "40", "42"], "брюки", "women")]
        colors = ["молочный", "тёмно-синий", "хаки", None, "серый меланж", "прозрачный"]
        for i, p in enumerate(prods):
            p["sizes"], p["type"], p["gender"] = sizes[i % len(sizes)]
            p["color"] = colors[i % len(colors)]
            p["composition"] = ["95% хлопок, 5% эластан", "натуральная кожа", None][i % 3]
            p["details"] = [["Приталенный крой (slim fit)", "В полоску"], ["С капюшоном"], []][i % 3]
        prods[1]["zk"] = ["W99"]                                 # готовые ключи из run.py берутся как есть
        prods[2]["kw"] = ["особое"]
        cf.write_site(self.site, {}, {}, prods, admin, legacy_max=100)
        m, shards = self._shards()
        D = m["dict"]
        self.assertEqual([g["name"] for g in D["cgroup"]], [n for n, _ in cf.describe.COLOR_GROUPS])
        self.assertTrue(all(re.fullmatch(r"#[0-9a-f]{6}", g["hex"]) for g in D["cgroup"]))
        self.assertEqual(D["zk"], [None] + cf.sizes_norm.sort_keys(D["zk"][1:]))   # номер ключа = порядок показа
        self.assertEqual(len(D["szk"]), len(D["size"]))
        self.assertEqual(len(D["ckw"]), len(D["color"]))
        for sh in shards:
            for col in ("cg", "zk", "kw"):
                self.assertEqual(len(sh[col]), sh["n"], col)
            self.assertTrue(any(z == 0 for z in sh["zk"]))      # ключи как у размеров — 0
        c = cf.PublicCatalog(self.site)
        want_cg = {"молочный": 1, "тёмно-синий": 3, "хаки": 6, None: -1, "серый меланж": 2, "прозрачный": -1}
        for i, p in enumerate(prods):
            e = c.extras(p["id"])
            exp_zk = p["zk"] if "zk" in p else cf.sizes_norm.filter_keys(p["sizes"], p["type"], p["gender"])
            exp_kw = cf.describe.color_keywords(p["color"]) + p["kw"] if "kw" in p else cf.describe.keywords(p)
            self.assertEqual(e["zk"], cf.sizes_norm.sort_keys(exp_zk), p["id"])
            self.assertEqual(sorted(e["kw"]), sorted(exp_kw), p["id"])
            self.assertEqual(e["cg"], want_cg[p["color"]], p["id"])
            self.assertEqual(c[p["id"]], {k: p.get(k) for k in cf.PUBLIC_FIELDS})   # карточка не изменилась
            self.assertNotIn("_zk", c.index_row(p["id"]))
        e = c.extras(prods[0]["id"])
        self.assertEqual(e["zk"], ["40", "40½", "41⅓"])
        self.assertEqual(e["kw"], ["молочн", "бел", "хлоп", "slim", "полос"])
        self.assertEqual(c.extras(prods[7]["id"])["zk"], ["W28", "W29", "W30"])
        self.assertEqual(c.extras(prods[8]["id"])["zk"], ["ворот 39", "ворот 40", "ворот 41"])
        self.assertEqual(c.extras(prods[3]["id"])["zk"], ["S", "M", "XXL"])
        self.assertEqual(c.extras(prods[4]["id"])["zk"], ["см 90", "см 95"])
        self.assertEqual(c.extras(prods[5]["id"])["zk"], ["38", "40", "42"])
        # счётчики фильтров
        f = m["facets"]
        live = [p for p in prods if p["in_stock"] is not False]
        self.assertEqual(sum(a for _, a in f["colors"]["all"]), sum(1 for p in live if want_cg[p["color"]] >= 0))
        self.assertEqual(f["colors"]["all"][1][0], sum(1 for p in prods if p["color"] == "молочный"))
        shoe = {D["zk"][k]: (n, a) for k, n, a in f["zk"]["обувь"]}
        self.assertEqual(shoe["40½"][0], sum(1 for p in prods if p["type"] == "обувь"))
        keys = [D["zk"][k] for k, _, _ in f["zk"]["all"]]
        self.assertEqual(keys, cf.sizes_norm.sort_keys(keys))
        # публичные файлы без закупки
        for fn in cf.published_files(self.site):
            data = (self.site / fn).read_bytes()
            self.assertFalse(any(mk in data for mk in cf.ADMIN_LEAK_MARKERS), fn)

    def test_legacy_products_carry_extras(self):
        prods, admin = make_products(300)
        cf.write_site(self.site, {}, {}, prods, admin)
        legacy = json.loads((self.site / "products.json").read_text(encoding="utf-8"))
        p0 = legacy["products"][0]
        self.assertTrue({"r", "zk", "kw", "cg"} <= set(p0))
        self.assertFalse(any(k.startswith("_") for k in p0))
        c = cf.PublicCatalog(self.site)
        self.assertEqual(c[prods[1]["id"]], {k: prods[1].get(k) for k in cf.PUBLIC_FIELDS})
        self.assertEqual(c.extras(prods[1]["id"])["zk"], ["S", "M", "XL"])

    def test_decode_old_shards_without_extras(self):
        prods, admin = make_products(400)
        cf.write_site(self.site, {}, {}, prods, admin)
        m = cf.load_manifest(self.site)
        for s in m["shards"]:                                   # часть старой сборки — без cg/zk/kw
            path = self.site / "data" / s["file"]
            obj = cf.unwrap(path.read_bytes())
            for k in ("cg", "zk", "kw"):
                obj.pop(k, None)
            path.write_bytes(cf.wrap(f"i/{obj['k']}", obj))
        c = cf.PublicCatalog(self.site)
        self.assertEqual(len(c), 400)
        self.assertIsNone(c.extras(prods[0]["id"]))
        self.assertEqual(c[prods[3]["id"]], {k: prods[3].get(k) for k in cf.PUBLIC_FIELDS})


if __name__ == "__main__":
    unittest.main()
