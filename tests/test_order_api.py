"""Проверка приёма заказов (python -m unittest discover -s tests -p "test_*.py"). Сервер поднимается на случайном
порту в потоке, данные — маленький выдуманный каталог во временной папке, Telegram и проверка наличия у
источников подменены (в сеть ничего не уходит)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import order_api  # noqa: E402
import order_lookup  # noqa: E402
import orders_db  # noqa: E402
import tg_bot  # noqa: E402

ORIGIN = "https://fayyoznaimov.github.io"
TOKEN = "123456:TEST-token-not-real"
PRODUCTS = {"summary": {"generated_at": "04.10.2026 10:00"}, "site": {}, "products": [
    {"id": "AAAAAA2", "brand": "Boss", "title": "Брюки Boss", "type": "брюки", "price_uzs": 1290000,
     "sizes": ["46", "48", "50"], "in_stock": True, "images": []},
    {"id": "BBBBBB3", "brand": "Cacharel", "title": "Рубашка Cacharel", "type": "рубашки", "price_uzs": 590000,
     "sizes": ["M", "L"], "in_stock": True, "images": []},
    {"id": "CCCCCC4", "brand": "Kiton", "title": "Пиджак Kiton", "type": "пиджаки", "price_uzs": 9990000,
     "sizes": [], "in_stock": False, "images": []},
    {"id": "DDDDDD5", "brand": "Lacoste", "title": "Поло Lacoste", "type": "поло", "price_uzs": 790000,
     "sizes": ["S", "M"], "in_stock": True, "images": []},
]}
ADMIN = {
    "_meta": {"generated_at": "04.10.2026 10:00"},
    "AAAAAA2": {"source": "yoox", "source_item_id": "123AB", "url": "https://www.yoox.com/it/123AB/item",
                "title_original": "Pantaloni, nero", "price_now": 61.0, "price_old": 250.0, "currency": "EUR",
                "cost_uzs": 990000, "margin_uzs": 300000},
    "BBBBBB3": {"source": "cacharel_tr", "source_item_id": "777", "url": "https://www.cacharel.com.tr/gomlek-777/",
                "title_original": "Gömlek", "price_now": 999.0, "price_old": 6999.0, "currency": "TRY",
                "cost_uzs": 290000, "margin_uzs": 300000},
    "DDDDDD5": {"source": "trendyol", "source_item_id": "555", "url": "https://www.trendyol.com/lacoste/polo-p-555",
                "title_original": "Polo", "price_now": 1500.0, "price_old": 3000.0, "currency": "TRY",
                "cost_uzs": 490000, "margin_uzs": 300000},
}


def write_site(root: Path, products=PRODUCTS, admin=ADMIN):
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "products.json").write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")
    (site / "products-admin.js").write_text("window.DEALS_ADMIN = " + json.dumps(admin, ensure_ascii=False) + ";",
                                            encoding="utf-8")


def valid_order(**over):
    o = {"items": [{"id": "AAAAAA2", "size": "48", "qty": 2, "price_uzs": 1},      # цена клиента игнорируется
                   {"id": "bbbbbb3", "size": "XL", "qty": 1}],                       # размера XL нет
         "customer": {"name": "Алишер <b>", "phone": "+998 (90) 123-45-67", "telegram": "@alisher_t",
                      "city": "Ташкент", "comment": "позвоните после 18:00 & спасибо"},
         "consent": True,
         "page_url": "https://fayyoznaimov.github.io/yurt/#/?p=AAAAAA2", "lang": "ru", "website": ""}
    o.update(over)
    return o


def make_init_data(token: str, user: dict, auth_date: int | None = None, **extra) -> str:
    """initData так, как его подписывает Telegram (для проверки нашей проверки)."""
    fields = {"auth_date": str(int(time.time()) if auth_date is None else auth_date), "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
              "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")), **extra}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeTelegram:
    """Подмена Bot API для tg_bot.Bot(http=...)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.n = 1000

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.n += 1
            return {"message_id": self.n, "chat": {"id": params.get("chat_id")}}, {"ok": True}
        return True, {"ok": True}


