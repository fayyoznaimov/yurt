"""Telegram-бот магазина: кнопки статусов заказа у продавца, сообщения покупателю, вход в магазин (Mini App).

Это ЕДИНСТВЕННЫЙ потребитель getUpdates для токена TELEGRAM_BOT_TOKEN (Telegram отдаёт обновления только
одному процессу). Бот запускается потоком внутри order_api.py (служба yurt-orders) — отдельной службы и
вебхука нет. Другие модули (например, channel.py) НЕ опрашивают Telegram сами, а регистрируют свои
обработчики здесь:

    import tg_bot
    def on_channel(bot, cq):            # cq — callback_query из Telegram
        ...                             # bot.call("editMessageText", ...), bot.send(chat_id, text)
        return "Готово"                 # текст всплывашки (answerCallbackQuery); None — без текста
    tg_bot.register_callback("ch:", on_channel)                  # только админы (по умолчанию)
    tg_bot.register_command("digest", on_digest, admin_only=True) # on_digest(bot, message, args)
    def tg_register(bot): ...           # необязательно: вызывается с работающим ботом после импорта

Регистрация действует только в процессе, где идёт опрос (служба yurt-orders), поэтому служба при старте
импортирует модули из TG_BOT_PLUGINS (через запятую, по умолчанию «channel»; нет модуля — пропуск).
Отправить сообщение без опроса (из любого процесса): tg_bot.call("sendMessage", chat_id=..., text=...).

    python tg_bot.py --set-menu      # кнопка меню бота «Открыть магазин» (web_app SHOP_URL)
    python tg_bot.py --info          # getMe + getWebhookInfo: настроен ли бот, нет ли вебхука
    python tg_bot.py --link YR-…     # подписанная ссылка «Получать статус в Telegram» для заказа (отправить вручную)
    python tg_bot.py --run           # опрос без order_api (только для отладки; служба делает это сама)

Обработка:
    callback 'st:<номер>:<статус>' (только админы) → статус в базе, сообщение заказа обновляется, кнопки
        следующего шага; покупателю — шаблон из server/order_messages.json, если к ЭТОМУ заказу привязан Telegram.
    /start o_<номер>_<подпись>   покупатель подключает статусы ЭТОГО заказа (подпись = первые 10 знаков
        base32(HMAC-SHA256(токен, номер)) — ссылку выдаёт только сервер: ответ /api/order → bot_url, или
        --link; заказ ≤30 дней и не привязан к другому Telegram). Ссылка без подписи / с неверной — отказ.
        Другие заказы с тем же телефоном не привязываются (телефон никто не проверяет).
    /start, /start p_<КОД>   приветствие + кнопка web_app «Открыть магазин» (SHOP_URL, для p_ — карточка товара).
    /stop              не присылать новинки (статусы активных заказов приходят и дальше).

Настройки (окружение, на сервере /etc/yurt/yurt.env):
    TELEGRAM_BOT_TOKEN       токен бота (им же подписываются ссылки /start o_…)
    TELEGRAM_ORDERS_CHAT_ID  чат заказов (иначе TELEGRAM_CHAT_ID)
    TELEGRAM_ADMIN_IDS       id продавца/помощников через запятую — кто может нажимать кнопки статусов.
                             Пусто: админ — владелец чата заказов (если это личный чат) или любой участник
                             группы заказов (если чат заказов — группа -100…); для кнопок канала ('ch:') и
                             /channel ещё и CHANNEL_REVIEW_CHAT_ID, если это личный id (> 0).
    SHOP_URL                 адрес магазина для кнопки Mini App (https://…)
    BOT_USERNAME             ник бота без @ (для ссылок t.me/<бот>?start=…)
    SELLER_TELEGRAM          ник продавца для {contact} в шаблонах (иначе orders_telegram_username /
                             contacts.telegram из site/content.json)
База заказов: Bot(db=…) принимает OrdersDB или функцию без аргументов, которая её возвращает (order_api
передаёт lambda: app._db() — база, открывшаяся позже, подхватывается без перезапуска).
В журнал не пишутся телефоны и полные id пользователей.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

import orders_db

ROOT = Path(__file__).resolve().parent
OFFSET_PATH = ROOT / "data" / "orders" / "bot_offset.json"
MESSAGES_PATH = ROOT / "server" / "order_messages.json"
CONTENT_PATH = ROOT / "site" / "content.json"
API_URL = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30
ORDER_NO_RE = orders_db.ORDER_NO_RE
PRODUCT_RE = re.compile(r"^[A-Z0-9]{7}$")
ANSWERED = object()          # обработчик callback сам вызвал answerCallbackQuery
CHANNEL_SCOPE_PREFIX = "ch:"     # кнопки канала: админом считается и CHANNEL_REVIEW_CHAT_ID (> 0), если нет списка админов
CHANNEL_SCOPE_COMMANDS = {"channel"}

STATUS_LABELS = orders_db.STATUS_LABELS
# кнопки следующего шага (callback_data 'st:<номер>:<статус>', ≤ 64 байт)
STATUS_BUTTONS = {
    "new": [("confirmed", "✅ Подтвердить"), ("cancelled", "❌ Отменить")],
    "confirmed": [("prepaid", "💳 Предоплата получена"), ("cancelled", "❌ Отменить")],
    "prepaid": [("ordered", "🛒 Заказан у бренда")],
    "ordered": [("shipped", "🚚 В пути")],
    "shipped": [("delivered", "📦 Выдан")],
}
STATUS_LINE_RE = re.compile(r"^Статус: .*$", re.M)
DEFAULT_MESSAGES = {
    "button_shop": "Открыть магазин",
    "start_welcome": "Здравствуйте! Это магазин оригинальной брендовой одежды. Откройте каталог кнопкой ниже.",
    "start_product": "Здравствуйте! Откройте товар кнопкой ниже — там размеры и цена.",
    "linked": "Здравствуйте, {name}! Будем присылать сюда статус заказа {no}.",
    "link_failed": "Не нашли такой заказ. Проверьте номер или напишите нам {contact}.",
    "stop": "Хорошо, больше не будем присылать новинки. Статусы ваших заказов будут приходить, как и раньше.",
    "help": "Здесь приходят статусы заказов. Вопросы — напишите нам {contact}.",
    "seller_copy": "Покупатель не подключил бота — отправьте ему сами:",
    "seller_linked": "🔗 Покупатель подключил бота: заказ {no}",
    "status": {},
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8")


def _log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [bot] {msg}", flush=True)


def uid_tag(uid) -> str:
    s = str(uid or "")
    return ("***" + s[-3:]) if s else "-"


def money(n) -> str:
    try:
        return f"{int(round(float(n))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "?"


def label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def status_line(status: str) -> str:
    """Строка статуса в сообщении заказа у продавца (бот заменяет её при смене статуса)."""
    return f"Статус: <b>{label(status)}</b>"


def set_status_line(text: str, status: str) -> str:
    line = status_line(status)
    if STATUS_LINE_RE.search(text):
        return STATUS_LINE_RE.sub(line, text, count=1)
    head, sep, rest = text.partition("\n")
    return head + "\n" + line + (sep + rest if sep else "")


def status_keyboard(order_no: str, status: str) -> dict | None:
    """inline-клавиатура следующего шага; None — финальный статус (кнопок нет)."""
    buttons = STATUS_BUTTONS.get(status)
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": t, "callback_data": f"st:{order_no}:{s}"} for s, t in buttons]]}


def parse_admin_ids(s: str | None) -> set[int]:
    out = set()
    for part in re.split(r"[,\s;]+", s or ""):
        if re.fullmatch(r"-?\d{1,20}", part.strip()):
            out.add(int(part))
    return out


def orders_chat_from_env() -> str:
    return (os.environ.get("TELEGRAM_ORDERS_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()


def private_uid(s) -> int | None:
    """id личного чата (положительное число) — это и id пользователя; группа/канал/@ник/пусто → None."""
    s = str(s or "").strip()
    if not re.fullmatch(r"\d{1,20}", s):
        return None
    v = int(s)
    return v if v > 0 else None


def channel_admin_warning(env=None) -> str:
    """Предупреждение для channel --status / order_api --check: CHANNEL_ENABLED=1, а TELEGRAM_ADMIN_IDS пуст.
    Пустая строка — всё в порядке."""
    env = os.environ if env is None else env
    if str(env.get("CHANNEL_ENABLED") or "").strip() != "1" or parse_admin_ids(env.get("TELEGRAM_ADMIN_IDS")):
        return ""
    rc = private_uid(env.get("CHANNEL_REVIEW_CHAT_ID"))
    if rc:
        return (f"CHANNEL_ENABLED=1, а TELEGRAM_ADMIN_IDS пуст: кнопки канала «Опубликовать/Пропустить» сможет "
                f"нажимать только CHANNEL_REVIEW_CHAT_ID ({uid_tag(rc)}). Лучше впишите свой Telegram id в "
                f"TELEGRAM_ADMIN_IDS (узнать — @userinfobot) и перезапустите yurt-orders.")
    return ("CHANNEL_ENABLED=1, а TELEGRAM_ADMIN_IDS пуст (и CHANNEL_REVIEW_CHAT_ID — не личный id): кнопки "
            "«Опубликовать/Пропустить» могут отвечать «Нет доступа». Впишите свой Telegram id в TELEGRAM_ADMIN_IDS "
            "(узнать — @userinfobot) и перезапустите yurt-orders.")


def order_bot_url(bot_username: str, token: str, order_no: str) -> str:
    """https://t.me/<бот>?start=o_<номер>_<подпись> — ссылка «Получать статус в Telegram» для ОДНОГО заказа.
    Пусто без верного ника бота или без токена (подписать нечем)."""
    u = (bot_username or "").strip().lstrip("@")
    payload = orders_db.start_payload((token or "").strip(), order_no)
    if not payload or not re.fullmatch(r"[A-Za-z0-9_]{4,32}", u):
        return ""
    return f"https://t.me/{u}?start={payload}"


def shop_link(shop_url: str, product: str = "") -> str:
    base = (shop_url or "").strip().split("#", 1)[0]
    if not base:
        return ""
    return base + (f"#/catalog?p={product}" if product else "")


def shop_button(text: str, url: str) -> dict | None:
    """web_app — только для https (требование Telegram); http — обычная ссылка (отладка)."""
    if url.startswith("https://"):
        return {"text": text, "web_app": {"url": url}}
    if url.startswith("http://"):
        return {"text": text, "url": url}
    return None


def format_items(items: list) -> str:
    """Строки заказа для покупателя — только публичные поля (бренд, название, размер, количество)."""
    out = []
    for it in items or []:
        if not isinstance(it, dict) or not it.get("price_uzs"):
            continue
        name = " — ".join(x for x in (str(it.get("brand") or "").strip(), str(it.get("title") or "").strip()) if x)
        s = f"• {name or it.get('id', '')}"
        if it.get("size"):
            s += f", размер {it['size']}"
        if int(it.get("qty") or 1) > 1:
            s += f" × {int(it['qty'])}"
        out.append(s)
    return "\n".join(out)


# ---------------------------------------------------------------- реестр обработчиков

_CALLBACKS: list[tuple[str, Callable, bool]] = []
_COMMANDS: dict[str, tuple[Callable, bool]] = {}
_REG_LOCK = threading.Lock()
_CURRENT: "Bot | None" = None


def register_callback(prefix: str, handler: Callable, admin_only: bool = True) -> None:
    """Обработчик callback_data, начинающихся с prefix (например 'ch:'). handler(bot, callback_query) →
    str (текст всплывашки) | None | tg_bot.ANSWERED. Повторная регистрация того же префикса заменяет прежнюю."""
    if not prefix or len(prefix.encode()) > 20:
        raise ValueError("prefix: 1–20 байт")
    with _REG_LOCK:
        _CALLBACKS[:] = [x for x in _CALLBACKS if x[0] != prefix]
        _CALLBACKS.append((prefix, handler, admin_only))
        _CALLBACKS.sort(key=lambda x: -len(x[0]))          # длинный префикс важнее


def register_command(name: str, handler: Callable, admin_only: bool = False) -> None:
    """Команда /name. handler(bot, message, args: str) — отвечает сам (bot.send). Встроенные /start и /stop
    не переопределяются."""
    name = name.lstrip("/").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,32}", name) or name in ("start", "stop"):
        raise ValueError(f"команда {name!r} недопустима")
    with _REG_LOCK:
        _COMMANDS[name] = (handler, admin_only)


def unregister(key: str) -> None:
    with _REG_LOCK:
        _CALLBACKS[:] = [x for x in _CALLBACKS if x[0] != key]
        _COMMANDS.pop(key.lstrip("/").lower(), None)


def current() -> "Bot | None":
    """Работающий в этом процессе бот (или None)."""
    return _CURRENT


def call(method: str, token: str | None = None, timeout: float = 20, **params):
    """Разовый вызов Bot API без опроса (для других модулей): result или None."""
    token = (token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return None
    return _http_call(token, method, params, timeout)[0]


def _http_call(token: str, method: str, params: dict, timeout: float) -> tuple[object, dict]:
    """(result | None, ответ Telegram или {'description': ошибка}). Один повтор на 429."""
    for attempt in range(2):
        try:
            r = requests.post(API_URL.format(token=token, method=method), json=params, timeout=timeout)
        except requests.RequestException as ex:
            return None, {"ok": False, "description": ex.__class__.__name__, "network": True}
        try:
            data = r.json()
        except ValueError:
            data = {"ok": False, "error_code": r.status_code, "description": r.text[:200]}
        if data.get("ok"):
            return data.get("result"), data
        if r.status_code == 429 and attempt == 0:
            wait = int((data.get("parameters") or {}).get("retry_after") or 3)
            time.sleep(min(wait, 10))
            continue
        return None, data
    return None, {"ok": False, "description": "retry"}


# ---------------------------------------------------------------- бот

class Bot:
    def __init__(self, token: str, db=None, admin_ids=None, orders_chat: str = "", shop_url: str = "",
                 bot_username: str = "", seller_telegram: str = "", messages_path: Path = MESSAGES_PATH,
                 offset_path: Path = OFFSET_PATH, http: Callable | None = None, log: Callable | None = None,
                 review_chat: str = ""):
        self.token = (token or "").strip()
        self.log = log or _log
        self.db = db                                       # OrdersDB, None или функция → OrdersDB | None
        self.admin_ids = set(admin_ids or ())
        self.orders_chat = str(orders_chat or "").strip()
        self.review_uid = private_uid(review_chat)         # CHANNEL_REVIEW_CHAT_ID, если это личный id
        self.shop_url = (shop_url or "").strip()
        self.bot_username = (bot_username or "").strip().lstrip("@")
        self._seller = (seller_telegram or "").strip().lstrip("@")
        self.messages_path = Path(messages_path)
        self.offset_path = Path(offset_path)
        self.http = http                                   # тесты: http(method, params) → (result, raw)
        self.offset = self._load_offset()
        self._msg_cache: tuple[float, dict] | None = None
        self._fails: dict[int, deque] = defaultdict(deque)  # неудачные /start o_… по пользователю
        self._hinted: dict[int, float] = {}
        self.last_error: dict = {}
        self.stop_event = threading.Event()

    @classmethod
    def from_env(cls, db=None, **kw) -> "Bot":
        return cls(os.environ.get("TELEGRAM_BOT_TOKEN", ""), db=db,
                   admin_ids=parse_admin_ids(os.environ.get("TELEGRAM_ADMIN_IDS")),
                   orders_chat=orders_chat_from_env(), shop_url=os.environ.get("SHOP_URL", ""),
                   bot_username=os.environ.get("BOT_USERNAME", ""),
                   seller_telegram=os.environ.get("SELLER_TELEGRAM", ""),
                   review_chat=os.environ.get("CHANNEL_REVIEW_CHAT_ID", ""), **kw)

    # ------------------------------------------------------------ база заказов

    @property
    def db(self):
        """OrdersDB или None. Если боту дали функцию (lambda: app._db()) — спрашиваем её при каждом обращении:
        база, которая не открылась при старте службы, подхватится, как только откроется."""
        src = self._db_src
        if callable(src) and not isinstance(src, orders_db.OrdersDB):
            try:
                return src()
            except Exception as ex:                        # база недоступна — бот отвечает «База недоступна»
                self.log(f"база заказов недоступна ({ex.__class__.__name__})")
                return None
        return src

    @db.setter
    def db(self, value) -> None:
        self._db_src = value

    def order_link(self, order_no: str) -> str:
        """Подписанная ссылка /start для заказа (пусто без BOT_USERNAME или токена)."""
        return order_bot_url(self.bot_username, self.token, order_no)

    # ------------------------------------------------------------ Bot API

    def call(self, method: str, timeout: float = 20, **params):
        params = {k: v for k, v in params.items() if v is not None}
        if self.http:
            res, raw = self.http(method, params)
        elif not self.token:
            res, raw = None, {"description": "нет токена"}
        else:
            res, raw = _http_call(self.token, method, params, timeout)
        if res is None:
            self.last_error = raw or {}
        return res

    def send(self, chat_id, text: str, reply_markup: dict | None = None, html: bool = False, **extra):
        p = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True, **extra}
        if html:
            p["parse_mode"] = "HTML"
        if reply_markup:
            p["reply_markup"] = reply_markup
        return self.call("sendMessage", **p)

    def answer(self, cq: dict, text: str | None = None, alert: bool = False) -> None:
        self.call("answerCallbackQuery", callback_query_id=cq.get("id"), text=(text or None) and text[:190],
                  show_alert=alert or None)

    # ------------------------------------------------------------ настройки и тексты

    def is_admin(self, user_id, chat_id=None, channel: bool = False) -> bool:
        """TELEGRAM_ADMIN_IDS задан — только они. Пусто: владелец личного чата заказов / участник группы заказов
        (кнопка нажата в ней); для кнопок канала (channel=True) — ещё и CHANNEL_REVIEW_CHAT_ID, если это личный id."""
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return False
        if self.admin_ids:
            return uid in self.admin_ids
        if channel and self.review_uid and uid == self.review_uid:
            return True
        oc = self.orders_chat
        if re.fullmatch(r"-?\d+", oc or ""):
            oc_i = int(oc)
            if oc_i > 0:
                return uid == oc_i                         # личный чат продавца: id чата = id продавца
            try:
                return chat_id is not None and int(chat_id) == oc_i   # закрытая группа заказов
            except (TypeError, ValueError):
                return False
        return False

    def messages(self) -> dict:
        """server/order_messages.json (перечитывается при изменении) поверх встроенных текстов."""
        try:
            mtime = self.messages_path.stat().st_mtime
        except OSError:
            mtime = -1.0
        if self._msg_cache and self._msg_cache[0] == mtime:
            return self._msg_cache[1]
        data = dict(DEFAULT_MESSAGES)
        if mtime >= 0:
            try:
                loaded = json.loads(self.messages_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update({k: v for k, v in loaded.items() if not k.startswith("_")})
            except (OSError, ValueError) as ex:
                self.log(f"order_messages.json не прочитан ({ex.__class__.__name__}) — встроенные тексты")
        self._msg_cache = (mtime, data)
        return data

    def seller_contact(self) -> str:
        u = self._seller
        if not u:
            try:
                c = json.loads(CONTENT_PATH.read_text(encoding="utf-8"))
                u = str(c.get("orders_telegram_username") or (c.get("contacts") or {}).get("telegram") or "")
            except (OSError, ValueError, AttributeError):
                u = ""
        u = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", u.strip()).lstrip("@").strip("/")
        return f"в Telegram @{u}" if re.fullmatch(r"[A-Za-z0-9_]{4,32}", u) else "в Telegram"

    def render(self, template: str, order: dict | None = None, **values) -> str:
        """Подстановка {name} {no} {items} {prepay} {total} {rest} {contact}; прочие {…} остаются как есть."""
        o = order or {}
        total = int(o.get("total_uzs") or 0)
        prepay = int(o.get("prepay_uzs") or 0)
        v = {"name": str(o.get("customer_name") or "").strip(), "no": str(o.get("order_no") or ""),
             "items": format_items(o.get("items") or []), "prepay": money(prepay), "total": money(total),
             "rest": money(max(total - prepay, 0)), "contact": self.seller_contact()}
        v.update({k: str(x) for k, x in values.items()})
        t = str(template or "")
        if not v["name"]:
            t = re.sub(r",?[ \t]*\{name\}", "", t)
        return re.sub(r"\{(name|no|items|prepay|total|rest|contact)\}", lambda m: v[m.group(1)], t).strip()

    # ------------------------------------------------------------ сообщение заказа у продавца

    def update_order_message(self, order: dict, msg: dict | None = None) -> bool:
        """Обновить строку статуса и кнопки на сообщении заказа (то, где нажали кнопку, или сохранённое)."""
        chat_id = ((msg or {}).get("chat") or {}).get("id") or order.get("tg_chat_id")
        message_id = (msg or {}).get("message_id") or order.get("tg_message_id")
        if not (chat_id and message_id):
            return False
        kb = status_keyboard(order["order_no"], order["status"]) or {"inline_keyboard": []}
        same = (str(chat_id) == str(order.get("tg_chat_id") or "") and
                str(message_id) == str(order.get("tg_message_id") or ""))
        text = set_status_line(order["tg_text"], order["status"]) if order.get("tg_text") and same else ""
        if text and len(text) <= 4096:
            if self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=kb) is not None:
                if self.db is not None:
                    self.db.set_tg_message(order["order_no"], chat_id, message_id, text)
                return True
        return self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=kb) is not None

    def notify_customer(self, order: dict, status: str, seller_chat=None) -> str:
        """Шаблон статуса покупателю. 'sent' | 'no_template' | 'not_linked' | 'failed'.
        Не получилось (бот не подключён/заблокирован) — текст уходит продавцу, чтобы отправить вручную."""
        tmpl = (self.messages().get("status") or {}).get(status)
        if not tmpl:
            return "no_template"
        text = self.render(tmpl, order)
        uid = order.get("telegram_user_id")                # Telegram, привязанный к ЭТОМУ заказу (не к телефону)
        result = "not_linked"
        if uid:
            ok = self.send(uid, text) is not None
            result = "sent" if ok else "failed"
            if self.db is not None:
                self.db.add_event(order["order_no"], f"customer_msg {status}: {result}", by="bot")
            if ok:
                return result
            self.log(f"заказ {order['order_no']}: покупателю ({uid_tag(uid)}) не доставлено: "
                     f"{str(self.last_error.get('description') or '')[:120]}")
        chat = seller_chat or self.orders_chat
        if chat:
            kb = None
            tg = str(order.get("customer_telegram") or "")
            if re.fullmatch(r"[A-Za-z0-9_]{4,32}", tg):
                kb = {"inline_keyboard": [[{"text": f"Написать @{tg}", "url": f"https://t.me/{tg}"}]]}
            head = self.messages().get("seller_copy") or DEFAULT_MESSAGES["seller_copy"]
            self.send(chat, f"{head} (заказ {order['order_no']})\n\n{text}", reply_markup=kb)
        return result

    # ------------------------------------------------------------ обработка обновлений

    def handle_update(self, upd: dict) -> None:
        if "callback_query" in upd:
            self.handle_callback(upd["callback_query"])
        elif "message" in upd:
            self.handle_message(upd["message"])

    def handle_callback(self, cq: dict) -> None:
        data = str(cq.get("data") or "")
        uid = (cq.get("from") or {}).get("id")
        chat_id = (((cq.get("message") or {}).get("chat")) or {}).get("id")
        with _REG_LOCK:
            handlers = list(_CALLBACKS)
        for prefix, handler, admin_only in handlers:
            if not data.startswith(prefix):
                continue
            if admin_only and not self.is_admin(uid, chat_id, channel=prefix.startswith(CHANNEL_SCOPE_PREFIX)):
                self.log(f"callback {prefix}: нет доступа у {uid_tag(uid)}")
                self.answer(cq, "Нет доступа", alert=True)
                return
            try:
                res = handler(self, cq)
            except Exception as ex:                        # чужой обработчик не роняет опрос
                self.log(f"callback {prefix}: ошибка {ex.__class__.__name__}: {str(ex)[:160]}")
                res = "Ошибка, попробуйте ещё раз"
            if res is not ANSWERED:
                self.answer(cq, res if isinstance(res, str) else None)
            return
        self.answer(cq)

    def handle_message(self, msg: dict) -> None:
        text = str(msg.get("text") or "")
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        if not text.startswith("/"):
            if chat.get("type") == "private" and not user.get("is_bot"):
                self._hint(chat.get("id"), user.get("id"))
            return
        cmd, _, args = text.partition(" ")
        cmd, _, addressed = cmd[1:].partition("@")
        cmd = cmd.lower()
        if addressed and self.bot_username and addressed.lower() != self.bot_username.lower():
            return                                         # команда другому боту в группе
        args = args.strip()
        if cmd == "start":
            if chat.get("type") == "private":
                self.cmd_start(msg, args)
            return
        if cmd == "stop":
            if chat.get("type") == "private":
                self.cmd_stop(msg)
            return
        with _REG_LOCK:
            entry = _COMMANDS.get(cmd)
        if not entry:
            if chat.get("type") == "private" and not self.is_admin(user.get("id"), chat.get("id")):
                self._hint(chat.get("id"), user.get("id"))
            return
        handler, admin_only = entry
        if admin_only and not self.is_admin(user.get("id"), chat.get("id"), channel=cmd in CHANNEL_SCOPE_COMMANDS):
            return
        try:
            handler(self, msg, args)
        except Exception as ex:
            self.log(f"/{cmd}: ошибка {ex.__class__.__name__}: {str(ex)[:160]}")

    def _hint(self, chat_id, uid) -> None:
        """Ответ на произвольный текст в личке — не чаще раза в 10 минут на человека."""
        if not chat_id or uid is None:
            return
        now = time.time()
        if now - self._hinted.get(uid, 0) < 600:
            return
        self._hinted[uid] = now
        if len(self._hinted) > 5000:
            self._hinted = {k: t for k, t in self._hinted.items() if now - t < 600}
        self.send(chat_id, self.render(self.messages().get("help") or DEFAULT_MESSAGES["help"]),
                  reply_markup=self._shop_markup())

    def _shop_markup(self, product: str = "") -> dict | None:
        btn = shop_button(self.messages().get("button_shop") or "Открыть магазин", shop_link(self.shop_url, product))
        return {"inline_keyboard": [[btn]]} if btn else None

    def _too_many_fails(self, uid) -> bool:
        q = self._fails[uid]
        now = time.time()
        while q and q[0] < now - 3600:
            q.popleft()
        return len(q) >= 5

    def cmd_start(self, msg: dict, payload: str) -> None:
        chat_id = (msg.get("chat") or {}).get("id")
        user = msg.get("from") or {}
        uid = user.get("id")
        m = self.messages()
        if payload.startswith("o_"):
            if self._too_many_fails(uid):
                self.send(chat_id, self.render(m.get("link_failed") or DEFAULT_MESSAGES["link_failed"]))
                return
            parsed = orders_db.parse_start_payload(payload)      # (номер, подпись); без подписи — None
            no = parsed[0] if parsed else re.sub(r"[^A-Za-z0-9_-]", "?", payload[2:22])
            db = self.db
            res = None
            why = "без подписи" if not parsed else ("нет базы" if db is None else "нет токена")
            if parsed and db is not None and self.token:
                res = db.link_telegram(parsed[0], user, sig=parsed[1], secret=self.token)
            if res is not None and res.ok:
                self.log(f"/start: заказ {no} привязан к {uid_tag(uid)} ({res.code})")
                self.send(chat_id, self.render(m.get("linked") or DEFAULT_MESSAGES["linked"], res.order),
                          reply_markup=self._shop_markup())
                if res.code == "linked" and self.orders_chat:
                    self.send(self.orders_chat, self.render(m.get("seller_linked") or DEFAULT_MESSAGES["seller_linked"],
                                                            res.order))
                return
            self._fails[uid].append(time.time())
            self.log(f"/start: заказ {no[:20]} не привязан ({res.code if res else why}) для {uid_tag(uid)}")
            self.send(chat_id, self.render(m.get("link_failed") or DEFAULT_MESSAGES["link_failed"]))
            return
        product = ""
        if payload.startswith("p_"):
            code = payload[2:].strip().upper()
            product = code if PRODUCT_RE.match(code) else ""
        text = m.get("start_product") if product else m.get("start_welcome")
        self.send(chat_id, self.render(text or DEFAULT_MESSAGES["start_welcome"]),
                  reply_markup=self._shop_markup(product))

    def cmd_stop(self, msg: dict) -> None:
        uid = (msg.get("from") or {}).get("id")
        db = self.db
        if db is not None and uid:
            db.set_marketing(uid, False)
        self.send((msg.get("chat") or {}).get("id"), self.render(self.messages().get("stop") or DEFAULT_MESSAGES["stop"]))

    # ------------------------------------------------------------ опрос

    def _load_offset(self) -> int:
        try:
            return int(json.loads(self.offset_path.read_text(encoding="utf-8")).get("offset") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            return 0

    def _save_offset(self) -> None:
        try:
            self.offset_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.offset_path.with_name(self.offset_path.name + ".tmp")
            tmp.write_text(json.dumps({"offset": self.offset, "updated_at": datetime.now().isoformat(timespec="seconds")}),
                           encoding="utf-8")
            os.replace(tmp, self.offset_path)
        except OSError as ex:
            self.log(f"offset не сохранён: {ex.__class__.__name__}")

    def poll_once(self, timeout: int = POLL_TIMEOUT) -> int | None:
        """Один getUpdates. Число обработанных обновлений или None (ошибка)."""
        res = self._get_updates(timeout)
        if res is None:
            return None
        n = 0
        for upd in res:
            try:
                uid = int(upd.get("update_id"))
            except (TypeError, ValueError):
                continue
            if uid < self.offset:
                continue
            self.offset = uid + 1
            self._save_offset()                            # до обработки: сбой на одном обновлении не зациклит бота
            try:
                self.handle_update(upd)
            except Exception as ex:
                self.log(f"обновление {uid}: ошибка {ex.__class__.__name__}: {str(ex)[:200]}")
            n += 1
        return n

    def _get_updates(self, timeout: int):
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if self.offset:
            params["offset"] = self.offset
        if self.http:
            res, raw = self.http("getUpdates", params)
        else:
            res, raw = _http_call(self.token, "getUpdates", params, timeout + 15)
        if res is None:
            self.last_error = raw or {}
        return res

    def run_forever(self, stop: threading.Event | None = None) -> None:
        global _CURRENT
        stop = stop or self.stop_event
        _CURRENT = self
        self.log(f"опрос Telegram запущен (админов: {len(self.admin_ids)}, чат заказов: {'да' if self.orders_chat else 'НЕТ'}, "
                 f"магазин: {'да' if self.shop_url else 'нет'})")
        backoff = 1.0
        while not stop.is_set():
            n = self.poll_once()
            if n is not None:
                backoff = 1.0
                continue
            err = self.last_error or {}
            code = err.get("error_code")
            if code == 409:
                self.log("getUpdates: конфликт — опрашивает другой процесс или стоит вебхук "
                         "(python tg_bot.py --info); жду 30 с")
                wait = 30.0
            elif code == 401:
                self.log("getUpdates: неверный TELEGRAM_BOT_TOKEN; жду 5 минут")
                wait = 300.0
            else:
                self.log(f"getUpdates: {str(err.get('description') or 'ошибка')[:120]}; повтор через {backoff:.0f} с")
                wait, backoff = backoff, min(backoff * 2, 60.0)
            stop.wait(wait)
        self.log("опрос Telegram остановлен")
        if _CURRENT is self:
            _CURRENT = None

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="tg-bot", daemon=True)
        t.start()
        return t


# ---------------------------------------------------------------- встроенный обработчик статусов

def _status_callback(bot: Bot, cq: dict):
    data = str(cq.get("data") or "")
    try:
        _, rest = data.split(":", 1)
        order_no, status = rest.rsplit(":", 1)
    except ValueError:
        return "Неверная кнопка"
    db = bot.db
    if db is None:
        return "База заказов недоступна"
    uid = (cq.get("from") or {}).get("id")
    res = db.set_status(order_no, status, by=f"tg:{uid}")
    msg = cq.get("message") or {}
    if not res.ok:
        if res.error == "not_found":
            return "Заказ не найден"
        if res.error == "not_allowed" and res.order:
            bot.update_order_message(res.order, msg)       # кнопки устарели — показать актуальные
            return f"Статус уже: {label(res.prev)}"
        return "Неизвестный статус"
    order = res.order or {}
    bot.update_order_message(order, msg)
    if res.changed:
        bot.log(f"заказ {order_no}: {res.prev} → {status} ({uid_tag(uid)})")
        bot.notify_customer(order, status, seller_chat=(msg.get("chat") or {}).get("id"))
    return f"Статус: {label(status)}"


register_callback("st:", _status_callback, admin_only=True)


def load_plugins(bot: Bot | None = None, names: str | None = None, log: Callable | None = None) -> list[str]:
    """Импортировать модули-плагины бота: TG_BOT_PLUGINS через запятую (по умолчанию 'channel').
    При импорте модуль сам вызывает register_callback/register_command; если в нём есть tg_register(bot) —
    он вызывается с работающим ботом. Нет такого модуля — молча пропускается. Импорт плагина не должен
    ничего делать, кроме регистрации (никакой сети и argparse на уровне модуля)."""
    log = log or _log
    raw = os.environ.get("TG_BOT_PLUGINS", "channel") if names is None else names
    loaded = []
    for name in [n.strip() for n in raw.split(",") if n.strip()]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,60}", name):
            continue
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as ex:
            if ex.name != name:
                log(f"плагин {name}: не загружен ({ex.__class__.__name__}: {ex.name})")
            continue
        except Exception as ex:                            # чужой модуль не мешает боту заказов
            log(f"плагин {name}: ошибка импорта {ex.__class__.__name__}: {str(ex)[:160]}")
            continue
        reg = getattr(mod, "tg_register", None)
        if callable(reg) and bot is not None:
            try:
                reg(bot)
            except Exception as ex:
                log(f"плагин {name}: tg_register упал ({ex.__class__.__name__}: {str(ex)[:160]})")
                continue
        loaded.append(name)
    return loaded


def start_in_thread(db=None, log: Callable | None = None) -> Bot | None:
    """Для order_api.main(): запустить опрос, если задан TELEGRAM_BOT_TOKEN (и подключить плагины,
    см. load_plugins). db — OrdersDB или функция, которая её возвращает (lambda: app._db()).
    Возвращает бота или None."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        return None
    bot = Bot.from_env(db=db, log=log)
    plugins = load_plugins(bot, log=bot.log)
    if plugins:
        bot.log(f"плагины бота: {', '.join(plugins)}")
    warn = channel_admin_warning()
    if warn:
        bot.log("ВНИМАНИЕ: " + warn)
    bot.start_thread()
    return bot


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram-бот магазина")
    ap.add_argument("--set-menu", action="store_true", help="кнопка меню «Открыть магазин» (web_app SHOP_URL)")
    ap.add_argument("--info", action="store_true", help="getMe и getWebhookInfo")
    ap.add_argument("--link", metavar="YR-…", help="подписанная ссылка «Получать статус в Telegram» для заказа")
    ap.add_argument("--run", action="store_true", help="опрос без order_api (отладка; не запускать рядом со службой)")
    args = ap.parse_args()
    bot = Bot.from_env()
    if not bot.token:
        print("TELEGRAM_BOT_TOKEN не задан")
        return 1
    if args.link:
        no = args.link.strip().upper()
        if not ORDER_NO_RE.match(no):
            print("Номер заказа вида YR-261005-ABCD")
            return 1
        url = bot.order_link(no)
        print(url or "Задайте BOT_USERNAME (ник бота без @) — без него ссылку не построить.")
        return 0 if url else 1
    if args.set_menu:
        url = shop_link(bot.shop_url)
        if not url.startswith("https://"):
            print("SHOP_URL должен начинаться с https:// (требование Telegram для Mini App)")
            return 1
        text = bot.messages().get("button_shop") or "Открыть магазин"
        ok = bot.call("setChatMenuButton", menu_button={"type": "web_app", "text": text, "web_app": {"url": url}})
        print("Кнопка меню установлена." if ok else f"Не получилось: {bot.last_error.get('description')}")
        return 0 if ok else 1
    if args.info:
        me = bot.call("getMe")
        wh = bot.call("getWebhookInfo")
        print(json.dumps({"me": me, "webhook": wh}, ensure_ascii=False, indent=1))
        if wh and wh.get("url"):
            print("ВНИМАНИЕ: у бота стоит вебхук — getUpdates не работает. Снять: deleteWebhook.")
        return 0 if me else 1
    if args.run:
        try:
            db = orders_db.OrdersDB(orders_db.default_path())
        except Exception as ex:                            # без базы бот всё равно отвечает на /start
            print(f"база недоступна: {ex}")
            db = None
        bot.db = db
        load_plugins(bot)
        try:
            bot.run_forever()
        except KeyboardInterrupt:
            pass
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
