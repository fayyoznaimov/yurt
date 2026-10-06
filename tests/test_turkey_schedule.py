"""Ежечасное наличие Турции: карта сайта Akinon раз в сутки (полезные категории — каждый час), Trendyol — ярусы
проверки размеров (hot / warm / cold), ночное окно выдачи, режимы listing / pdp, бюджет времени; run.py — сбор без
сборки (--collect-only, свои замки источников), сборка только при изменениях (--if-changed), горячие товары из базы
заказов; файлы сервера (systemd, yurt-run.sh, setup.sh) согласованы и ничего не берут с yoox.com. Сети нет:
страницы магазинов подменены.

    python -m unittest tests.test_turkey_schedule
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import run  # noqa: E402
import sync_state  # noqa: E402
from sources import akinon, trendyol  # noqa: E402
from sources.base import Product, Query  # noqa: E402


def iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


# ---------------------------------------------------------------- Akinon

def ak_item(pk: int) -> dict:
    return {"pk": pk, "price": "100", "retail_price": "200", "name": f"Gömlek {pk}", "absolute_url": f"/gomlek-{pk}/",
            "attributes": {"filterable_product_base_type": "gömlek", "filterable_gender": "erkek"},
            "extra_data": {"variants": [{"attribute_key": "integration_size", "options": [{"in_stock": True, "label": "M"}]}]},
            "productimage_set": []}


class AkinonDiscoverTest(unittest.TestCase):
    LISTINGS = {"/erkek-1/": [1, 2], "/cat-a/": [2], "/cat-b/": [3], "/cat-c/": [1]}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "akinon_pcardin_tr.json"
        self.pages: list[str] = []
        self.sitemap = 0
        test = self

        class FakeClient:
            def __init__(self, site, cfg):
                self.requests_made = 0

            def page(self, path, params):
                test.pages.append(path)
                return path

        def parse_listing(key):
            pks = test.LISTINGS.get(key, [])
            return {"current_page": 1, "num_pages": 1, "total_count": len(pks)}, [ak_item(p) for p in pks]

        def category_paths(client, seeds):
            test.sitemap += 1
            return ["/cat-a/", "/cat-b/", "/cat-c/"]

        self.patches = [mock.patch.object(akinon, "_Client", FakeClient),
                        mock.patch.object(akinon, "parse_listing", parse_listing),
                        mock.patch.object(akinon, "_category_paths", category_paths),
                        mock.patch.object(akinon, "_sitemap_locs", lambda client, name: [])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fetch(self, every=24):
        q = Query(max_pages=5, source_opts={"sections": ["/erkek-1/"], "discover": True, "discover_every_hours": every,
                                            "discover_state_file": str(self.state)})
        return {p.source_item_id for p in quiet(akinon.fetch, q, site="pcardin_tr")}

    def test_full_pass_once_a_day_then_sections_and_useful(self):
        self.assertEqual(self.fetch(), {"1", "2", "3"})
        self.assertEqual(self.sitemap, 1)
        st = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(st["productive"], ["/cat-b/"])          # только категория с товарами вне разделов
        self.assertTrue(akinon.LAST_RUN["discovered"])
        self.pages.clear()
        self.assertEqual(self.fetch(), {"1", "2", "3"})          # товар из полезной категории не потерян
        self.assertEqual(self.sitemap, 1)                          # карта сайта не читалась
        self.assertEqual(self.pages, ["/erkek-1/", "/cat-b/"])
        self.assertFalse(akinon.LAST_RUN["discovered"])
        st["discovered_at"] = iso_ago(25)                          # прошли сутки — снова полный проход
        self.state.write_text(json.dumps(st), encoding="utf-8")
        self.fetch()
        self.assertEqual(self.sitemap, 2)

    def test_every_zero_is_old_behaviour(self):
        self.fetch(every=0)
        self.fetch(every=0)
        self.assertEqual(self.sitemap, 2)
        self.assertTrue(akinon.discovery_due({}, 24))
        self.assertFalse(akinon.discovery_due({"discovered_at": iso_ago(2)}, 24))


# ---------------------------------------------------------------- Trendyol

def ty_rec(cid: str, brand: str = "Lufian", disc: float | None = 30.0, official: bool = True) -> dict:
    p = Product(source="trendyol", source_item_id=cid, brand=brand, title=f"Gömlek {cid}", category="Gömlek",
                type="рубашки", gender="men", price_now=500.0, price_old=900.0 if disc else None, currency="TRY",
                discount_pct=disc, sizes=[], colors=[], images=[f"https://cdn.dsmcdn.com/ty/{cid}.jpg"],
                url=f"https://www.trendyol.com/lufian/gomlek-p-{cid}")
    return dict(p.to_dict(), _official=official, _merchant_id=1, _seen_at=iso_ago(1))


class TrendyolTiersTest(unittest.TestCase):
    TIERS = {"hot": 3.0, "warm": 24.0, "cold": 168.0}

    def test_plan_order(self):
        items = {c: ty_rec(c) for c in ("h1", "h2", "w1", "w2", "c1", "n1", "n2")}
        items["n2"]["discount_pct"] = None
        cache = {"h1": {"checked_at": iso_ago(5), "sizes": ["M"]},      # горячий, пора (5 ч > 3 ч)
                 "h2": {"checked_at": iso_ago(1), "sizes": ["M"]},      # горячий, свежий
                 "w1": {"checked_at": iso_ago(30), "sizes": ["M"]},     # на сайте, пора
                 "w2": {"checked_at": iso_ago(10), "sizes": ["M"]},     # на сайте, свежий
                 "c1": {"checked_at": iso_ago(200), "sizes": []}}       # остальной, пора (неделя)
        q, st = trendyol._pdp_plan(items, cache, ["Lufian"], 100, self.TIERS, {"h1", "h2"}, {"w1", "w2"})
        self.assertEqual(q, ["h1", "w1", "n1", "n2", "c1"])           # hot → warm → никогда (со скидкой раньше) → cold
        self.assertEqual((st["hot"]["fresh"], st["warm"]["fresh"], st["never"]), (1, 1, 2))
        self.assertEqual(trendyol._pdp_plan(items, cache, ["Lufian"], 2, self.TIERS, {"h1"}, {"w1"})[0], ["h1", "w1"])
        self.assertEqual(trendyol.tier_hours({"pdp_max_age_hours": 72}), {"hot": 3.0, "warm": 24.0, "cold": 72.0})

    def test_listing_window(self):
        prev = {"items": {"1": {}}, "brands": ["Lufian"], "complete": True, "crawled_at": iso_ago(20)}
        so = {"listing_window": [1, 6]}
        self.assertTrue(trendyol.listing_due(prev, ["Lufian"], "auto", so, hour=2)[0])
        self.assertFalse(trendyol.listing_due(prev, ["Lufian"], "auto", so, hour=14)[0])      # вне окна — сохранённая
        self.assertFalse(trendyol.listing_due(dict(prev, crawled_at=iso_ago(3)), ["Lufian"], "auto", so, hour=2)[0])
        self.assertTrue(trendyol.listing_due(dict(prev, crawled_at=iso_ago(50)), ["Lufian"], "auto", so, hour=14)[0])
        self.assertTrue(trendyol.listing_due(dict(prev, complete=False), ["Lufian"], "auto", so, hour=14)[0])
        self.assertTrue(trendyol.listing_due(prev, ["Lufian"], "listing", so, hour=14)[0])
        self.assertFalse(trendyol.listing_due(dict(prev, crawled_at=iso_ago(500)), ["Lufian"], "pdp", so, hour=2)[0])
        self.assertTrue(trendyol._in_window(23, [22, 4]) and trendyol._in_window(3, [22, 4]))
        self.assertFalse(trendyol._in_window(12, [22, 4]))
        old = {"listing_max_age_hours": 20}                                                    # без окна — как раньше
        self.assertFalse(trendyol.listing_due(dict(prev, crawled_at=iso_ago(5)), ["Lufian"], "auto", old)[0])
        self.assertTrue(trendyol.listing_due(dict(prev, crawled_at=iso_ago(25)), ["Lufian"], "auto", old)[0])


class TrendyolModesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.listing = self.tmp / "listing.json"
        self.cache = self.tmp / "cache.json"
        self.checked: list[str] = []
        test = self

        class FakeClient:
            def __init__(self, delay=(1.0, 2.0)):
                self.requests, self.listings_ok = 1, 1

        def fake_pdp(client, url, info=None):
            cid = url.rsplit("-p-", 1)[1]
            test.checked.append(cid)
            sizes = [] if cid == "w2" else ["M", "L"]
            return {"sizes": sizes, "all_sizes": ["M", "L"], "in_stock": bool(sizes), "gender": "men", "gender_raw": "Erkek",
                    "color": "Beyaz", "category_path": "Giyim/Gömlek", "merchant": "Lufian", "merchant_id": 1, "price": 500.0,
                    "attrs": {}, "images": []}

        class FakeCrawl:
            def __init__(self, client, query, image_size, official_only, merchant_ids):
                self.items, self.errors, self.truncated, self.capped = {}, 0, False, 0
                self.excluded, self.skipped_seller, self.discovered, self.cat_map = Counter(), 0, 0, {}

            def crawl_brand(self, brand, slug, bid):
                self.items["new1"] = ty_rec("new1", brand)
                return {"brand_total": 1}

        self.patches = [mock.patch.object(trendyol, "_Client", FakeClient),
                        mock.patch.object(trendyol, "_pdp_details", fake_pdp),
                        mock.patch.object(trendyol, "_Crawl", FakeCrawl)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def query(self, **so) -> Query:
        base = {"brand_slugs": {"Lufian": "lufian-x-b364"}, "pdp_cache_file": str(self.cache),
                "listing_file": str(self.listing), "pdp_concurrency": 1, "pdp_delay": 0.5}
        base.update(so)
        return Query(max_pages=138, source_opts=base)

    def write_listing(self, ids):
        self.listing.write_text(json.dumps({"crawled_at": iso_ago(3), "complete": True, "brands": ["Lufian"],
                                            "items": {c: ty_rec(c) for c in ids}}), encoding="utf-8")

    def test_pdp_mode_tiers_and_gone(self):
        self.write_listing(["h1", "w1", "w2", "c1"])
        self.cache.write_text(json.dumps({
            "h1": {"checked_at": iso_ago(4), "sizes": ["M"]}, "w1": {"checked_at": iso_ago(2), "sizes": ["M"]},
            "w2": {"checked_at": iso_ago(30), "sizes": ["M"]}}), encoding="utf-8")
        q = self.query(_mode="pdp", _hot=["h1"], _warm=["w1", "w2"], pdp_per_run=2)
        out = quiet(trendyol.fetch, q)
        self.assertEqual(self.checked, ["h1", "w2"])                 # горячий, потом устаревший товар сайта; c1 ждёт
        self.assertEqual({p.source_item_id for p in out}, {"h1", "w1"})
        self.assertIn("w2", trendyol.LAST_RUN["gone_ids"])           # проверен — размеров нет → распродан
        self.assertEqual(trendyol.LAST_RUN["stats"]["tiers"]["hot"], {"total": 1, "fresh": 1, "due": 0})
        self.assertEqual(json.loads(self.listing.read_text(encoding="utf-8"))["crawled_at"][:4], iso_ago(3)[:4])

    def test_budget_stops_early(self):
        self.write_listing(["a", "b", "c"])
        with mock.patch.object(trendyol.time, "time", side_effect=[0, 0] + [10_000] * 50):
            res = quiet(trendyol._run_pdp, ["a", "b", "c"], {c: ty_rec(c) for c in "abc"}, {}, self.cache, 0.5, 1, "", 60)
        self.assertTrue(res["budget_hit"])
        self.assertLess(res["done"], 3)

    def test_listing_mode_only_listing(self):
        self.write_listing(["old1"])
        out = quiet(trendyol.fetch, self.query(_mode="listing"))
        self.assertEqual(out, [])
        self.assertTrue(trendyol.LAST_RUN["no_new_data"])           # run.py оставит прошлые товары, сайт не трогает
        self.assertEqual(self.checked, [])                           # размеры не проверялись
        self.assertFalse(self.cache.exists())
        saved = json.loads(self.listing.read_text(encoding="utf-8"))
        self.assertEqual(list(saved["items"]), ["new1"])

    def test_pdp_mode_without_listing_fails(self):
        with self.assertRaises(RuntimeError):
            quiet(trendyol.fetch, self.query(_mode="pdp"))


# ---------------------------------------------------------------- run.py

class RunJobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (run.DATA, run.SITE)
        run.DATA = self.tmp / "data"
        run.SITE = self.tmp / "site"
        run.DATA.mkdir()
        self.env = mock.patch.dict(os.environ, {"ORDERS_DB_PATH": str(self.tmp / "nope.sqlite"), "IMG_BASE": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        run.DATA, run.SITE = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_content_fp_ignores_fetch_time(self):
        a = [{"source_item_id": "1", "price_now": 10, "fetched_at": "2026-10-06T10:00:00Z"}]
        b = [{"source_item_id": "1", "price_now": 10, "fetched_at": "2026-10-06T11:00:00Z"}]
        c = [{"source_item_id": "1", "price_now": 11, "fetched_at": "2026-10-06T11:00:00Z"}]
        self.assertEqual(run.content_fp(a), run.content_fp(b))
        self.assertNotEqual(run.content_fp(a), run.content_fp(c))

    def test_sources_changed(self):
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        cfg = {"sources": ["pcardin_tr"]}
        rows = [{"source_item_id": "1", "price_now": 10, "fetched_at": "2026-10-06T10:00:00Z"}]
        run.write_raw("pcardin_tr", rows)
        state = {"sources": {"pcardin_tr": {"raw_sha1": sync_state.file_sha1(run.raw_path("pcardin_tr")),
                                            "content_fp": run.content_fp(rows)}},
                 "build": {"config_sha1": sync_state.file_sha1(cfg_path)}, "items": {}}
        self.assertEqual(run.sources_changed(cfg, state, cfg_path), [])
        run.write_raw("pcardin_tr", [dict(rows[0], fetched_at="2026-10-06T11:00:00Z")])
        self.assertEqual(run.sources_changed(cfg, state, cfg_path), [])          # только время сбора — не пересобираем
        run.write_raw("pcardin_tr", [dict(rows[0], price_now=9)])
        self.assertEqual(run.sources_changed(cfg, state, cfg_path), ["pcardin_tr"])
        cfg_path.write_text('{"x": 1}', encoding="utf-8")
        self.assertIn("config.json", run.sources_changed(cfg, state, cfg_path))

    def test_demand_ids_from_orders_and_carts(self):
        db = self.tmp / "orders.sqlite"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE orders(created_at TEXT, items_json TEXT)")
        con.execute("CREATE TABLE carts(updated_at TEXT, items_json TEXT)")
        now = datetime.now().isoformat(timespec="seconds")
        old = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
        con.execute("INSERT INTO orders VALUES(?, ?)", (now, json.dumps([{"id": "AAAAAAA", "source": "trendyol", "source_item_id": "111"},
                                                                         {"id": "BBBBBBB", "source": "yoox", "source_item_id": "9"}])))
        con.execute("INSERT INTO orders VALUES(?, ?)", (old, json.dumps([{"source": "trendyol", "source_item_id": "999"}])))
        con.execute("INSERT INTO carts VALUES(?, ?)", (now, json.dumps([{"id": "CCCCCCC", "size": "M", "qty": 1}])))
        con.commit()
        con.close()
        state = {"items": {"CCCCCCC": {"source": "trendyol", "item_id": "333"}}}
        self.assertEqual(run.demand_ids(state, "trendyol", db=db), {"111", "333"})

    def test_collect_lock_busy_and_listing_running(self):
        lk = sync_state.Lock(run.DATA / "collect_trendyol-listing.lock", "тест")
        self.assertTrue(lk.try_acquire())
        try:
            rows, info = quiet(run.collect_locked, "trendyol", {"source_opts": {}}, [{"source_item_id": "1"}], set(), False,
                               {"_mode": "listing"})
            self.assertEqual(info["status"], "busy")                 # тот же сбор уже идёт — прошлые данные
            self.assertEqual(rows, [{"source_item_id": "1"}])
            state = {"items": {}, "sources": {}}
            ex = quiet(run.source_extras, "trendyol", {"source_opts": {"trendyol": {}}}, state, "auto")
            self.assertEqual(ex["_mode"], "pdp")                     # ночной обход идёт — выдачу не трогаем
            self.assertTrue(ex["_listing_running"])
        finally:
            lk.release()
        ex = quiet(run.source_extras, "trendyol", {"source_opts": {"trendyol": {}}}, {"items": {}, "sources": {}}, "listing")
        self.assertEqual(ex, {"_mode": "listing"})


class BuildIfChangedTest(unittest.TestCase):
    """Сборка из data/raw_*.json как на сервере: свой хост фото, --if-changed, распроданное по meta сбора."""

    def setUp(self):
        import content
        import pricing
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (run.DATA, run.SITE, pricing.FX_CACHE, content.build, run.ROOT)
        run.DATA, run.SITE, run.ROOT = self.tmp / "data", self.tmp / "site", self.tmp
        run.DATA.mkdir()
        pricing.FX_CACHE = self.tmp / "data" / "fx.json"
        content.build = lambda *a, **k: None                 # не трогать настоящий site/content.js
        run._TOKMAP = None
        self.cfg_path = self.tmp / "config.json"
        self.cfg = {"sources": ["pcardin_tr"], "filters": {"brands": [], "discount_min": 0}, "source_filters": {},
                    "pricing": {"fx_manual": {"TRY": 380.0}, "margin_pct": 25, "round_to_uzs": 10000,
                                "cargo_uzs_per_item": {"TR": 40000}},
                    "source_opts": {}, "sync": {}, "site": {"name": "t"}}
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")
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

    def row(self, i: int, fetched="2026-10-06T10:00:00Z") -> dict:
        p = Product(source="pcardin_tr", source_item_id=str(i), brand="Pierre Cardin", title=f"Gömlek {i}", category="Gömlek",
                    type="рубашки", gender="men", price_now=1000.0 + i, price_old=2000.0, currency="TRY", discount_pct=50.0,
                    sizes=["M", "L"], colors=["Beyaz"], url=f"https://www.pierrecardin.com.tr/gomlek-{i}/",
                    images=[f"https://25d163-pcardin.akinoncloudcdn.com/products/2026/08/04/{i}/{k}.jpg" for k in range(2)])
        return dict(p.to_dict(), fetched_at=fetched)

    def collected(self, rows, gone=()):
        """Как после run.py --collect-only: raw + meta с итогом сбора."""
        run.write_raw("pcardin_tr", rows)
        sync_state.write_json(run.meta_path("pcardin_tr"), {
            "source": "pcardin_tr", "status": "ok", "collected_at": iso_ago(0), "absence_means_gone": False,
            "gone_ids": list(gone), "raw_sha1": sync_state.file_sha1(run.raw_path("pcardin_tr"))}, indent=1)

    def build(self, if_changed=True) -> str:
        args = SimpleNamespace(offline=True, if_changed=if_changed, config=str(self.cfg_path), accept_drop=False,
                               trendyol_mode="auto")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run.build(self.cfg, run.sync_cfg(self.cfg), set(), args)
        return buf.getvalue()

    def test_server_build_cycle(self):
        import catalog_files as cf
        self.collected([self.row(i) for i in range(6)])
        self.build(if_changed=False)
        man_file = run.SITE / "data" / "manifest.json"
        man = cf.load_manifest(run.SITE)
        self.assertEqual(man["img"]["base"], "https://img.example.uz")
        blob = "".join((run.SITE / f).read_text(encoding="utf-8") for f in cf.published_files(run.SITE))
        self.assertNotIn("akinon", blob)
        self.assertTrue((run.DATA / "img_map.json").is_file())
        t0 = man_file.stat().st_mtime_ns
        # тот же сбор, только другое время сбора — сайт не пересобирается
        self.collected([self.row(i, fetched="2026-10-06T11:00:00Z") for i in range(6)])
        self.assertIn("не пересобираю", self.build())
        self.assertEqual(man_file.stat().st_mtime_ns, t0)
        # распродан товар 3 (подтверждено сбором) — пересборка, на сайте «Нет в наличии», фото — те же токены
        pid3 = run.public_id("pcardin_tr", "3")
        toks = cf.PublicCatalog(run.SITE)[pid3]["images"]
        self.collected([self.row(i) for i in range(6) if i != 3], gone=["3"])
        out = self.build()
        self.assertIn("Изменилось с прошлой сборки: pcardin_tr", out)
        cat = cf.PublicCatalog(run.SITE)
        self.assertFalse(cat[pid3]["in_stock"])
        self.assertEqual(cat[pid3]["images"], toks)
        self.assertEqual(len(cat), 6)
        # config.json поменялся — пересборка даже без новых данных
        self.cfg_path.write_text(json.dumps(dict(self.cfg, x=1)), encoding="utf-8")
        self.assertIn("Изменилось с прошлой сборки: config.json", self.build())

    def test_load_raw_consistent(self):
        rows = [self.row(1)]
        self.collected(rows)
        got, sha, meta = run.load_raw_consistent("pcardin_tr")
        self.assertEqual((got, sha), (rows, meta["raw_sha1"]))
        run.write_raw("pcardin_tr", [self.row(2)])                 # новый raw, meta ещё старая (сбор посередине)
        with mock.patch.object(run.time, "sleep") as sl:
            got, sha, meta = run.load_raw_consistent("pcardin_tr")
        self.assertEqual(sl.call_count, 2)                          # перечитали, потом сдались (правлен вручную)
        self.assertEqual(got[0]["source_item_id"], "2")


# ---------------------------------------------------------------- файлы сервера

class ServerFilesTest(unittest.TestCase):
    SRV = ROOT / "server"

    def test_units_jobs_and_timers_consistent(self):
        script = (self.SRV / "yurt-run.sh").read_text(encoding="utf-8")
        jobs = set(re.search(r"\n\s+(turkey-akinon\|[^)]*)\)", script).group(1).split("|"))
        self.assertTrue({"turkey-akinon", "trendyol-pdp", "trendyol-listing", "yoox", "fx", "cleanup"} <= jobs)
        units = {p.name: p.read_text(encoding="utf-8") for p in (self.SRV / "systemd").iterdir()}
        self.assertNotIn("yurt-update.timer", units)                 # монолит каждые 3 ч больше не включается
        for name, text in units.items():
            m = re.search(r"(?m)^ExecStart=.*yurt-run\.sh (\S+)", text)
            if name.endswith(".service") and m and "%i" not in m.group(1):
                self.assertIn(m.group(1), jobs, name)
            if name.endswith(".timer"):
                unit = re.search(r"^Unit=(\S+)", text, re.M).group(1)
                tmpl = re.sub(r"@[^.]+\.service$", "@.service", unit)
                self.assertTrue(unit in units or tmpl in units, f"{name}: нет {unit}")
        self.assertIn("TimeoutStartSec=5h", units["yurt-trendyol-listing.service"])
        self.assertIn("OnCalendar=*-*-* *:05:00", units["yurt-turkey-akinon.timer"])
        self.assertIn("OnCalendar=*-*-* *:20:00", units["yurt-trendyol-pdp.timer"])
        self.assertIn("01:10:00 Asia/Tashkent", units["yurt-trendyol-listing.timer"])
        setup = (self.SRV / "setup.sh").read_text(encoding="utf-8")
        timers = re.search(r'^TIMERS="([^"]+)"', setup, re.M).group(1).split()
        for t in timers:
            self.assertIn(t, units)
        self.assertIn("pillow", setup)
        env = (self.SRV / "yurt.env.example").read_text(encoding="utf-8")
        loop = re.search(r"for v in (.+?); do", setup, re.S).group(1).replace("\\\n", " ").split()
        for v in loop:
            self.assertRegex(env, rf"(?m)^{v}=", v)                 # иначе setup.sh (set -e) упадёт на grep

    def test_no_automated_yoox_access(self):
        for p in [self.SRV / "yurt-run.sh", self.SRV / "crontab.example", *(self.SRV / "systemd").iterdir()]:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.lstrip().startswith("#"):
                    continue                                    # пояснения можно; команды — нет
                self.assertNotIn("yoox.com", line, p.name)
                self.assertNotRegex(line, r"run\.py[^\n]*--only[^\n]*\byoox\b(?!_import)", p.name)
        self.assertIn("yoox", img_api_never())

    def test_shell_syntax(self):
        cands = []
        git = shutil.which("git")
        if git:                                                  # Windows: bash из Git for Windows (не WSL)
            g = Path(git).resolve().parent.parent
            cands += [g / "bin" / "bash.exe", g / "usr" / "bin" / "bash.exe"]
        cands.append(shutil.which("bash"))
        bash = None
        for c in cands:
            if c and Path(c).is_file():
                try:
                    if subprocess.run([str(c), "-c", "true"], capture_output=True, timeout=20).returncode == 0:
                        bash = str(c)
                        break
                except (OSError, subprocess.SubprocessError):
                    continue
        if not bash:
            self.skipTest("рабочий bash не найден")
        for name in ("yurt-run.sh", "setup.sh"):
            r = subprocess.run([bash, "-n", str(self.SRV / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


def img_api_never() -> tuple:
    import img_api
    return img_api.NEVER_PREWARM


if __name__ == "__main__":
    unittest.main()
