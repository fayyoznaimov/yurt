"""Telegram-бот tg_bot.py: кнопки статусов, сообщения покупателю, /start, реестр обработчиков, опрос.
Bot API подменён (FakeTelegram) — в сеть ничего не уходит."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orders_db  # noqa: E402
import tg_bot  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ADMIN = 111
SELLER_CHAT = -100500
NO = "YR-261005-ABCD"
ITEMS = [{"id": "AAAAAA2", "brand": "Boss", "title": "Брюки Boss", "size": "48", "qty": 2, "price_uzs": 1290000,
          "source": "yoox", "shop": "YOOX", "url": "https://www.yoox.com/it/123AB/item", "cost_uzs": 990000,
          "currency": "EUR", "price_now": 61.0}]
FORBIDDEN = ("yoox", "trendyol", "akinon", "dsmcdn", "€", "₺", "eur", "try", "себестоим", "маржа", "закуп", "возврат")
TOKEN = "123:TEST"


def start(no: str, token: str = TOKEN) -> str:
    """/start с подписанным параметром (как в bot_url сервера)."""
    return "/start " + orders_db.start_payload(token, no)


def link(db, no: str, uid: int, **user) -> orders_db.LinkResult:
    return db.link_telegram(no, {"id": uid, **user}, sig=orders_db.link_sig(TOKEN, no), secret=TOKEN)


class FakeTelegram:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[list] = []
        self.fail: dict[str, dict] = {}
        self.n = 2000
        self.on_get_updates = None

    def __call__(self, method, params):
        self.calls.append((method, json.loads(json.dumps(params))))
        if method in self.fail:
            return None, self.fail[method]
        if method == "getUpdates":
            if self.on_get_updates:
                return self.on_get_updates(params)
            return (self.updates.pop(0) if self.updates else []), {"ok": True}
        if method == "sendMessage":
            self.n += 1
            return {"message_id": self.n, "chat": {"id": params.get("chat_id")}}, {"ok": True}
        return True, {"ok": True}

    def of(self, method):
        return [p for m, p in self.calls if m == method]


def cq(data, uid=ADMIN, chat=SELLER_CHAT, message_id=42, cid="cb1"):
    return {"callback_query": {"id": cid, "from": {"id": uid, "first_name": "S"}, "data": data,
                               "message": {"message_id": message_id, "chat": {"id": chat, "type": "supergroup"}}}}


def msg(text, uid=4242, chat_type="private", username="real_name"):
    return {"message": {"message_id": 1, "from": {"id": uid, "first_name": "Алишер", "username": username},
                        "chat": {"id": uid if chat_type == "private" else SELLER_CHAT, "type": chat_type}, "text": text}}


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = orders_db.OrdersDB(self.dir / "orders.sqlite")
        self.db.add_order(order_no=NO, customer={"name": "Алишер", "phone": "+998901234567", "telegram": "alisher_t"},
                          items=ITEMS, total_uzs=2580000)
        self.db.set_tg_message(NO, SELLER_CHAT, 42, f"<b>Заказ {NO}</b>\n{tg_bot.status_line('new')}\nТелефон: +998901234567")
        self.fake = FakeTelegram()
        self.logs: list[str] = []
        self.bot = self.make_bot()
        self._env = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "SHOP_URL", "SELLER_TELEGRAM",
                                                    "CHANNEL_REVIEW_CHAT_ID", "BOT_USERNAME", "TELEGRAM_ADMIN_IDS")}

    def make_bot(self, **kw):
        args = dict(db=self.db, admin_ids={ADMIN}, orders_chat=str(SELLER_CHAT), shop_url="https://shop.example/yurt/",
                    bot_username="ipak_shop_bot", seller_telegram="ipak_seller",
                    messages_path=ROOT / "server" / "order_messages.json", offset_path=self.dir / "bot_offset.json",
                    http=self.fake, log=self.logs.append)
        args.update(kw)
        return tg_bot.Bot(TOKEN, **args)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.db.close()
        self.tmp.cleanup()

    # --- кнопки статусов

    def test_status_button_flow(self):
        self.bot.handle_update(cq(f"st:{NO}:confirmed"))
        self.assertEqual(self.db.get_order(NO)["status"], "confirmed")
        edit = self.fake.of("editMessageText")[-1]
        self.assertEqual((edit["chat_id"], edit["message_id"]), (SELLER_CHAT, 42))
        self.assertIn("Статус: <b>подтверждён</b>", edit["text"])
        self.assertNotIn("Статус: <b>новый</b>", edit["text"])
        self.assertEqual(edit["parse_mode"], "HTML")
        buttons = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(buttons, [f"st:{NO}:prepaid", f"st:{NO}:cancelled"])
        self.assertIn("Статус: <b>подтверждён</b>", self.db.get_order(NO)["tg_text"])     # сохранён новый текст
        ans = self.fake.of("answerCallbackQuery")[-1]
        self.assertEqual(ans["text"], "Статус: подтверждён")
        # покупатель не подключил бота → текст продавцу, чтобы отправить вручную (с кнопкой на его ник)
        seller = [p for p in self.fake.of("sendMessage") if p["chat_id"] == SELLER_CHAT][-1]
        self.assertIn("не подключил бота", seller["text"])
        self.assertIn("Заказ YR-261005-ABCD подтверждён", seller["text"])
        self.assertIn("1 290 000", seller["text"])                          # предоплата 50% от 2 580 000
        self.assertEqual(seller["reply_markup"]["inline_keyboard"][0][0]["url"], "https://t.me/alisher_t")
        # дальше по цепочке до «выдан»: кнопки по шагам, в конце — без кнопок
        expect = {"prepaid": [f"st:{NO}:ordered"], "ordered": [f"st:{NO}:shipped"], "shipped": [f"st:{NO}:delivered"],
                  "delivered": []}
        for st, want in expect.items():
            self.bot.handle_update(cq(f"st:{NO}:{st}"))
            kb = self.fake.of("editMessageText")[-1]["reply_markup"]["inline_keyboard"]
            self.assertEqual([b["callback_data"] for row in kb for b in row], want, st)
        self.assertEqual(self.db.get_order(NO)["status"], "delivered")
        ev = self.db.get_order(NO, with_events=True)["events"]
        self.assertEqual([e["status"] for e in ev if e["status"]], ["new", "confirmed", "prepaid", "ordered", "shipped", "delivered"])
        self.assertEqual(ev[1]["by"], f"tg:{ADMIN}")

    def test_button_texts(self):
        kb = tg_bot.status_keyboard(NO, "new")["inline_keyboard"][0]
        self.assertIn("Подтвердить", kb[0]["text"])
        self.assertIn("Отменить", kb[1]["text"])
        self.assertIn("Предоплата получена", tg_bot.status_keyboard(NO, "confirmed")["inline_keyboard"][0][0]["text"])
        self.assertIn("Заказан у бренда", tg_bot.status_keyboard(NO, "prepaid")["inline_keyboard"][0][0]["text"])
        self.assertIn("В пути", tg_bot.status_keyboard(NO, "ordered")["inline_keyboard"][0][0]["text"])
        self.assertIn("Выдан", tg_bot.status_keyboard(NO, "shipped")["inline_keyboard"][0][0]["text"])
        self.assertIsNone(tg_bot.status_keyboard(NO, "delivered"))
        self.assertIsNone(tg_bot.status_keyboard(NO, "cancelled"))
        for st in tg_bot.STATUS_BUTTONS:
            for row in tg_bot.status_keyboard(NO, st)["inline_keyboard"]:
                for b in row:
                    self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_customer_gets_template_when_linked(self):
        link(self.db, NO, 4242, username="real_name")
        self.bot.handle_update(cq(f"st:{NO}:confirmed"))
        to_user = [p for p in self.fake.of("sendMessage") if p["chat_id"] == 4242]
        self.assertEqual(len(to_user), 1)
        text = to_user[0]["text"]
        self.assertIn("Здравствуйте, Алишер! Заказ YR-261005-ABCD подтверждён", text)
        self.assertIn("Boss — Брюки Boss, размер 48 × 2", text)
        self.assertIn("Предоплата 50% — 1 290 000 сум", text)
        self.assertIn("Заказываем после оплаты", text)
        self.assertNotIn("parse_mode", to_user[0])                          # обычный текст
        for bad in FORBIDDEN:
            self.assertNotIn(bad, text.lower())
        self.assertFalse([p for p in self.fake.of("sendMessage") if p["chat_id"] == SELLER_CHAT])
        notes = [e["note"] for e in self.db.get_order(NO, with_events=True)["events"]]
        self.assertIn("customer_msg confirmed: sent", notes)
        # покупатель заблокировал бота → текст продавцу
        self.fake.fail["sendMessage"] = {"ok": False, "error_code": 403, "description": "bot was blocked by the user"}
        self.bot.handle_update(cq(f"st:{NO}:prepaid"))
        del self.fake.fail["sendMessage"]
        notes = [e["note"] for e in self.db.get_order(NO, with_events=True)["events"]]
        self.assertIn("customer_msg prepaid: failed", notes)
        self.assertTrue(any("не доставлено" in m for m in self.logs))

    def test_repeat_and_stale_buttons(self):
        self.bot.handle_update(cq(f"st:{NO}:confirmed"))
        n_msgs = len(self.fake.of("sendMessage"))
        self.bot.handle_update(cq(f"st:{NO}:confirmed", cid="cb2"))       # двойное нажатие
        self.assertEqual(len(self.fake.of("sendMessage")), n_msgs)          # второй раз покупателю не пишем
        self.bot.handle_update(cq(f"st:{NO}:prepaid"))
        self.bot.handle_update(cq(f"st:{NO}:cancelled", cid="cb3"))        # старая кнопка «Отменить»?
        self.assertEqual(self.db.get_order(NO)["status"], "cancelled")     # из prepaid отмена разрешена
        self.bot.handle_update(cq(f"st:{NO}:shipped", cid="cb4"))          # из отменённого — нельзя
        self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Статус уже: отменён")
        self.assertEqual(self.fake.of("editMessageText")[-1]["reply_markup"], {"inline_keyboard": []})
        self.bot.handle_update(cq("st:YR-000000-XXXX:confirmed", cid="cb5"))
        self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Заказ не найден")
        self.bot.handle_update(cq(f"st:{NO}:bogus", cid="cb6"))
        self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Неизвестный статус")
        self.bot.handle_update(cq("st:broken", cid="cb7"))
        self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Неверная кнопка")

    def test_other_message_only_markup_edited(self):
        self.bot.handle_update(cq(f"st:{NO}:confirmed", message_id=99))    # кнопка не на сохранённом сообщении
        self.assertFalse(self.fake.of("editMessageText"))
        mk = self.fake.of("editMessageReplyMarkup")[-1]
        self.assertEqual(mk["message_id"], 99)
        # если правка текста не удалась — меняем хотя бы кнопки
        self.fake.fail["editMessageText"] = {"ok": False, "error_code": 400, "description": "message is not modified"}
        self.bot.handle_update(cq(f"st:{NO}:prepaid"))
        self.assertEqual(self.fake.of("editMessageReplyMarkup")[-1]["message_id"], 42)

    def test_non_admin_rejected(self):
        self.bot.handle_update(cq(f"st:{NO}:confirmed", uid=999))
        self.assertEqual(self.db.get_order(NO)["status"], "new")
        ans = self.fake.of("answerCallbackQuery")[-1]
        self.assertEqual(ans["text"], "Нет доступа")
        self.assertTrue(ans["show_alert"])
        self.assertFalse(self.fake.of("editMessageText"))

    def test_admin_rules(self):
        self.assertTrue(self.bot.is_admin(ADMIN))
        self.assertFalse(self.bot.is_admin(999, SELLER_CHAT))               # список админов задан — только он
        b = self.make_bot(admin_ids=set(), orders_chat="555")                # личный чат продавца
        self.assertTrue(b.is_admin(555))
        self.assertFalse(b.is_admin(556, 555))
        g = self.make_bot(admin_ids=set(), orders_chat="-100500")            # закрытая группа заказов
        self.assertTrue(g.is_admin(777, -100500))
        self.assertFalse(g.is_admin(777, 777))
        self.assertFalse(self.make_bot(admin_ids=set(), orders_chat="").is_admin(1, 1))
        self.assertFalse(self.make_bot(admin_ids=set(), orders_chat="@channel").is_admin(1, 1))
        self.assertEqual(tg_bot.parse_admin_ids("111, 222;bad 333"), {111, 222, 333})
        self.assertEqual(tg_bot.parse_admin_ids(""), set())

    def test_review_chat_is_admin_for_channel_buttons_only(self):
        """Чат заказов — группа, предложения канала — в личку продавца, TELEGRAM_ADMIN_IDS пуст."""
        seen = []
        prev_cb = [x for x in tg_bot._CALLBACKS if x[0] == "ch:"]           # регистрацию channel.py вернём
        prev_cmd = tg_bot._COMMANDS.get("channel")
        tg_bot.register_callback("ch:", lambda bot, q: seen.append(q["data"]) or "ok")
        tg_bot.register_command("channel", lambda bot, m, a: seen.append("/channel"), admin_only=True)
        try:
            b = self.make_bot(admin_ids=set(), orders_chat="-100500", review_chat="555")
            self.assertTrue(b.is_admin(555, 555, channel=True))
            self.assertFalse(b.is_admin(555, 555))                          # статусы заказов — нет
            b.handle_update(cq("ch:pub:AAAAAA2", uid=555, chat=555))
            self.assertEqual(seen, ["ch:pub:AAAAAA2"])
            b.handle_update(cq("ch:pub:AAAAAA2", uid=556, chat=556))        # другой человек — нет доступа
            self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Нет доступа")
            b.handle_update(cq(f"st:{NO}:confirmed", uid=555, chat=555))    # кнопки заказа — только группа
            self.assertEqual(self.db.get_order(NO)["status"], "new")
            b.handle_update(msg("/channel", uid=555))
            self.assertEqual(seen[-1], "/channel")
            # список админов задан — CHANNEL_REVIEW_CHAT_ID сам по себе прав не даёт
            a = self.make_bot(admin_ids={ADMIN}, orders_chat="-100500", review_chat="555")
            self.assertFalse(a.is_admin(555, 555, channel=True))
            # группа / мусор в CHANNEL_REVIEW_CHAT_ID — не админ
            for rc in ("-100777", "@me", "", "abc"):
                self.assertFalse(self.make_bot(admin_ids=set(), orders_chat="", review_chat=rc).is_admin(
                    -100777, -100777, channel=True), rc)
            os.environ["CHANNEL_REVIEW_CHAT_ID"] = "555"
            self.assertEqual(tg_bot.Bot.from_env(http=self.fake, offset_path=self.dir / "o.json").review_uid, 555)
        finally:
            os.environ.pop("CHANNEL_REVIEW_CHAT_ID", None)
            tg_bot.unregister("ch:")
            tg_bot.unregister("channel")
            for prefix, handler, admin_only in prev_cb:
                tg_bot.register_callback(prefix, handler, admin_only=admin_only)
            if prev_cmd:
                tg_bot.register_command("channel", prev_cmd[0], admin_only=prev_cmd[1])

    def test_channel_admin_warning(self):
        self.assertEqual(tg_bot.channel_admin_warning({}), "")
        self.assertEqual(tg_bot.channel_admin_warning({"CHANNEL_ENABLED": "0"}), "")
        self.assertEqual(tg_bot.channel_admin_warning({"CHANNEL_ENABLED": "1", "TELEGRAM_ADMIN_IDS": "111"}), "")
        w = tg_bot.channel_admin_warning({"CHANNEL_ENABLED": "1", "TELEGRAM_ADMIN_IDS": ""})
        self.assertIn("TELEGRAM_ADMIN_IDS", w)
        self.assertIn("Нет доступа", w)
        w = tg_bot.channel_admin_warning({"CHANNEL_ENABLED": "1", "CHANNEL_REVIEW_CHAT_ID": "123456789"})
        self.assertIn("CHANNEL_REVIEW_CHAT_ID", w)
        self.assertNotIn("123456789", w)                                      # полный id в журнал не пишем

    # --- /start, /stop

    def test_start_links_order(self):
        # без подписи (просто номер заказа) — не привязывается: номер угадывается перебором
        self.bot.handle_update(msg(f"/start o_{NO}"))
        self.assertIsNone(self.db.get_order(NO)["telegram_user_id"])
        self.assertIn("Не нашли такой заказ", self.fake.of("sendMessage")[-1]["text"])
        self.bot.handle_update(msg(start(NO, token="999:other-bot")))     # подпись чужим токеном
        self.assertIsNone(self.db.get_order(NO)["telegram_user_id"])
        # подписанная ссылка из ответа сервера — привязывает
        self.bot.handle_update(msg(start(NO)))
        o = self.db.get_order(NO)
        self.assertEqual(o["telegram_user_id"], 4242)
        self.assertEqual(o["customer_telegram_user_id"], 4242)
        reply = [p for p in self.fake.of("sendMessage") if p["chat_id"] == 4242][-1]
        self.assertIn("Здравствуйте, Алишер! Будем присылать сюда статус заказа YR-261005-ABCD", reply["text"])
        self.assertEqual(reply["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"], "https://shop.example/yurt/")
        seller = [p for p in self.fake.of("sendMessage") if p["chat_id"] == str(SELLER_CHAT)]
        self.assertTrue(seller and NO in seller[-1]["text"])
        # чужой пользователь с той же ссылкой — не привязывается, ответ без подробностей
        self.bot.handle_update(msg(start(NO), uid=5555))
        self.assertEqual(self.db.get_order(NO)["telegram_user_id"], 4242)
        reply = [p for p in self.fake.of("sendMessage") if p["chat_id"] == 5555][-1]
        self.assertIn("Не нашли такой заказ", reply["text"])
        self.assertIn("@ipak_seller", reply["text"])
        self.assertNotIn("Алишер", reply["text"])
        # строчные буквы и повтор тем же пользователем — нормально
        self.bot.handle_update(msg(start(NO).lower()))
        self.assertIn("Будем присылать", [p for p in self.fake.of("sendMessage") if p["chat_id"] == 4242][-1]["text"])
        # бот без токена подпись проверить не может — не привязывает ничего
        self.db.add_order(order_no="YR-261005-ZZZZ", customer={"name": "Б", "phone": "+998935554433"}, items=[],
                          total_uzs=0)
        self.make_bot(http=self.fake).handle_update(msg(start("YR-261005-ZZZZ"), uid=8888))
        self.assertEqual(self.db.get_order("YR-261005-ZZZZ")["telegram_user_id"], 8888)
        nt = tg_bot.Bot("", db=self.db, http=self.fake, log=self.logs.append, offset_path=self.dir / "o2.json")
        self.db.add_order(order_no="YR-261005-YYYY", customer={"name": "В", "phone": "+998935554400"}, items=[],
                          total_uzs=0)
        nt.handle_update(msg(start("YR-261005-YYYY"), uid=9999))
        self.assertIsNone(self.db.get_order("YR-261005-YYYY")["telegram_user_id"])

    def test_start_link_bruteforce_limited(self):
        for i in range(5):
            self.bot.handle_update(msg(f"/start o_YR-261005-ZZZ{i + 2}_AAAAAAAAAA", uid=6666))
        self.bot.handle_update(msg(start(NO), uid=6666))                    # даже верная ссылка после 5 ошибок
        self.assertIsNone(self.db.get_order(NO)["telegram_user_id"])
        self.bot.handle_update(msg("/start o_<script>", uid=7777))
        self.assertIn("Не нашли", [p for p in self.fake.of("sendMessage") if p["chat_id"] == 7777][-1]["text"])
        self.assertFalse(any("<script>" in m for m in self.logs))            # мусор из ссылки — не в журнал

    def test_poc_status_messages_only_to_order_owner(self):
        """PoC из ревью на уровне бота: заказ с телефоном жертвы + свой Telegram злоумышленника."""
        link(self.db, NO, 4242, username="alisher_real")                     # жертва подключила свой заказ
        bad_no = "YR-261005-EVIL"
        self.db.add_order(order_no=bad_no, customer={"name": "Служба безопасности банка", "phone": "+998901234567"},
                          items=ITEMS, total_uzs=2580000)
        # 1) заказ злоумышленника без привязки: его статусы НЕ уходят жертве (раньше уходили — фишинг от бота)
        self.bot.handle_update(cq(f"st:{bad_no}:confirmed"))
        self.assertEqual([p for p in self.fake.of("sendMessage") if p["chat_id"] == 4242], [])
        # 2) злоумышленник подключает СВОЙ заказ своей ссылкой — получает только его
        self.bot.handle_update(msg(start(bad_no), uid=777, username="evil_one"))
        self.assertEqual(self.db.get_order(bad_no)["telegram_user_id"], 777)
        self.assertEqual(self.db.get_order(NO)["telegram_user_id"], 4242)
        self.assertEqual([o["order_no"] for o in self.db.orders_for_telegram_user(777)], [bad_no])
        # 3) статусы заказа жертвы — только жертве, злоумышленнику ничего
        n777 = len([p for p in self.fake.of("sendMessage") if p["chat_id"] == 777])
        self.bot.handle_update(cq(f"st:{NO}:confirmed"))
        to_victim = [p for p in self.fake.of("sendMessage") if p["chat_id"] == 4242]
        self.assertEqual(len(to_victim), 1)
        self.assertIn("Алишер", to_victim[0]["text"])
        self.assertNotIn("банка", to_victim[0]["text"])
        self.assertEqual(len([p for p in self.fake.of("sendMessage") if p["chat_id"] == 777]), n777)
        # 4) подобрать подпись к заказу жертвы нельзя (своя подпись к чужому номеру)
        forged = "/start o_" + NO + "_" + orders_db.link_sig(TOKEN, bad_no)
        self.bot.handle_update(msg(forged, uid=777))
        self.assertEqual(self.db.get_order(NO)["telegram_user_id"], 4242)
        self.assertIn("Не нашли", [p for p in self.fake.of("sendMessage") if p["chat_id"] == 777][-1]["text"])

    def test_db_provider_picks_up_late_db(self):
        """Бот получает функцию вместо базы: база, открывшаяся после старта, работает без перезапуска."""
        box = {"db": None}
        b = self.make_bot(db=lambda: box["db"])
        b.handle_update(cq(f"st:{NO}:confirmed"))
        self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "База заказов недоступна")
        box["db"] = self.db                                                   # app._db() открыл базу позже
        b.handle_update(cq(f"st:{NO}:confirmed", cid="cb2"))
        self.assertEqual(self.db.get_order(NO)["status"], "confirmed")
        self.assertIs(b.db, self.db)

        def boom():
            raise OSError("disk")
        b2 = self.make_bot(db=boom)
        self.assertIsNone(b2.db)                                              # ошибка поставщика — «недоступна»
        b2.handle_update(cq(f"st:{NO}:prepaid", cid="cb3"))
        self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "База заказов недоступна")
        b2.db = self.db                                                       # обычное присваивание тоже работает
        self.assertIs(b2.db, self.db)

    def test_order_link(self):
        url = self.bot.order_link(NO)
        self.assertEqual(url, f"https://t.me/ipak_shop_bot?start=o_{NO}_{orders_db.link_sig(TOKEN, NO)}")
        self.assertRegex(url, r"^https://t\.me/[A-Za-z0-9_]{4,32}\?start=[A-Za-z0-9_-]{1,64}$")   # проверка сайта
        self.assertEqual(self.make_bot(bot_username="").order_link(NO), "")
        self.assertEqual(tg_bot.order_bot_url("ipak_shop_bot", "", NO), "")
        self.assertEqual(tg_bot.order_bot_url("bad name", TOKEN, NO), "")
        self.assertEqual(tg_bot.order_bot_url("@ipak_shop_bot", TOKEN, NO), url)

    def test_start_shop_buttons(self):
        self.bot.handle_update(msg("/start p_aaaaaa2"))
        r = self.fake.of("sendMessage")[-1]
        self.assertEqual(r["reply_markup"]["inline_keyboard"][0][0],
                         {"text": "Открыть магазин", "web_app": {"url": "https://shop.example/yurt/#/catalog?p=AAAAAA2"}})
        self.assertIn("Откройте товар", r["text"])
        self.bot.handle_update(msg("/start"))
        r = self.fake.of("sendMessage")[-1]
        self.assertEqual(r["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"], "https://shop.example/yurt/")
        self.assertIn("Предоплата 50%", r["text"])
        self.assertIn("доставка до 10 дней", r["text"])
        self.bot.handle_update(msg("/start p_../../x"))                     # неверный код — просто магазин
        self.assertEqual(self.fake.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"],
                         "https://shop.example/yurt/")
        self.bot.handle_update(msg("/start@ipak_shop_bot"))
        self.assertEqual(len(self.fake.of("sendMessage")), 4)
        self.bot.handle_update(msg("/start@other_bot"))                      # команда другому боту
        self.bot.handle_update(msg("/start", chat_type="supergroup"))       # в группе /start молчит
        self.assertEqual(len(self.fake.of("sendMessage")), 4)
        nohttps = self.make_bot(shop_url="")
        nohttps.handle_update(msg("/start p_AAAAAA2"))
        self.assertNotIn("reply_markup", self.fake.of("sendMessage")[-1])
        self.assertEqual(tg_bot.shop_link("https://x.uz/#/old", "AAAAAA2"), "https://x.uz/#/catalog?p=AAAAAA2")
        self.assertEqual(tg_bot.shop_button("Открыть магазин", "http://127.0.0.1:8000/")["url"], "http://127.0.0.1:8000/")

    def test_stop_and_hint(self):
        link(self.db, NO, 4242)
        self.db.set_marketing(4242, True)
        self.assertEqual([m["telegram_user_id"] for m in self.db.marketing_customers()], [4242])
        self.bot.handle_update(msg("/stop"))
        self.assertEqual(self.db.marketing_customers(), [])
        self.assertIn("больше не будем присылать новинки", self.fake.of("sendMessage")[-1]["text"])
        n = len(self.fake.of("sendMessage"))
        self.bot.handle_update(msg("где мой заказ?"))
        self.bot.handle_update(msg("алло?"))                                 # подсказка не чаще раза в 10 минут
        self.assertEqual(len(self.fake.of("sendMessage")), n + 1)
        self.assertIn("@ipak_seller", self.fake.of("sendMessage")[-1]["text"])
        self.bot.handle_update(msg("текст в группе", chat_type="supergroup"))
        self.assertEqual(len(self.fake.of("sendMessage")), n + 1)

    def test_seller_contact_from_content(self):
        b = self.make_bot(seller_telegram="")
        orig = tg_bot.CONTENT_PATH
        p = self.dir / "content.json"
        try:
            tg_bot.CONTENT_PATH = p
            p.write_text(json.dumps({"orders_telegram_username": "", "contacts": {"telegram": "https://t.me/ipak_uz"}}),
                         encoding="utf-8")
            self.assertEqual(b.seller_contact(), "в Telegram @ipak_uz")
            p.write_text(json.dumps({"orders_telegram_username": "your_username!"}), encoding="utf-8")
            self.assertEqual(b.seller_contact(), "в Telegram")
            tg_bot.CONTENT_PATH = self.dir / "missing.json"
            self.assertEqual(b.seller_contact(), "в Telegram")
        finally:
            tg_bot.CONTENT_PATH = orig

    # --- шаблоны

    def test_templates_clean(self):
        data = json.loads((ROOT / "server" / "order_messages.json").read_text(encoding="utf-8"))
        texts = [v for k, v in data.items() if isinstance(v, str) and not k.startswith("_")]
        texts += list(data["status"].values())
        self.assertEqual(set(data["status"]), {"confirmed", "prepaid", "ordered", "shipped", "delivered", "cancelled"})
        for t in texts:
            low = t.lower()
            for bad in FORBIDDEN:
                self.assertNotIn(bad, low, t)
            self.assertFalse(re.search(r"\btl\b", low), t)
            for ph in re.findall(r"\{(\w+)\}", t):
                self.assertIn(ph, ("name", "no", "items", "prepay", "total", "rest", "contact",
                                   "group", "cart", "ref_link", "bonus"), t)        # последние — воронка (funnel.py)
        self.assertIn("Предоплата 50%", data["status"]["confirmed"])
        self.assertIn("Заказываем после оплаты", data["status"]["confirmed"])
        self.assertIn("при получении", data["status"]["confirmed"])
        self.assertIn("до 10 дней", data["status"]["prepaid"])

    def test_render(self):
        o = self.db.get_order(NO)
        self.assertEqual(self.bot.render("Здравствуйте, {name}! {no} {prepay} {rest} {total} {unknown} {name.__class__}", o),
                         "Здравствуйте, Алишер! YR-261005-ABCD 1 290 000 1 290 000 2 580 000 {unknown} {name.__class__}")
        o["customer_name"] = ""
        self.assertEqual(self.bot.render("Здравствуйте, {name}! Заказ {no}.", o), "Здравствуйте! Заказ YR-261005-ABCD.")
        items = tg_bot.format_items(ITEMS + [{"id": "ZZZZZZ9", "price_uzs": 0}])
        self.assertEqual(items, "• Boss — Брюки Boss, размер 48 × 2")         # без источника и снятых товаров
        # файл шаблонов битый — встроенные тексты, без падения
        bad = self.dir / "bad.json"
        bad.write_text("{oops", encoding="utf-8")
        b = self.make_bot(messages_path=bad)
        self.assertEqual(b.messages()["button_shop"], "Открыть магазин")

    # --- реестр обработчиков

    def test_register_callback_and_command(self):
        seen = []

        def on_ch(bot, q):
            seen.append(q["data"])
            return "Опубликовано"

        def boom(bot, q):
            raise RuntimeError("x")

        def on_digest(bot, m, args):
            seen.append(("digest", args))

        tg_bot.register_callback("ch:", on_ch)
        tg_bot.register_callback("chx:", boom, admin_only=False)
        tg_bot.register_command("digest", on_digest, admin_only=True)
        try:
            self.bot.handle_update(cq("ch:post:123"))
            self.assertEqual(seen, ["ch:post:123"])
            self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Опубликовано")
            self.bot.handle_update(cq("ch:post:124", uid=999))              # не админ
            self.assertEqual(len(seen), 1)
            self.bot.handle_update(cq("chx:1", uid=999))                    # ошибка обработчика не роняет бота
            self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Ошибка, попробуйте ещё раз")
            self.bot.handle_update(cq("zz:unknown"))
            self.assertNotIn("text", self.fake.of("answerCallbackQuery")[-1])
            self.bot.handle_update(msg("/digest week", uid=ADMIN))
            self.bot.handle_update(msg("/digest week", uid=999))
            self.assertEqual(seen[-1], ("digest", "week"))
            self.assertEqual(sum(1 for x in seen if isinstance(x, tuple)), 1)
            with self.assertRaises(ValueError):
                tg_bot.register_command("start", on_digest)
            with self.assertRaises(ValueError):
                tg_bot.register_command("bad name", on_digest)
            with self.assertRaises(ValueError):
                tg_bot.register_callback("", on_ch)
        finally:
            tg_bot.unregister("ch:")
            tg_bot.unregister("chx:")
            tg_bot.unregister("digest")
        self.bot.handle_update(cq("ch:post:125"))
        self.assertEqual(len(seen), 2)
        self.assertTrue(any(p == "st:" for p, _, _ in tg_bot._CALLBACKS))   # встроенный остался

    def test_load_plugins(self):
        plug = self.dir / "ipak_test_plugin.py"
        plug.write_text(
            "import tg_bot\n"
            "SEEN = []\n"
            "def _h(bot, q):\n"
            "    SEEN.append(q['data'])\n"
            "    return 'ok'\n"
            "tg_bot.register_callback('tp:', _h)\n"
            "def tg_register(bot):\n"
            "    SEEN.append(('bot', bot.bot_username))\n", encoding="utf-8")
        (self.dir / "ipak_broken_plugin.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        sys.path.insert(0, str(self.dir))
        try:
            got = tg_bot.load_plugins(self.bot, "ipak_test_plugin, ipak_no_such_module, ipak_broken_plugin, bad name!",
                                      log=self.logs.append)
            self.assertEqual(got, ["ipak_test_plugin"])
            mod = sys.modules["ipak_test_plugin"]
            self.assertEqual(mod.SEEN, [("bot", "ipak_shop_bot")])
            self.bot.handle_update(cq("tp:1"))
            self.assertEqual(mod.SEEN[-1], "tp:1")
            self.assertTrue(any("ipak_broken_plugin" in m for m in self.logs))
            self.assertFalse(any("ipak_no_such_module" in m for m in self.logs))   # нет модуля — молча
        finally:
            sys.path.remove(str(self.dir))
            tg_bot.unregister("tp:")
            for m in ("ipak_test_plugin", "ipak_broken_plugin"):
                sys.modules.pop(m, None)

    # --- опрос

    def test_poll_once_and_offset(self):
        self.fake.updates = [[{"update_id": 10, **msg("/start")}, {"update_id": 11, **cq(f"st:{NO}:confirmed")},
                              {"update_id": 9, **msg("/start")}]]                   # старое — пропустить
        self.assertEqual(self.bot.poll_once(timeout=0), 2)
        self.assertEqual(self.db.get_order(NO)["status"], "confirmed")
        self.assertEqual(json.loads((self.dir / "bot_offset.json").read_text())["offset"], 12)
        b2 = self.make_bot()
        self.assertEqual(b2.offset, 12)
        b2.poll_once(timeout=0)
        gu = self.fake.of("getUpdates")[-1]
        self.assertEqual(gu["offset"], 12)
        self.assertEqual(gu["allowed_updates"], ["message", "callback_query"])
        # обновление, на котором обработчик падает, не зацикливает бота
        self.fake.updates = [[{"update_id": 12, "message": None}]]
        self.assertEqual(b2.poll_once(timeout=0), 1)
        self.assertEqual(b2.offset, 13)

    def test_run_forever_stops_and_backs_off(self):
        stop = threading.Event()
        count = {"n": 0}

        def gu(params):
            count["n"] += 1
            if count["n"] >= 1:
                stop.set()
            return None, {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates"}
        self.fake.on_get_updates = gu
        t = threading.Thread(target=self.bot.run_forever, args=(stop,))
        t.start()
        stop.wait(5)
        stop.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertTrue(any("конфликт" in m for m in self.logs))
        self.assertIsNone(tg_bot.current())

    def test_start_in_thread_needs_token(self):
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        self.assertIsNone(tg_bot.start_in_thread())

    def test_cli_set_menu(self):
        calls = []
        orig, argv = tg_bot._http_call, sys.argv
        tg_bot._http_call = lambda token, method, params, timeout: (calls.append((method, params)), (True, {"ok": True}))[1]
        os.environ["TELEGRAM_BOT_TOKEN"] = "123:TEST"
        try:
            os.environ["SHOP_URL"] = "https://shop.example/yurt/"
            sys.argv = ["tg_bot.py", "--set-menu"]
            self.assertEqual(tg_bot.main(), 0)
            self.assertEqual(calls[-1], ("setChatMenuButton", {"menu_button": {
                "type": "web_app", "text": "Открыть магазин", "web_app": {"url": "https://shop.example/yurt/"}}}))
            os.environ["SHOP_URL"] = "http://insecure.example/"
            self.assertEqual(tg_bot.main(), 1)                                  # web_app — только https
            self.assertEqual(len(calls), 1)
        finally:
            tg_bot._http_call, sys.argv = orig, argv

    def test_logs_without_phones(self):
        link(self.db, NO, 4242)
        self.fake.fail["sendMessage"] = {"ok": False, "error_code": 403, "description": "blocked"}
        self.bot.handle_update(cq(f"st:{NO}:confirmed"))
        self.bot.handle_update(msg(start(NO), uid=4242))
        self.bot.handle_update(msg(f"/start o_{NO}", uid=4242))             # без подписи
        joined = "\n".join(self.logs)
        self.assertNotIn("901234567", joined)
        self.assertNotIn("4242", joined)                                    # id пользователя — только хвост
        self.assertNotIn(orders_db.link_sig(TOKEN, NO), joined)             # подпись ссылки — тоже не в журнал
        self.assertIn("***242", joined)

    def test_cli_link(self):
        orig, argv = tg_bot._http_call, sys.argv
        tg_bot._http_call = lambda *a, **k: (_ for _ in ()).throw(AssertionError("сеть"))
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        saved = os.environ.get("BOT_USERNAME")
        out = io.StringIO()
        try:
            os.environ["BOT_USERNAME"] = "ipak_shop_bot"
            sys.argv = ["tg_bot.py", "--link", NO.lower()]
            with contextlib.redirect_stdout(out):
                self.assertEqual(tg_bot.main(), 0)
            self.assertIn(f"https://t.me/ipak_shop_bot?start=o_{NO}_{orders_db.link_sig(TOKEN, NO)}", out.getvalue())
            sys.argv = ["tg_bot.py", "--link", "nonsense"]
            with contextlib.redirect_stdout(out):
                self.assertEqual(tg_bot.main(), 1)
            os.environ["BOT_USERNAME"] = ""
            sys.argv = ["tg_bot.py", "--link", NO]
            with contextlib.redirect_stdout(out):
                self.assertEqual(tg_bot.main(), 1)
        finally:
            tg_bot._http_call, sys.argv = orig, argv
            if saved is None:
                os.environ.pop("BOT_USERNAME", None)
            else:
                os.environ["BOT_USERNAME"] = saved


class FunnelBotTest(unittest.TestCase):
    setUp, tearDown, make_bot = BotTest.setUp, BotTest.tearDown, BotTest.make_bot
    def test_parse_start(self):
        self.assertEqual(tg_bot.parse_start("p_aaaaaa2-s_Insta-r_abcd2345"),
                         {"product": "AAAAAA2", "src": "insta", "ref": "abcd2345"})
        self.assertEqual(tg_bot.parse_start("s_bad!tag-p_../x"), {"product": "", "src": "", "ref": ""})
        self.assertEqual(tg_bot.shop_link("https://x.uz/?a=1#/old", "AAAAAA2", "Blog"),
                         "https://x.uz/?a=1&from=blog#/catalog?p=AAAAAA2")

    def test_start_src_and_ref(self):
        self.db.touch_tg_user(9001, "friend_one", started=True)
        code = self.db.ensure_ref_code(9001, orders_db.ref_code_for(TOKEN, 9001))
        self.bot.handle_update(msg(f"/start s_tg_channel-r_{code}"))
        u = self.db.tg_user(4242)
        self.assertEqual((u["src_last"], u["referred_by"], u["username"]), ("tg_channel", 9001, "real_name"))
        self.assertTrue(u["started_at"])
        url = self.fake.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
        self.assertEqual(url, "https://shop.example/yurt/?from=tg_channel")
        self.bot.handle_update(msg("/start r_nosuchcd", uid=5151))             # неизвестный код — просто приветствие
        self.assertIsNone(self.db.tg_user(5151)["referred_by"])

    def test_send_customer_leak_guard_and_block(self):
        self.assertIsNone(self.bot.send_customer(4242, "Доставка из Trendyol"))
        self.assertIsNone(self.bot.send_customer(4242, "Без возврата"))
        self.assertEqual(self.fake.of("sendMessage"), [])
        self.assertIn("leak", self.bot.last_error)
        self.fake.fail["sendMessage"] = {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}
        self.db.touch_tg_user(4242, started=True)
        self.assertIsNone(self.bot.send_customer(4242, "Здравствуйте"))
        self.assertTrue(self.db.tg_user(4242)["blocked_at"])
        self.assertFalse(self.db.can_message(4242))


if __name__ == "__main__":
    unittest.main(verbosity=2)
