"""База заказов orders_db.py: покупатели, заказы, статусы, привязка Telegram, экспорт, копии, потоки."""
from __future__ import annotations

import csv
import gzip
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orders_db  # noqa: E402

ITEMS = [{"id": "AAAAAA2", "brand": "Boss", "title": "Брюки Boss", "size": "48", "qty": 2, "price_uzs": 1290000,
          "ok": True, "source": "yoox", "shop": "YOOX", "url": "https://www.yoox.com/it/123AB/item",
          "price_now": 61.0, "currency": "EUR", "cost_uzs": 990000, "margin_uzs": 300000, "source_item_id": "123AB"},
         {"id": "BBBBBB3", "brand": "Cacharel", "title": "Рубашка", "size": "M", "qty": 1, "price_uzs": 590000,
          "ok": False, "source": "cacharel_tr", "shop": "Cacharel TR", "url": "https://www.cacharel.com.tr/x/",
          "price_now": 999.0, "currency": "TRY", "cost_uzs": 290000}]
CUST = {"name": "Алишер", "phone": "+998901234567", "telegram": "Alisher_T", "city": "Ташкент"}
TOKEN = "123456:TEST-token-not-real"


def sig(no: str, token: str = TOKEN) -> str:
    return orders_db.link_sig(token, no)


class OrdersDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "orders" / "orders.sqlite"
        self.db = orders_db.OrdersDB(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, no="YR-261005-AAAA", **kw):
        args = dict(order_no=no, customer=CUST, items=ITEMS, total_uzs=3170000)
        args.update(kw)
        return self.db.add_order(**args)

    def test_schema_and_migrate_idempotent(self):
        self.db.migrate()
        db2 = orders_db.OrdersDB(self.path)                  # второй раз — без ошибок
        try:
            tables = {r[0] for r in db2.query("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"customers", "customer_cids", "orders", "order_events", "meta"} <= tables)
            cols = {r["name"] for r in db2.query("PRAGMA table_info(customers)")}
            for c in ("phone", "telegram", "telegram_user_id", "name", "city", "first_seen", "last_order_at",
                      "orders_count", "marketing_opt_in", "opt_in_at", "note", "deleted_at"):
                self.assertIn(c, cols)
            cols = {r["name"] for r in db2.query("PRAGMA table_info(orders)")}
            for c in ("order_no", "client_order_id", "customer_id", "created_at", "status", "items_json", "total_uzs",
                      "prepay_uzs", "source", "tg_message_id", "tg_chat_id", "order_name", "order_phone", "order_city",
                      "order_telegram", "telegram_user_id", "telegram_username", "telegram_linked_at",
                      "marketing_opt_in"):
                self.assertIn(c, cols)
            self.assertEqual(db2.query("PRAGMA journal_mode")[0][0].lower(), "wal")
            self.assertEqual(db2.query("SELECT value FROM meta WHERE key='schema_version'")[0][0],
                             str(orders_db.SCHEMA_VERSION))
        finally:
            db2.close()

    def test_prepay(self):
        self.assertEqual(orders_db.prepay_for(3170000), 1585000)
        self.assertEqual(orders_db.prepay_for(1290001), 646000)            # вверх до 1000
        self.assertEqual(orders_db.prepay_for(0), 0)
        self.assertEqual(orders_db.prepay_for(None), 0)

    def test_add_order_and_customer(self):
        r = self.add(cid="a" * 24, marketing_opt_in=True, client_order_id="coid-0001", comment="после 18")
        self.assertFalse(r["duplicate"])
        self.assertEqual(r["orders_count"], 1)
        o = self.db.get_order("YR-261005-AAAA", with_events=True)
        self.assertEqual(o["status"], "new")
        self.assertEqual(o["prepay_uzs"], 1585000)
        self.assertEqual(o["customer_phone"], "+998901234567")
        self.assertEqual(o["customer_telegram"], "alisher_t")                # ник — строчными, без @
        self.assertEqual(o["items"][0]["cost_uzs"], 990000)                  # база закрытая: всё хранится
        self.assertEqual(o["client_order_id"], "coid-0001")
        self.assertEqual(o["comment"], "после 18")
        self.assertEqual([e["status"] for e in o["events"]], ["new"])
        c = self.db.get_customer(o["customer_id"])
        self.assertEqual(c["marketing_opt_in"], 1)
        self.assertTrue(c["opt_in_at"])
        self.assertEqual(c["first_seen"], o["created_at"])
        self.assertEqual(self.db.query("SELECT cid FROM customer_cids")[0][0], "a" * 24)
        self.assertEqual(o["order_name"], "Алишер")                          # контакты — и в самом заказе
        self.assertEqual(o["customer_city"], "Ташкент")
        self.assertEqual(o["marketing_opt_in"], 1)
        self.assertIsNone(o["telegram_user_id"])                             # Telegram не привязан
        # тот же телефон — тот же покупатель, счётчик растёт; карточку НЕ переписывает (телефон не проверяется),
        # а заказ хранит то, что ввели в нём
        r2 = self.add("YR-261005-BBBB", customer={**CUST, "name": "Алишер Т.", "city": "", "telegram": ""})
        self.assertEqual(r2["customer_id"], r["customer_id"])
        self.assertEqual(r2["orders_count"], 2)
        c = self.db.get_customer(r["customer_id"])
        self.assertEqual(c["name"], "Алишер")                                # первое имя остаётся
        self.assertEqual(c["city"], "Ташкент")
        self.assertEqual(c["telegram"], "alisher_t")
        self.assertEqual(c["marketing_opt_in"], 1)                           # без галочки не отзывается
        o2 = self.db.get_order("YR-261005-BBBB")
        self.assertEqual(o2["customer_name"], "Алишер Т.")                   # имя этого заказа
        self.assertIsNone(o2["customer_city"])
        self.assertIsNone(o2["customer_telegram"])
        self.assertEqual(o2["marketing_opt_in"], 0)
        self.assertEqual(self.db.get_order("YR-261005-AAAA")["customer_name"], "Алишер")   # первый не изменился
        # пустые поля карточки дополняются
        self.db.query("UPDATE customers SET city=NULL")
        self.add("YR-261005-CCCC", customer={**CUST, "city": "Бухара"})
        self.assertEqual(self.db.get_customer(r["customer_id"])["city"], "Бухара")

    def test_customer_matching_by_telegram(self):
        a = self.db.find_or_create_customer(phone="", telegram="@Some_User", name="A")
        b = self.db.find_or_create_customer(phone="+998935554433", telegram="some_user", name="A")
        self.assertEqual(a, b)                                               # телефон дописан к покупателю из Telegram
        self.assertEqual(self.db.get_customer(a)["phone"], "+998935554433")
        c = self.db.find_or_create_customer(phone="+998935554400", telegram="some_user")
        self.assertNotEqual(c, a)                                            # другой телефон — другой покупатель
        d = self.db.find_or_create_customer(phone="", telegram="some_user")
        self.assertEqual(d, a)
        e = self.db.find_or_create_customer(phone="", telegram="bad name!", name="X")
        self.assertIsNone(self.db.get_customer(e)["telegram"])
        self.assertEqual(len(self.db.query("SELECT * FROM customers")), 3)

    def test_order_no_generator_and_duplicates(self):
        self.add("YR-261005-AAAA")
        seq = iter(["YR-261005-AAAA", "YR-261005-AAAA", "YR-261005-CCCC"])
        r = self.add(lambda: next(seq))
        self.assertEqual(r["order_no"], "YR-261005-CCCC")
        with self.assertRaises(orders_db.DuplicateOrderNo):
            self.add("YR-261005-AAAA")
        with self.assertRaises(orders_db.DuplicateOrderNo):
            self.add(lambda: "YR-261005-AAAA", attempts=3)
        self.assertEqual(self.db.query("SELECT orders_count FROM customers")[0][0], 2)   # неудачные не считаются

    def test_client_order_id_idempotent(self):
        r1 = self.add("YR-261005-AAAA", client_order_id="same-id-123")
        r2 = self.add("YR-261005-BBBB", client_order_id="same-id-123")
        self.assertTrue(r2["duplicate"])
        self.assertEqual(r2["order_no"], r1["order_no"])
        self.assertEqual(len(self.db.query("SELECT * FROM orders")), 1)
        self.assertEqual(self.db.query("SELECT orders_count FROM customers")[0][0], 1)
        self.assertEqual(self.db.get_order_by_client_id("same-id-123")["order_no"], "YR-261005-AAAA")
        self.add("YR-261005-CCCC")                                           # без id — каждый раз новый
        self.add("YR-261005-DDDD")
        self.assertEqual(len(self.db.query("SELECT * FROM orders")), 3)

    def test_status_transitions(self):
        self.add()
        no = "YR-261005-AAAA"
        r = self.db.set_status(no, "confirmed", by="tg:1")
        self.assertTrue(r.ok and r.changed)
        self.assertEqual((r.prev, r.status, r.order["status"]), ("new", "confirmed", "confirmed"))
        r = self.db.set_status(no, "confirmed", by="tg:1")                  # повторное нажатие
        self.assertTrue(r.ok)
        self.assertFalse(r.changed)
        r = self.db.set_status(no, "shipped")                                # через шаг — нельзя
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "not_allowed")
        self.assertEqual(r.order["status"], "confirmed")
        self.assertEqual(self.db.set_status(no, "bogus").error, "bad_status")
        self.assertEqual(self.db.set_status("YR-000000-XXXX", "confirmed").error, "not_found")
        for st in ("prepaid", "ordered", "shipped", "delivered"):
            self.assertTrue(self.db.set_status(no, st, by="tg:1").changed, st)
        self.assertEqual(self.db.set_status(no, "cancelled").error, "not_allowed")   # выданный не отменить
        self.assertTrue(self.db.set_status(no, "cancelled", force=True).changed)
        ev = self.db.get_order(no, with_events=True)["events"]
        self.assertEqual([e["status"] for e in ev],
                         ["new", "confirmed", "prepaid", "ordered", "shipped", "delivered", "cancelled"])
        self.assertEqual(ev[1]["by"], "tg:1")
        for st, nxt in orders_db.TRANSITIONS.items():
            self.assertIn(st, orders_db.STATUSES)
            self.assertTrue(set(nxt) <= set(orders_db.STATUSES))
            self.assertIn(st, orders_db.STATUS_LABELS)

    def test_link_signature(self):
        no = "YR-261005-AAAA"
        s = orders_db.link_sig(TOKEN, no)
        self.assertRegex(s, r"^[A-Z2-7]{10}$")
        self.assertEqual(s, orders_db.link_sig(TOKEN, no.lower()))            # регистр номера не важен
        self.assertNotEqual(s, orders_db.link_sig(TOKEN, "YR-261005-AAAB"))
        self.assertNotEqual(s, orders_db.link_sig("999:other", no))
        self.assertEqual(orders_db.link_sig("", no), "")
        self.assertTrue(orders_db.check_link_sig(TOKEN, no, s))
        self.assertTrue(orders_db.check_link_sig(TOKEN, no, s.lower()))
        for bad in ("", None, s[:-1], s + "A", "A" * 10, "ЖЖЖЖЖЖЖЖЖЖ", s[:9] + "1"):
            self.assertFalse(orders_db.check_link_sig(TOKEN, no, bad), bad)
        self.assertFalse(orders_db.check_link_sig("", no, s))                 # без токена — никогда
        p = orders_db.start_payload(TOKEN, no)
        self.assertEqual(p, f"o_{no}_{s}")
        self.assertRegex(p, r"^[A-Za-z0-9_-]{1,64}$")                        # допустимый start-параметр Telegram
        self.assertEqual(orders_db.start_payload("", no), "")
        self.assertEqual(orders_db.parse_start_payload(p), (no, s))
        self.assertEqual(orders_db.parse_start_payload(p.lower()), (no, s))
        for bad in (f"o_{no}", f"o_{no}_", f"o_{no}_{s}x", "o_<script>", "p_AAAAAA2", "", None, f"x_{no}_{s}"):
            self.assertIsNone(orders_db.parse_start_payload(bad), bad)

    def test_link_telegram(self):
        self.add()
        no = "YR-261005-AAAA"
        user = {"id": 4242, "username": "Real_Name", "first_name": "Алишер"}
        self.assertEqual(self.db.link_telegram(no, user, sig="", secret=TOKEN).code, "bad_sig")
        self.assertEqual(self.db.link_telegram(no, user, sig=sig(no, "999:other"), secret=TOKEN).code, "bad_sig")
        self.assertEqual(self.db.link_telegram(no, user, sig=sig(no), secret="").code, "bad_sig")
        self.assertIsNone(self.db.get_order(no)["telegram_user_id"])
        r = self.db.link_telegram(no, user, sig=sig(no), secret=TOKEN)
        self.assertEqual(r.code, "linked")
        self.assertTrue(r.ok)
        self.assertEqual(r.order["telegram_user_id"], 4242)
        self.assertEqual(r.order["customer_telegram_user_id"], 4242)
        self.assertEqual(r.order["customer_telegram"], "real_name")         # настоящий ник из Telegram
        self.assertTrue(r.order["telegram_linked_at"])
        self.assertEqual(self.db.link_telegram(no.lower(), user, sig=sig(no).lower(), secret=TOKEN).code, "already")
        t = self.db.link_telegram(no, {"id": 999}, sig=sig(no), secret=TOKEN)
        self.assertEqual(t.code, "taken")
        self.assertIsNone(t.order)                                           # при отказе — без данных заказа
        self.assertEqual(self.db.get_order(no)["telegram_user_id"], 4242)
        self.assertEqual(self.db.link_telegram("YR-000000-XXXX", user, sig=sig("YR-000000-XXXX"),
                                               secret=TOKEN).code, "not_found")
        self.assertEqual(self.db.link_telegram(no, {"id": "abc"}, sig=sig(no), secret=TOKEN).code, "not_found")
        old = (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds")
        self.add("YR-260901-OLD2", customer={"name": "Б", "phone": "+998930000000"}, created_at=old)
        self.assertEqual(self.db.link_telegram("YR-260901-OLD2", {"id": 5}, sig=sig("YR-260901-OLD2"),
                                               secret=TOKEN).code, "expired")
        self.assertEqual([o["order_no"] for o in self.db.orders_for_telegram_user(4242)], [no])
        self.assertEqual(self.db.orders_for_telegram_user(5), [])
        notes = [e["note"] for e in self.db.get_order(no, with_events=True)["events"]]
        self.assertIn("telegram linked", notes)
        self.assertIn("telegram link repeated", notes)
        # привязка заказа не трогает карточку покупателя
        self.assertIsNone(self.db.query("SELECT telegram_user_id FROM customers")[0][0])

    def test_link_is_per_order(self):
        """Второй заказ того же покупателя не наследует Telegram первого — и наоборот."""
        self.add("YR-261005-AAAA")
        self.add("YR-261005-BBBB")
        self.assertTrue(self.db.link_telegram("YR-261005-AAAA", {"id": 4242}, sig=sig("YR-261005-AAAA"),
                                              secret=TOKEN).ok)
        self.assertIsNone(self.db.get_order("YR-261005-BBBB")["telegram_user_id"])
        self.add("YR-261005-CCCC")                                           # новый заказ — тоже без привязки
        self.assertIsNone(self.db.get_order("YR-261005-CCCC")["customer_telegram_user_id"])
        self.assertEqual([o["order_no"] for o in self.db.orders_for_telegram_user(4242)], ["YR-261005-AAAA"])

    def test_poc_attacker_with_victim_phone_gets_nothing(self):
        """PoC из ревью: злоумышленник оформляет заказ с телефоном жертвы и подключает свой Telegram."""
        victim = {"name": "Алишер", "phone": "+998901234567", "city": "Ташкент", "telegram": "alisher_t"}
        attacker = {"name": "Attacker", "phone": "+998901234567", "city": "Нукус", "telegram": "evil_one"}
        self.add("YR-261005-AAAA", customer=victim)
        self.assertTrue(self.db.link_telegram("YR-261005-AAAA", {"id": 4242, "username": "alisher_real"},
                                              sig=sig("YR-261005-AAAA"), secret=TOKEN).ok)
        ra = self.add("YR-261005-BBBB", customer=attacker, items=[ITEMS[1]], total_uzs=590000)
        # своя ссылка (её злоумышленник честно получил в ответе на свой заказ) привязывает только его заказ
        r = self.db.link_telegram("YR-261005-BBBB", {"id": 777, "username": "evil_one"},
                                  sig=sig("YR-261005-BBBB"), secret=TOKEN)
        self.assertEqual(r.code, "linked")
        self.assertEqual(r.order["order_no"], "YR-261005-BBBB")
        self.assertEqual(ra["customer_id"], r.customer_id)                  # покупатель тот же (тот же телефон) …
        v = self.db.get_order("YR-261005-AAAA")
        self.assertEqual(v["telegram_user_id"], 4242)                        # … но заказ жертвы не перешёл к 777
        self.assertEqual(v["customer_name"], "Алишер")                       # и имя жертвы не заменено
        self.assertEqual(v["customer_city"], "Ташкент")
        self.assertEqual(v["customer_telegram"], "alisher_real")
        self.assertEqual([o["order_no"] for o in self.db.orders_for_telegram_user(777)], ["YR-261005-BBBB"])
        self.assertEqual([o["order_no"] for o in self.db.orders_for_telegram_user(4242)], ["YR-261005-AAAA"])
        card = self.db.get_customer(ra["customer_id"])
        self.assertEqual((card["name"], card["city"], card["telegram"]), ("Алишер", "Ташкент", "alisher_t"))
        a = self.db.get_order("YR-261005-BBBB")
        self.assertEqual(a["customer_name"], "Attacker")                     # придуманное имя — только в его заказе
        self.assertEqual(a["telegram_user_id"], 777)
        # подобрать подпись к заказу жертвы нельзя: своя подпись к чужому номеру и мусор не подходят
        for s in (sig("YR-261005-BBBB"), "A" * 10, "", sig("YR-261005-AAAA", "1:guess")):
            self.assertEqual(self.db.link_telegram("YR-261005-AAAA", {"id": 777}, sig=s, secret=TOKEN).code, "bad_sig")
        self.assertEqual(self.db.get_order("YR-261005-AAAA")["telegram_user_id"], 4242)
        # даже верная подпись не перепривязывает уже привязанный заказ
        self.assertEqual(self.db.link_telegram("YR-261005-AAAA", {"id": 777}, sig=sig("YR-261005-AAAA"),
                                               secret=TOKEN).code, "taken")

    def test_poc_attacker_first_does_not_own_victim_orders(self):
        """Злоумышленник успел первым: карточка создана с его данными, но заказы жертвы — её собственные."""
        self.add("YR-261005-BBBB", customer={"name": "Attacker", "phone": "+998901234567", "telegram": "evil_one"},
                 telegram_user={"id": 777})
        self.add("YR-261005-AAAA", customer={"name": "Алишер", "phone": "+998901234567"})
        v = self.db.get_order("YR-261005-AAAA")
        self.assertIsNone(v["telegram_user_id"])                             # Telegram злоумышленника не унаследован
        self.assertEqual(v["customer_name"], "Алишер")
        self.assertIsNone(v["customer_telegram"])                            # и его ник тоже
        self.assertEqual([o["order_no"] for o in self.db.orders_for_telegram_user(777)], ["YR-261005-BBBB"])

    def test_add_order_with_telegram_user(self):
        r = self.add(telegram_user={"id": 77, "username": "ann_x"})
        self.assertEqual(r["telegram_link"], "linked")
        o = self.db.get_order("YR-261005-AAAA", with_events=True)
        self.assertEqual((o["telegram_user_id"], o["telegram_username"]), (77, "ann_x"))
        self.assertIn("telegram linked (miniapp)", [e["note"] for e in o["events"]])
        r2 = self.add("YR-261005-BBBB", telegram_user={"id": 88})
        self.assertEqual(r2["telegram_link"], "linked")                     # свой заказ — свой Telegram
        self.assertEqual(self.db.get_order("YR-261005-BBBB")["telegram_user_id"], 88)
        self.assertEqual(self.db.get_order("YR-261005-AAAA")["telegram_user_id"], 77)   # первый не перезаписан
        self.assertEqual([x["order_no"] for x in self.db.orders_for_telegram_user(77)], ["YR-261005-AAAA"])
        self.assertEqual([x["order_no"] for x in self.db.orders_for_telegram_user(88)], ["YR-261005-BBBB"])
        r3 = self.add("YR-261005-CCCC", telegram_user={"id": "x"})
        self.assertEqual(r3["telegram_link"], "bad")
        self.assertIsNone(self.db.get_order("YR-261005-CCCC")["telegram_user_id"])

    def test_marketing_and_forget(self):
        self.add(telegram_user={"id": 77}, marketing_opt_in=True)
        self.add("YR-261005-BBBB", customer={**CUST, "phone": "+998935554433"}, telegram_user={"id": 88})  # без галочки
        m = self.db.marketing_customers()
        self.assertEqual([x["telegram_user_id"] for x in m], [77])
        self.assertEqual(self.db.set_marketing(88, True), 1)                 # согласие, данное в самом Telegram
        self.assertEqual({x["telegram_user_id"] for x in self.db.marketing_customers()}, {77, 88})
        self.assertEqual(self.db.set_marketing(88, False), 1)
        self.assertEqual(self.db.set_marketing(77, False), 1)                # /stop
        self.assertEqual(self.db.marketing_customers(), [])
        self.assertEqual(self.db.set_marketing("bad", False), 0)
        self.db.set_tg_message("YR-261005-AAAA", -100123, 55, "<b>Заказ</b>\nТелефон: +998901234567")
        self.assertTrue(self.db.forget_customer("+998901234567"))
        c = self.db.query("SELECT * FROM customers WHERE id=1")[0]
        self.assertIsNone(c["phone"])
        self.assertIsNone(c["name"])
        self.assertIsNone(c["telegram_user_id"])
        self.assertTrue(c["deleted_at"])
        o = self.db.get_order("YR-261005-AAAA")
        self.assertEqual(o["total_uzs"], 3170000)                           # учёт остаётся
        for k in ("customer_name", "customer_phone", "customer_city", "customer_telegram", "telegram_user_id",
                  "tg_text"):
            self.assertIsNone(o[k], k)                                       # контакты стёрты и в заказе
        self.assertEqual(self.db.orders_for_telegram_user(77), [])
        self.assertFalse(self.db.forget_customer("+998901234567"))
        r = self.add("YR-261005-CCCC")                                       # снова заказал — новый покупатель
        self.assertEqual(r["orders_count"], 1)

    def test_migrate_from_schema_1(self):
        """База схемы 1 (Telegram у покупателя): привязка переносится только на заказ, где её сделали."""
        path = Path(self.tmp.name) / "old.sqlite"
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES('schema_version', '1');
            CREATE TABLE customers(id INTEGER PRIMARY KEY, phone TEXT UNIQUE, telegram TEXT, telegram_user_id INTEGER,
                name TEXT, city TEXT, first_seen TEXT, last_order_at TEXT, orders_count INTEGER NOT NULL DEFAULT 0,
                marketing_opt_in INTEGER NOT NULL DEFAULT 0, opt_in_at TEXT, note TEXT, deleted_at TEXT);
            CREATE TABLE customer_cids(cid TEXT NOT NULL, customer_id INTEGER NOT NULL, first_seen TEXT,
                last_seen TEXT, PRIMARY KEY(cid, customer_id));
            CREATE TABLE orders(id INTEGER PRIMARY KEY, order_no TEXT UNIQUE NOT NULL, client_order_id TEXT UNIQUE,
                customer_id INTEGER, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', status_at TEXT,
                items_json TEXT NOT NULL DEFAULT '[]', total_uzs INTEGER, prepay_uzs INTEGER,
                source TEXT NOT NULL DEFAULT 'site', tg_message_id INTEGER, tg_chat_id TEXT, tg_text TEXT, cid TEXT,
                lang TEXT, comment TEXT, page_url TEXT);
            CREATE TABLE order_events(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, at TEXT NOT NULL,
                status TEXT, "by" TEXT, note TEXT);
            INSERT INTO customers(id, phone, telegram, telegram_user_id, name, city, orders_count)
                VALUES(1, '+998901234567', 'evil_one', 777, 'Attacker', 'Нукус', 2);
            INSERT INTO orders(id, order_no, customer_id, created_at, total_uzs, prepay_uzs)
                VALUES(1, 'YR-261001-AAAA', 1, '2026-10-01T10:00:00', 1000, 1000),
                      (2, 'YR-261002-BBBB', 1, '2026-10-02T10:00:00', 2000, 1000);
            INSERT INTO order_events(order_id, at, status, "by", note)
                VALUES(1, '2026-10-01T10:00:00', 'new', 'site', NULL),
                      (2, '2026-10-02T10:00:00', 'new', 'site', NULL),
                      (2, '2026-10-02T11:00:00', NULL, 'tg:777', 'telegram linked');
        """)
        c.close()
        db = orders_db.OrdersDB(path)
        try:
            self.assertIsNone(db.get_order("YR-261001-AAAA")["telegram_user_id"])   # не наследуется
            o2 = db.get_order("YR-261002-BBBB")
            self.assertEqual(o2["telegram_user_id"], 777)                    # где привязали — там и осталось
            self.assertEqual(o2["telegram_linked_at"], "2026-10-02T11:00:00")
            self.assertEqual(o2["customer_name"], "Attacker")                # старые заказы — из карточки
            self.assertIsNone(db.query("SELECT telegram_user_id FROM customers")[0][0])
            self.assertEqual(db.query("SELECT value FROM meta WHERE key='schema_version'")[0][0], "2")
            self.assertEqual([o["order_no"] for o in db.orders_for_telegram_user(777)], ["YR-261002-BBBB"])
        finally:
            db.close()
        db = orders_db.OrdersDB(path)                                        # повторно — без изменений и ошибок
        try:
            self.assertEqual(db.get_order("YR-261002-BBBB")["telegram_user_id"], 777)
        finally:
            db.close()

    def test_tg_message_and_events(self):
        self.add()
        self.db.set_tg_message("YR-261005-AAAA", -100123, 55, "<b>Заказ</b>\nСтатус: <b>новый</b>")
        o = self.db.get_order("YR-261005-AAAA")
        self.assertEqual((o["tg_chat_id"], o["tg_message_id"]), ("-100123", 55))
        self.assertIn("Статус", o["tg_text"])
        self.db.set_tg_message("YR-261005-AAAA", -100123, 56)               # текст без изменений
        self.assertIn("Статус", self.db.get_order("YR-261005-AAAA")["tg_text"])
        self.assertTrue(self.db.add_event("YR-261005-AAAA", "заметка", by="tg:1"))
        self.assertFalse(self.db.add_event("YR-000000-XXXX", "x"))
        st = self.db.stats()
        self.assertEqual(st["orders"], 1)
        self.assertEqual(st["by_status"], {"new": 1})
        self.assertEqual(st["customers"], 1)

    def test_export_csv(self):
        self.add()
        out = Path(self.tmp.name) / "export" / "orders.csv"
        n = self.db.export_csv(out)
        self.assertEqual(n, 1)
        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))                     # utf-8-sig — Excel открывает
        text = raw.decode("utf-8-sig")
        rows = list(csv.reader(text.splitlines(), delimiter=";"))
        self.assertEqual(rows[0][:3], ["order_no", "created_at", "status"])
        self.assertEqual(rows[1][0], "YR-261005-AAAA")
        self.assertIn("+998901234567", text)
        self.assertIn("Boss — Брюки Boss, размер 48 ×2", text)
        for leak in ("yoox", "cacharel.com", "990000", "EUR", "TRY", "cost"):
            self.assertNotIn(leak, text)                                     # без закупочных данных
        full = Path(self.tmp.name) / "export" / "orders-private.csv"
        self.db.export_csv(full, include_private=True)
        t2 = full.read_text(encoding="utf-8-sig")
        self.assertIn("https://www.yoox.com/it/123AB/item", t2)
        self.assertIn("2270000", t2)                                         # себестоимость: 990000×2 + 290000
        cust = Path(self.tmp.name) / "export" / "customers.csv"
        self.assertEqual(self.db.export_csv(cust, customers=True), 1)
        self.assertIn("alisher_t", cust.read_text(encoding="utf-8-sig"))

    def test_backup(self):
        self.add()
        gz = self.db.backup(Path(self.tmp.name) / "backup" / "orders-20261005.sqlite.gz")
        self.assertTrue(gz.exists())
        restored = Path(self.tmp.name) / "restored.sqlite"
        with gzip.open(gz, "rb") as f:
            restored.write_bytes(f.read())
        c = sqlite3.connect(restored)
        try:
            self.assertEqual(c.execute("SELECT order_no FROM orders").fetchone()[0], "YR-261005-AAAA")
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            c.close()
        plain = self.db.backup(Path(self.tmp.name) / "backup" / "copy.sqlite")
        self.assertTrue(plain.exists())
        self.assertEqual(len(list((Path(self.tmp.name) / "backup").glob(".orders-backup-*"))), 0)   # без мусора
        self.assertEqual(orders_db.prune_backups(Path(self.tmp.name) / "backup", keep_days=30), 0)

    def test_import_jsonl(self):
        log = Path(self.tmp.name) / "orders.jsonl"
        recs = [{"order_no": "YR-261001-AAAA", "created_at": "2026-10-01T10:00:00", "customer": CUST,
                 "items": ITEMS, "total_uzs": 3170000, "lang": "ru"},
                {"order_no": "YR-261001-AAAA", "event": "telegram_failed"},
                {"order_no": "YR-261002-BBBB", "created_at": "2026-10-02T10:00:00",
                 "customer": {**CUST, "phone": "+998935554433"}, "items": [], "total_uzs": 0}]
        log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\nnot json\n", encoding="utf-8")
        added, skipped = self.db.import_jsonl(log)
        self.assertEqual((added, skipped), (2, 1))
        self.assertEqual(self.db.import_jsonl(log), (0, 3))                 # повторно — ничего не добавляет
        self.assertEqual(self.db.get_order("YR-261001-AAAA")["created_at"], "2026-10-01T10:00:00")

    def test_threads(self):
        errors = []

        def worker(k):
            try:
                for i in range(15):
                    self.db.add_order(order_no=f"YR-261005-{k}{i:03d}", customer={"name": "N", "phone": f"+99890{k}{i:06d}"},
                                      items=[], total_uzs=1000, client_order_id=f"thread-{k}-{i:04d}")
                    self.db.set_status(f"YR-261005-{k}{i:03d}", "confirmed", by="t")
            except Exception as ex:                                          # pragma: no cover
                errors.append(ex)
        ts = [threading.Thread(target=worker, args=(k,)) for k in range(1, 7)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM orders")[0][0], 90)
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM orders WHERE status='confirmed'")[0][0], 90)
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM order_events")[0][0], 180)

    def test_mask_phone(self):
        self.assertEqual(orders_db.mask_phone("+998901234567"), "***4567")
        self.assertEqual(orders_db.mask_phone(""), "-")


if __name__ == "__main__":
    unittest.main(verbosity=2)