class Server:
    def __init__(self, root: Path, **kw):
        kw.setdefault("verify", False)
        self.app = order_api.OrderApp(root=root, origins=[ORIGIN], **kw)
        self.srv = order_api.make_server(self.app, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def post(self, data, origin=ORIGIN, raw: bytes | None = None, ctype="application/json"):
        h = {"Content-Type": ctype}
        if origin:
            h["Origin"] = origin
        body = raw if raw is not None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        return requests.post(self.url + "/api/order", data=body, headers=h, timeout=10)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.app.close()


class OrderApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_site(self.root)
        self.sent: list[str] = []
        self.markups: list = []
        self.replies: list = []
        self.logs: list[str] = []
        self.verify_calls: list[tuple[str, list]] = []
        self.verify_result: dict[str, dict] = {}
        self._msg_id = 500
        self._send, self._log, self._lv = order_api.send_telegram, order_api.log, order_api.load_verifier
        self._http = tg_bot._http_call
        self._env = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "BOT_USERNAME", "ORDER_VERIFY")}
        for k in self._env:
            os.environ.pop(k, None)

        def fake_send(text, reply_markup=None, reply_to=None):
            self.sent.append(text)
            self.markups.append(reply_markup)
            self.replies.append(reply_to)
            self._msg_id += 1
            return {"message_id": self._msg_id, "chat": {"id": -100777}}

        def fake_verifier(source):
            def verify(rows, query=None, **opts):
                self.verify_calls.append((source, rows))
                res = self.verify_result.get(source)
                if isinstance(res, Exception):
                    raise res
                return res or {}
            return verify, {"site": source}

        def no_network(token, method, params, timeout):
            raise AssertionError(f"сеть в тестах: {method}")

        order_api.send_telegram = fake_send
        order_api.log = lambda msg: self.logs.append(msg)
        order_api.load_verifier = fake_verifier
        tg_bot._http_call = no_network
        self.s = Server(self.root, rate_limit=50, verify=False)          # проверку наличия включают её тесты

    def tearDown(self):
        self.s.close()
        order_api.send_telegram, order_api.log, order_api.load_verifier = self._send, self._log, self._lv
        tg_bot._http_call = self._http
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def db(self) -> orders_db.OrdersDB:
        return self.s.app.db

    def wait_bg(self):
        self.s.app.join_background(10)

    # --- основной сценарий

    def test_valid_order_repriced_and_sent(self):
        r = self.s.post(valid_order())
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertRegex(d["order_no"], r"^YR-\d{6}-[A-Z2-9]{4}$")
        self.assertEqual(d["total_uzs"], 1290000 * 2 + 590000)              # цены сервера, не клиента
        self.assertEqual(d["prepay_uzs"], 1585000)                          # 50%, вверх до 1000
        self.assertEqual(d["unavailable"], ["BBBBBB3 (XL)"])
        self.assertTrue(d["notified"])
        self.assertFalse(d["duplicate"])
        self.assertFalse(d["telegram_linked"])
        self.assertEqual(d["bot_url"], "")                                  # BOT_USERNAME не задан
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), ORIGIN)
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertIn(d["order_no"], msg)
        self.assertIn("Статус: <b>новый</b>", msg)
        self.assertIn("Клиент: 1-й заказ (новый покупатель)", msg)
        self.assertIn("https://www.yoox.com/it/123AB/item", msg)              # ссылка на источник (чат продавца)
        self.assertIn("https://www.cacharel.com.tr/gomlek-777/", msg)
        self.assertIn("Наша цена: 1 290 000 сум", msg)                        # наша цена
        self.assertIn("61 EUR (было 250)", msg)                               # цена в магазине
        self.assertIn("999 TRY", msg)
        self.assertIn("Себестоимость 990 000 · маржа 300 000 сум", msg)
        self.assertIn('<a href="tel:+998901234567">', msg)
        self.assertIn('<a href="https://t.me/alisher_t">@alisher_t</a>', msg)
        self.assertIn("Алишер &lt;b&gt;", msg)                               # HTML клиента экранирован
        self.assertIn("&amp; спасибо", msg)
        self.assertIn("РАЗМЕРА XL НЕТ (сейчас есть: M, L)", msg)
        self.assertIn("в наличии, размер 48 есть", msg)
        self.assertIn("Итого: 3 170 000 сум", msg)
        self.assertIn("Предоплата 50%: 1 585 000 сум · остаток 1 585 000 сум", msg)
        self.assertIn("YOOX", msg)
        self.assertIn("Cacharel TR", msg)
        # кнопки статусов на сообщении
        kb = self.markups[0]["inline_keyboard"]
        datas = [b["callback_data"] for row in kb for b in row]
        self.assertEqual(datas, [f"st:{d['order_no']}:confirmed", f"st:{d['order_no']}:cancelled"])
        self.assertTrue(all(len(x.encode()) <= 64 for x in datas))
        # заказ сохранён целиком (с телефоном) в orders.jsonl ...
        rec = json.loads((self.root / "data/orders/orders.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["order_no"], d["order_no"])
        self.assertEqual(rec["customer"]["phone"], "+998901234567")
        self.assertEqual(rec["items"][0]["price_uzs"], 1290000)
        self.assertTrue(rec["consent"])
        self.assertTrue(rec["db"])
        # ... и в базе: покупатель, заказ, сообщение Telegram
        o = self.db().get_order(d["order_no"], with_events=True)
        self.assertEqual(o["status"], "new")
        self.assertEqual(o["customer_phone"], "+998901234567")
        self.assertEqual(o["customer_telegram"], "alisher_t")
        self.assertEqual(o["total_uzs"], 3170000)
        self.assertEqual(o["prepay_uzs"], 1585000)
        self.assertEqual(o["source"], "site")
        self.assertEqual(o["tg_message_id"], 501)
        self.assertEqual(o["tg_chat_id"], "-100777")
        self.assertIn("Статус: <b>новый</b>", o["tg_text"])
        self.assertEqual([i["id"] for i in o["items"]], ["AAAAAA2", "BBBBBB3"])
        self.assertIn("seller_notified", [ev["note"] for ev in o["events"]])
        # ... а в журнале полного номера нет
        self.wait_bg()
        joined = "\n".join(self.logs)
        self.assertIn(d["order_no"], joined)
        self.assertNotIn("901234567", joined)
        self.assertIn("***4567", joined)

    def test_unknown_and_sold_out_items(self):
        r = self.s.post(valid_order(items=[{"id": "AAAAAA2", "size": "46", "qty": 1},
                                           {"id": "ZZZZZZ9", "size": "M", "qty": 1},
                                           {"id": "CCCCCC4", "size": "", "qty": 1}]))
        d = r.json()
        self.assertTrue(d["ok"], r.text)
        self.assertEqual(d["total_uzs"], 1290000 + 9990000)                # неизвестный код не считается
        self.assertIn("ZZZZZZ9 (M)", d["unavailable"])
        self.assertIn("CCCCCC4", d["unavailable"])
        self.assertIn("НЕТ В КАТАЛОГЕ", self.sent[0])
        self.assertIn("НЕТ В НАЛИЧИИ", self.sent[0])
        self.assertIn("нет закрытых данных", self.sent[0])                  # у CCCCCC4 нет admin-записи
        r = self.s.post(valid_order(items=[{"id": "ZZZZZZ9", "size": "M", "qty": 1}]))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    def test_reload_when_files_change(self):
        self.s.post(valid_order(items=[{"id": "AAAAAA2", "size": "48", "qty": 1}]))
        p = json.loads(json.dumps(PRODUCTS))
        p["products"][0]["price_uzs"] = 1111000
        p["products"][0]["sizes"] = ["50"]
        write_site(self.root, products=p)
        st = (self.root / "site/products.json").stat()
        os.utime(self.root / "site/products.json", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        d = self.s.post(valid_order(items=[{"id": "AAAAAA2", "size": "48", "qty": 1}])).json()
        self.assertEqual(d["total_uzs"], 1111000)
        self.assertIn("РАЗМЕРА 48 НЕТ", self.sent[-1])
        # битый (недописанный) файл — остаются прежние данные
        (self.root / "site/products.json").write_text('{"products": [', encoding="utf-8")
        os.utime(self.root / "site/products.json", ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
        d = self.s.post(valid_order(items=[{"id": "AAAAAA2", "size": "50", "qty": 1}])).json()
        self.assertEqual(d["total_uzs"], 1111000)

    def test_telegram_failure_still_saves(self):
        order_api.send_telegram = lambda text, reply_markup=None, reply_to=None: False
        d = self.s.post(valid_order()).json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["notified"])
        lines = (self.root / "data/orders/orders.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1]), {**json.loads(lines[-1]), "event": "telegram_failed"})
        o = self.db().get_order(d["order_no"], with_events=True)
        self.assertIsNone(o["tg_message_id"])
        self.assertIn("seller_notify_failed", [ev["note"] for ev in o["events"]])

    # --- покупатели, повторы, согласия

    def test_same_customer_counted_across_phone_formats(self):
        d1 = self.s.post(valid_order()).json()
        cust = {**valid_order()["customer"], "phone": "90 123 45 67", "name": "Алишер Т.", "city": "Самарканд"}
        d2 = self.s.post(valid_order(customer=cust)).json()
        cust3 = {**cust, "phone": "998901234567"}
        d3 = self.s.post(valid_order(customer=cust3)).json()
        self.assertTrue(d1["ok"] and d2["ok"] and d3["ok"])
        self.assertIn("Клиент: 2-й заказ — постоянный покупатель", self.sent[1])
        self.assertIn("Клиент: 3-й заказ", self.sent[2])
        rows = self.db().query("SELECT * FROM customers")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["orders_count"], 3)
        self.assertEqual(rows[0]["name"], "Алишер <b>")                   # карточку не переписывают (телефон
        self.assertEqual(rows[0]["city"], "Ташкент")                      # никто не проверяет) …
        o2 = self.db().get_order(d2["order_no"])
        self.assertEqual(o2["customer_name"], "Алишер Т.")               # … а у заказа — его собственные данные
        self.assertEqual(o2["customer_city"], "Самарканд")
        self.assertEqual(self.db().get_order(d1["order_no"])["customer_name"], "Алишер <b>")
        # другой номер — другой покупатель
        d4 = self.s.post(valid_order(customer={**cust, "phone": "+998 93 000 11 22"})).json()
        self.assertTrue(d4["ok"])
        self.assertIn("Клиент: 1-й заказ", self.sent[3])
        self.assertEqual(len(self.db().query("SELECT * FROM customers")), 2)

    def test_client_order_id_idempotent(self):
        coid = "8f14e45f-ceea-467a-9575-0ff2b5b3a1c2"
        r1 = self.s.post(valid_order(client_order_id=coid))
        r2 = self.s.post(valid_order(client_order_id=coid))
        d1, d2 = r1.json(), r2.json()
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(d1["order_no"], d2["order_no"])
        self.assertFalse(d1["duplicate"])
        self.assertTrue(d2["duplicate"])
        self.assertTrue(d2["notified"])
        self.assertEqual(d2["total_uzs"], d1["total_uzs"])
        self.assertEqual(d2["prepay_uzs"], d1["prepay_uzs"])
        self.assertEqual(d2["unavailable"], ["BBBBBB3 (XL)"])
        self.assertEqual(len(self.sent), 1)                                 # второго сообщения нет
        self.assertEqual(len(self.db().query("SELECT * FROM orders")), 1)
        self.assertEqual(self.db().query("SELECT orders_count FROM customers")[0][0], 1)
        # повторы не тратят лимит заказов
        s2 = Server(self.root, rate_limit=1, rate_window=600)
        try:
            coid2 = "11111111-2222-3333-4444-555555555555"
            codes = [s2.post(valid_order(client_order_id=coid2)).status_code for _ in range(3)]
            self.assertEqual(codes, [200, 200, 200])
            self.assertEqual(s2.post(valid_order(client_order_id="another-id-0001")).status_code, 429)
        finally:
            s2.close()
        # неверный формат id — заказ принимается без защиты от повтора
        r3 = self.s.post(valid_order(client_order_id="bad id!"))
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertNotEqual(r3.json()["order_no"], d1["order_no"])

    def test_concurrent_duplicate_submissions(self):
        coid = "concurrent-0000-1111-2222"
        results = []

        def go():
            results.append(self.s.post(valid_order(client_order_id=coid)).json())
        ts = [threading.Thread(target=go) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len({r["order_no"] for r in results}), 1)
        self.assertEqual(sum(1 for r in results if not r["duplicate"]), 1)
        self.assertEqual(len(self.sent), 1)

    def test_consent_cid_marketing_city(self):
        cust = {**valid_order()["customer"]}
        cust.pop("city")
        self.assertRejected(self.s.post(valid_order(consent=False)), text="consent")
        o = valid_order()
        o.pop("consent")
        self.assertRejected(self.s.post(o), text="consent")
        self.assertRejected(self.s.post(valid_order(consent="no")), text="consent")
        self.assertEqual(self.sent, [])
        cid = "a1b2c3d4-e5f6-4711-8899-aabbccddeeff"
        d = self.s.post(valid_order(customer=cust, city="Бухара", cid=cid, consent_marketing=True)).json()
        self.assertTrue(d["ok"])
        self.assertIn("Город: Бухара", self.sent[-1])
        self.assertIn("Согласие на новинки: да", self.sent[-1])
        c = self.db().query("SELECT * FROM customers")[0]
        self.assertEqual(c["city"], "Бухара")
        self.assertEqual(c["marketing_opt_in"], 1)
        self.assertTrue(c["opt_in_at"])
        self.assertEqual(self.db().query("SELECT cid FROM customer_cids")[0][0], cid)
        self.assertEqual(self.db().get_order(d["order_no"])["cid"], cid)
        # снятая галочка на следующем заказе не отзывает согласие; неверный cid игнорируется
        d2 = self.s.post(valid_order(cid="<script>", consent_marketing=False)).json()
        self.assertTrue(d2["ok"])
        self.assertEqual(self.db().query("SELECT marketing_opt_in FROM customers")[0][0], 1)
        self.assertIsNone(self.db().get_order(d2["order_no"])["cid"])
        self.assertNotIn("Согласие на новинки", self.sent[-1])

    # --- Telegram Mini App

    def test_tg_init_data_links_customer(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        os.environ["BOT_USERNAME"] = "ipak_shop_bot"
        fake = FakeTelegram()
        self.s.app.bot = tg_bot.Bot(TOKEN, db=self.db(), http=fake, log=lambda m: None)
        user = {"id": 424242, "first_name": "Алишер", "username": "Alisher_T", "language_code": "ru"}
        r = self.s.post(valid_order(tg_init_data=make_init_data(TOKEN, user, start_param="p_AAAAAA2")))
        d = r.json()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(d["telegram_linked"])
        self.assertEqual(d["bot_url"], f"https://t.me/ipak_shop_bot?start=o_{d['order_no']}_"
                                       f"{orders_db.link_sig(TOKEN, d['order_no'])}")
        o = self.db().get_order(d["order_no"])
        self.assertEqual(o["telegram_user_id"], 424242)
        self.assertEqual(o["customer_telegram_user_id"], 424242)
        self.assertEqual(o["customer_telegram"], "alisher_t")
        self.assertEqual(o["source"], "miniapp")
        self.assertIn("Telegram подтверждён", self.sent[0])
        self.assertIn("Mini App", self.sent[0])
        self.assertNotIn("?start=", self.sent[0])                           # уже привязан — ссылка не нужна
        self.wait_bg()
        to_user = [p for m, p in fake.calls if m == "sendMessage" and p["chat_id"] == 424242]
        self.assertEqual(len(to_user), 1)                                   # «Заказ принят» покупателю
        self.assertIn(d["order_no"], to_user[0]["text"])
        self.assertIn("Предоплата 50%", to_user[0]["text"])
        for bad in ("cacharel.com", "yoox", "try", "eur", "себестоимость", "маржа"):
            self.assertNotIn(bad, to_user[0]["text"].lower())
        self.assertNotIn("возврат", to_user[0]["text"].lower())

    def test_tg_init_data_bad_signature_ignored(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        self.s.app.bot = tg_bot.Bot(TOKEN, db=self.db(), http=FakeTelegram(), log=lambda m: None)
        user = {"id": 777, "first_name": "X"}
        bad = [make_init_data("999:other-token", user),                               # чужой токен
               make_init_data(TOKEN, user, auth_date=int(time.time()) - 25 * 3600),   # старше 24 ч
               make_init_data(TOKEN, user).replace("auth_date=", "auth_date=1"),     # подменено поле
               "user=%7B%22id%22%3A1%7D&hash=zz", "garbage", "x" * 5000]
        for init in bad:
            r = self.s.post(valid_order(tg_init_data=init))
            self.assertEqual(r.status_code, 200, r.text)                    # заказ не отклоняется
            d = r.json()
            self.assertFalse(d["telegram_linked"])
            self.assertEqual(self.db().get_order(d["order_no"])["source"], "site")
        self.assertEqual(self.db().query("SELECT COUNT(*) FROM customers WHERE telegram_user_id IS NOT NULL")[0][0], 0)
        for m in self.sent:
            self.assertNotIn("Telegram подтверждён", m)

    def test_tg_user_links_only_its_own_order(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        self.s.app.bot = tg_bot.Bot(TOKEN, db=self.db(), http=FakeTelegram(), log=lambda m: None)
        d1 = self.s.post(valid_order(tg_init_data=make_init_data(TOKEN, {"id": 1001, "first_name": "A"}))).json()
        d = self.s.post(valid_order(tg_init_data=make_init_data(TOKEN, {"id": 2002, "first_name": "B"}))).json()
        self.assertTrue(d["telegram_linked"])                               # свой заказ — свой Telegram
        self.assertEqual(self.db().get_order(d["order_no"])["telegram_user_id"], 2002)
        self.assertEqual(self.db().get_order(d1["order_no"])["telegram_user_id"], 1001)   # первый не перехвачен
        self.assertEqual([o["order_no"] for o in self.db().orders_for_telegram_user(2002)], [d["order_no"]])
        d3 = self.s.post(valid_order()).json()                              # с сайта, без Telegram
        self.assertFalse(d3["telegram_linked"])
        self.assertIsNone(self.db().get_order(d3["order_no"])["telegram_user_id"])

    def test_poc_victim_phone_end_to_end(self):
        """PoC из ревью целиком: жертва заказывает с сайта и подключает бота; злоумышленник заказывает с её
        телефоном (с сайта и из Mini App) и подключает свой Telegram — ни данных жертвы, ни сообщений жертве."""
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        os.environ["BOT_USERNAME"] = "ipak_shop_bot"
        fake = FakeTelegram()
        bot = tg_bot.Bot(TOKEN, db=self.db(), admin_ids={9}, orders_chat="-100777", http=fake, log=lambda m: None,
                         bot_username="ipak_shop_bot", offset_path=self.root / "data" / "orders" / "bot_offset.json")
        self.s.app.bot = bot
        victim = self.s.post(valid_order()).json()
        self.assertTrue(victim["bot_url"])
        payload = victim["bot_url"].split("?start=", 1)[1]
        bot.handle_update({"message": {"message_id": 1, "text": f"/start {payload}", "chat": {"id": 4242, "type": "private"},
                                       "from": {"id": 4242, "first_name": "Алишер"}}})
        self.assertEqual(self.db().get_order(victim["order_no"])["telegram_user_id"], 4242)
        evil = {**valid_order()["customer"], "name": "Attacker", "telegram": "", "city": "Нукус"}
        a1 = self.s.post(valid_order(customer=evil)).json()                       # с сайта, телефон жертвы
        payload_a = a1["bot_url"].split("?start=", 1)[1]
        bot.handle_update({"message": {"message_id": 2, "text": f"/start {payload_a}", "chat": {"id": 777, "type": "private"},
                                       "from": {"id": 777, "first_name": "X"}}})
        a2 = self.s.post(valid_order(customer=evil, tg_init_data=make_init_data(TOKEN, {"id": 777, "first_name": "X"}))).json()
        self.assertTrue(a2["telegram_linked"])
        self.wait_bg()
        self.assertEqual(sorted(o["order_no"] for o in self.db().orders_for_telegram_user(777)),
                         sorted([a1["order_no"], a2["order_no"]]))
        v = self.db().get_order(victim["order_no"])
        self.assertEqual((v["telegram_user_id"], v["customer_name"]), (4242, "Алишер <b>"))
        to_777 = " ".join(p["text"] for m, p in fake.calls if m == "sendMessage" and p["chat_id"] == 777)
        self.assertNotIn(victim["order_no"], to_777)
        self.assertNotIn("Алишер", to_777)
        # продавец двигает статусы: жертве — только про её заказ, злоумышленнику — только про его
        for no in (victim["order_no"], a1["order_no"]):
            bot.handle_update({"callback_query": {"id": no, "from": {"id": 9}, "data": f"st:{no}:confirmed",
                                                  "message": {"message_id": 1, "chat": {"id": -100777}}}})
        to_victim = [p["text"] for m, p in fake.calls if m == "sendMessage" and p["chat_id"] == 4242]
        to_777 = [p["text"] for m, p in fake.calls if m == "sendMessage" and p["chat_id"] == 777]
        self.assertTrue(any(victim["order_no"] in t for t in to_victim))
        self.assertFalse(any(a1["order_no"] in t or "Attacker" in t for t in to_victim))
        self.assertTrue(any(a1["order_no"] in t for t in to_777))
        self.assertFalse(any(victim["order_no"] in t for t in to_777))

    def test_bot_url_signed_only_with_token_and_username(self):
        os.environ["BOT_USERNAME"] = "ipak_shop_bot"
        d = self.s.post(valid_order()).json()
        self.assertEqual(d["bot_url"], "")                                  # без токена подписать нечем
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        coid = "dup-check-0000-1111"
        d = self.s.post(valid_order(client_order_id=coid)).json()
        sig = orders_db.link_sig(TOKEN, d["order_no"])
        self.assertEqual(d["bot_url"], f"https://t.me/ipak_shop_bot?start=o_{d['order_no']}_{sig}")
        self.assertIn(d["bot_url"], self.sent[-1])                          # продавцу — чтобы переслать вручную
        self.assertNotIn("?start=", self.sent[0])                           # без токена ссылки нет
        self.assertRegex(d["bot_url"], r"^https://t\.me/[A-Za-z0-9_]{4,32}\?start=[A-Za-z0-9_-]{1,64}$")  # как на сайте
        dup = self.s.post(valid_order(client_order_id=coid)).json()
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["bot_url"], d["bot_url"])
        self.assertFalse(dup["telegram_linked"])
        os.environ["BOT_USERNAME"] = ""
        self.assertEqual(self.s.post(valid_order()).json()["bot_url"], "")

    def test_sender_uses_db_provider(self):
        """Бот получает функцию базы: база, открывшаяся позже, подхватывается без перезапуска службы."""
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        app = self.s.app
        app.db.close()
        app.db = None
        app._db_retry_at = time.time() + 3600                               # «база не открылась при старте»
        sender = app.sender()
        self.assertIsNone(sender.db)
        app._db_retry_at = 0                                                 # минута прошла — снова пробуем
        self.assertIsNotNone(sender.db)
        self.assertIs(sender.db, app.db)

    def test_check_warns_channel_without_admins(self):
        class Stub:
            origins = {ORIGIN}
            catalog = type("C", (), {"admin": {}})()

            def __init__(self, **kw):
                pass

            def health(self):
                return {"products": 1, "telegram": True, "db": True}

            @staticmethod
            def bot_url(no):
                return ""
        saved_app, argv = order_api.OrderApp, sys.argv
        keys = ("CHANNEL_ENABLED", "TELEGRAM_ADMIN_IDS", "CHANNEL_REVIEW_CHAT_ID")
        env = {k: os.environ.get(k) for k in keys}
        try:
            order_api.OrderApp = Stub
            sys.argv = ["order_api.py", "--check"]
            os.environ.update({"CHANNEL_ENABLED": "1", "TELEGRAM_ADMIN_IDS": "", "CHANNEL_REVIEW_CHAT_ID": ""})
            self.assertEqual(order_api.main(), 0)
            self.assertTrue(any("ВНИМАНИЕ" in m and "TELEGRAM_ADMIN_IDS" in m for m in self.logs))
            self.logs.clear()
            os.environ["TELEGRAM_ADMIN_IDS"] = "111"
            self.assertEqual(order_api.main(), 0)
            self.assertFalse(any("TELEGRAM_ADMIN_IDS" in m for m in self.logs))
        finally:
            order_api.OrderApp, sys.argv = saved_app, argv
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_bot_buttons_on_real_order_message(self):
        d = self.s.post(valid_order()).json()
        fake = FakeTelegram()
        bot = tg_bot.Bot(TOKEN, db=self.db(), admin_ids={9}, orders_chat="-100777", http=fake, log=lambda m: None,
                         offset_path=self.root / "data" / "orders" / "bot_offset.json")
        bot.handle_update({"callback_query": {"id": "1", "from": {"id": 9}, "data": f"st:{d['order_no']}:confirmed",
                                              "message": {"message_id": 501, "chat": {"id": -100777}}}})
        edit = [p for m, p in fake.calls if m == "editMessageText"][-1]
        self.assertTrue(edit["text"].startswith(f"<b>Заказ {d['order_no']}</b>\nСтатус: <b>подтверждён</b>\n"))
        self.assertEqual(edit["text"].count("Статус:"), 1)
        self.assertIn("Клиент: 1-й заказ", edit["text"])
        self.assertIn("Себестоимость", edit["text"])                         # чат продавца — всё на месте
        self.assertEqual(self.db().get_order(d["order_no"])["status"], "confirmed")

    # --- проверка наличия после заказа

    def test_background_verify_message(self):
        self.s.app.verify_enabled = True
        self.verify_result = {"cacharel_tr": {"777": {"sizes": ["M", "L"], "in_stock": True}},
                              "trendyol": {"555": None}}
        d = self.s.post(valid_order(items=[{"id": "AAAAAA2", "size": "48", "qty": 1},
                                           {"id": "BBBBBB3", "size": "M", "qty": 1},
                                           {"id": "DDDDDD5", "size": "S", "qty": 1}])).json()
        self.assertTrue(d["ok"])
        self.wait_bg()
        self.assertEqual(sorted(s for s, _ in self.verify_calls), ["cacharel_tr", "trendyol"])
        self.assertEqual(dict(self.verify_calls)["cacharel_tr"],
                         [{"source_item_id": "777", "url": "https://www.cacharel.com.tr/gomlek-777/"}])
        self.assertEqual(len(self.sent), 2)
        v = self.sent[1]
        self.assertIn(f"Проверка наличия · заказ {d['order_no']}", v)
        self.assertIn("<code>BBBBBB3</code> Cacharel, размер M: ✅ есть, размеры сейчас: M, L", v)
        self.assertIn("<code>DDDDDD5</code> Lacoste, размер S: ❌ нет в наличии", v)
        self.assertIn("<code>AAAAAA2</code> Boss, размер 48: 🔗 YOOX — проверить по ссылке вручную", v)
        self.assertEqual(self.replies[1], 501)                              # ответом на сообщение заказа
        self.assertIsNone(self.markups[1])
        self.assertNotIn("yoox.com", "".join(str(r) for _, r in self.verify_calls))   # YOOX не запрашивается
        notes = [ev["note"] for ev in self.db().get_order(d["order_no"], with_events=True)["events"]]
        self.assertIn("verify sent", notes)

    def test_background_verify_size_gone_and_errors(self):
        self.s.app.verify_enabled = True
        self.verify_result = {"cacharel_tr": {"777": {"sizes": ["L"], "in_stock": True}},
                              "trendyol": RuntimeError("blocked")}
        self.s.post(valid_order(items=[{"id": "BBBBBB3", "size": "M", "qty": 1},
                                       {"id": "DDDDDD5", "size": "S", "qty": 1}]))
        self.wait_bg()
        v = self.sent[1]
        self.assertIn("⚠ размера M нет, размеры сейчас: L", v)
        self.assertIn("DDDDDD5</code> Lacoste, размер S: ❓ проверить не удалось", v)
        # верификатор ответил, но про товар ничего не сказал — тоже «не удалось»
        self.verify_result = {"cacharel_tr": {}}
        self.s.post(valid_order(items=[{"id": "BBBBBB3", "size": "M", "qty": 1}]))
        self.wait_bg()
        self.assertIn("❓ проверить не удалось", self.sent[-1])

    def test_no_verify_for_yoox_only_or_disabled(self):
        self.s.app.verify_enabled = True
        self.s.post(valid_order(items=[{"id": "AAAAAA2", "size": "48", "qty": 1}]))
        self.wait_bg()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.verify_calls, [])
        s2 = Server(self.root, verify=False)
        try:
            s2.post(valid_order(items=[{"id": "BBBBBB3", "size": "M", "qty": 1}]))
            s2.app.join_background(5)
        finally:
            s2.close()
        self.assertEqual(self.verify_calls, [])

    # --- база недоступна

    def test_db_failure_still_accepts_order(self):
        self.s.app.db.close()
        self.s.app.db = None
        self.s.app._db_retry_at = time.time() + 3600                        # не переоткрывать
        d = self.s.post(valid_order()).json()
        self.assertTrue(d["ok"])
        self.assertRegex(d["order_no"], r"^YR-\d{6}-[A-Z2-9]{4}$")
        self.assertIn("НЕ записан в базу", self.sent[0])
        self.assertIsNone(self.markups[0])                                  # без базы кнопок нет
        rec = json.loads((self.root / "data/orders/orders.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertFalse(rec["db"])
        self.assertFalse(requests.get(self.s.url + "/api/health", timeout=10).json()["db"])

    def test_daily_backup(self):
        self.s.post(valid_order())
        app = self.s.app
        self.assertIsNone(app.daily_backup(now=datetime(2026, 10, 5, 2, 0)))     # до 03:00 — рано
        made = app.daily_backup(now=datetime(2026, 10, 5, 3, 30))
        self.assertTrue(made and made.exists())
        self.assertEqual(made.name, "orders-20261005.sqlite.gz")
        self.assertIsNone(app.daily_backup(now=datetime(2026, 10, 5, 4, 0)))     # за сегодня уже есть
        old = made.with_name("orders-20200101.sqlite.gz")
        old.write_bytes(b"x")
        os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
        app.daily_backup(now=datetime(2026, 10, 6, 3, 30))
        self.assertFalse(old.exists())

    # --- проверка ввода

    def assertRejected(self, resp, code=400, text=None):
        self.assertEqual(resp.status_code, code, resp.text)
        d = resp.json()
        self.assertFalse(d["ok"])
        if text:
            self.assertIn(text, d["error"])

    def test_validation(self):
        o = valid_order
        cust = valid_order()["customer"]
        self.assertRejected(self.s.post(o(website="http://spam")), text="spam")
        self.assertRejected(self.s.post(o(customer={**cust, "website": "x"})), text="spam")
        self.assertRejected(self.s.post(o(items=[{"id": "AAAAAA2", "size": "48", "qty": 1}] * 31)), text="30")
        self.assertRejected(self.s.post(o(items=[])), text="items")
        self.assertRejected(self.s.post(o(items="AAAAAA2")), text="items")
        self.assertRejected(self.s.post(o(items=[{"id": "AAAA'2;", "qty": 1}])), text="код")
        self.assertRejected(self.s.post(o(items=[{"id": "AAAAAA2", "qty": 0}])), text="qty")
        self.assertRejected(self.s.post(o(items=[{"id": "AAAAAA2", "qty": 11}])), text="qty")
        self.assertRejected(self.s.post(o(items=[{"id": "AAAAAA2", "qty": 1.5}])), text="qty")
        self.assertRejected(self.s.post(o(items=[{"id": "AAAAAA2", "qty": True}])), text="qty")
        self.assertRejected(self.s.post(o(items=[{"id": "AAAAAA2", "size": "4" * 21, "qty": 1}])), text="size")
        self.assertRejected(self.s.post(o(customer={**cust, "name": "А" * 81})), text="name")
        self.assertRejected(self.s.post(o(customer={**cust, "name": " "})), text="name")
        self.assertRejected(self.s.post(o(customer={**cust, "phone": "call me maybe"})), text="phone")
        self.assertRejected(self.s.post(o(customer={**cust, "phone": "12-34"})), text="phone")
        self.assertRejected(self.s.post(o(customer={**cust, "phone": "123 45 67 89"})), text="phone")   # не код УЗ
        self.assertRejected(self.s.post(o(customer={**cust, "phone": "7 916 123 45 67"})), text="phone")  # без +
        self.assertRejected(self.s.post(o(customer={**cust, "telegram": "@a b"})), text="telegram")
        self.assertRejected(self.s.post(o(customer={**cust, "phone": "", "telegram": ""})), text="телефон")
        self.assertRejected(self.s.post(o(customer={**cust, "comment": "x" * 501})), text="comment")
        self.assertRejected(self.s.post(o(customer={**cust, "city": {"a": 1}})), text="city")
        self.assertRejected(self.s.post(o(customer=None)), text="customer")
        self.assertRejected(self.s.post(None, raw=b"{not json"), text="JSON")
        self.assertRejected(self.s.post(None, raw=b"[1,2]"))
        self.assertRejected(self.s.post(None, raw=b'{"a":"' + b"x" * 21000 + b'"}'), code=413)
        self.assertRejected(self.s.post(o(), ctype="application/x-www-form-urlencoded"), code=415)
        self.assertEqual(self.sent, [])                                     # ничего не ушло в Telegram
        # телефон без +998, t.me-ссылка вместо ника, неизвестный язык — принимаются и нормализуются
        r = self.s.post(o(customer={**cust, "phone": "90 123 45 67", "telegram": "https://t.me/alisher_t"}, lang="de"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('href="tel:+998901234567"', self.sent[-1])
        self.assertIn("язык: ru", self.sent[-1])
        r = self.s.post(o(customer={**cust, "phone": "+7 916 123-45-67"}))     # иностранный — с «+»
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('href="tel:+79161234567"', self.sent[-1])

    # --- CORS

    def test_cors(self):
        u = self.s.url + "/api/order"
        r = requests.options(u, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
                                         "Access-Control-Request-Headers": "content-type"}, timeout=10)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertIn("POST", r.headers["Access-Control-Allow-Methods"])
        self.assertIn("Content-Type", r.headers["Access-Control-Allow-Headers"])
        r = requests.options(u, headers={"Origin": "https://evil.example"}, timeout=10)
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)
        self.assertRejected(self.s.post(valid_order(), origin="https://evil.example"), code=403)
        self.assertRejected(self.s.post(valid_order(), origin=None), code=403)
        self.assertEqual(self.sent, [])
        h = requests.get(self.s.url + "/api/health", timeout=10)
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.json()["products"], 4)
        self.assertTrue(h.json()["db"])
        self.assertEqual(requests.get(self.s.url + "/products-admin.js", timeout=10).status_code, 404)

    # --- ограничение частоты

    def test_rate_limit(self):
        s2 = Server(self.root, rate_limit=2, rate_window=600)
        try:
            for _ in range(2):
                self.assertEqual(s2.post(valid_order()).status_code, 200)
            r = s2.post(valid_order())
            self.assertRejected(r, code=429)
            self.assertGreater(int(r.headers["Retry-After"]), 0)
            self.assertEqual(len(self.sent), 2)
        finally:
            s2.close()

    def test_rate_limit_garbage_requests(self):
        s2 = Server(self.root, rate_limit=1, rate_window=600)          # мусор: лимит max(30, 6×) попыток
        try:
            codes = [s2.post(None, raw=b"{bad").status_code for _ in range(31)]
            self.assertEqual(codes[:30], [400] * 30)
            self.assertEqual(codes[30], 429)
        finally:
            s2.close()


class HelpersTest(unittest.TestCase):
    def test_norm_phone(self):
        ok = {"90 123 45 67": "+998901234567", "+998 (90) 123-45-67": "+998901234567",
              "998901234567": "+998901234567", "00998901234567": "+998901234567", "33 123 45 67": "+998331234567",
              "71 200 00 00": "+998712000000", "20 123 45 67": "+998201234567", "+7 916 123 45 67": "+79161234567",
              "0049 30 1234567": "+49301234567", "": ""}
        for raw, want in ok.items():
            self.assertEqual(order_api.norm_phone(raw), want, raw)
        for raw in ("901234567 1", "12-34", "8 90 123 45 67", "+99890123456", "+9989012345678", "123456789",
                    "+0123456789", "998101234567", "4 123 45 67 8", "+12", "90-123-45-67-ext"):
            with self.assertRaises(order_api.Invalid, msg=raw):
                order_api.norm_phone(raw)

    def test_verify_init_data(self):
        user = {"id": 5, "first_name": "Ann <b>", "username": "ann_x"}
        now = 1_760_000_000
        good = make_init_data(TOKEN, user, auth_date=now - 60, start_param="p_AAAAAA2")
        u = order_api.verify_init_data(good, TOKEN, now=now)
        self.assertEqual(u["id"], 5)
        self.assertEqual(u["username"], "ann_x")
        self.assertEqual(u["start_param"], "p_AAAAAA2")
        self.assertIsNone(order_api.verify_init_data(good, TOKEN + "x", now=now))
        self.assertIsNone(order_api.verify_init_data(good, TOKEN, now=now + 25 * 3600))    # устарел
        self.assertIsNone(order_api.verify_init_data(good, TOKEN, now=now - 3600))         # из будущего
        self.assertIsNone(order_api.verify_init_data(good + "&hash=" + "0" * 64, TOKEN, now=now))   # повтор ключа
        self.assertIsNone(order_api.verify_init_data(good, "", now=now))
        self.assertIsNone(order_api.verify_init_data("", TOKEN, now=now))
        no_user = make_init_data(TOKEN, {}, auth_date=now).replace("user=%7B%7D&", "")
        self.assertIsNone(order_api.verify_init_data(no_user, TOKEN, now=now))
        bad_id = make_init_data(TOKEN, {"id": "5"}, auth_date=now)
        self.assertIsNone(order_api.verify_init_data(bad_id, TOKEN, now=now))

    def test_split_message(self):
        text = "\n".join(f"<b>строка {i}</b> " + "x" * 100 for i in range(120))
        parts = order_api.split_message(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        self.assertEqual("\n".join(parts), text)                          # ничего не потерялось
        for p in parts:                                                   # теги не разрезаны
            self.assertEqual(p.count("<b>"), p.count("</b>"))
        huge = order_api.split_message("y" * 9000)
        self.assertTrue(all(len(p) <= 4096 for p in huge))
        self.assertEqual(sum(len(p) for p in huge), 9000)

    def test_long_order_message_splits(self):
        items = [{"id": "AAAAAA2", "size": "48", "qty": 1}] * 30
        order = order_api.validate(valid_order(items=items))
        tmp = tempfile.TemporaryDirectory()
        try:
            write_site(Path(tmp.name))
            priced = order_api.price_order(order, order_api.Catalog(Path(tmp.name) / "site"))
            msg = order_api.build_message("YR-261004-TEST", order, priced, datetime.now(), {"customer_orders": 4})
            self.assertGreater(len(msg), 4096)
            self.assertTrue(all(len(p) <= 4096 for p in order_api.split_message(msg)))
            self.assertIn("Клиент: 4-й заказ", msg)
        finally:
            tmp.cleanup()

    def test_send_telegram_keyboard_on_last_part(self):
        calls = []

        class R:
            status_code = 200

            def __init__(self, n):
                self.n = n

            def json(self):
                return {"ok": True, "result": {"message_id": self.n, "chat": {"id": -1}}}

        orig_post, env = order_api.requests.post, {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ORDERS_CHAT_ID")}
        order_api.requests.post = lambda url, timeout=None, json=None, **kw: (calls.append(json), R(len(calls)))[1]
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_ORDERS_CHAT_ID"] = TOKEN, "-1"
        try:
            kb = tg_bot.status_keyboard("YR-261004-ABCD", "new")
            res = order_api.send_telegram("\n".join("строка " + "x" * 100 for _ in range(100)), reply_markup=kb, reply_to=7)
        finally:
            order_api.requests.post = orig_post
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertGreater(len(calls), 1)
        self.assertEqual(res["message_id"], len(calls))                   # id последней части (с кнопками)
        self.assertNotIn("reply_markup", calls[0])
        self.assertEqual(calls[-1]["reply_markup"], kb)
        self.assertEqual(calls[0]["reply_parameters"]["message_id"], 7)
        self.assertNotIn("reply_parameters", calls[-1])

    def test_order_lookup_extract(self):
        known = {"AAAAAA2", "BBBBBB3"}
        text = "Здравствуйте! Хочу aaaaaa2 размер 48, и ещё BBBBBB3 (L). Код XYZ1234 тоже? Спасибо, ПРИВЕТИК"
        found, unknown = order_lookup.extract_codes(text, known)
        self.assertEqual(found, ["AAAAAA2", "BBBBBB3"])
        self.assertEqual(unknown, ["XYZ1234"])                             # «ПРИВЕТИК» — не код (8 букв)
        tmp = tempfile.TemporaryDirectory()
        try:
            write_site(Path(tmp.name))
            cat = order_api.Catalog(Path(tmp.name) / "site")
            prod, adm = cat.get("AAAAAA2")
            out = order_lookup.describe("AAAAAA2", prod, adm)
            for s in ("Boss", "1 290 000 сум", "YOOX", "https://www.yoox.com/it/123AB/item", "61 EUR (было 250)",
                      "46, 48, 50"):
                self.assertIn(s, out)
        finally:
            tmp.cleanup()

    def test_order_lookup_order_numbers(self):
        self.assertEqual(order_lookup.extract_order_numbers("статус yr-261005-abcd и YR-261005-ZZ22?"),
                         ["YR-261005-ABCD", "YR-261005-ZZ22"])
        tmp = tempfile.TemporaryDirectory()
        try:
            db = orders_db.OrdersDB(Path(tmp.name) / "o.sqlite")
            db.add_order(order_no="YR-261005-ABCD", customer={"name": "Иван", "phone": "+998901112233"},
                         items=[{"id": "AAAAAA2", "brand": "Boss", "title": "Брюки", "size": "48", "qty": 1,
                                 "price_uzs": 1290000}], total_uzs=1290000)
            db.set_status("YR-261005-ABCD", "confirmed", by="test")
            out = order_lookup.describe_order(db.get_order("YR-261005-ABCD", with_events=True))
            for s in ("YR-261005-ABCD", "подтверждён", "Иван", "+998901112233", "Boss", "1 290 000", "645 000"):
                self.assertIn(s, out)
            db.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
