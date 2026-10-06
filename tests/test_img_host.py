"""Свой хост фото: токены и закрытая карта (img_map.py), раскладка каталога без адресов магазинов
(catalog_files.write_site с img=…), служба img_api.py (кэш, 3:4 с белыми полями, заглушка на 403/429, лимиты на хост,
прогрев без YOOX), проверка публикации deploy.py, режим --imgtok тестового сервера. Сети нет: оригиналы фото отдаёт
подменённая функция скачивания, фото магазинов не загружаются.

    python -m unittest tests.test_img_host
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import catalog_files as cf  # noqa: E402
import img_api  # noqa: E402
import img_map  # noqa: E402

from PIL import Image  # noqa: E402

SECRET = "test-secret-1234567890"
SHOP_HOSTS = re.compile(r"yoox|akinon|dsmcdn|ynap", re.I)


def jpeg(w: int, h: int, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def products(n: int = 40) -> list[dict]:
    hosts = ["https://www.yoox.com/images/items/10/{i}_{k}.jpg?width=464&height=591&impolicy=crop",
             "https://pcardin.akinoncloudcdn.com/products/2026/08/04/{i}/{k}.jpg",
             "https://cdn.dsmcdn.com/mnresize/1200/1800/ty1/prod/{i}/{k}.jpg"]
    out = []
    for i in range(n):
        pid = f"P{i:06d}"
        out.append({"id": pid, "brand": "Boss", "title": f"Рубашка {i}", "type": "рубашки", "gender": "men",
                    "origin": "IT", "price_uzs": 1_000_000 + i * 10_000, "discount_pct": 30.0, "sizes": ["M", "L"],
                    "sizes_out": [], "size_system": "INT", "color": "белый", "composition": None, "details": [],
                    "description": "Описание", "in_stock": True, "fetched_at": "2026-10-06T10:00:00Z",
                    "first_seen": "2026-10-01T10:00:00Z",
                    "images": [hosts[i % 3].format(i=i, k=k) for k in range(1 + i % 3)] + (["img/p/LOCAL_1.jpg"] if i == 5 else [])})
    return out


class TokensTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.env = mock.patch.dict(os.environ, {"IMG_BASE": "", "IMG_SECRET": "", "IMG_MAP": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_token_format_and_secret(self):
        t = img_map.make_token(SECRET, "ABCDEFG", 0, "https://www.yoox.com/a.jpg")
        self.assertRegex(t, r"^[a-z2-7]{16}$")
        self.assertTrue(img_map.is_token(t))
        self.assertEqual(t, img_map.make_token(SECRET, "ABCDEFG", 0, "https://www.yoox.com/a.jpg"))   # стабилен
        self.assertNotEqual(t, img_map.make_token(SECRET + "x", "ABCDEFG", 0, "https://www.yoox.com/a.jpg"))
        self.assertNotEqual(t, img_map.make_token(SECRET, "ABCDEFG", 1, "https://www.yoox.com/a.jpg"))
        self.assertNotEqual(t, img_map.make_token(SECRET, "ABCDEFG", 0, "https://www.yoox.com/b.jpg"))
        self.assertFalse(img_map.is_token("img/p/ABC_1.jpg"))
        self.assertFalse(img_map.is_token("https://x/abcdefghijklmnop"))
        self.assertEqual(img_map.url_for("https://img.x/", t, 480), f"https://img.x/p/{t}.480.webp")

    def test_settings_env_beats_config(self):
        cfg = {"images": {"base": "https://cfg.example/"}}
        self.assertEqual(img_map.settings(cfg)["base"], "https://cfg.example")
        with mock.patch.dict(os.environ, {"IMG_BASE": "https://img.example/", "IMG_SECRET": "s"}):
            st = img_map.settings(cfg)
            self.assertEqual(st, {"base": "https://img.example", "secret": "s"})
        self.assertIsNone(img_map.Tokenizer.from_settings({"base": "", "secret": ""}, self.data))
        with self.assertRaises(SystemExit):                     # хост задан, секрета нет — не работаем
            img_map.Tokenizer.from_settings({"base": "https://img.example", "secret": ""}, self.data)

    def test_tokenize_keeps_known_drops_unknown_and_saves(self):
        tk = img_map.Tokenizer("https://img.example", SECRET, self.data, day=100)
        toks = tk.tokenize("AAAAAAA", ["//cdn.dsmcdn.com/x.jpg", "img/p/AAAAAAA_2.jpg", "", None])
        self.assertEqual(len(toks), 2)
        self.assertEqual(tk.items[toks[0]][0], "https://cdn.dsmcdn.com/x.jpg")      # «//» → https:
        self.assertEqual(tk.items[toks[1]][:3], ["img/p/AAAAAAA_2.jpg", "AAAAAAA", 1])
        tk.save()
        self.assertTrue((self.data / "img_map.json").is_file())
        # следующая сборка: снимок распроданного с прошлым токеном — остаётся; неизвестный токен — выбрасывается
        tk2 = img_map.Tokenizer("https://img.example", SECRET, self.data, day=101)
        self.assertEqual(tk2.tokenize("AAAAAAA", [toks[0], "zzzzzzzzzzzzzzzz"]), [toks[0]])
        tk2.tokenize("BBBBBBB", ["https://cdn.dsmcdn.com/y.jpg"])
        self.assertEqual(tk2.save(), 3)                          # toks[1] не в сборке, но свежий — остаётся
        tk3 = img_map.Tokenizer("https://img.example", SECRET, self.data, day=101 + img_map.KEEP_DAYS + 1)
        tk3.tokenize("CCCCCCC", ["https://cdn.dsmcdn.com/z.jpg"])
        self.assertEqual(tk3.save(), 1)                          # давно не было в сборках — убраны
        items = img_map.load_items(self.data / "img_map.json")
        self.assertEqual(img_map.detokenize(list(items) + ["img/p/x.jpg", "zzzzzzzzzzzzzzzz"], items),
                         ["https://cdn.dsmcdn.com/z.jpg", "img/p/x.jpg"])


class CatalogTokensTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.site = self.tmp / "site"
        self.data = self.tmp / "data"
        self.env = mock.patch.dict(os.environ, {"IMG_MAP": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def public_bytes(self) -> bytes:
        files = [self.site / f for f in cf.published_files(self.site)]
        files += [self.site / "products.js", self.site / "products.json"]
        return b"".join(f.read_bytes() for f in files if f.is_file())

    def test_no_shop_hosts_with_img_base(self):
        prods = products()
        tk = img_map.Tokenizer("https://img.example.uz", SECRET, self.data)
        cf.write_site(self.site, {}, {"name": "t"}, prods, {}, img=tk, legacy_max=10)
        blob = self.public_bytes()
        self.assertIsNone(SHOP_HOSTS.search(blob.decode("utf-8")), "в публичных данных остался хост магазина")
        self.assertNotIn(b"img/p/LOCAL_1.jpg", blob)
        man = cf.load_manifest(self.site)
        self.assertEqual(man["img"], {"base": "https://img.example.uz", "w": [160, 480, 960], "full": 960, "fmt": "webp"})
        self.assertIn('"img":{"base":"https://img.example.uz"', (self.site / "products.js").read_text(encoding="utf-8"))
        cat = cf.PublicCatalog(self.site)
        idx = cf.PublicCatalog(self.site)
        items = img_map.load_items(self.data / "img_map.json")
        for p in prods:
            got = cat[p["id"]]["images"]
            self.assertEqual(len(got), len(p["images"]))
            self.assertTrue(all(img_map.is_token(t) for t in got), got)
            self.assertEqual([items[t][0] for t in got], p["images"])          # карта знает исходники
            self.assertEqual(idx.index_row(p["id"])["images"], got[:1])        # индекс — первое фото
            self.assertTrue(all(items[t][1] == p["id"] and items[t][2] == k for k, t in enumerate(got)))
        # карта приватная: её нет среди публикуемых файлов сайта
        self.assertFalse(any("img_map" in f for f in cf.published_files(self.site)))

    def test_small_catalog_legacy_json_has_img(self):
        tk = img_map.Tokenizer("https://img.example.uz", SECRET, self.data)
        cf.write_site(self.site, {}, {}, products(6), {}, img=tk)              # ≤ LEGACY_MAX — products.json целиком
        whole = json.loads((self.site / "products.json").read_text(encoding="utf-8"))
        self.assertEqual(whole["img"]["base"], "https://img.example.uz")
        self.assertTrue(all(img_map.is_token(u) for p in whole["products"] for u in p["images"]))

    def test_without_img_base_unchanged_and_tokens_resolved(self):
        prods = products(9)
        cf.write_site(self.site, {}, {}, prods, {})
        cat = cf.PublicCatalog(self.site)
        self.assertEqual(cat[prods[1]["id"]]["images"], prods[1]["images"])   # как раньше — адреса как есть
        self.assertIsNone(cf.load_manifest(self.site).get("img"))
        # снимок распроданного из сборки со своим хостом (токены) — сборка без хоста переводит их по карте обратно
        tk = img_map.Tokenizer("https://img.example.uz", SECRET, self.data)
        toks = tk.tokenize("SOLD001", ["https://cdn.dsmcdn.com/a.jpg", "img/p/SOLD001_2.jpg"])
        tk.save()
        sold = dict(prods[0], id="SOLD001", images=toks + ["zzzzzzzzzzzzzzzz"], in_stock=False)
        cf.write_site(self.site, {}, {}, prods + [sold], {})
        self.assertEqual(cf.PublicCatalog(self.site)["SOLD001"]["images"],
                         ["https://cdn.dsmcdn.com/a.jpg", "img/p/SOLD001_2.jpg"])


class FakeFetch:
    def __init__(self, responses: dict):
        self.responses = responses            # адрес -> (код, тип, байты)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        return self.responses.get(url, (404, "text/html", b"nope"))


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


class ImgApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "cache"
        self.site = self.tmp / "site"
        (self.site / "img" / "p").mkdir(parents=True)
        self.map = self.tmp / "img_map.json"
        self.urls = {
            "akinon": "https://25d163-pcardin.akinoncloudcdn.com/products/a.jpg",       # имя хоста как у настоящего CDN
            "akinon2": "https://pcardin.akinoncloudcdn.com/products/b.jpg",
            "ty": "https://cdn.dsmcdn.com/ty/c.jpg",
            "yoox": "https://www.yoox.com/images/items/d.jpg",
            "evil": "https://evil.example.com/e.jpg",
        }
        self.tok = {k: img_map.make_token(SECRET, "PID" + k, 0, u) for k, u in self.urls.items()}
        items = {self.tok[k]: [u, "PID" + k, 0, img_map.today()] for k, u in self.urls.items()}
        self.tok["local"] = img_map.make_token(SECRET, "LOCAL", 1, "img/p/L_2.jpg")
        items[self.tok["local"]] = ["img/p/L_2.jpg", "LOCAL", 1, img_map.today()]
        (self.site / "img" / "p" / "L_2.jpg").write_bytes(jpeg(300, 300, (10, 120, 10)))
        self.map.write_text(json.dumps({"v": 1, "items": items}), encoding="utf-8")
        self.fetch = FakeFetch({
            self.urls["akinon"]: (200, "image/jpeg", jpeg(1200, 600)),          # широкая: поля сверху и снизу
            self.urls["akinon2"]: (200, "image/jpeg", jpeg(600, 900)),
            self.urls["ty"]: (403, "text/html", b"forbidden"),
            self.urls["yoox"]: (200, "image/jpeg", jpeg(464, 591)),
        })
        self.clock = FakeClock()
        self.svc = img_api.ImgService(self.cache, self.map, self.site, fetch=self.fetch, clock=self.clock,
                                      limiter=img_api.HostLimiter(clock=self.clock, sleep=self.clock.sleep))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_miss_renders_all_widths_then_hit(self):
        code, body, cache, tag = self.svc.get(self.tok["akinon"], 480)
        self.assertEqual((code, tag, cache), (200, "rendered", img_api.LONG_CACHE))
        self.assertEqual(len(self.fetch.calls), 1)
        url, headers = self.fetch.calls[0]
        self.assertEqual(headers["Referer"], "https://www.pierrecardin.com.tr/")
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        for w in (160, 480, 960):                                   # одно скачивание — все три ширины
            p = self.cache / "p" / f"{self.tok['akinon']}.{w}.webp"
            self.assertTrue(p.is_file(), w)
            with Image.open(p) as im:
                self.assertEqual((im.format, im.size), ("WEBP", (w, w * 4 // 3)))
        im = Image.open(io.BytesIO(body)).convert("RGB")
        self.assertEqual(im.size, (480, 640))
        top, mid = im.getpixel((240, 5)), im.getpixel((240, 320))
        self.assertTrue(all(c > 245 for c in top), top)            # поля белые (вписано, не обрезано)
        self.assertGreater(mid[0], 150)
        self.assertLess(mid[1], 90)                                 # середина — само фото (красное)
        code, body2, cache, tag = self.svc.get(self.tok["akinon"], 960)
        self.assertEqual((code, tag, cache), (200, "hit", img_api.LONG_CACHE))
        self.assertEqual(len(self.fetch.calls), 1)                  # из кэша, без скачивания

    def test_403_placeholder_no_retry_and_host_paused(self):
        code, body, cache, tag = self.svc.get(self.tok["ty"], 480)
        self.assertEqual((code, tag, cache), (200, "placeholder", "no-store"))
        self.assertEqual(Image.open(io.BytesIO(body)).size, (480, 640))
        self.assertFalse((self.cache / "p" / f"{self.tok['ty']}.480.webp").exists())
        n = len(self.fetch.calls)
        self.svc.get(self.tok["ty"], 160)                           # тот же хост — не трогаем
        self.assertEqual(len(self.fetch.calls), n)
        self.clock.t += img_api.BLOCK_MINUTES * 60 + 1              # через полчаса — можно снова
        self.svc.get(self.tok["ty"], 160)
        self.assertEqual(len(self.fetch.calls), n + 1)

    def test_404_unknown_bad_width_and_foreign_host(self):
        self.fetch.responses[self.urls["akinon2"]] = (404, "text/html", b"")
        self.assertEqual(self.svc.get(self.tok["akinon2"], 480)[3], "placeholder")
        n = len(self.fetch.calls)
        self.svc.get(self.tok["akinon2"], 960)                      # неудачный токен — не долбим
        self.assertEqual(len(self.fetch.calls), n)
        self.assertEqual(self.svc.get("zzzzzzzzzzzzzzzz", 480)[0], 404)
        self.assertEqual(self.svc.get(self.tok["akinon"], 700)[0], 404)
        self.assertEqual(self.svc.get(self.tok["evil"], 480)[3], "placeholder")
        self.assertFalse(any("evil" in u for u, _ in self.fetch.calls))

    def test_local_file(self):
        code, body, cache, tag = self.svc.get(self.tok["local"], 160)
        self.assertEqual((code, tag), (200, "rendered"))
        self.assertEqual(self.fetch.calls, [])
        self.assertEqual(Image.open(io.BytesIO(body)).size, (160, 213))

    def test_rate_limit_per_host(self):
        lim = img_api.HostLimiter(clock=self.clock, sleep=self.clock.sleep)
        for _ in range(3):
            self.assertTrue(lim.wait("www.yoox.com", 1.0, 8))
        self.assertEqual(self.clock.slept, [1.0, 1.0])              # YOOX — не чаще 1 в секунду
        self.clock.slept.clear()
        for _ in range(3):
            self.assertTrue(lim.wait("cdn.dsmcdn.com", 2.0, 8))
        self.assertEqual(self.clock.slept, [0.5, 0.5])              # остальные — 2 в секунду
        no_sleep = img_api.HostLimiter(clock=lambda: 0.0, sleep=lambda s: None)
        self.assertTrue(no_sleep.wait("h", 1.0, 2))
        self.assertTrue(no_sleep.wait("h", 1.0, 2))
        self.assertTrue(no_sleep.wait("h", 1.0, 2))
        self.assertFalse(no_sleep.wait("h", 1.0, 2))                # очередь длиннее max_wait — отказ (заглушка)
        self.assertEqual(img_api.host_rule("www.yoox.com"), ("https://www.yoox.com/", 1.0))
        self.assertEqual(img_api.host_rule("cdn.dsmcdn.com")[1], 2.0)
        self.assertEqual(img_api.host_rule("25d163-pcardin.akinoncloudcdn.com")[0], "https://www.pierrecardin.com.tr/")
        self.assertEqual(img_api.host_rule("25d163-cacharel.akinoncloudcdn.com")[0], "https://www.cacharel.com.tr/")
        self.assertIsNone(img_api.host_rule("evil-yoox.com"))
        self.assertIsNone(img_api.host_rule("evil.example.com"))
        self.assertIsNone(img_api.host_rule("yoox.com.evil.example"))

    def test_prewarm_skips_yoox(self):
        res = self.svc.prewarm(limit=10, say=lambda *_: None)
        fetched = [u for u, _ in self.fetch.calls]
        self.assertNotIn(self.urls["yoox"], fetched)                # YOOX заранее не качается никогда
        self.assertIn(self.urls["akinon"], fetched)
        self.assertNotIn(self.urls["evil"], fetched)
        self.assertEqual(res["done"], 2)                            # akinon, akinon2; trendyol ответил 403
        self.assertEqual(res["stopped"], [])
        # фото YOOX — только по просмотру
        self.assertEqual(self.svc.get(self.tok["yoox"], 480)[3], "rendered")
        self.assertEqual(self.fetch.calls[-1][1]["Referer"], "https://www.yoox.com/")

    def test_http_handler(self):
        srv = img_api.ThreadingHTTPServer(("127.0.0.1", 0), img_api.make_handler(self.svc))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{base}/p/{self.tok['akinon']}.480.webp") as r:
                self.assertEqual(r.headers["Content-Type"], "image/webp")
                self.assertIn("immutable", r.headers["Cache-Control"])
                self.assertEqual(Image.open(io.BytesIO(r.read())).size, (480, 640))
            with urllib.request.urlopen(f"{base}/img/p/{self.tok['akinon']}.160.webp") as r:   # путь /img/ (без поддомена)
                self.assertEqual(r.status, 200)
            with urllib.request.urlopen(f"{base}/health") as r:
                self.assertTrue(json.loads(r.read())["ok"])
            for bad in ("/p/../../config.json", "/p/ABCDEFGHIJKLMNOP.480.webp", "/data/img_map.json"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + bad)
                self.assertEqual(e.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()


class DeployLeakTest(unittest.TestCase):
    def setUp(self):
        import deploy
        self.deploy = deploy
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "root"
        self.site = self.root / "site"
        (self.site / "img" / "p").mkdir(parents=True)
        (self.site / "img" / "p" / "X_1.jpg").write_bytes(b"jpg")
        (self.site / "index.html").write_text("<!doctype html><html><head><title>t</title></head><body></body></html>",
                                              encoding="utf-8")
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        self.saved = {k: getattr(deploy, k) for k in ("ROOT", "SITE", "OUT", "git")}
        deploy.ROOT, deploy.SITE = self.root, self.site
        deploy.git = lambda *a, **k: ""                              # без настоящего git и сети
        self.out = self.tmp / "out"
        self.env = mock.patch.dict(os.environ, {"IMG_BASE": "", "IMG_MAP": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for k, v in self.saved.items():
            setattr(self.deploy, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_deploy(self) -> str | None:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                self.deploy.main(["--out", str(self.out), "--dry-run"])
            except SystemExit as e:
                return str(e)
        return None

    def test_hosts_in_data_stop_publish_when_img_base_set(self):
        cf.write_site(self.site, {}, {}, products(12), {})          # сборка без своего хоста: адреса магазинов
        self.assertIsNone(self.run_deploy())                        # как раньше — публикуется
        self.assertTrue((self.out / "img" / "p" / "X_1.jpg").is_file())
        os.environ["IMG_BASE"] = "https://img.example.uz"
        fail = self.run_deploy()
        self.assertIsNotNone(fail)
        self.assertIn("Стоп", fail)
        self.assertIn("адреса магазинов", fail)

    def test_tokenized_build_publishes_without_img_p(self):
        os.environ["IMG_BASE"] = "https://img.example.uz"
        tk = img_map.Tokenizer("https://img.example.uz", SECRET, self.root / "data")
        cf.write_site(self.site, {}, {}, products(12), {}, img=tk)
        self.assertIsNone(self.run_deploy())
        self.assertFalse((self.out / "img" / "p").exists())        # свои фото отдаёт img_api, не gh-pages
        self.assertEqual(self.deploy.shop_host_leaks(self.out), [])
        self.assertFalse(any(p.name == "img_map.json" for p in self.out.rglob("*")))
        # хост магазина, попавший в часть каталога, — стоп
        f = next((self.out / "data" / "d").glob("*.js"))
        f.write_text(f.read_text(encoding="utf-8") + "//cdn.dsmcdn.com/x.jpg", encoding="utf-8")
        self.assertTrue(self.deploy.shop_host_leaks(self.out))


class ServeTestImgTokTest(unittest.TestCase):
    """tools/serve_test.py --imgtok: данные как со своим хостом фото, фото — по /__imgh/p/<токен>.<w>.webp (локальные)."""

    def setUp(self):
        import serve_test
        self.st = serve_test
        self.tmp = Path(tempfile.mkdtemp())
        self.site = self.tmp / "site"
        cf.write_site(self.site, {}, {}, products(30), {}, legacy_max=5)
        (self.site / "img" / "p").mkdir(parents=True, exist_ok=True)
        (self.site / "img" / "p" / "A_1.jpg").write_bytes(jpeg(40, 50))
        (self.site / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
        self._opt, self._site = dict(serve_test.OPT), serve_test.SITE
        serve_test.SITE = self.site
        serve_test.PHOTOS.clear()
        serve_test.CACHE.clear()
        serve_test.LOG.clear()

    def tearDown(self):
        self.st.OPT.clear()
        self.st.OPT.update(self._opt)
        self.st.SITE = self._site
        self.st.PHOTOS.clear()
        self.st.CACHE.clear()
        self.st.LOG.clear()                      # журнал фото общий для модуля — не оставлять другим тестам
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tokens_and_mock_photos(self):
        st = self.st
        st.OPT.update(img="path", placeholder=False, dead=[], delay_data=0, delay_img=0, imgtok=True)
        srv = st.ThreadingHTTPServer(("127.0.0.1", 0), st.Handler)
        st.OPT["port"] = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{st.OPT['port']}"
        try:
            get = lambda p: urllib.request.urlopen(base + p).read().decode("utf-8")   # noqa: E731
            man = json.loads(get("/data/manifest.json"))
            self.assertEqual(man["img"]["base"], "/__imgh")
            blob = "".join(get("/data/" + s["file"]) for s in man["shards"])
            blob += "".join(get("/data/" + f) for f in man["detail"]["files"])
            self.assertIsNone(SHOP_HOSTS.search(blob))
            sh = cf.unwrap(get("/data/" + man["shards"][0]["file"]))
            tok = next(t for t in sh["im"] if t)
            self.assertTrue(img_map.is_token(tok))
            with urllib.request.urlopen(f"{base}/__imgh/p/{tok}.480.webp") as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(r.read())
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{base}/__imgh/p/{tok}.700.webp")
            log = json.loads(get("/__log"))
            self.assertEqual([(x["host"], x["w"]) for x in log], [("imgh", 480)])
            legacy = json.loads(get("/legacy/products.json"))
            self.assertEqual(legacy["img"]["base"], "/__imgh")
            self.assertTrue(all(img_map.is_token(u) for p in legacy["products"] for u in p["images"]))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
