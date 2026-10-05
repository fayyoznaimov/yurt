"""Воронка (funnel.py): анкета в боте, личные подборки, брошенные корзины, «Всё подошло?», рекомендации, отчёт.
Маленький сайт собирается catalog_files.write_site, Bot API подменён (FakeTelegram) — в сеть ничего не уходит.

    python -m unittest tests.test_funnel
"""
from __future__ import annotations

import json
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
import funnel  # noqa: E402
import orders_db  # noqa: E402
import ranking  # noqa: E402
import tg_bot  # noqa: E402

NOW = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)          # 10:00 по Ташкенту
TOKEN = "123:TEST"
ADMIN = 111
SELLER_CHAT = -100500
USER = 4242
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 4000
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_IDS": str(ADMIN), "TELEGRAM_ORDERS_CHAT_ID": str(SELLER_CHAT),
       "SHOP_URL": "https://shop.example/yurt/", "BOT_USERNAME": "ipak_shop_bot", "SELLER_TELEGRAM": "ipak_seller"}
FORBIDDEN = ("yoox", "trendyol", "akinon", "dsmcdn", "€", "₺", "себестоим", "маржа", "закуп", "возврат")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prod(pid: str, **kw) -> dict:
    p = {"id": pid, "brand": "Boss", "title": "Джинсы, синие", "type": "джинсы", "gender": "men", "origin": "IT",
         "price_uzs": 3_000_000, "discount_pct": 50.0, "sizes": ["30", "32"], "sizes_out": [], "size_system": "W",
         "color": "синий", "composition": "100% хлопок", "details": [], "description": "Описание",
         "images": [f"img/p/{pid}-1.jpg", f"img/p/{pid}-2.jpg"], "in_stock": True,
         "fetched_at": "2026-10-04T10:00:00Z", "first_seen": iso(NOW - timedelta(hours=10))}
    p.update(kw)
    return p


CATALOG = [
    prod("JEANS01"),                                                            # подходит: джинсы W32, Boss
    prod("JEANS02", brand="Liu Jo Man", sizes=["34", "36"]),                   # размер не тот (W34/W36)
    prod("JEANS03", price_uzs=12_000_000),                                     # дороже бюджета
    prod("JEANS04", first_seen=iso(NOW - timedelta(days=30))),                 # старый (не новинка)
    prod("SHOES01", title="Кроссовки, белые", type="обувь", sizes=["41", "42.5"], size_system="EU"),   # 42½ → 42
    prod("SHOES02", title="Кроссовки, чёрные", type="обувь", sizes=["44", "45"], size_system="EU"),     # не тот размер
    prod("DRESS01", title="Платье, чёрное", type="платья", gender="women", sizes=["S", "M"], size_system="INT"),
    prod("SHIRT01", title="Рубашка, белая", type="рубашки", sizes=["M", "L"], size_system="INT", in_stock=False),
    prod("JEANS05", brand="Boss", sizes=["32"], discount_pct=70.0),            # подходит
    prod("JEANS06", brand="Boss", sizes=["32"]),
    prod("JEANS07", brand="Boss", sizes=["32"]),
    prod("JEANS08", brand="Boss", sizes=["32"]),
]


class FakeTelegram:
    """Bot API: http(method, params, files=None) — и для channel.TG, и для tg_bot.Bot."""

    def __init__(self):
        self.calls: list[tuple[str, dict, dict | None]] = []
        self.n = 700
        self.fail: dict[str, dict] = {}

    def __call__(self, method, params, files=None):
        self.calls.append((method, json.loads(json.dumps(params, ensure_ascii=False)), files))
        if method in self.fail:
            return None, self.fail[method]
        if method in ("sendMessage", "sendPhoto"):
            self.n += 1
            return {"message_id": self.n, "chat": {"id": params.get("chat_id")}}, {"ok": True}
        if method == "sendMediaGroup":
            out = []
            for _ in params["media"]:
                self.n += 1
                out.append({"message_id": self.n, "chat": {"id": params.get("chat_id")}})
            return out, {"ok": True}
        return True, {"ok": True}

    def of(self, method):
        return [p for m, p, f in self.calls if m == method]

    def texts(self):
        out = []
        for m, p, f in self.calls:
            out.append(json.dumps(p, ensure_ascii=False))
        return "\n".join(out)


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def cq(data, uid=USER, chat=USER, message_id=None, cid="cb1"):
    return {"callback_query": {"id": cid, "from": {"id": uid, "first_name": "Алишер", "username": "real_name"},
                               "data": data, "message": {"message_id": message_id or 0, "chat": {"id": chat, "type": "private"}}}}


