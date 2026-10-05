"""Свои фото сайта (run.py): нейтральные имена site/img/p/<код IPAK>_<n>.jpg, переименование прежних имён без
повторного скачивания; тестовый сервер tools/serve_test.py не отдаёт браузеру адреса магазинов.

    python -m unittest tests.test_publish_images
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import run  # noqa: E402
import serve_test  # noqa: E402

SHOP_NAME = re.compile(r"^\d{8}[a-z]{2}_", re.I)          # имя файла магазина: 8 цифр + 2 буквы + _
NEUTRAL = re.compile(r"^[A-Z2-7]{7,12}_\d+\.[a-z0-9]+$")


class PublishImagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.site = self.tmp / "site"
        (self.site / "img" / "yoox").mkdir(parents=True)
        (self.site / "img" / "p").mkdir(parents=True)
        self._old_site = run.SITE
        run.SITE = self.site
        self.code = run.public_id("yoox", "16012345AB")

    def tearDown(self):
        run.SITE = self._old_site
        shutil.rmtree(self.tmp, ignore_errors=True)

    def src(self, name: str, data: bytes = b"jpegdata") -> str:
        (self.site / "img" / "yoox" / name).write_bytes(data)
        return f"img/yoox/{name}"

    def pub(self, name: str, data: bytes = b"jpegdata") -> Path:
        f = self.site / "img" / "p" / name
        f.write_bytes(data)
        return f

    def names(self) -> list[str]:
        return sorted(f.name for f in (self.site / "img" / "p").iterdir())

    def test_name_format(self):
        self.assertEqual(run.pub_image_name("ABCDEFG", 2), "ABCDEFG_2.jpg")
        self.assertEqual(run.pub_image_name("ABCDEFG", 1, ".JPG"), "ABCDEFG_1.jpg")
        self.assertRegex(run.pub_image_name(self.code, 1), NEUTRAL)
        self.assertEqual(run.legacy_pub_name("ABCDEFG-3.jpg"), "ABCDEFG_3.jpg")
        self.assertIsNone(run.legacy_pub_name("32124427dl_14_e.jpg"))
        self.assertIsNone(run.legacy_pub_name("ABCDEFG_3.jpg"))

    def test_publish_from_local_source(self):
        used: set[str] = set()
        out = run.publish_images(self.code, [self.src("16012345AB_1.jpg"), "https://cdn.example.com/x.jpg"], used)
        self.assertEqual(out, [f"img/p/{self.code}_1.jpg", "https://cdn.example.com/x.jpg"])
        self.assertEqual(used, {f"{self.code}_1.jpg"})
        self.assertEqual(self.names(), [f"{self.code}_1.jpg"])
        self.assertEqual((self.site / out[0]).read_bytes(), b"jpegdata")

    def test_legacy_copy_is_renamed_not_copied_again(self):
        old = self.pub(f"{self.code}-1.jpg", b"published")
        used: set[str] = set()
        out = run.publish_images(self.code, ["img/yoox/16012345AB_1.jpg"], used)   # оригинала нет (как в облаке)
        self.assertEqual(out, [f"img/p/{self.code}_1.jpg"])
        self.assertFalse(old.exists())
        self.assertEqual((self.site / out[0]).read_bytes(), b"published")
        self.assertEqual(self.names(), [f"{self.code}_1.jpg"])

    def test_shop_named_copy_is_renamed(self):
        self.pub("16012345AB_1.jpg", b"shopnamed")
        out = run.publish_images(self.code, ["img/yoox/16012345AB_1.jpg"], set())
        self.assertEqual(out, [f"img/p/{self.code}_1.jpg"])
        self.assertEqual(self.names(), [f"{self.code}_1.jpg"])

    def test_source_wins_over_stale_copy(self):
        self.pub(f"{self.code}-1.jpg", b"old")
        out = run.publish_images(self.code, [self.src("16012345AB_1.jpg", b"newer-photo")], set())
        self.assertEqual((self.site / out[0]).read_bytes(), b"newer-photo")
        self.assertEqual(self.names(), [f"{self.code}_1.jpg"])

    def test_missing_everything_is_skipped(self):
        used: set[str] = set()
        self.assertEqual(run.publish_images(self.code, ["img/yoox/nothing_1.jpg"], used), [])
        self.assertEqual(used, set())

    def test_migrate_published_names(self):
        self.pub("ABCDEFG-1.jpg")
        self.pub("ABCDEFG-2.jpg")
        self.pub("HIJKLMN-1.jpg", b"dup-old")
        self.pub("HIJKLMN_1.jpg", b"current")
        self.pub("notes.txt")
        self.assertEqual(run.migrate_published_names(), 3)
        self.assertEqual(self.names(), ["ABCDEFG_1.jpg", "ABCDEFG_2.jpg", "HIJKLMN_1.jpg", "notes.txt"])
        self.assertEqual((self.site / "img" / "p" / "HIJKLMN_1.jpg").read_bytes(), b"current")
        self.assertEqual(run.migrate_published_names(), 0)

    def test_keep_published_images_maps_old_references(self):
        self.pub("ABCDEFG-1.jpg")
        used: set[str] = set()
        out = run.keep_published_images(["img/p/ABCDEFG-1.jpg", "img/p/GONEGON-1.jpg", "https://cdn.example.com/a.jpg",
                                         "img/yoox/123_1.jpg"], used)
        self.assertEqual(out, ["img/p/ABCDEFG_1.jpg", "https://cdn.example.com/a.jpg"])
        self.assertEqual(used, {"ABCDEFG_1.jpg"})
        self.assertEqual(self.names(), ["ABCDEFG_1.jpg"])

    def test_cleanup_keeps_only_used(self):
        self.pub("ABCDEFG_1.jpg")
        self.pub("32124427dl_14_e.jpg")
        self.assertEqual(run.cleanup_images({"ABCDEFG_1.jpg"}), 1)
        self.assertEqual(self.names(), ["ABCDEFG_1.jpg"])
        self.assertFalse(any(SHOP_NAME.match(n) for n in self.names()))


class ServeTestRewriteTest(unittest.TestCase):
    """tools/serve_test.py: внешние адреса фото заменяются на локальные, у HTML — CSP на картинки."""

    def setUp(self):
        self._opt = dict(serve_test.OPT)
        self._site = serve_test.SITE
        self.tmp = Path(tempfile.mkdtemp())
        self.site = self.tmp / "site"
        (self.site / "data" / "d").mkdir(parents=True)
        (self.site / "img" / "p").mkdir(parents=True)
        (self.site / "img" / "p" / "ABCDEFG_1.jpg").write_bytes(b"\xff\xd8local")
        (self.site / "index.html").write_text('<!doctype html><html><head><meta charset="utf-8"><title>t</title>'
                                              '</head><body></body></html>', encoding="utf-8")
        shard = {"pre": ["", "https://img.shop-a.example/items/10/", "img/p/"], "suf": ["", "?w=464&h=591"],
                 "links": ["https://t.me/someone"]}
        (self.site / "data" / "d" / "000.js").write_text('__DS("d/000",\n' + json.dumps(shard) + "\n);",
                                                          encoding="utf-8")
        serve_test.SITE = self.site
        serve_test.PHOTOS.clear()
        serve_test.CACHE.clear()

    def tearDown(self):
        serve_test.OPT.clear()
        serve_test.OPT.update(self._opt)
        serve_test.SITE = self._site
        serve_test.PHOTOS.clear()
        serve_test.CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rewrite_modes(self):
        text = '["https://img.shop-a.example/items/10/","//cdn.shop-b.example/a.jpg","https://t.me/x","img/p/A_1.jpg"]'
        serve_test.OPT.update(img="path", port=8800)
        out = serve_test.rewrite(text)
        self.assertIn('"/__img/img.shop-a.example/items/10/"', out)
        self.assertIn('"/__img/cdn.shop-b.example/a.jpg"', out)
        self.assertIn("https://t.me/x", out)
        self.assertIn('"img/p/A_1.jpg"', out)
        serve_test.OPT.update(img="hosts", port=8811)
        out = serve_test.rewrite(text)
        self.assertNotIn("shop-a.example/", out)
        self.assertRegex(out, r"http://img-[0-9a-f]{8}\.localhost:8811/items/10/")
        self.assertEqual(serve_test.rewrite("// comment example.com/x"), "// comment example.com/x")
        self.assertEqual(serve_test.rewrite("a //example.com/y"), "a //example.com/y")

    def test_server_blocks_external_images(self):
        serve_test.OPT.update(img="path", placeholder=False, dead=["shop-b"], delay_data=0, delay_img=0)
        srv = serve_test.ThreadingHTTPServer(("127.0.0.1", 0), serve_test.Handler)
        serve_test.OPT["port"] = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{serve_test.OPT['port']}"
        try:
            with urllib.request.urlopen(base + "/?tgmock=pre") as r:
                html = r.read().decode("utf-8")
                self.assertIn("img-src 'self' data: blob:", r.headers.get("Content-Security-Policy", ""))
            self.assertIn('<script src="/__tg/mock.js"></script>', html)
            with urllib.request.urlopen(base + "/data/d/000.js") as r:
                body = r.read().decode("utf-8")
            self.assertNotIn("shop-a.example/items", body.replace("/__img/img.shop-a.example/items", ""))
            self.assertIn("https://t.me/someone", body)
            with urllib.request.urlopen(base + "/__img/img.shop-a.example/items/10/X_1.jpg?w=464&h=591") as r:
                self.assertEqual(r.read(), b"\xff\xd8local")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/__img/cdn.shop-b.example/a.jpg")
            self.assertEqual(cm.exception.code, 404)
            with urllib.request.urlopen(base + "/__log") as r:
                log = json.loads(r.read())
            self.assertEqual([x["host"] for x in log], ["img.shop-a.example", "cdn.shop-b.example"])
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/../config.json")
            self.assertEqual(cm.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
