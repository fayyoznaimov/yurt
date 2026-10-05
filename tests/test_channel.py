"""Канал Telegram (channel.py): ворота, квоты дня, дедупликация, «⛔ Продано», проверка на утечку, пробный режим
по умолчанию, фото только файлом, кнопки предложений через tg_bot. Маленький сайт собирается catalog_files.write_site,
Telegram и скачивание фото подменены — в сеть ничего не уходит.

    python -m unittest tests.test_channel
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import catalog_files as cf  # noqa: E402
import channel  # noqa: E402
import ranking  # noqa: E402
import tg_bot  # noqa: E402

NOW = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)          # 12:00 по Ташкенту
ADMIN = 111
CHANNEL = "@ipak_test"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 4000
ENV_ON = {"CHANNEL_ENABLED": "1", "TELEGRAM_BOT_TOKEN": "123:abc", "CHANNEL_ID": CHANNEL,
          "TELEGRAM_ADMIN_IDS": str(ADMIN)}
FORBIDDEN = ("yoox", "trendyol", "akinon", "dsmcdn", "€", "₺", "себестоим", "маржа", "возврат")


def prod(i: int, **kw) -> dict:
    pid = kw.pop("id", f"P{i:06d}")
    p = {"id": pid, "brand": "Boss", "title": "Рубашка Boss", "type": "рубашки", "gender": "men", "origin": "IT",
         "price_uzs": 3_000_000, "discount_pct": 60.0, "sizes": ["M", "L", "XL"], "sizes_out": [], "size_system": "INT",
         "color": f"цвет{i}", "composition": "100% хлопок", "details": [], "description": "Описание",
         "images": [f"img/p/{pid}-1.jpg", f"img/p/{pid}-2.jpg", f"img/p/{pid}-3.jpg"], "in_stock": True,
         "fetched_at": "2026-10-04T10:00:00Z", "first_seen": "2026-10-04T10:00:00Z"}
    p.update(kw)
    return p


class FakeTelegram:
    """Bot API: http(method, params, files) для channel.TG и http(method, params) для tg_bot.Bot."""

    def __init__(self):
        self.calls: list[tuple[str, dict, dict | None]] = []
        self.n = 500
        self.fail: dict[str, dict] = {}

    def __call__(self, method, params, files=None):
        self.calls.append((method, json.loads(json.dumps(params, ensure_ascii=False)), files))
        if method in self.fail:
            return None, self.fail[method]
        if method == "sendPhoto":
            self.n += 1
            fid = params["photo"] if isinstance(params.get("photo"), str) else f"FILE{self.n}"
            return {"message_id": self.n, "chat": {"id": params.get("chat_id")},
                    "photo": [{"file_id": "small"}, {"file_id": fid}]}, {"ok": True}
        if method == "sendMediaGroup":
            out = []
            for m in params["media"]:
                self.n += 1
                out.append({"message_id": self.n, "chat": {"id": params.get("chat_id")},
                            "photo": [{"file_id": f"FILE{self.n}"}]})
            return out, {"ok": True}
        if method == "sendMessage":
            self.n += 1
            return {"message_id": self.n, "chat": {"id": params.get("chat_id")}}, {"ok": True}
        return True, {"ok": True}

    def of(self, method):
        return [(p, f) for m, p, f in self.calls if m == method]


class FakeFetch:
    def __init__(self, status=200):
        self.urls: list[tuple[str, dict]] = []
        self.status = status

    def __call__(self, url, headers, timeout=20):
        self.urls.append((url, dict(headers)))
        return self.status, "image/jpeg", JPEG


class ChannelBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.site = self.tmp / "site"
        self.fake = FakeTelegram()
        self.fetch = FakeFetch()
        self.logs: list[str] = []
        self.cfg = channel.load_config(ROOT / "channel.json")
        self.cfg.update(site_url="https://shop.example/yurt/", contact_url="https://t.me/ipak_seller", mini_app_url="")
        self.ranker = ranking.Ranker({}, 0.5)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, products: list[dict], admin_titles: dict | None = None):
        admin = {"_meta": {}}
        for p in products:
            admin[p["id"]] = {"source": "x", "source_item_id": p["id"], "url": "https://shop/x",
                              "title_original": (admin_titles or {}).get(p["id"], f"Item {p['id']}"),
                              "cost_uzs": 1, "margin_uzs": 1}
            for u in p.get("images") or []:
                if not u.startswith("http"):
                    f = self.site / u
                    f.parent.mkdir(parents=True, exist_ok=True)
                    if not f.exists():
                        f.write_bytes(JPEG)
        cf.write_site(self.site, {}, {"name": "t"}, products, admin)

    def ch(self, env=None, now=NOW, **kw) -> channel.Channel:
        return channel.Channel(site=self.site, cfg=self.cfg, state_path=self.tmp / "channel_state.json",
                               lock_path=self.tmp / "channel.lock", log_dir=self.tmp / "logs",
                               env=ENV_ON if env is None else env, http=self.fake, fetch=self.fetch, now=now,
                               ranker=self.ranker, log=self.logs.append, sleep=lambda s: None, **kw)

    def state(self) -> dict:
        return json.loads((self.tmp / "channel_state.json").read_text(encoding="utf-8"))


class GatesTest(ChannelBase):
    def test_gates(self):
        prods = [
            prod(1),                                                   # проходит
            prod(2, discount_pct=30.0),                                # скидка
            prod(3, price_uzs=1_000_000),                              # дешевле 1,2 млн
            prod(4, price_uzs=16_000_000),                             # дороже 15 млн
            prod(5, sizes=["M"]),                                      # один размер
            prod(6, sizes=["58", "60"], size_system="IT"),             # нет ходового (мужские 48–54)
            prod(7, images=["img/p/P000007-1.jpg"]),                   # одно фото
            prod(8, in_stock=False),                                   # распродано
            prod(9, gender="women", sizes=["S", "M"], title="Платье Boss", type="платья"),
        ]
        self.build(prods)
        ch = self.ch(env={})
        cands, stats = ch.candidates(ch.load_state())
        self.assertEqual({c.id for c in cands}, {"P000001", "P000009"})
        for why in ("скидка меньше порога", "цена вне диапазона", "размеров меньше 2", "нет ходового размера",
                    "фото меньше 2", "нет в наличии"):
            self.assertIn(why, stats)
        self.assertEqual(stats["цена вне диапазона"], 2)
        c = cands[0]
        self.assertGreater(c.total, c.rank)                           # свежесть и экономия добавляются к ranking.score
        self.assertAlmostEqual(c.rank, self.ranker.score(ch.site.rows[c.id], NOW))

    def test_yoox_photos_never_fetched(self):
        remote = [f"https://www.yoox.com/images/items/1/x_{k}.jpg" for k in range(3)]
        self.build([prod(1, images=remote), prod(2)])
        ch = self.ch()
        pk, _ = ch.picker(ch.load_state())
        c = pk.next()
        self.assertEqual(c.id, "P000002")
        self.assertIn("нет фото для отправки (своего файла нет, по ссылке нельзя)", pk.dropped)
        self.assertIsNone(ch.photo_bytes(remote[0]))
        self.assertIsNone(ch.post_id("P000001", force=True))
        self.assertEqual(self.fetch.urls, [])                          # к YOOX — ни одного запроса
        self.assertEqual(self.fake.of("sendPhoto"), [])


class QuotaTest(ChannelBase):
    def test_day_quotas_and_gender_plan(self):
        prods = []
        i = 0
        for brand in ("Gucci", "Boss", "Kiton", "Canali", "Bally"):
            for t, title in (("рубашки", "Рубашка"), ("брюки", "Брюки"), ("обувь", "Туфли"), ("куртки и пальто", "Куртка")):
                for g in ("men", "women"):
                    i += 1
                    sizes = (["M", "L"] if g == "men" else ["S", "M"]) if t != "обувь" else (["42", "43"] if g == "men" else ["37", "38"])
                    prods.append(prod(i, brand=brand, type=t, title=f"{title} {brand}", gender=g, sizes=sizes,
                                      price_uzs=12_000_000 if brand == "Gucci" else 2_000_000 + i * 10_000,
                                      discount_pct=70.0 if brand == "Gucci" else 50.0 + i % 7))
        self.build(prods)
        ch = self.ch(env={})
        picked = ch.plan(8, out=lambda s: None)
        self.assertEqual(len(picked), 8)
        rows = [c.row for c in picked]
        self.assertEqual([r["gender"] for r in rows[:5]], ["men", "women", "men", "men", "women"])
        self.assertLessEqual(max(sum(r["brand"] == b for r in rows) for b in {r["brand"] for r in rows}), 2)
        self.assertLessEqual(max(sum(r["type"] == t for r in rows) for t in {r["type"] for r in rows}), 2)
        self.assertEqual(sum(r["price_uzs"] > 10_000_000 for r in rows), 1)

    def test_quotas_count_todays_posts(self):
        self.build([prod(1, brand="Boss"), prod(2, brand="Boss", type="брюки", title="Брюки Boss"),
                    prod(3, brand="Boss", type="обувь", title="Туфли Boss", sizes=["42", "43"]),
                    prod(4, brand="Kiton", type="джинсы", title="Джинсы Kiton", sizes=["32", "33"])])
        ch = self.ch()
        self.assertIsNotNone(ch.post_id("P000001"))
        self.assertIsNotNone(ch.post_id("P000002"))
        post = self.ch().post_auto()                                  # Boss уже 2 сегодня → только Kiton
        self.assertEqual(self.state()["posts"]["P000004"]["message_id"], post["message_id"])
        self.assertNotIn("P000003", self.state()["posts"])
        tomorrow = self.ch(now=NOW + timedelta(days=1)).post_auto()  # новый день — квота снова свободна
        self.assertIsNotNone(tomorrow)
        self.assertIn("P000003", self.state()["posts"])


class DedupeTest(ChannelBase):
    def test_dedupe_key_14_days_and_never_twice(self):
        titles = {"P000001": "Camicia, bianco", "P000002": "Camicia 2024, avorio", "P000003": "Polo, bianco"}
        self.build([prod(1, color="белый", discount_pct=70.0), prod(2, color="белый"), prod(3, color="белый")], titles)
        ch = self.ch(env={})
        picked = [c.id for c in ch.plan(3, out=lambda s: None)]
        self.assertIn("P000001", picked)
        self.assertNotIn("P000002", picked)                           # тот же бренд | camicia | белый
        self.assertIn("P000003", picked)                              # другое исходное название

        self.assertIsNotNone(self.ch().post_id("P000001"))
        self.assertIsNone(self.ch().post_id("P000001", force=True))   # один код — только один раз, даже с --force
        self.assertIsNone(self.ch(now=NOW + timedelta(days=2)).post_id("P000002"))   # ключ закрыт 14 дней
        self.assertIsNotNone(self.ch(now=NOW + timedelta(days=15)).post_id("P000002"))
        self.assertEqual(len(self.fake.of("sendPhoto")), 2)
        text = (self.tmp / "channel_state.json").read_text(encoding="utf-8").lower()
        self.assertNotIn("camicia", text)                              # исходное название не хранится — только хэш
        self.assertNotIn("cost_uzs", text)


class CaptionTest(ChannelBase):
    def test_caption_exact(self):
        self.build([prod(1, brand="Tod's", title="Женский тренч Tod's", gender="women", type="куртки и пальто",
                         price_uzs=3_190_000, discount_pct=73.1, sizes=["40", "36", "38"], size_system="IT")])
        ch = self.ch(env={})
        r = ch.render(ch.site.rows["P000001"])
        # только наша цена и процент скидки (как на сайте); вычисленной «старой цены» нет
        self.assertEqual(r["caption"], "<b>Tod's</b> — Женский тренч\n\n<b>3 190 000 сум</b> −73%\n"
                                       "Размеры: 36, 38, 40\n\nОригинал · предоплата 50% · доставка до 10 дней")
        self.assertNotIn("<s>", r["caption"])
        self.assertNotIn("11 860 000", r["caption"])
        self.assertEqual(r["discount"], 73)
        self.assertEqual(r["markup"], {"inline_keyboard": [[
            {"text": "Заказать", "url": "https://shop.example/yurt/#/catalog?p=P000001"},
            {"text": "Написать нам", "url": "https://t.me/ipak_seller"}]]})
        self.cfg["mini_app_url"] = "https://t.me/ipak_shop_bot"
        self.assertEqual(self.ch(env={}).order_url("P000001"), "https://t.me/ipak_shop_bot?startapp=p_P000001")
        self.cfg["contact_url"] = ""
        r = self.ch(env={"SELLER_TELEGRAM": "your_username"}).render(ch.site.rows["P000001"])
        self.assertEqual(len(r["markup"]["inline_keyboard"][0]), 1)    # заглушка ника — без «Написать нам»

    def test_caption_without_discount_or_bad_discount(self):
        self.build([prod(1, discount_pct=0)])
        ch = self.ch(env={})
        rows = [ch.site.rows["P000001"]] + [dict(ch.site.rows["P000001"], discount_pct=d) for d in ("?", None, 100, -5)]
        for row in rows:
            cap = ch.render(row)["caption"]
            self.assertIn("<b>3 000 000 сум</b>\nРазмеры:", cap, row["discount_pct"])   # ни «−0%», ни зачёркнутой
            self.assertNotIn("<s>", cap)

    def test_escape_and_length(self):
        sizes = [str(x) for x in range(30, 30 + 400, 2)]
        self.build([prod(1, brand="Dolce&Gabbana", title="Сумка <Dolce&Gabbana> \"x\"", sizes=sizes)])
        ch = self.ch(env={})
        r = ch.render(ch.site.rows["P000001"])
        self.assertTrue(r["caption"].startswith("<b>Dolce&amp;Gabbana</b> — Сумка &lt;"))
        self.assertLessEqual(len(r["caption"]), 1000)
        self.assertIn(" …", r["sizes_line"])

    def test_album_mode_links_in_caption(self):
        self.cfg["photos"] = {"album": True, "album_max": 3}
        self.build([prod(1)])
        post = self.ch().post_id("P000001")
        self.assertTrue(post["album"])
        (params, files), = self.fake.of("sendMediaGroup")
        self.assertEqual(len(params["media"]), 3)
        self.assertEqual(sorted(files), ["p0", "p1", "p2"])
        self.assertIn('<a href="https://shop.example/yurt/#/catalog?p=P000001">Заказать</a>', params["media"][0]["caption"])
        self.assertEqual(len(post["file_ids"]), 3)


class SyncTest(ChannelBase):
    def test_sold_out_edit_and_missing_needs_two_runs(self):
        self.build([prod(1), prod(2, brand="Kiton", title="Рубашка Kiton")])
        self.assertIsNotNone(self.ch().post_id("P000001"))
        self.assertIsNotNone(self.ch().post_id("P000002"))
        posted = self.state()["posts"]["P000001"]
        self.build([prod(1, in_stock=False)])                         # 1 — распродан, 2 — пропал из каталога
        res = self.ch(now=NOW + timedelta(hours=3)).sync()
        self.assertEqual(res["продано"], 1)
        (edit, _), = self.fake.of("editMessageCaption")
        self.assertEqual(edit["message_id"], posted["message_id"])
        self.assertEqual(edit["caption"], posted["caption"] + "\n\n⛔ Продано")
        self.assertEqual(edit["reply_markup"], {"inline_keyboard": []})
        st = self.state()["posts"]
        self.assertEqual(st["P000001"]["status"], "sold")
        self.assertEqual(st["P000002"]["status"], "live")             # разовое исчезновение — не трогаем
        self.assertEqual(st["P000002"]["miss"], 1)
        res = self.ch(now=NOW + timedelta(hours=6)).sync()
        self.assertEqual(res["продано"], 1)
        self.assertEqual(len(self.fake.of("editMessageCaption")), 2)
        self.assertEqual(self.state()["posts"]["P000002"]["status"], "sold")
        self.ch(now=NOW + timedelta(hours=9)).sync()
        self.assertEqual(len(self.fake.of("editMessageCaption")), 2)  # проданное больше не правится

    def test_popular_sizes_line_after_two_runs(self):
        self.build([prod(1, sizes=["M", "L", "XL"])])
        self.assertIsNotNone(self.ch().post_id("P000001"))
        self.build([prod(1, sizes=["XS", "S"])])                      # ходовые пропали
        self.ch().sync()
        self.build([prod(1, sizes=["L", "S"])])                       # мигание: L вернулся
        self.ch().sync()
        self.assertEqual(self.fake.of("editMessageCaption"), [])
        self.assertEqual(self.state()["posts"]["P000001"]["pop_gone"], 0)
        self.build([prod(1, sizes=["XS", "S"])])
        self.ch().sync()
        self.assertEqual(self.fake.of("editMessageCaption"), [])
        self.ch().sync()                                              # второй запуск подряд — правим
        (edit, _), = self.fake.of("editMessageCaption")
        self.assertIn("\nРазмеры: XS, S\n", edit["caption"])
        self.assertNotIn("Продано", edit["caption"])
        self.assertEqual(edit["reply_markup"]["inline_keyboard"][0][0]["text"], "Заказать")   # кнопки остаются
        self.assertEqual(self.state()["posts"]["P000001"]["sizes"], ["XS", "S"])

    def test_deleted_message_marked_gone(self):
        self.build([prod(1)])
        self.ch().post_id("P000001")
        self.build([prod(1, in_stock=False)])
        self.fake.fail["editMessageCaption"] = {"ok": False, "error_code": 400,
                                                "description": "Bad Request: message to edit not found"}
        res = self.ch().sync()
        self.assertEqual(res["пост удалён"], 1)
        self.assertEqual(self.state()["posts"]["P000001"]["status"], "gone")


class LeakTest(ChannelBase):
    def test_find_leak(self):
        for bad in ("Цена 1500 TL", "1500TL", "99,90 EUR", "₺", "€ 12", "себестоимость", "Маржа 20%", "TRY",
                    "https://www.yoox.com/x", "cdn.dsmcdn.com", "akinon", "Trendyol", "без возврата"):
            self.assertIsNotNone(channel.find_leak(bad), bad)
        for ok in ("HQHGTLB", "P000001", "https://shop.example/yurt/#/catalog?p=TLA2345", "ch:pub:EURO234",
                   "Размеры: 50, 52, 54", "<b>4 140 000 сум</b> <s>16 630 000</s> −75%", "Country", "Бутылка"):
            self.assertIsNone(channel.find_leak(ok), ok)

    def test_leak_blocks_send_and_logs(self):
        self.build([prod(1, brand="Trendyol Basic", title="Рубашка")])
        ch = self.ch()
        self.assertIsNone(ch.post_id("P000001", force=True))
        self.assertEqual(self.fake.calls, [])                         # ничего не отправлено
        self.assertEqual(ch.leaks, [("P000001", "trendyol")])
        log = (self.tmp / "logs" / "channel_leaks.log").read_text(encoding="utf-8")
        self.assertIn("P000001", log)
        self.assertIn("P000001", self.state()["blocked"])
        tg = channel.TG("t", http=self.fake, log=self.logs.append)
        with self.assertRaises(channel.LeakBlocked):
            tg.call("sendMessage", {"chat_id": 1, "text": "ok",
                                    "reply_markup": {"inline_keyboard": [[{"text": "x", "url": "https://www.yoox.com/"}]]}})
        self.assertIsNone(tg.call("sendPhoto", {"chat_id": 1, "photo": "https://cdn.example.com/a.jpg"}))
        self.assertEqual(self.fake.calls, [])                         # фото по ссылке тоже не уходит

    def test_captions_have_no_forbidden_words(self):
        self.build([prod(i, brand=b) for i, b in enumerate(("Boss", "Kiton", "Gucci", "Tod's", "Moorer"), 1)])
        lines: list[str] = []
        self.ch(env={}).plan(5, out=lines.append)
        text = "\n".join(lines).lower()
        for w in FORBIDDEN:
            self.assertNotIn(w, text)


class DryRunTest(ChannelBase):
    def test_dry_run_is_default(self):
        self.build([prod(1), prod(2, brand="Kiton"), prod(3, type="брюки", title="Брюки Boss",
                                                          images=["https://cdn.dsmcdn.com/a/1.jpg",
                                                                  "https://cdn.dsmcdn.com/a/2.jpg"])])
        for env in ({}, {"CHANNEL_ENABLED": "1", "TELEGRAM_BOT_TOKEN": "x"}, {"TELEGRAM_BOT_TOKEN": "x", "CHANNEL_ID": "@c"}):
            ch = self.ch(env=env)
            self.assertTrue(ch.dry)
            self.assertIn("ПРОБНЫЙ", ch.mode_line())
            self.assertEqual(ch.propose(3), 3)
            self.assertIsNotNone(ch.post_auto())
            ch.sync()
        self.assertEqual(self.fake.calls, [])                         # в Telegram — ничего
        self.assertEqual(self.fetch.urls, [])                         # фото не скачивались
        self.assertFalse((self.tmp / "channel_state.json").exists())  # состояние не менялось
        self.assertTrue(any("[пробно] sendPhoto" in s for s in self.logs))
        self.assertTrue(self.ch(dry=True).dry)                        # --plan пробный даже при включённом канале
        self.assertFalse(self.ch().dry)


class TelegramErrorTest(ChannelBase):
    def test_stop_after_telegram_rejects(self):
        self.build([prod(i, brand=b) for i, b in enumerate(("Boss", "Kiton", "Gucci"), 1)])
        self.fake.fail["sendPhoto"] = {"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member"}
        self.assertIsNone(self.ch().post_auto())
        self.assertEqual(len(self.fake.of("sendPhoto")), 1)          # не перебирает все товары впустую
        self.assertEqual(self.state()["posts"], {})


class PhotoTest(ChannelBase):
    def test_download_with_referer_and_stop_on_403(self):
        imgs = lambda k: [f"https://cdn.dsmcdn.com/ty/{k}/1.jpg", f"https://cdn.dsmcdn.com/ty/{k}/2.jpg"]
        self.build([prod(1, images=imgs(1)), prod(2, brand="Kiton", images=imgs(2))])
        post = self.ch().post_id("P000001")
        self.assertEqual(post["file_ids"], [f"FILE{self.fake.n}"])
        url, headers = self.fetch.urls[0]
        self.assertEqual(headers["Referer"], "https://www.trendyol.com/")
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        (params, files), = self.fake.of("sendPhoto")
        self.assertNotIn("photo", params)                             # фото — файлом (multipart), не ссылкой
        self.assertEqual(files["photo"][1], JPEG)
        self.fetch.status = 403
        ch = self.ch()
        self.assertIsNone(ch.post_id("P000002"))
        self.assertEqual(len(self.fetch.urls), 2)                     # один запрос, 403 — без повторов
        self.assertIn("cdn.dsmcdn.com", ch.blocked_hosts)
        self.assertEqual(ch.sendable_photos("P000002"), [])


class ProposeCallbackTest(ChannelBase):
    def setUp(self):
        super().setUp()
        self._saved = {k: getattr(channel, k) for k in ("SITE", "STATE_PATH", "LOCK_PATH", "LOG_DIR", "CONFIG_PATH")}
        self._env = {k: os.environ.get(k) for k in list(ENV_ON) + ["CHANNEL_REVIEW_CHAT_ID", "SHOP_URL", "PUBLIC_URL"]}
        channel.SITE, channel.STATE_PATH = self.site, self.tmp / "channel_state.json"
        channel.LOCK_PATH, channel.LOG_DIR = self.tmp / "channel.lock", self.tmp / "logs"
        cfg_path = self.tmp / "channel.json"
        cfg_path.write_text(json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8")
        channel.CONFIG_PATH = cfg_path
        for k in self._env:
            os.environ.pop(k, None)
        channel._register(tg_bot)                                  # на случай, если другой тест снял «ch:»
        self.bot = tg_bot.Bot("123:abc", admin_ids={ADMIN}, orders_chat=str(ADMIN), http=self.fake,
                              offset_path=self.tmp / "off.json", log=self.logs.append)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(channel, k, v)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def press(self, data, uid=ADMIN):
        n = len(self.fake.calls)
        self.bot.handle_update({"callback_query": {"id": "cb", "from": {"id": uid}, "data": data,
                                                   "message": {"message_id": 7, "chat": {"id": ADMIN, "type": "private"}}}})
        return [c for c in self.fake.calls[n:]]

    def test_propose_publish_skip(self):
        self.build([prod(1), prod(2, brand="Kiton", title="Рубашка Kiton"), prod(3, brand="Bally", gender="women",
                                                                                   sizes=["S", "M"], title="Блузка Bally")])
        os.environ.update(ENV_ON)
        ch = self.ch(now=None)
        self.assertEqual(ch.propose(2), 2)
        sent = self.fake.of("sendPhoto")
        self.assertEqual({p["chat_id"] for p, _ in sent}, {str(ADMIN)})
        kb = sent[0][0]["reply_markup"]["inline_keyboard"]
        code = kb[0][0]["callback_data"].split(":")[2]
        self.assertEqual([b["text"] for b in kb[0]], ["Опубликовать", "Пропустить"])
        self.assertEqual(kb[0][1]["callback_data"], f"ch:skip:{code}")
        props = self.state()["proposals"]
        self.assertEqual({p["status"] for p in props.values()}, {"pending"})
        fid = props[code]["file_ids"][0]

        self.assertEqual(self.press(f"ch:pub:{code}", uid=999)[-1][1]["text"], "Нет доступа")
        calls = self.press(f"ch:pub:{code}")
        post_call = [p for m, p, f in calls if m == "sendPhoto"]
        self.assertEqual(len(post_call), 1)
        self.assertEqual(post_call[0]["chat_id"], CHANNEL)
        self.assertEqual(post_call[0]["photo"], fid)                  # то же фото по file_id, без повторной загрузки
        self.assertIn("Заказать", json.dumps(post_call[0]["reply_markup"], ensure_ascii=False))
        edit = [p for m, p, f in calls if m == "editMessageReplyMarkup"][0]
        self.assertEqual(edit["reply_markup"]["inline_keyboard"][0][0]["text"], "✅ Опубликовано")
        self.assertEqual([p for m, p, f in calls if m == "answerCallbackQuery"][-1]["text"], "Опубликовано в канале")
        st = self.state()
        self.assertEqual(st["proposals"][code]["status"], "published")
        self.assertIn(code, st["posts"])
        again = self.press(f"ch:pub:{code}")
        self.assertEqual([p for m, p, f in again if m == "sendPhoto"], [])
        self.assertEqual([p for m, p, f in again if m == "answerCallbackQuery"][-1]["text"], "Уже опубликовано")

        other = [c for c in props if c != code][0]
        calls = self.press(f"ch:skip:{other}")
        self.assertEqual([p for m, p, f in calls if m == "answerCallbackQuery"][-1]["text"],
                         "Пропущено — этот товар больше не предложу")
        st = self.state()
        self.assertEqual(st["proposals"][other]["status"], "skipped")
        self.assertIn(other, st["skipped"])
        self.assertEqual(self.ch(now=None).propose(5), 1)             # опубликованный и пропущенный больше не предлагаются
        self.assertEqual(self.press("ch:done")[-1][1]["text"], "Уже обработано")

    def test_channel_command_status(self):
        self.build([prod(1)])
        n = len(self.fake.calls)
        self.bot.handle_update({"message": {"message_id": 1, "from": {"id": ADMIN}, "text": "/channel",
                                            "chat": {"id": ADMIN, "type": "private"}}})
        sent = [p for m, p, f in self.fake.calls[n:] if m == "sendMessage"]
        self.assertEqual(len(sent), 1)
        self.assertIn("ПРОБНЫЙ", sent[0]["text"])
        self.assertNotIn("ВНИМАНИЕ", sent[0]["text"])                       # CHANNEL_ENABLED не задан

    def test_status_warns_enabled_without_admins(self):
        self.build([prod(1)])
        env = {**ENV_ON, "TELEGRAM_ADMIN_IDS": ""}
        text = self.ch(env=env).status_text()
        self.assertIn("ВНИМАНИЕ", text)
        self.assertIn("TELEGRAM_ADMIN_IDS", text)
        self.assertIsNone(channel.find_leak(text))
        self.assertNotIn("ВНИМАНИЕ", self.ch(env=ENV_ON).status_text())     # админы заданы — тихо
        out = io.StringIO()
        saved = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            with contextlib.redirect_stdout(out):
                self.assertEqual(channel.main(["--status", "--site", str(self.site), "--config",
                                               str(self.tmp / "channel.json")]), 0)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertIn("ВНИМАНИЕ", out.getvalue())

    def test_review_chat_can_press_when_no_admin_ids(self):
        """TELEGRAM_ADMIN_IDS пуст, чат заказов — группа, предложения — в личку продавца (CHANNEL_REVIEW_CHAT_ID)."""
        self.build([prod(1)])
        bot = tg_bot.Bot("123:abc", admin_ids=set(), orders_chat="-100500", review_chat=str(ADMIN), http=self.fake,
                         offset_path=self.tmp / "off2.json", log=self.logs.append)
        n = len(self.fake.calls)
        bot.handle_update({"callback_query": {"id": "cb", "from": {"id": ADMIN}, "data": "ch:skip:P000001",
                                              "message": {"message_id": 7, "chat": {"id": ADMIN, "type": "private"}}}})
        answers = [p for m, p, f in self.fake.calls[n:] if m == "answerCallbackQuery"]
        self.assertNotEqual(answers[-1].get("text"), "Нет доступа")

    def test_publish_needs_enabled_channel(self):
        self.build([prod(1)])
        calls = self.press("ch:pub:P000001")
        self.assertEqual(calls[-1][1]["text"], "Канал выключен: нужны CHANNEL_ENABLED=1 и CHANNEL_ID")
        self.assertEqual([m for m, p, f in calls if m.startswith("send")], [])

    def test_plugin_registration(self):
        self.assertIn("channel", tg_bot.load_plugins(names="channel"))
        prefixes = [p for p, h, a in tg_bot._CALLBACKS]
        self.assertIn("ch:", prefixes)
        self.assertIn("channel", tg_bot._COMMANDS)


class PlanOutputTest(ChannelBase):
    def test_plan_prints_breakdown_and_caption(self):
        self.build([prod(1), prod(2, brand="Kiton", gender="women", sizes=["S", "M"], title="Платье Kiton", type="платья")])
        lines: list[str] = []
        picked = self.ch(env=ENV_ON, dry=True).plan(20, out=lines.append)
        text = "\n".join(lines)
        self.assertEqual(len(picked), 2)
        self.assertIn("итог", text)
        self.assertIn("рейтинг", text)
        self.assertIn("свежесть", text)
        self.assertIn("экономия", text)
        self.assertIn("Оригинал · предоплата 50% · доставка до 10 дней", text)
        self.assertIn("Квоты дня позволяют только 2 из 20.", text)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