def msg(text, uid=USER, username="real_name", **extra):
    m = {"message_id": 77, "from": {"id": uid, "first_name": "Алишер", "username": username},
         "chat": {"id": uid, "type": "private"}, "text": text}
    m.update(extra)
    if "photo" in extra:
        m.pop("text")
    return {"message": m}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.site = self.tmp / "site"
        self.db = orders_db.OrdersDB(self.tmp / "orders.sqlite")
        self.fake = FakeTelegram()
        self.clock = FakeClock()
        self.logs: list[str] = []
        self.cfg = channel.load_config(ROOT / "channel.json")
        self.cfg.update(site_url="https://shop.example/yurt/", mini_app_url="", contact_url="")
        self.build(CATALOG)
        funnel._SUMMARY.update(sig=None, dir=None, data=None)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, products):
        admin = {"_meta": {}}
        for p in products:
            admin[p["id"]] = {"source": "yoox", "source_item_id": p["id"], "url": "https://www.yoox.com/x",
                              "title_original": "Jeans", "cost_uzs": 1, "margin_uzs": 1, "price_now": 10, "currency": "EUR"}
            for u in p.get("images") or []:
                f = self.site / u
                f.parent.mkdir(parents=True, exist_ok=True)
                if not f.exists():
                    f.write_bytes(JPEG)
        cf.write_site(self.site, {}, {"name": "t"}, products, admin)

    def funnel(self, now=NOW, **kw) -> funnel.Funnel:
        args = dict(token=TOKEN, http=self.fake, site_dir=self.site, events_dir=self.tmp / "events", env=ENV, now=now,
                    sleep=self.clock.sleep, clock=self.clock, log=self.logs.append, cfg=self.cfg,
                    messages_path=ROOT / "server" / "order_messages.json", ranker=ranking.Ranker({}, 0.5),
                    fetch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("скачивание в тестах")))
        args.update(kw)
        return funnel.Funnel(self.db, **args)

    def bot(self, **kw) -> tg_bot.Bot:
        b = tg_bot.Bot(TOKEN, db=self.db, admin_ids={ADMIN}, orders_chat=str(SELLER_CHAT),
                       shop_url="https://shop.example/yurt/", bot_username="ipak_shop_bot", seller_telegram="ipak_seller",
                       messages_path=ROOT / "server" / "order_messages.json", offset_path=self.tmp / "off.json",
                       http=lambda m, p: self.fake(m, p), log=self.logs.append, **kw)
        b.site_dir = self.site
        b.events_dir = self.tmp / "events"
        return b

    def prefs(self, uid=USER, gender="men", types=("джинсы", "обувь"), sizes=None, brands=("Boss",), budget="2",
              opt_in=None):
        self.db.touch_tg_user(uid, "real_name", started=True)
        self.db.activate_prefs(uid, gender, list(types), sizes or {"jeans": ["W32"], "shoes": ["42"]}, list(brands),
                               budget, at=opt_in or iso(NOW - timedelta(days=1)))


# ---------------------------------------------------------------- подбор

