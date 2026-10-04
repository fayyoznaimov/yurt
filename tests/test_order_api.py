"""Проверка приёма заказов (python tests/test_order_api.py). Сервер поднимается на случайном порту в потоке,
данные — маленький выдуманный каталог во временной папке, Telegram подменён (ничего не отправляется)."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import order_api  # noqa: E402
import order_lookup  # noqa: E402

ORIGIN = "https://fayyoznaimov.github.io"
PRODUCTS = {"summary": {"generated_at": "04.10.2026 10:00"}, "site": {}, "products": [
    {"id": "AAAAAA2", "brand": "Boss", "title": "Брюки Boss", "type": "брюки", "price_uzs": 1290000,
     "sizes": ["46", "48", "50"], "in_stock": True, "images": []},
    {"id": "BBBBBB3", "brand": "Cacharel", "title": "Рубашка Cacharel", "type": "рубашки", "price_uzs": 590000,
     "sizes": ["M", "L"], "in_stock": True, "images": []},
    {"id": "CCCCCC4", "brand": "Kiton", "title": "Пиджак Kiton", "type": "пиджаки", "price_uzs": 9990000,
     "sizes": [], "in_stock": False, "images": []},
]}
ADMIN = {
    "_meta": {"generated_at": "04.10.2026 10:00"},
    "AAAAAA2": {"source": "yoox", "source_item_id": "123AB", "url": "https://www.yoox.com/it/123AB/item",
                "title_original": "Pantaloni, nero", "price_now": 61.0, "price_old": 250.0, "currency": "EUR",
                "cost_uzs": 990000, "margin_uzs": 300000},
    "BBBBBB3": {"source": "cacharel_tr", "source_item_id": "777", "url": "https://www.cacharel.com.tr/gomlek-777/",
                "title_original": "Gömlek", "price_now": 999.0, "price_old": 6999.0, "currency": "TRY",
                "cost_uzs": 290000, "margin_uzs": 300000},
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
         "page_url": "https://fayyoznaimov.github.io/yurt/#/?p=AAAAAA2", "lang": "ru", "website": ""}
    o.update(over)
    return o


class Server:
    def __init__(self, root: Path, **kw):
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


class OrderApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_site(self.root)
        self.sent: list[str] = []
        self.logs: list[str] = []
        self._send, self._log = order_api.send_telegram, order_api.log
        order_api.send_telegram = lambda text: (self.sent.append(text), True)[1]
        order_api.log = lambda msg: self.logs.append(msg)
        self.s = Server(self.root)

    def tearDown(self):
        self.s.close()
        order_api.send_telegram, order_api.log = self._send, self._log
        self.tmp.cleanup()

    # --- основной сценарий

    def test_valid_order_repriced_and_sent(self):
        r = self.s.post(valid_order())
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertRegex(d["order_no"], r"^YR-\d{6}-[A-Z2-9]{4}$")
        self.assertEqual(d["total_uzs"], 1290000 * 2 + 590000)              # цены сервера, не клиента
        self.assertEqual(d["unavailable"], ["BBBBBB3 (XL)"])
        self.assertTrue(d["notified"])
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), ORIGIN)
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertIn(d["order_no"], msg)
        self.assertIn("https://www.yoox.com/it/123AB/item", msg)              # ссылка на источник
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
        self.assertIn("YOOX", msg)
        self.assertIn("Cacharel TR", msg)
        # заказ сохранён целиком (с телефоном) в orders.jsonl ...
        rec = json.loads((self.root / "data/orders/orders.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["order_no"], d["order_no"])
        self.assertEqual(rec["customer"]["phone"], "+998901234567")
        self.assertEqual(rec["items"][0]["price_uzs"], 1290000)
        # ... а в журнале полного номера нет
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
        order_api.send_telegram = lambda text: False
        d = self.s.post(valid_order()).json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["notified"])
        lines = (self.root / "data/orders/orders.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1]), {**json.loads(lines[-1]), "event": "telegram_failed"})

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
        # телефон без +, t.me-ссылка вместо ника, неизвестный язык — принимаются и нормализуются
        r = self.s.post(o(customer={**cust, "phone": "90 123 45 67", "telegram": "https://t.me/alisher_t"}, lang="de"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('href="tel:901234567"', self.sent[-1])
        self.assertIn("язык: ru", self.sent[-1])

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
        self.assertEqual(h.json()["products"], 3)
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
            from datetime import datetime
            msg = order_api.build_message("YR-261004-TEST", order, priced, datetime.now())
            self.assertGreater(len(msg), 4096)
            self.assertTrue(all(len(p) <= 4096 for p in order_api.split_message(msg)))
        finally:
            tmp.cleanup()

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