class MatchTest(Base):
    def test_matches_and_groups(self):
        rows = channel.Site(self.site).rows
        p = {"gender": "men", "types": ["джинсы", "обувь"], "sizes": {"jeans": ["W32"], "shoes": ["42"]},
             "brands": ["Boss"], "budget": "2"}
        ok = {pid for pid, r in rows.items() if funnel.matches(p, r)}
        self.assertEqual(ok, {"JEANS01", "JEANS04", "JEANS05", "JEANS06", "JEANS07", "JEANS08", "SHOES01"})
        self.assertIn("W32", funnel.size_keys(rows["JEANS01"]))             # нормализованные ключи zk
        self.assertEqual(funnel.groups_for("обувь", "men"), ["shoes"])
        self.assertEqual(funnel.groups_for("рубашки", "men"), ["letters", "it_m", "collar"])
        self.assertEqual(funnel.groups_for("платья", "women"), ["letters", "it_w"])
        self.assertEqual(funnel.groups_for("сумки", "women"), [])
        self.assertTrue(funnel.size_match({"40"}, ["40½"]))
        self.assertFalse(funnel.size_match({"40"}, ["41"]))
        # без размеров и брендов — всё мужское в бюджете (кроме распроданного)
        p2 = {"gender": "men", "types": [], "sizes": {}, "brands": [], "budget": "0"}
        ok2 = {pid for pid, r in rows.items() if funnel.matches(p2, r)}
        self.assertNotIn("DRESS01", ok2)
        self.assertNotIn("SHIRT01", ok2)                                    # нет в наличии
        self.assertIn("JEANS03", ok2)

    def test_throttle(self):
        clock = FakeClock()
        th = funnel.Throttle(per_chat_s=1.0, per_sec=25, sleep=clock.sleep, clock=clock)
        th.wait("a")
        th.wait("a")                                                        # тот же чат — ждёт секунду
        self.assertAlmostEqual(sum(clock.slept), 1.0)
        clock.slept.clear()
        for i in range(30):                                                 # 30 разных чатов — не больше 25 в секунду
            th.wait(f"c{i}")
        self.assertGreater(sum(clock.slept), 0.5)


# ---------------------------------------------------------------- анкета

class PicksTest(Base):
    def test_start_shows_picks_button_and_full_flow(self):
        bot = self.bot()
        bot.handle_update(msg("/start s_Insta"))
        r = self.fake.of("sendMessage")[-1]
        rows = r["reply_markup"]["inline_keyboard"]
        self.assertEqual(rows[0][0]["web_app"]["url"], "https://shop.example/yurt/?from=insta")   # метку забирает сайт
        self.assertEqual(rows[1][0], {"text": "Подобрать вещи моего размера", "callback_data": "pf:go"})
        u = self.db.tg_user(USER)
        self.assertEqual((u["src_first"], u["src_last"]), ("insta", "insta"))
        self.assertTrue(u["started_at"])

        bot.handle_update(cq("pf:go"))                                      # новое сообщение с первым шагом
        first = self.fake.of("sendMessage")[-1]
        mid = self.fake.n
        self.assertIn("Для кого", first["text"])
        self.assertEqual([b["callback_data"] for b in first["reply_markup"]["inline_keyboard"][0]], ["pf:g:m", "pf:g:w"])

        def click(data):
            bot.handle_update(cq(data, message_id=mid))
            return self.fake.of("editMessageText")[-1]

        e = click("pf:g:m")
        labels = [b["text"] for row in e["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn("Джинсы", labels)
        self.assertIn("Обувь", labels)
        self.assertNotIn("Платья", labels)                                  # женское не предлагается
        types = self.db.get_prefs(USER)["draft"]["opts"]
        e = click(f"pf:t:{types.index('джинсы')}")
        self.assertIn("✓ Джинсы", [b["text"] for row in e["reply_markup"]["inline_keyboard"] for b in row])
        click(f"pf:t:{types.index('обувь')}")
        e = click("pf:td")                                                  # размеры: буквы → IT → талия → обувь
        self.assertIn("буквенный", e["text"])
        e = click("pf:zd")
        self.assertIn("итальянский", e["text"])
        e = click("pf:zd")
        self.assertIn("талии", e["text"])
        e = click(f"pf:z:{funnel.SIZE_GROUPS['jeans'][1].index('W32')}")
        self.assertIn("✓ W32", [b["text"] for row in e["reply_markup"]["inline_keyboard"] for b in row])
        e = click("pf:zd")
        self.assertIn("обуви", e["text"])
        click(f"pf:z:{funnel.SIZE_GROUPS['shoes'][1].index('42')}")
        e = click("pf:zd")                                                  # бренды
        self.assertIn("бренды", e["text"])
        bopts = self.db.get_prefs(USER)["draft"]["bopts"]
        self.assertIn("Boss", bopts)
        self.assertNotIn("", bopts)
        click(f"pf:b:{bopts.index('Boss')}")
        e = click("pf:bd")
        self.assertIn("Бюджет", e["text"])
        e = click("pf:u:2")
        self.assertIn("не чаще раза в день", e["text"])
        self.assertIn("/stop", e["text"])
        self.assertIn("/settings", e["text"])
        self.assertIn("W32", e["text"])
        p = self.db.get_prefs(USER)
        self.assertEqual((p["gender"], sorted(p["types"]), p["sizes"], p["brands"], p["budget"]),
                         ("men", ["джинсы", "обувь"], {"jeans": ["W32"], "shoes": ["42"]}, ["Boss"], "2"))
        self.assertTrue(p["opt_in_at"])
        self.assertIsNone(p["stopped_at"])
        self.assertEqual(p["draft"], None)
        self.assertEqual([x["telegram_user_id"] for x in self.db.active_prefs()], [USER])
        click("pf:td")                                                      # старая кнопка после сохранения
        self.assertIn("устарела", self.fake.of("editMessageText")[-1]["text"])

        bot.handle_update(msg("/start p_jeans01-s_blog_1"))                  # товар + метка
        self.assertEqual(self.fake.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"],
                         "https://shop.example/yurt/?from=blog_1#/catalog?p=JEANS01")
        self.assertEqual(len(self.fake.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"]), 1)   # без анкеты
        self.assertEqual((self.db.tg_user(USER)["src_first"], self.db.tg_user(USER)["src_last"]), ("insta", "blog_1"))
        bot.handle_update(msg("/stop"))
        self.assertEqual(self.db.active_prefs(), [])
        bot.handle_update(msg("/settings"))                                 # заново, с прежними ответами
        self.assertIn("Для кого", self.fake.of("sendMessage")[-1]["text"])
        self.assertEqual(self.db.get_prefs(USER)["draft"]["types"], ["джинсы", "обувь"])
        self.assertNotIn(FORBIDDEN[0], self.fake.texts().lower())

    def test_cancel_and_foreign_message(self):
        bot = self.bot()
        bot.handle_update(cq("pf:go"))
        mid = self.fake.n
        bot.handle_update(cq("pf:g:w", message_id=mid + 5))                 # чужое/старое сообщение анкеты
        self.assertIn("устарела", self.fake.of("editMessageText")[-1]["text"])
        bot.handle_update(cq("pf:x", message_id=mid))
        self.assertIn("/settings", self.fake.of("editMessageText")[-1]["text"])
        self.assertIsNone(self.db.get_prefs(USER)["draft"])
        self.assertEqual(self.db.active_prefs(), [])


# ---------------------------------------------------------------- подборки

class DigestTest(Base):
    def test_digest_sends_best_five_and_never_repeats(self):
        self.prefs()
        f = self.funnel()
        st = f.digest()
        self.assertEqual(st["подборок отправлено"], 1)
        album = self.fake.of("sendMediaGroup")
        self.assertEqual(len(album), 1)
        self.assertEqual(album[0]["chat_id"], USER)
        self.assertLessEqual(len(album[0]["media"]), 5)
        text_msg = self.fake.of("sendMessage")[-1]
        self.assertEqual(text_msg["chat_id"], USER)
        self.assertIn("Новинки вашего размера", text_msg["text"])
        buttons = [b for row in text_msg["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(len(buttons), 5)
        sent = self.db.sent_ids(USER)
        self.assertEqual(len(sent), 5)
        self.assertTrue(sent <= {"JEANS01", "JEANS05", "JEANS06", "JEANS07", "JEANS08", "SHOES01"})
        self.assertIn("JEANS05", sent)                                      # большая скидка — выше в ranking
        for b in buttons:
            self.assertRegex(b["url"], r"^https://shop\.example/yurt/#/catalog\?p=[A-Z0-9]{7}$")
        self.assertNotIn("JEANS04", sent)                                   # старый товар — не новинка
        low = self.fake.texts().lower()
        for w in FORBIDDEN:
            self.assertNotIn(w, low)
        self.assertGreaterEqual(sum(self.clock.slept), 0.99)               # альбом и список — с паузой 1 с

        # шестой подходящий (не вошёл в пятёрку) уже не новинка; новый товар после подборки — придёт завтра
        self.build(CATALOG + [prod("JEANS09", first_seen=iso(NOW + timedelta(hours=12)))])
        n = len(self.fake.calls)
        f2 = self.funnel(now=NOW + timedelta(days=1))
        f2.digest()
        msgs = self.fake.calls[n:]
        sent2 = self.db.sent_ids(USER) - sent
        self.assertEqual(sent2, {"JEANS09"})
        self.assertEqual([m for m, _, _ in msgs], ["sendPhoto"])            # одно фото — подпись со списком и кнопками
        self.assertEqual(msgs[0][1]["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://shop.example/yurt/#/catalog?p=JEANS09")
        n = len(self.fake.calls)
        self.funnel(now=NOW + timedelta(days=2)).digest()                   # больше нечего — ничего не шлём
        self.assertEqual(len(self.fake.calls), n)

    def test_price_drop_and_mini_app_links(self):
        self.prefs(types=("джинсы",), brands=(), sizes={"jeans": ["W32"]})
        self.funnel().digest()                                              # цены запомнены, 5 отправлено
        self.db.add_sent(USER, ["JEANS01", "JEANS05", "JEANS06", "JEANS07", "JEANS08"])
        cat = [dict(p) for p in CATALOG]
        for p in cat:
            if p["id"] == "JEANS04":
                p["price_uzs"] = 2_600_000                                  # −13%: старый товар подешевел
            if p["id"] == "JEANS02":
                p["price_uzs"] = 2_900_000                                  # −3% (и размер не тот)
        self.build(cat)
        self.cfg["mini_app_url"] = "https://t.me/ipak_shop_bot/shop"
        n = len(self.fake.calls)
        f = self.funnel(now=NOW + timedelta(days=1), env=dict(ENV, FUNNEL_DIGEST_PHOTOS="0"))
        st = f.digest()
        self.assertEqual(st["подешевели на ≥10%"], 1)
        self.assertEqual(self.db.price_drops()["JEANS04"]["drop_from"], 3_000_000)
        out = [p for m, p, _ in self.fake.calls[n:] if m == "sendMessage"]
        self.assertEqual(len(out), 1)
        self.assertIn("цена снижена", out[0]["text"])
        self.assertEqual(out[0]["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://t.me/ipak_shop_bot/shop?startapp=p_JEANS04")

    def test_no_matches_blocked_and_leak(self):
        self.prefs(budget="1")                                              # до 1,5 млн — ничего нет
        self.funnel().digest()
        self.assertEqual(self.fake.calls, [])
        self.prefs()
        self.fake.fail["sendMediaGroup"] = {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}
        self.funnel().digest()
        self.assertEqual(self.db.active_prefs(), [])                        # заблокировал — подборки выключены
        self.assertTrue(self.db.tg_user(USER)["blocked_at"])
        self.assertEqual(self.db.sent_ids(USER), set())
        # утечка в названии — сообщение не уходит
        self.fake.fail.clear()
        self.db.touch_tg_user(USER, started=True)                           # снова написал боту
        self.prefs()
        cat = [dict(p, title="Джинсы Trendyol") if p["id"].startswith("JEANS") else p for p in CATALOG]
        self.build(cat)
        n = len(self.fake.calls)
        st = self.funnel(env=dict(ENV, FUNNEL_DIGEST_PHOTOS="0")).digest()
        self.assertEqual(len(self.fake.calls), n)
        self.assertEqual(st["утечка — не отправлено"], 1)
        self.assertEqual(self.db.sent_ids(USER), set())

    def test_dry_writes_nothing(self):
        self.prefs()
        self.funnel(dry=True).digest()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.db.sent_ids(USER), set())
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM price_seen")[0][0], 0)


# ---------------------------------------------------------------- корзины

class CartTest(Base):
    def test_abandoned_cart_once(self):
        old = funnel.local_naive(NOW - timedelta(hours=25))
        self.db.touch_tg_user(USER, started=True)
        self.db.set_cart(USER, [{"id": "JEANS01", "size": "32", "qty": 2}, {"id": "SHIRT01", "size": "M", "qty": 1}], at=old)
        self.db.set_cart(777, [{"id": "JEANS01", "size": "32", "qty": 1}], at=old)        # не писал боту — нельзя
        self.db.set_cart(888, [{"id": "JEANS01", "size": "32", "qty": 1}],
                         at=funnel.local_naive(NOW - timedelta(hours=2)))                  # свежая корзина
        st = self.funnel().carts()
        self.assertEqual(st["напомнили"], 1)
        out = self.fake.of("sendMessage")
        self.assertEqual([m["chat_id"] for m in out], [USER])
        self.assertIn("Вы оставили в корзине", out[0]["text"])
        self.assertIn("Boss — Джинсы, синие, размер 32 × 2", out[0]["text"])
        self.assertNotIn("Рубашка", out[0]["text"])                         # распроданное не показываем
        self.assertIn("Оформить заказ?", out[0]["text"])
        self.assertEqual(out[0]["reply_markup"]["inline_keyboard"][0][0],
                         {"text": "Оформить заказ", "web_app": {"url": "https://shop.example/yurt/#/cart"}})
        self.funnel(now=NOW + timedelta(hours=1)).carts()                   # второй раз — никогда
        self.assertEqual(len(self.fake.of("sendMessage")), 1)
        # та же корзина ещё раз — не новая; другая корзина — новое напоминание через сутки
        self.assertEqual(self.db.set_cart(USER, [{"id": "JEANS01", "size": "32", "qty": 2},
                                                 {"id": "SHIRT01", "size": "M", "qty": 1}]), "same")
        self.assertEqual(self.db.set_cart(USER, [], at=old), "cleared")
        self.assertIsNone(self.db.get_cart(USER))

    def test_no_reminder_after_order(self):
        old = funnel.local_naive(NOW - timedelta(hours=30))
        self.db.set_cart(USER, [{"id": "JEANS01", "size": "32", "qty": 1}], at=old)
        self.db.add_order(order_no="YR-261005-AAAA", customer={"name": "А", "phone": "+998901234567"}, items=[],
                          total_uzs=1000, created_at=funnel.local_naive(NOW - timedelta(hours=29)),
                          telegram_user={"id": USER})                     # заказ после корзины + привязан
        self.funnel().carts()
        self.assertEqual(self.fake.of("sendMessage"), [])


# ---------------------------------------------------------------- после выдачи и рекомендации

class FollowupTest(Base):
    def delivered_order(self, no="YR-261001-AAAA", uid=USER, days=8):
        self.db.add_order(order_no=no, customer={"name": "Алишер", "phone": "+998901234567", "telegram": "alisher_t"},
                          items=[{"id": "JEANS01", "brand": "Boss", "title": "Джинсы", "size": "32", "qty": 1,
                                  "price_uzs": 3_000_000, "source": "yoox", "cost_uzs": 1}],
                          total_uzs=3_000_000, telegram_user={"id": uid, "username": "real_name"} if uid else None)
        self.db.set_status(no, "delivered", force=True)
        with self.db.tx() as c:
            c.execute("UPDATE orders SET status_at=? WHERE order_no=?",
                      (funnel.local_naive(NOW - timedelta(days=days)), no))

    def test_followup_ok_referral_and_photo(self):
        self.delivered_order()
        self.delivered_order("YR-261003-BBBB", days=3)                     # меньше 7 дней — рано
        self.delivered_order("YR-261001-CCCC", uid=None)                   # нет Telegram
        st = self.funnel().followup()
        self.assertEqual(st["спросили"], 1)
        ask = self.fake.of("sendMessage")[-1]
        self.assertEqual(ask["chat_id"], USER)
        self.assertIn("Всё подошло?", ask["text"])
        self.assertEqual([b["callback_data"] for b in ask["reply_markup"]["inline_keyboard"][0]],
                         ["fu:ok:YR-261001-AAAA", "fu:help:YR-261001-AAAA"])
        self.assertEqual(self.db.get_order("YR-261001-AAAA")["followup_result"], "asked")
        self.funnel(now=NOW + timedelta(days=1)).followup()                # не повторяется
        self.assertEqual(len(self.fake.of("sendMessage")), 1)

        bot = self.bot()
        bot.handle_update(cq("fu:ok:YR-261001-AAAA", uid=999))             # чужой заказ
        self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Заказ не найден")
        bot.handle_update(cq("fu:ok:YR-261001-AAAA", message_id=5))
        thanks = self.fake.of("sendMessage")[-1]
        self.assertEqual(thanks["chat_id"], USER)
        self.assertIn("фото-отзыв", thanks["text"])
        code = self.db.tg_user(USER)["ref_code"]
        self.assertRegex(code, r"^[a-z2-7]{8}$")
        self.assertIn(f"https://t.me/ipak_shop_bot?start=r_{code}", thanks["text"])
        self.assertIn("Подарочный сертификат 200 000 сум", thanks["text"])
        self.assertNotIn("возврат", thanks["text"].lower())
        self.assertEqual(self.db.get_order("YR-261001-AAAA")["followup_result"], "ok")
        self.assertEqual(self.fake.of("editMessageReplyMarkup")[-1]["reply_markup"], {"inline_keyboard": []})

        bot.handle_update(msg("", photo=[{"file_id": "x1"}]))              # фото-отзыв → продавцу
        fwd = self.fake.of("forwardMessage")[-1]
        self.assertEqual((fwd["chat_id"], fwd["from_chat_id"], fwd["message_id"]), (str(SELLER_CHAT), USER, 77))
        self.assertTrue(any("Фото-отзыв по заказу YR-261001-AAAA" in p["text"] for p in self.fake.of("sendMessage")
                            if p["chat_id"] == str(SELLER_CHAT)))
        self.assertIn("Спасибо за фото", self.fake.of("sendMessage")[-1]["text"])

        # друг приходит по ссылке → его заказ из Mini App записывает рекомендацию
        friend = 5555
        bot.handle_update(msg(f"/start r_{code}", uid=friend, username="friend_1"))
        self.assertEqual(self.db.tg_user(friend)["referred_by"], USER)
        bot.handle_update(msg(f"/start r_{code}", uid=USER))                 # сам себя — нет
        self.assertIsNone(self.db.tg_user(USER)["referred_by"])
        r = self.db.add_order(order_no="YR-261005-FRND", customer={"name": "Друг", "phone": "+998907777777"}, items=[],
                              total_uzs=2_000_000, telegram_user={"id": friend, "username": "friend_1"})
        o = self.db.get_order(r["order_no"])
        self.assertEqual((o["ref_user_id"], o["ref_code"]), (USER, code))
        report = funnel.build_report(self.db, self.tmp / "events", 7, datetime.now(timezone.utc) + timedelta(minutes=1))
        self.assertIn("Рекомендации", report)
        self.assertIn("@real_name", report)

    def test_followup_help_notifies_seller(self):
        self.delivered_order()
        self.funnel().followup()
        bot = self.bot()
        bot.handle_update(cq("fu:help:YR-261001-AAAA", message_id=5))
        seller = [p for p in self.fake.of("sendMessage") if p["chat_id"] == str(SELLER_CHAT)][-1]
        self.assertIn("Нужна помощь с размером: заказ YR-261001-AAAA", seller["text"])
        self.assertEqual(seller["reply_markup"]["inline_keyboard"][0][0]["url"], "https://t.me/real_name")
        self.assertIn("Продавец напишет", self.fake.of("sendMessage")[-1]["text"])
        self.assertEqual(self.db.get_order("YR-261001-AAAA")["followup_result"], "help")
        bot.handle_update(cq("fu:ok:YR-261001-AAAA", message_id=5))        # второй ответ не принимается
        self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Уже отмечено, спасибо!")
        bot.handle_update(msg("", photo=[{"file_id": "x1"}]))              # фото без просьбы — не пересылаем
        self.assertEqual(self.fake.of("forwardMessage"), [])


# ---------------------------------------------------------------- отчёт

class ReportTest(Base):
    def write_events(self, recs):
        d = self.tmp / "events"
        d.mkdir(exist_ok=True)
        by_day = {}
        for r in recs:
            by_day.setdefault(r["day"], []).append(r)
        for day, rs in by_day.items():
            (d / f"{day}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rs) + "битая строка\n",
                                            encoding="utf-8")

    def ev(self, s, src, evs, hours_ago=1):
        t = NOW - timedelta(hours=hours_ago)
        return {"ts": int(t.timestamp()), "day": t.astimezone(funnel.TZ).date().isoformat(), "s": s, "src": src, "ev": evs}

    def test_report_numbers_and_sending(self):
        self.write_events([
            self.ev("a" * 12, "insta", [{"t": "visit", "route": "#/"}, {"t": "view", "id": "JEANS01"}]),
            self.ev("a" * 12, "", [{"t": "cart", "id": "JEANS01"}, {"t": "co"}, {"t": "order", "no": "YR-261005-AAAA"}]),
            self.ev("b" * 12, "insta", [{"t": "view", "id": "SHOES01"}, {"t": "q", "q": "Gucci сумка", "n": 0}]),
            self.ev("c" * 12, "", [{"t": "visit"}, {"t": "view", "id": "SHOES01"}, {"t": "cart", "id": "SHOES01"},
                                    {"t": "order_tg"}]),
            self.ev("d" * 12, "tg_channel", [{"t": "q", "q": "gucci  сумка", "n": 0}, {"t": "q", "q": "boss", "n": 5}]),
            self.ev("e" * 12, "insta", [{"t": "view", "id": "OLD0001"}], hours_ago=24 * 9),   # вне периода
        ])
        self.db.add_order(order_no="YR-261005-AAAA", customer={"name": "А", "phone": "+998901234567"},
                          items=[{"id": "JEANS01", "price_uzs": 3_000_000, "qty": 1}], total_uzs=3_000_000,
                          src_first="insta", src_last="insta", created_at=funnel.local_naive(NOW - timedelta(hours=1)))
        self.db.add_order(order_no="YR-261005-BBBB", customer={"name": "Б", "phone": "+998901234568"}, items=[],
                          total_uzs=5_000_000, created_at=funnel.local_naive(NOW - timedelta(hours=2)))
        self.db.set_status("YR-261005-BBBB", "cancelled")
        f = self.funnel()
        printed = []
        text = f.report(7, out=printed.append)
        self.assertEqual(printed, [text])
        self.assertIn("Всего: сессий 4 → смотрели товар 3 (75%) → корзина 2 (67%) → оформление 1 (50%) → заказы 2", text)
        self.assertIn("[сайт 1, через Telegram 1]", text)
        self.assertIn("• insta: сессий 2 → смотрели товар 2", text)
        self.assertIn("• (без метки): сессий 1", text)
        self.assertIn("выручка 3 000 000 сум", text)
        self.assertIn("Заказы в базе: 2 (отменено 1), выручка без отменённых: 3 000 000 сум", text)
        self.assertIn("1. SHOES01 · Boss — Кроссовки, белые — 2 сесс.", text)   # JEANS01 заказан — не в списке
        self.assertNotIn("JEANS01 ·", text)
        self.assertNotIn("OLD0001", text)
        self.assertIn("• «gucci сумка» — 2", text)
        self.assertNotIn("boss»", text)
        self.assertIn("• insta — 1 зак., 3 000 000 сум (впервые пришли отсюда: 1)", text)
        sent = self.fake.of("sendMessage")
        self.assertEqual([p["chat_id"] for p in sent], [ADMIN])
        self.assertEqual(sent[0]["text"], text)

        bot = self.bot()                                                    # /report у бота — только админ
        bot.handle_update({"message": {"message_id": 1, "from": {"id": 999}, "chat": {"id": 999, "type": "private"},
                                       "text": "/report"}})
        self.assertEqual(len(self.fake.of("sendMessage")), 1)
        bot.handle_update({"message": {"message_id": 1, "from": {"id": ADMIN}, "chat": {"id": ADMIN, "type": "private"},
                                       "text": "/report 30"}})
        self.assertIn("Воронка за 30 дн.", self.fake.of("sendMessage")[-1]["text"])

    def test_cli_report_no_send(self):
        import io
        import contextlib
        import os
        old = os.environ.get("ORDERS_DB_PATH")
        os.environ["ORDERS_DB_PATH"] = str(self.tmp / "cli.sqlite")
        saved_data = funnel.DATA
        funnel.DATA = self.tmp
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = funnel.main(["--report", "--no-send", "--site", str(self.site)])
            self.assertEqual(rc, 0)
            self.assertIn("Воронка за 7 дн.", buf.getvalue())
            self.assertFalse((self.tmp / "funnel-report.lock").exists())
        finally:
            funnel.DATA = saved_data
            if old is None:
                os.environ.pop("ORDERS_DB_PATH", None)
            else:
                os.environ["ORDERS_DB_PATH"] = old


if __name__ == "__main__":
    unittest.main()
