"""База заказов и покупателей (SQLite, только на сервере продавца; в git и на сайт не попадает никогда).

    python orders_db.py migrate                       # создать/обновить таблицы
    python orders_db.py stats                         # сколько заказов и покупателей, по статусам
    python orders_db.py export --out data/orders/export/orders.csv [--private] [--customers]
    python orders_db.py backup [--out data/orders/backup/orders-YYYYMMDD.sqlite.gz]
    python orders_db.py import-jsonl [data/orders/orders.jsonl]   # одноразово: старый журнал → база

Файл базы: data/orders/orders.sqlite (ORDERS_DB_PATH). Режим WAL; каждый поток работает со своим
соединением, записи идут транзакциями BEGIN IMMEDIATE (несколько потоков order_api.py и бота не мешают).
data/orders/orders.jsonl остаётся сырым журналом (его пишет order_api.py) — вторая копия каждого заказа.

Таблицы:
    customers(id, phone UNIQUE E.164, telegram, name, city, first_seen, last_order_at, orders_count,
              marketing_opt_in, opt_in_at, note, deleted_at)   карточка покупателя для продавца (группировка
              заказов по телефону, «постоянный покупатель»). Имя/город/ник заполняются ПЕРВЫМ заказом и
              следующими заказами не перезаписываются (только пустые поля). Столбец telegram_user_id остался
              от схемы 1 и больше не используется (миграция 2 его очищает).
    customer_cids(cid, customer_id)                  анонимный id браузера → покупатель
    orders(id, order_no UNIQUE, client_order_id UNIQUE NULL, customer_id, created_at, status, items_json,
           total_uzs, prepay_uzs, source, tg_message_id, tg_chat_id,
           order_name, order_phone, order_city, order_telegram     контакты, введённые В ЭТОМ заказе,
           telegram_user_id, telegram_username, telegram_linked_at  Telegram, привязанный к ЭТОМУ заказу,
           marketing_opt_in, …)                                    галочка «новинки» этого заказа
    order_events(order_id, at, status, by, note)     история статусов и сообщений
  Схема 3 (воронка, funnel.py):
    orders + src_first, src_last (метки источника с сайта/бота), ref_code, ref_user_id (кто порекомендовал),
             followup_at, followup_result (вопрос «Всё подошло?» через 7 дней после выдачи)
    tg_users(id, user_id UNIQUE, username, first_seen, last_seen, started_at, src_first, src_last, ref_code UNIQUE,
             referred_by, referred_at, blocked_at, review_order, review_until)   кто писал боту (ему можно писать),
             метки /start s_…, своя реферальная ссылка r_<код>, кто привёл (/start r_…), ждём ли фото-отзыв
    prefs(telegram_user_id PK, gender, types_json, sizes_json, brands_json, budget, draft_json, created_at,
          updated_at, opt_in_at, stopped_at, last_digest_at)   анкета «Подобрать вещи моего размера»
    digest_sent(telegram_user_id, product_id, sent_at)    что уже присылали (никогда не повторяем)
    price_seen(product_id PK, price_uzs, seen_at, drop_from, dropped_at)   последняя цена товара (снижение ≥10%)
    carts(telegram_user_id PK, items_json, created_at, updated_at, reminded_at)   корзина Mini App (/api/cart)
  Время: orders/tg_users/carts — местное (как created_at заказов); prefs/digest_sent/price_seen — UTC «…Z»
  (сравниваются с first_seen каталога).
Telegram привязывается к ЗАКАЗУ, а не к покупателю: телефон никто не проверяет, поэтому заказ с чужим
номером не даёт доступа к чужим заказам. Привязка — только (а) подписанной ссылкой /start o_<номер>_<подпись>
(подпись = HMAC-SHA256(токен бота, номер), её знает только сервер — см. start_payload) или (б) проверенным
initData Mini App при оформлении. Сообщения о статусе уходят только order.telegram_user_id.
Статусы: new → confirmed → prepaid → ordered → shipped → delivered; из new/confirmed/prepaid — cancelled.
items_json хранит строки заказа целиком, вместе с закупочными данными (магазин, ссылка, себестоимость) —
база закрытая, как и чат заказов. Экспорт без --private эти поля не выгружает.
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = ROOT / "data" / "orders" / "orders.sqlite"
SCHEMA_VERSION = 3
ORDER_NO_RE = re.compile(r"^YR-\d{6}-[A-Z2-9]{4}$")
TAG_RE = re.compile(r"^[a-z0-9_]{1,32}$")                 # метка источника / реферальный код (contract: src, ref)
REF_CODE_LEN = 8
LINK_SIG_LEN = 10                                          # base32: 50 бит
START_RE = re.compile(r"^o_(YR-\d{6}-[A-Z2-9]{4})_([A-Z2-7]{%d})$" % LINK_SIG_LEN, re.I)

STATUSES = ("new", "confirmed", "prepaid", "ordered", "shipped", "delivered", "cancelled")
STATUS_LABELS = {
    "new": "новый", "confirmed": "подтверждён", "prepaid": "предоплата получена",
    "ordered": "заказан у бренда", "shipped": "в пути", "delivered": "выдан", "cancelled": "отменён",
}
# куда можно перейти из статуса (кнопки бота показывают ровно эти переходы)
TRANSITIONS = {
    "new": ("confirmed", "cancelled"),
    "confirmed": ("prepaid", "cancelled"),
    "prepaid": ("ordered", "cancelled"),
    "ordered": ("shipped",),
    "shipped": ("delivered",),
    "delivered": (),
    "cancelled": (),
}
# поля строки заказа, которые нельзя показывать никому, кроме продавца
PRIVATE_ITEM_FIELDS = ("source", "shop", "url", "title_original", "price_now", "price_old", "currency",
                       "cost_uzs", "margin_uzs", "source_item_id")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def prepay_for(total_uzs: int | float | None) -> int:
    """Предоплата 50%, округлённая вверх до 1000 сум (так же считает сайт — см. contract.md, T1)."""
    try:
        t = float(total_uzs or 0)
    except (TypeError, ValueError):
        return 0
    return int(math.ceil(t / 2 / 1000) * 1000) if t > 0 else 0


def mask_phone(p: str | None) -> str:
    d = re.sub(r"\D", "", p or "")
    return ("***" + d[-4:]) if d else "-"


def utc_iso(dt: datetime | None = None) -> str:
    """UTC «2026-10-05T07:00:00Z» (как first_seen каталога)."""
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_tag(v) -> str:
    """Метка источника / реферальный код: нижний регистр, ^[a-z0-9_]{1,32}$; иначе ''."""
    s = str(v or "").strip().lower() if isinstance(v, (str, int)) and not isinstance(v, bool) else ""
    return s if TAG_RE.match(s) else ""


def ref_code_for(secret: str, user_id) -> str:
    """Реферальный код пользователя: первые 8 знаков base32(HMAC-SHA256(токен бота, 'ref:<id>')), нижний регистр.
    Пусто без секрета. Код сохраняется в tg_users.ref_code — поиск по нему идёт в базе."""
    uid = _user_id(user_id)
    if not secret or uid is None:
        return ""
    d = hmac.new(str(secret).encode("utf-8"), f"ref:{uid}".encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(d).decode("ascii")[:REF_CODE_LEN].lower()


# ---------------------------------------------------------------- подписанная ссылка на бота

def link_sig(secret: str, order_no: str) -> str:
    """Подпись номера заказа для /start: первые 10 знаков base32(HMAC-SHA256(токен бота, номер)).
    Пустой секрет — пустая подпись (такую ссылку бот не примет)."""
    if not secret:
        return ""
    d = hmac.new(str(secret).encode("utf-8"), str(order_no).strip().upper().encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(d).decode("ascii")[:LINK_SIG_LEN]


def check_link_sig(secret: str, order_no: str, sig) -> bool:
    """Подпись верна (сравнение за постоянное время). Без секрета — всегда False."""
    want = link_sig(secret, order_no)
    got = str(sig or "").strip().upper()
    if not want or not re.fullmatch(r"[A-Z2-7]{%d}" % LINK_SIG_LEN, got):
        return False
    return hmac.compare_digest(want, got)


def start_payload(secret: str, order_no: str) -> str:
    """Параметр t.me/<бот>?start=… для заказа: o_<номер>_<подпись> (≤ 64 знаков, только A-Z a-z 0-9 _ -).
    Пусто, если нет секрета (токена бота)."""
    sig = link_sig(secret, order_no)
    return f"o_{str(order_no).strip().upper()}_{sig}" if sig else ""


def parse_start_payload(payload) -> tuple[str, str] | None:
    """'o_YR-261005-ABCD_XXXXXXXXXX' → ('YR-261005-ABCD', 'XXXXXXXXXX'); без подписи / мусор → None."""
    m = START_RE.match(str(payload or "").strip())
    return (m.group(1).upper(), m.group(2).upper()) if m else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS customers(
    id INTEGER PRIMARY KEY,
    phone TEXT UNIQUE,
    telegram TEXT,
    telegram_user_id INTEGER,
    name TEXT,
    city TEXT,
    first_seen TEXT,
    last_order_at TEXT,
    orders_count INTEGER NOT NULL DEFAULT 0,
    marketing_opt_in INTEGER NOT NULL DEFAULT 0,
    opt_in_at TEXT,
    note TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_customers_tg ON customers(telegram);
CREATE INDEX IF NOT EXISTS ix_customers_tguid ON customers(telegram_user_id);
CREATE TABLE IF NOT EXISTS customer_cids(
    cid TEXT NOT NULL,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY(cid, customer_id)
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY,
    order_no TEXT UNIQUE NOT NULL,
    client_order_id TEXT UNIQUE,
    customer_id INTEGER REFERENCES customers(id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    status_at TEXT,
    items_json TEXT NOT NULL DEFAULT '[]',
    total_uzs INTEGER,
    prepay_uzs INTEGER,
    source TEXT NOT NULL DEFAULT 'site',
    tg_message_id INTEGER,
    tg_chat_id TEXT,
    tg_text TEXT,
    cid TEXT,
    lang TEXT,
    comment TEXT,
    page_url TEXT,
    order_name TEXT,
    order_phone TEXT,
    order_city TEXT,
    order_telegram TEXT,
    telegram_user_id INTEGER,
    telegram_username TEXT,
    telegram_linked_at TEXT,
    marketing_opt_in INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_orders_customer ON orders(customer_id, created_at);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status, created_at);
CREATE TABLE IF NOT EXISTS order_events(
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    at TEXT NOT NULL,
    status TEXT,
    "by" TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_order ON order_events(order_id, at);
CREATE TABLE IF NOT EXISTS tg_users(
    id INTEGER PRIMARY KEY,
    user_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    first_seen TEXT,
    last_seen TEXT,
    started_at TEXT,
    src_first TEXT,
    src_last TEXT,
    ref_code TEXT UNIQUE,
    referred_by INTEGER,
    referred_at TEXT,
    blocked_at TEXT,
    review_order TEXT,
    review_until TEXT
);
CREATE INDEX IF NOT EXISTS ix_tg_users_ref ON tg_users(referred_by);
CREATE TABLE IF NOT EXISTS prefs(
    telegram_user_id INTEGER PRIMARY KEY,
    gender TEXT,
    types_json TEXT,
    sizes_json TEXT,
    brands_json TEXT,
    budget TEXT,
    draft_json TEXT,
    created_at TEXT,
    updated_at TEXT,
    opt_in_at TEXT,
    stopped_at TEXT,
    last_digest_at TEXT
);
CREATE TABLE IF NOT EXISTS digest_sent(
    telegram_user_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    sent_at TEXT,
    PRIMARY KEY(telegram_user_id, product_id)
);
CREATE TABLE IF NOT EXISTS price_seen(
    product_id TEXT PRIMARY KEY,
    price_uzs INTEGER,
    seen_at TEXT,
    drop_from INTEGER,
    dropped_at TEXT
);
CREATE TABLE IF NOT EXISTS carts(
    telegram_user_id INTEGER PRIMARY KEY,
    items_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT,
    updated_at TEXT,
    reminded_at TEXT
);
"""
# столбцы orders, которых не было в первых версиях базы (ALTER TABLE при migrate)
ORDER_COLUMNS = (("tg_text", "TEXT"), ("cid", "TEXT"), ("lang", "TEXT"), ("comment", "TEXT"), ("page_url", "TEXT"),
                 ("status_at", "TEXT"), ("order_name", "TEXT"), ("order_phone", "TEXT"), ("order_city", "TEXT"),
                 ("order_telegram", "TEXT"), ("telegram_user_id", "INTEGER"), ("telegram_username", "TEXT"),
                 ("telegram_linked_at", "TEXT"), ("marketing_opt_in", "INTEGER NOT NULL DEFAULT 0"),
                 ("src_first", "TEXT"), ("src_last", "TEXT"), ("ref_code", "TEXT"), ("ref_user_id", "INTEGER"),
                 ("followup_at", "TEXT"), ("followup_result", "TEXT"))
# Контакты заказа для показа и шаблонов: у заказов схемы 2 (order_name не NULL) — ТОЛЬКО свои, введённые в этом
# заказе; у старых заказов (до схемы 2) — из карточки покупателя. Telegram id — только привязанный к заказу.
CONTACT_SQL = (
    "CASE WHEN o.order_name IS NULL THEN c.name ELSE o.order_name END AS customer_name, "
    "CASE WHEN o.order_name IS NULL THEN c.phone ELSE o.order_phone END AS customer_phone, "
    "CASE WHEN o.order_name IS NULL THEN c.city ELSE o.order_city END AS customer_city, "
    "COALESCE(o.telegram_username, CASE WHEN o.order_name IS NULL THEN c.telegram ELSE o.order_telegram END) "
    "AS customer_telegram, "
    "o.telegram_user_id AS customer_telegram_user_id, "
    "c.orders_count AS customer_orders_count, c.marketing_opt_in AS customer_marketing_opt_in")


class DuplicateOrderNo(Exception):
    """Сгенерированные номера заказа заняты (после всех попыток)."""


@dataclass
class StatusChange:
    ok: bool                    # статус стал тем, что просили (или уже был им — changed=False)
    changed: bool = False
    prev: str = ""
    status: str = ""
    order: dict | None = None
    error: str = ""             # not_found | bad_status | not_allowed


@dataclass
class LinkResult:
    code: str                   # linked | already | taken | not_found | expired | bad_sig
    order: dict | None = None   # только при ok (linked/already)
    customer_id: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code in ("linked", "already")


def _clean_username(u) -> str:
    s = str(u or "").strip().lstrip("@").lower()
    return s if re.fullmatch(r"[a-z0-9_]{4,32}", s) else ""


def _user_id(u) -> int | None:
    try:
        v = int(u)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


class OrdersDB:
    """Потокобезопасный доступ к базе: своё соединение у каждого потока, запись — BEGIN IMMEDIATE."""

    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self.migrate()

    # ------------------------------------------------------------ соединения

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is not None:
            return c
        new = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        c.execute("PRAGMA synchronous=NORMAL")
        if new:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._local.conn = c
        with self._conns_lock:
            self._conns.append(c)
        return c

    def close(self) -> None:
        """Закрыть все соединения (всех потоков). После этого объект снова откроет соединение по запросу."""
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    class _Tx:
        def __init__(self, db: "OrdersDB"):
            self.db = db

        def __enter__(self) -> sqlite3.Connection:
            self.db._write_lock.acquire()
            try:
                self.c = self.db._conn()
                self.c.execute("BEGIN IMMEDIATE")
            except BaseException:
                self.db._write_lock.release()
                raise
            return self.c

        def __exit__(self, et, ev, tb):
            try:
                self.c.execute("COMMIT" if et is None else "ROLLBACK")
            finally:
                self.db._write_lock.release()
            return False

    def tx(self) -> "OrdersDB._Tx":
        return OrdersDB._Tx(self)

    def query(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, args).fetchall()

    # ------------------------------------------------------------ схема

    def migrate(self) -> None:
        with self._write_lock:
            c = self._conn()
            prev = None
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                r = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                prev = int(r[0]) if r and str(r[0]).isdigit() else None
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(orders)")}
            for col, decl in ORDER_COLUMNS:
                if col not in cols:
                    c.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")
            c.execute("CREATE INDEX IF NOT EXISTS ix_orders_tguid ON orders(telegram_user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_orders_ref ON orders(ref_user_id)")
            if prev is not None and prev < 2:
                self._migrate_v2(c)
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))

    @staticmethod
    def _migrate_v2(c: sqlite3.Connection) -> None:
        """Схема 1 привязывала Telegram к ПОКУПАТЕЛЮ (по непроверенному телефону). Переносим привязку только на
        те заказы, где её сделали (/start o_… — события 'telegram linked' с by='tg:<id>'), и стираем
        customers.telegram_user_id: заказы с тем же телефоном больше не наследуют чужой Telegram."""
        c.execute("BEGIN IMMEDIATE")
        try:
            rows = c.execute('SELECT order_id, at, "by" FROM order_events WHERE note IN '
                             "('telegram linked', 'telegram link repeated') ORDER BY id").fetchall()
            for r in rows:
                uid = _user_id(str(r["by"] or "").removeprefix("tg:"))
                if uid:
                    c.execute("UPDATE orders SET telegram_user_id=?, telegram_linked_at=? "
                              "WHERE id=? AND telegram_user_id IS NULL", (uid, r["at"], r["order_id"]))
            c.execute("UPDATE customers SET telegram_user_id=NULL WHERE telegram_user_id IS NOT NULL")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ покупатели

    def find_or_create_customer(self, phone: str = "", telegram: str = "", name: str = "", city: str = "",
                                cid: str = "", marketing_opt_in: bool = False, at: str | None = None,
                                _c: sqlite3.Connection | None = None) -> int:
        """id покупателя. Ищет по телефону (E.164), без телефона — по нику Telegram. У найденного дописывает
        только ПУСТЫЕ поля (имя, город, ник, телефон): телефон никто не проверяет, поэтому чужой заказ с тем же
        номером не должен переписывать карточку (имя, введённое в заказе, хранится в самом заказе — order_name).
        Согласие на новинки в карточке только включается (рассылка — по заказам с привязанным Telegram, см.
        marketing_customers). cid только запоминается (по нему не объединяем — один браузер у разных людей)."""
        if _c is None:
            with self.tx() as c:
                return self.find_or_create_customer(phone, telegram, name, city, cid, marketing_opt_in, at, _c=c)
        c = _c
        at = at or now_iso()
        phone = (phone or "").strip() or None
        tg = _clean_username(telegram)
        row = None
        if phone:
            row = c.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
            if row is None and tg:          # тот же ник без телефона (заказывал через Telegram) — дописываем телефон
                row = c.execute("SELECT * FROM customers WHERE telegram=? AND phone IS NULL AND deleted_at IS NULL "
                                "ORDER BY id LIMIT 1", (tg,)).fetchone()
        elif tg:
            row = c.execute("SELECT * FROM customers WHERE telegram=? AND deleted_at IS NULL ORDER BY "
                            "(phone IS NULL) DESC, id LIMIT 1", (tg,)).fetchone()
        if row is None:
            cur = c.execute(
                "INSERT INTO customers(phone, telegram, name, city, first_seen, marketing_opt_in, opt_in_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (phone, tg or None, name or None, city or None, at, 1 if marketing_opt_in else 0,
                 at if marketing_opt_in else None))
            cust_id = int(cur.lastrowid)
        else:
            cust_id = int(row["id"])
            c.execute(
                "UPDATE customers SET phone=COALESCE(phone, ?), telegram=COALESCE(NULLIF(telegram, ''), ?), "
                "name=COALESCE(NULLIF(name, ''), ?), city=COALESCE(NULLIF(city, ''), ?), deleted_at=NULL WHERE id=?",
                (phone, tg or None, (name or "").strip() or None, (city or "").strip() or None, cust_id))
            if marketing_opt_in and not row["marketing_opt_in"]:
                c.execute("UPDATE customers SET marketing_opt_in=1, opt_in_at=? WHERE id=?", (at, cust_id))
        if cid:
            c.execute("INSERT INTO customer_cids(cid, customer_id, first_seen, last_seen) VALUES(?,?,?,?) "
                      "ON CONFLICT(cid, customer_id) DO UPDATE SET last_seen=excluded.last_seen",
                      (cid, cust_id, at, at))
        return cust_id

    def get_customer(self, customer_id: int) -> dict | None:
        r = self._conn().execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        return dict(r) if r else None

    def set_marketing(self, telegram_user_id: int, opt_in: bool) -> int:
        """Включить/выключить новинки для заказов, привязанных к этому Telegram (команда /stop). Карточки
        покупателей не трогает: один человек не может отписать или подписать чужую карточку. Сколько заказов изменено."""
        uid = _user_id(telegram_user_id)
        if uid is None:
            return 0
        with self.tx() as c:
            cur = c.execute("UPDATE orders SET marketing_opt_in=? WHERE telegram_user_id=?", (1 if opt_in else 0, uid))
            return cur.rowcount

    def forget_customer(self, phone: str) -> bool:
        """Обезличить покупателя по его просьбе: контакты стираются (в карточке и во всех его заказах, вместе с
        привязкой Telegram и текстом сообщения продавцу), заказы и суммы остаются для учёта."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
            if not row:
                return False
            for (uid,) in c.execute("SELECT DISTINCT telegram_user_id FROM orders WHERE customer_id=? AND "
                                    "telegram_user_id IS NOT NULL", (row["id"],)).fetchall():
                for t in ("prefs", "digest_sent", "carts"):
                    c.execute(f"DELETE FROM {t} WHERE telegram_user_id=?", (uid,))
                c.execute("UPDATE tg_users SET username=NULL, src_first=NULL, src_last=NULL, review_order=NULL, "
                          "review_until=NULL WHERE user_id=?", (uid,))
            c.execute("UPDATE customers SET phone=NULL, telegram=NULL, telegram_user_id=NULL, name=NULL, city=NULL, "
                      "note=NULL, marketing_opt_in=0, opt_in_at=NULL, deleted_at=? WHERE id=?", (now_iso(), row["id"]))
            c.execute("DELETE FROM customer_cids WHERE customer_id=?", (row["id"],))
            c.execute("UPDATE orders SET comment=NULL, cid=NULL, tg_text=NULL, order_name=NULL, order_phone=NULL, "
                      "order_city=NULL, order_telegram=NULL, telegram_user_id=NULL, telegram_username=NULL, "
                      "marketing_opt_in=0 WHERE customer_id=?", (row["id"],))
            return True

    def marketing_customers(self) -> list[dict]:
        """Кому можно слать новинки в Telegram (для будущих рассылок): пользователи Telegram, у которых есть
        привязанный заказ с галочкой «новинки» (и /stop после этого не было). Одна строка на пользователя:
        {telegram_user_id, telegram_username, orders, last_order_at}."""
        return [dict(r) for r in self.query(
            "SELECT o.telegram_user_id, MAX(o.telegram_username) AS telegram_username, COUNT(*) AS orders, "
            "MAX(o.created_at) AS last_order_at FROM orders o LEFT JOIN customers c ON c.id=o.customer_id "
            "WHERE o.telegram_user_id IS NOT NULL AND o.marketing_opt_in=1 AND c.deleted_at IS NULL "
            "GROUP BY o.telegram_user_id ORDER BY last_order_at DESC")]

    # ------------------------------------------------------------ заказы

    def add_order(self, *, order_no: str | Callable[[], str], customer: dict, items: list, total_uzs: int,
                  prepay_uzs: int | None = None, created_at: str | None = None, client_order_id: str | None = None,
                  cid: str | None = None, source: str = "site", lang: str = "", comment: str = "",
                  page_url: str = "", marketing_opt_in: bool = False, telegram_user: dict | None = None,
                  src_first: str = "", src_last: str = "", ref_code: str = "", ref_user_id: int | None = None,
                  attempts: int = 10) -> dict:
        """Новый заказ в одной транзакции: покупатель (найти/создать), заказ, событие 'new'.
        order_no — строка или функция-генератор (номер проверяется на уникальность, до attempts попыток).
        Тот же client_order_id второй раз — возвращает прежний заказ с duplicate=True, ничего не пишет.
        Контакты из customer сохраняются в самом заказе (order_name/phone/city/telegram); карточка покупателя
        получает только недостающие поля. telegram_user — ПРОВЕРЕННЫЙ пользователь (initData Mini App):
        привязывается только к этому заказу (telegram_link 'linked'; неверный id — 'bad'; не передан — '').
        src_first/src_last — метки источника (clean_tag), ref_code/ref_user_id — кто порекомендовал (свой же
        Telegram не считается). Метки и реферер не переданы, а заказ из Mini App — берутся из tg_users этого пользователя.
        Возвращает {id, order_no, customer_id, orders_count, duplicate, telegram_link}."""
        at = created_at or now_iso()
        prepay = prepay_for(total_uzs) if prepay_uzs is None else int(prepay_uzs)
        gen = order_no if callable(order_no) else None
        tg_uid = _user_id((telegram_user or {}).get("id")) if telegram_user else None
        tg_uname = _clean_username((telegram_user or {}).get("username")) or None
        tg_link = ("linked" if tg_uid else "bad") if telegram_user else ""
        src_first, src_last, ref_code = clean_tag(src_first), clean_tag(src_last), clean_tag(ref_code)
        ref_uid = _user_id(ref_user_id)
        with self.tx() as c:
            if tg_uid:
                u = c.execute("SELECT src_first, src_last, referred_by FROM tg_users WHERE user_id=?", (tg_uid,)).fetchone()
                if u is not None:
                    if not (src_first or src_last):
                        src_first, src_last = u["src_first"] or "", u["src_last"] or ""
                    if ref_uid is None and u["referred_by"]:
                        ref_uid = int(u["referred_by"])
            if ref_uid is not None and ref_uid == tg_uid:
                ref_uid, ref_code = None, ""                       # сам себя не рекомендует
            if ref_uid is not None and not ref_code:
                r = c.execute("SELECT ref_code FROM tg_users WHERE user_id=?", (ref_uid,)).fetchone()
                ref_code = (r["ref_code"] if r else "") or ""
            src_first = src_first or src_last
            src_last = src_last or src_first
            if client_order_id:
                r = c.execute("SELECT o.id, o.order_no, o.customer_id, c.orders_count FROM orders o "
                              "LEFT JOIN customers c ON c.id=o.customer_id WHERE o.client_order_id=?",
                              (client_order_id,)).fetchone()
                if r:
                    return {"id": r["id"], "order_no": r["order_no"], "customer_id": r["customer_id"],
                            "orders_count": r["orders_count"] or 0, "duplicate": True, "telegram_link": ""}
            cust_id = self.find_or_create_customer(
                customer.get("phone", ""), customer.get("telegram", ""), customer.get("name", ""),
                customer.get("city", ""), cid or "", marketing_opt_in, at, _c=c)
            no = None
            for _ in range(max(1, attempts)):
                cand = gen() if gen else str(order_no)
                if not c.execute("SELECT 1 FROM orders WHERE order_no=?", (cand,)).fetchone():
                    no = cand
                    break
                if not gen:
                    break
            if no is None:
                raise DuplicateOrderNo(str(order_no if not gen else "generator"))
            try:
                cur = c.execute(
                    "INSERT INTO orders(order_no, client_order_id, customer_id, created_at, status, status_at, items_json, "
                    "total_uzs, prepay_uzs, source, cid, lang, comment, page_url, order_name, order_phone, order_city, "
                    "order_telegram, telegram_user_id, telegram_username, telegram_linked_at, marketing_opt_in, "
                    "src_first, src_last, ref_code, ref_user_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (no, client_order_id or None, cust_id, at, "new", at, json.dumps(items, ensure_ascii=False),
                     int(total_uzs or 0), prepay, source or "site", cid or None, lang or None, comment or None,
                     page_url or None, str(customer.get("name") or "").strip(),       # '' (не NULL) — заказ схемы 2
                     str(customer.get("phone") or "").strip() or None,
                     str(customer.get("city") or "").strip() or None,
                     _clean_username(customer.get("telegram")) or None,
                     tg_uid, tg_uname if tg_uid else None, at if tg_uid else None, 1 if marketing_opt_in else 0,
                     src_first or None, src_last or None, ref_code or None, ref_uid))
            except sqlite3.IntegrityError as ex:      # гонка невозможна под BEGIN IMMEDIATE, но на всякий случай
                raise DuplicateOrderNo(str(ex))
            oid = int(cur.lastrowid)
            c.execute("UPDATE customers SET orders_count=orders_count+1, last_order_at=? WHERE id=?", (at, cust_id))
            c.execute('INSERT INTO order_events(order_id, at, status, "by", note) VALUES(?,?,?,?,?)',
                      (oid, at, "new", source or "site", None))
            if tg_uid:
                c.execute('INSERT INTO order_events(order_id, at, status, "by", note) VALUES(?,?,?,?,?)',
                          (oid, at, None, f"tg:{tg_uid}", "telegram linked (miniapp)"))
            cnt = c.execute("SELECT orders_count FROM customers WHERE id=?", (cust_id,)).fetchone()[0]
            return {"id": oid, "order_no": no, "customer_id": cust_id, "orders_count": int(cnt),
                    "duplicate": False, "telegram_link": tg_link}

    def _order_row(self, where: str, args) -> dict | None:
        r = self._conn().execute(
            f"SELECT o.*, {CONTACT_SQL} FROM orders o LEFT JOIN customers c ON c.id=o.customer_id WHERE {where}",
            args).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["items"] = json.loads(d.get("items_json") or "[]")
        except ValueError:
            d["items"] = []
        return d

    def get_order(self, order_no: str, with_events: bool = False) -> dict | None:
        d = self._order_row("o.order_no=?", (order_no,))
        if d and with_events:
            d["events"] = [dict(e) for e in self._conn().execute(
                'SELECT at, status, "by", note FROM order_events WHERE order_id=? ORDER BY id', (d["id"],))]
        return d

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        return self._order_row("o.client_order_id=?", (client_order_id,))

    def set_tg_message(self, order_no: str, chat_id, message_id, text: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE orders SET tg_chat_id=?, tg_message_id=?, tg_text=COALESCE(?, tg_text) WHERE order_no=?",
                      (None if chat_id is None else str(chat_id), message_id, text, order_no))

    def add_event(self, order_no: str, note: str, by: str = "", status: str | None = None) -> bool:
        with self.tx() as c:
            r = c.execute("SELECT id FROM orders WHERE order_no=?", (order_no,)).fetchone()
            if not r:
                return False
            c.execute('INSERT INTO order_events(order_id, at, status, "by", note) VALUES(?,?,?,?,?)',
                      (r["id"], now_iso(), status, by or None, note))
            return True

    def set_status(self, order_no: str, status: str, by: str = "", note: str = "", force: bool = False) -> StatusChange:
        """Сменить статус. Разрешены только переходы из TRANSITIONS (force=True — любой). Повторное нажатие
        той же кнопки — ok=True, changed=False (сообщения покупателю второй раз не нужны)."""
        if status not in STATUSES:
            return StatusChange(False, error="bad_status", status=status)
        with self.tx() as c:
            r = c.execute("SELECT id, status FROM orders WHERE order_no=?", (order_no,)).fetchone()
            if not r:
                return StatusChange(False, error="not_found", status=status)
            prev = r["status"]
            if prev == status:
                res = StatusChange(True, False, prev, status)
            elif not force and status not in TRANSITIONS.get(prev, ()):
                res = StatusChange(False, False, prev, status, error="not_allowed")
            else:
                at = now_iso()
                c.execute("UPDATE orders SET status=?, status_at=? WHERE id=?", (status, at, r["id"]))
                c.execute('INSERT INTO order_events(order_id, at, status, "by", note) VALUES(?,?,?,?,?)',
                          (r["id"], at, status, by or None, note or None))
                res = StatusChange(True, True, prev, status)
        res.order = self.get_order(order_no)
        return res

    def link_telegram(self, order_no: str, user: dict, sig: str, secret: str, max_age_days: int = 30) -> LinkResult:
        """/start o_<номер>_<подпись>: привязать Telegram к ЭТОМУ заказу (и только к нему — другие заказы того же
        телефона не затрагиваются). Сначала проверяется подпись (check_link_sig, secret — токен бота): без верной
        подписи 'bad_sig', база даже не читается. Дальше: заказ есть ('not_found'), не старше max_age_days
        ('expired'), не привязан к другому пользователю Telegram ('taken'). Тот же пользователь — 'already'."""
        uid = _user_id((user or {}).get("id"))
        no = str(order_no or "").strip().upper()
        if uid is None:
            return LinkResult("not_found")
        if not check_link_sig(secret, no, sig):
            return LinkResult("bad_sig")
        uname = _clean_username((user or {}).get("username")) or None
        with self.tx() as c:
            r = c.execute("SELECT id, customer_id, created_at, telegram_user_id FROM orders WHERE order_no=?",
                          (no,)).fetchone()
            if not r:
                return LinkResult("not_found")
            try:
                age = datetime.now() - datetime.fromisoformat(r["created_at"])
            except (TypeError, ValueError):
                age = timedelta(0)
            if age > timedelta(days=max_age_days):
                return LinkResult("expired", None, r["customer_id"])
            if r["telegram_user_id"] and int(r["telegram_user_id"]) != uid:
                return LinkResult("taken", None, r["customer_id"])
            code = "already" if r["telegram_user_id"] else "linked"
            at = now_iso()
            c.execute("UPDATE orders SET telegram_user_id=?, telegram_username=COALESCE(?, telegram_username), "
                      "telegram_linked_at=COALESCE(telegram_linked_at, ?) WHERE id=?", (uid, uname, at, r["id"]))
            u = c.execute("SELECT src_first, src_last, referred_by, ref_code FROM tg_users WHERE user_id=?",
                          (uid,)).fetchone()
            if u is not None:                              # метки и реферер из бота — если у заказа их нет
                c.execute("UPDATE orders SET src_first=COALESCE(src_first, ?), src_last=COALESCE(src_last, ?) "
                          "WHERE id=?", (u["src_first"], u["src_last"], r["id"]))
                if u["referred_by"] and int(u["referred_by"]) != uid:
                    rc = c.execute("SELECT ref_code FROM tg_users WHERE user_id=?", (u["referred_by"],)).fetchone()
                    c.execute("UPDATE orders SET ref_user_id=?, ref_code=COALESCE(ref_code, ?) "
                              "WHERE id=? AND ref_user_id IS NULL", (u["referred_by"], rc["ref_code"] if rc else None, r["id"]))
            c.execute('INSERT INTO order_events(order_id, at, status, "by", note) VALUES(?,?,?,?,?)',
                      (r["id"], at, None, f"tg:{uid}", "telegram linked" if code == "linked" else "telegram link repeated"))
        return LinkResult(code, self.get_order(no), r["customer_id"])

    def orders_for_telegram_user(self, telegram_user_id: int, limit: int = 20) -> list[dict]:
        """Заказы, привязанные к этому пользователю Telegram (только они — не все заказы с тем же телефоном)."""
        uid = _user_id(telegram_user_id)
        if uid is None:
            return []
        return [dict(r) for r in self.query(
            "SELECT o.order_no, o.status, o.created_at, o.total_uzs, o.prepay_uzs FROM orders o "
            "WHERE o.telegram_user_id=? ORDER BY o.id DESC LIMIT ?", (uid, limit))]

    # ------------------------------------------------------------ воронка: пользователи бота и рефералы

    def touch_tg_user(self, user_id, username: str = "", started: bool = False, src: str = "",
                      at: str | None = None) -> dict | None:
        """Запомнить пользователя бота: last_seen, ник; started=True — он сам написал боту в личку (ему можно
        писать; снимает отметку «заблокировал»). src — метка /start s_<метка>: src_first только первый раз,
        src_last — всегда. Возвращает строку tg_users."""
        uid = _user_id(user_id)
        if uid is None:
            return None
        at = at or now_iso()
        uname = _clean_username(username) or None
        tag = clean_tag(src) or None
        with self.tx() as c:
            c.execute("INSERT INTO tg_users(user_id, username, first_seen, last_seen, started_at, src_first, src_last) "
                      "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen, "
                      "username=COALESCE(excluded.username, username), "
                      "started_at=COALESCE(started_at, excluded.started_at), "
                      "blocked_at=CASE WHEN excluded.started_at IS NOT NULL THEN NULL ELSE blocked_at END, "
                      "src_first=COALESCE(src_first, excluded.src_first), "
                      "src_last=COALESCE(excluded.src_last, src_last)",
                      (uid, uname, at, at, at if started else None, tag, tag))
        return self.tg_user(uid)

    def tg_user(self, user_id) -> dict | None:
        uid = _user_id(user_id)
        if uid is None:
            return None
        r = self._conn().execute("SELECT * FROM tg_users WHERE user_id=?", (uid,)).fetchone()
        return dict(r) if r else None

    def can_message(self, user_id) -> bool:
        """Боту можно писать этому человеку: он сам начинал чат (/start или сообщение) или привязал заказ,
        и не заблокировал бота."""
        uid = _user_id(user_id)
        if uid is None:
            return False
        u = self.tg_user(uid)
        if u and u.get("blocked_at"):
            return False
        if u and u.get("started_at"):
            return True
        return bool(self._conn().execute("SELECT 1 FROM orders WHERE telegram_user_id=? LIMIT 1", (uid,)).fetchone())

    def mark_blocked(self, user_id, at: str | None = None) -> None:
        """Пользователь заблокировал бота (Telegram ответил 403): больше не пишем, подборки выключены."""
        uid = _user_id(user_id)
        if uid is None:
            return
        at = at or now_iso()
        with self.tx() as c:
            c.execute("INSERT INTO tg_users(user_id, first_seen, last_seen, blocked_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET blocked_at=excluded.blocked_at", (uid, at, at, at))
            c.execute("UPDATE prefs SET stopped_at=COALESCE(stopped_at, ?) WHERE telegram_user_id=?", (utc_iso(), uid))

    def ensure_ref_code(self, user_id, code: str) -> str:
        """Реферальный код пользователя (ref_code_for): записывается один раз; возвращает сохранённый."""
        uid = _user_id(user_id)
        code = clean_tag(code)
        if uid is None or not code:
            return ""
        with self.tx() as c:
            r = c.execute("SELECT ref_code FROM tg_users WHERE user_id=?", (uid,)).fetchone()
            if r and r["ref_code"]:
                return r["ref_code"]
            taken = c.execute("SELECT user_id FROM tg_users WHERE ref_code=?", (code,)).fetchone()
            if taken and int(taken["user_id"]) != uid:
                return ""                                  # совпадение 40 бит — практически невозможно
            at = now_iso()
            c.execute("INSERT INTO tg_users(user_id, first_seen, last_seen, ref_code) VALUES(?,?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET ref_code=excluded.ref_code", (uid, at, at, code))
            return code

    def user_by_ref_code(self, code) -> dict | None:
        code = clean_tag(code)
        if not code:
            return None
        r = self._conn().execute("SELECT * FROM tg_users WHERE ref_code=?", (code,)).fetchone()
        return dict(r) if r else None

    def set_referrer(self, user_id, ref_user_id, at: str | None = None) -> bool:
        """/start r_<код>: кто привёл пользователя. Только первый раз и не сам себя. True — записано."""
        uid, ref = _user_id(user_id), _user_id(ref_user_id)
        if uid is None or ref is None or uid == ref:
            return False
        at = at or now_iso()
        with self.tx() as c:
            c.execute("INSERT INTO tg_users(user_id, first_seen, last_seen) VALUES(?,?,?) ON CONFLICT(user_id) DO NOTHING",
                      (uid, at, at))
            cur = c.execute("UPDATE tg_users SET referred_by=?, referred_at=? WHERE user_id=? AND referred_by IS NULL",
                            (ref, at, uid))
            return cur.rowcount > 0

    def set_review(self, user_id, order_no: str, until: str) -> None:
        """Ждём фото-отзыв по заказу до until (местное время): присланные фото уходят продавцу."""
        uid = _user_id(user_id)
        if uid is None:
            return
        with self.tx() as c:
            at = now_iso()
            c.execute("INSERT INTO tg_users(user_id, first_seen, last_seen, review_order, review_until) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET review_order=excluded.review_order, "
                      "review_until=excluded.review_until", (uid, at, at, order_no, until))

    def review_target(self, user_id, at: str | None = None) -> str | None:
        """Номер заказа, по которому ждём фото от этого пользователя (или None)."""
        u = self.tg_user(user_id)
        at = at or now_iso()
        if u and u.get("review_order") and str(u.get("review_until") or "") >= at:
            return u["review_order"]
        return None

    # ------------------------------------------------------------ воронка: анкета и подборки

    @staticmethod
    def _prefs_row(r) -> dict | None:
        if not r:
            return None
        d = dict(r)
        for k, default in (("types_json", []), ("sizes_json", {}), ("brands_json", []), ("draft_json", None)):
            try:
                d[k[:-5]] = json.loads(d.get(k) or "null")
            except ValueError:
                d[k[:-5]] = None
            if d[k[:-5]] is None:
                d[k[:-5]] = default
        return d

    def get_prefs(self, user_id) -> dict | None:
        """Анкета: {gender, types:[…], sizes:{группа:[…]}, brands:[…], budget, draft, opt_in_at, stopped_at, …}."""
        uid = _user_id(user_id)
        if uid is None:
            return None
        return self._prefs_row(self._conn().execute("SELECT * FROM prefs WHERE telegram_user_id=?", (uid,)).fetchone())

    def save_draft(self, user_id, draft: dict | None) -> None:
        uid = _user_id(user_id)
        if uid is None:
            return
        at = utc_iso()
        with self.tx() as c:
            c.execute("INSERT INTO prefs(telegram_user_id, draft_json, created_at, updated_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(telegram_user_id) DO UPDATE SET draft_json=excluded.draft_json, "
                      "updated_at=excluded.updated_at",
                      (uid, None if draft is None else json.dumps(draft, ensure_ascii=False), at, at))

    def activate_prefs(self, user_id, gender: str, types: list, sizes: dict, brands: list, budget: str,
                       at: str | None = None) -> None:
        """Сохранить анкету и включить подборки (opt_in_at — время согласия, stopped_at снимается)."""
        uid = _user_id(user_id)
        if uid is None:
            return
        at = at or utc_iso()
        with self.tx() as c:
            c.execute("INSERT INTO prefs(telegram_user_id, created_at) VALUES(?,?) ON CONFLICT(telegram_user_id) DO NOTHING",
                      (uid, at))
            c.execute("UPDATE prefs SET gender=?, types_json=?, sizes_json=?, brands_json=?, budget=?, draft_json=NULL, "
                      "updated_at=?, opt_in_at=CASE WHEN opt_in_at IS NULL OR stopped_at IS NOT NULL THEN ? ELSE opt_in_at END, "
                      "stopped_at=NULL WHERE telegram_user_id=?",
                      (gender, json.dumps(list(types), ensure_ascii=False), json.dumps(sizes, ensure_ascii=False),
                       json.dumps(list(brands), ensure_ascii=False), budget, at, at, uid))

    def stop_prefs(self, user_id) -> bool:
        """/stop: подборки выключены (анкета остаётся — /settings включит снова). True — было включено."""
        uid = _user_id(user_id)
        if uid is None:
            return False
        with self.tx() as c:
            cur = c.execute("UPDATE prefs SET stopped_at=? WHERE telegram_user_id=? AND stopped_at IS NULL "
                            "AND opt_in_at IS NOT NULL", (utc_iso(), uid))
            return cur.rowcount > 0

    def active_prefs(self) -> list[dict]:
        """Кому слать подборки: анкета заполнена, согласие есть, /stop не было, бот не заблокирован."""
        rows = self.query("SELECT p.* FROM prefs p LEFT JOIN tg_users u ON u.user_id=p.telegram_user_id "
                          "WHERE p.opt_in_at IS NOT NULL AND p.stopped_at IS NULL AND p.gender IS NOT NULL "
                          "AND u.blocked_at IS NULL ORDER BY p.telegram_user_id")
        return [self._prefs_row(r) for r in rows]

    def set_last_digest(self, user_id, at: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE prefs SET last_digest_at=? WHERE telegram_user_id=?", (at, _user_id(user_id)))

    def sent_ids(self, user_id) -> set[str]:
        return {r[0] for r in self.query("SELECT product_id FROM digest_sent WHERE telegram_user_id=?",
                                         (_user_id(user_id),))}

    def add_sent(self, user_id, product_ids, at: str | None = None) -> None:
        uid = _user_id(user_id)
        at = at or utc_iso()
        with self.tx() as c:
            c.executemany("INSERT OR IGNORE INTO digest_sent(telegram_user_id, product_id, sent_at) VALUES(?,?,?)",
                          [(uid, str(p), at) for p in product_ids])

    def update_prices(self, prices: dict, at: str | None = None, drop_pct: float = 10.0) -> int:
        """Запомнить цены каталога {код: цена}. Цена упала на ≥ drop_pct % от прежней — dropped_at=at,
        drop_from=прежняя. Пишутся только новые и изменившиеся. Сколько снижений найдено."""
        at = at or utc_iso()
        old = {r["product_id"]: r["price_uzs"] for r in self.query("SELECT product_id, price_uzs FROM price_seen")}
        new_rows, changed, drops = [], [], []
        for pid, price in prices.items():
            try:
                price = int(price or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            prev = old.get(pid)
            if prev is None:
                new_rows.append((pid, price, at))
            elif price != prev:
                if prev > 0 and price <= prev * (1 - drop_pct / 100):
                    drops.append((price, at, prev, at, pid))
                else:
                    changed.append((price, at, pid))
        with self.tx() as c:
            c.executemany("INSERT INTO price_seen(product_id, price_uzs, seen_at) VALUES(?,?,?)", new_rows)
            c.executemany("UPDATE price_seen SET price_uzs=?, seen_at=? WHERE product_id=?", changed)
            c.executemany("UPDATE price_seen SET price_uzs=?, seen_at=?, drop_from=?, dropped_at=? WHERE product_id=?",
                          drops)
        return len(drops)

    def price_drops(self) -> dict[str, dict]:
        """{код: {dropped_at, drop_from, price_uzs}} — товары, у которых цена когда-либо падала на ≥10%."""
        return {r["product_id"]: {"dropped_at": r["dropped_at"], "drop_from": r["drop_from"], "price_uzs": r["price_uzs"]}
                for r in self.query("SELECT * FROM price_seen WHERE dropped_at IS NOT NULL")}

    # ------------------------------------------------------------ воронка: корзины

    def set_cart(self, user_id, items: list, at: str | None = None) -> str:
        """Корзина Mini App (/api/cart) — целиком заменяется; пустой список — удалить. Та же корзина ещё раз
        (сайт присылает её при каждом открытии) время не обновляет; другая — новая корзина (напоминание заново).
        'cleared' | 'same' | 'saved'."""
        uid = _user_id(user_id)
        if uid is None:
            return "cleared"
        at = at or now_iso()
        data = json.dumps(list(items or []), ensure_ascii=False, sort_keys=True)
        with self.tx() as c:
            if not items:
                c.execute("DELETE FROM carts WHERE telegram_user_id=?", (uid,))
                return "cleared"
            r = c.execute("SELECT items_json FROM carts WHERE telegram_user_id=?", (uid,)).fetchone()
            if r and r["items_json"] == data:
                return "same"
            c.execute("INSERT INTO carts(telegram_user_id, items_json, created_at, updated_at, reminded_at) "
                      "VALUES(?,?,?,?,NULL) ON CONFLICT(telegram_user_id) DO UPDATE SET items_json=excluded.items_json, "
                      "updated_at=excluded.updated_at, reminded_at=NULL", (uid, data, at, at))
            return "saved"

    def get_cart(self, user_id) -> dict | None:
        r = self._conn().execute("SELECT * FROM carts WHERE telegram_user_id=?", (_user_id(user_id),)).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["items"] = json.loads(d["items_json"] or "[]")
        except ValueError:
            d["items"] = []
        return d

    def carts_due(self, older_than: str, not_before: str) -> list[dict]:
        """Корзины для напоминания: обновлены между not_before и older_than, с вещами, ещё не напоминали,
        после обновления корзины этот пользователь заказа не оформлял, писать ему можно."""
        out = []
        for r in self.query("SELECT * FROM carts WHERE reminded_at IS NULL AND updated_at<=? AND updated_at>=? "
                            "AND items_json NOT IN ('', '[]') ORDER BY updated_at", (older_than, not_before)):
            uid = r["telegram_user_id"]
            if self._conn().execute("SELECT 1 FROM orders WHERE telegram_user_id=? AND created_at>=? LIMIT 1",
                                    (uid, r["updated_at"])).fetchone():
                continue
            if not self.can_message(uid):
                continue
            d = dict(r)
            try:
                d["items"] = json.loads(d["items_json"] or "[]")
            except ValueError:
                continue
            out.append(d)
        return out

    def mark_cart_reminded(self, user_id, at: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE carts SET reminded_at=? WHERE telegram_user_id=?", (at or now_iso(), _user_id(user_id)))

    # ------------------------------------------------------------ воронка: после выдачи

    def followups_due(self, delivered_before: str) -> list[dict]:
        """Заказы «выдан» не позже delivered_before, с привязанным Telegram, которым ещё не писали «Всё подошло?»."""
        return [d for d in (self.get_order(r["order_no"]) for r in self.query(
            "SELECT order_no FROM orders WHERE status='delivered' AND status_at<=? AND telegram_user_id IS NOT NULL "
            "AND followup_at IS NULL ORDER BY status_at", (delivered_before,))) if d]

    def mark_followup(self, order_no: str, result: str, at: str | None = None) -> bool:
        """result: asked | ok | help | failed. Время ставится один раз (при вопросе)."""
        with self.tx() as c:
            cur = c.execute("UPDATE orders SET followup_at=COALESCE(followup_at, ?), followup_result=? WHERE order_no=?",
                            (at or now_iso(), result, order_no))
            return cur.rowcount > 0

    # ------------------------------------------------------------ воронка: отчёт

    def orders_between(self, start: str, end: str) -> list[dict]:
        """Заказы с created_at в [start, end) (местное время): для отчёта воронки."""
        out = []
        for r in self.query("SELECT o.order_no, o.created_at, o.status, o.total_uzs, o.source, o.src_first, o.src_last, "
                            "o.ref_code, o.ref_user_id, o.items_json, o.telegram_user_id, u.username AS ref_username, "
                            "u.id AS ref_client_no FROM orders o LEFT JOIN tg_users u ON u.user_id=o.ref_user_id "
                            "WHERE o.created_at>=? AND o.created_at<? ORDER BY o.id", (start, end)):
            d = dict(r)
            try:
                d["items"] = json.loads(d.pop("items_json") or "[]")
            except ValueError:
                d["items"] = []
            out.append(d)
        return out

    def referrer_info(self, user_id) -> dict | None:
        """Для строки «По рекомендации: @ник / клиент N» (N — номер в tg_users)."""
        u = self.tg_user(user_id)
        return {"username": u.get("username") or "", "client_no": u.get("id")} if u else None

    def stats(self) -> dict:
        c = self._conn()
        by = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM orders GROUP BY status")}
        return {"orders": sum(by.values()), "by_status": by,
                "customers": c.execute("SELECT COUNT(*) FROM customers WHERE deleted_at IS NULL").fetchone()[0],
                "marketing_opt_in": c.execute("SELECT COUNT(*) FROM customers WHERE marketing_opt_in=1 "
                                              "AND deleted_at IS NULL").fetchone()[0]}

    # ------------------------------------------------------------ экспорт и копии

    @staticmethod
    def items_summary(items: list, private: bool = False) -> str:
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            s = f"{it.get('id', '')} {it.get('brand', '')} — {it.get('title', '')}".strip()
            s += f", размер {it.get('size') or '—'} ×{it.get('qty', 1)}"
            if private:
                extra = [str(it.get(k)) for k in ("shop", "url") if it.get(k)]
                if it.get("cost_uzs") is not None:
                    extra.append(f"себест. {it.get('cost_uzs')}")
                if extra:
                    s += " [" + " | ".join(extra) + "]"
            out.append(s)
        return "; ".join(out)

    def export_csv(self, path: Path | str, include_private: bool = False, customers: bool = False) -> int:
        """CSV для Excel (utf-8-sig, разделитель ';', права 600). Заказы (или покупатели при customers=True).
        include_private=False — без закупочных данных (магазин, ссылка, себестоимость); контакты покупателей
        есть в обоих вариантах: файл только для продавца. Возвращает число строк."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        n = 0
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            w = csv.writer(f, delimiter=";")
            if customers:
                cols = ["id", "phone", "telegram", "name", "city", "first_seen", "last_order_at",
                        "orders_count", "marketing_opt_in", "opt_in_at", "note"]
                w.writerow(cols)
                for r in self.query("SELECT * FROM customers WHERE deleted_at IS NULL ORDER BY id"):
                    w.writerow([r[k] if r[k] is not None else "" for k in cols])
                    n += 1
            else:
                head = ["order_no", "created_at", "status", "status_at", "source", "name", "phone", "telegram", "city",
                        "customer_orders", "bot", "items", "total_uzs", "prepay_uzs", "comment"]
                if include_private:
                    head += ["cost_uzs", "items_private"]
                w.writerow(head)
                for r in self.query(
                        f"SELECT o.*, {CONTACT_SQL} FROM orders o LEFT JOIN customers c ON c.id=o.customer_id "
                        "ORDER BY o.id"):
                    try:
                        items = json.loads(r["items_json"] or "[]")
                    except ValueError:
                        items = []
                    row = [r["order_no"], r["created_at"], r["status"], r["status_at"] or "", r["source"],
                           r["customer_name"] or "", r["customer_phone"] or "", r["customer_telegram"] or "",
                           r["customer_city"] or "", r["customer_orders_count"] or 0,
                           "да" if r["telegram_user_id"] else "", self.items_summary(items), r["total_uzs"] or 0,
                           r["prepay_uzs"] or 0, r["comment"] or ""]
                    if include_private:
                        cost = sum((it.get("cost_uzs") or 0) * int(it.get("qty") or 1)
                                   for it in items if isinstance(it, dict) and isinstance(it.get("cost_uzs"), (int, float)))
                        row += [cost or "", self.items_summary(items, private=True)]
                    w.writerow(row)
                    n += 1
        os.replace(tmp, path)
        return n

    def backup(self, path: Path | str) -> Path:
        """Целая копия базы через sqlite3 backup API (можно на ходу). .gz в имени — сжать. Права 600."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = path.with_suffix("") if path.suffix == ".gz" else path
        fd, tmpname = tempfile.mkstemp(prefix=".orders-backup-", suffix=".sqlite", dir=str(path.parent))
        os.close(fd)
        try:
            dst = sqlite3.connect(tmpname)
            try:
                with self._write_lock:
                    self._conn().backup(dst)
            finally:
                dst.close()
            if path.suffix == ".gz":
                gz_tmp = tmpname + ".gz"
                with open(tmpname, "rb") as src, gzip.open(gz_tmp, "wb", compresslevel=6) as out:
                    shutil.copyfileobj(src, out)
                os.replace(gz_tmp, path)
            else:
                os.replace(tmpname, raw)
            try:
                os.chmod(path if path.suffix == ".gz" else raw, 0o600)
            except OSError:
                pass
        finally:
            for p in (tmpname, tmpname + ".gz"):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return path if path.suffix == ".gz" else raw

    def import_jsonl(self, path: Path | str) -> tuple[int, int]:
        """Перенести заказы из старого orders.jsonl (то, чего ещё нет в базе). (добавлено, пропущено)."""
        added = skipped = 0
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(rec, dict) or rec.get("event") or not rec.get("order_no") or not isinstance(rec.get("customer"), dict):
                continue
            if self.get_order(rec["order_no"]):
                skipped += 1
                continue
            cust = rec["customer"]
            # telegram_user_id в журнале пишет сам сервер после проверки initData — привязка этого же заказа
            tg_uid = _user_id(rec.get("telegram_user_id"))
            self.add_order(order_no=rec["order_no"], customer=cust, items=rec.get("items") or [],
                           total_uzs=int(rec.get("total_uzs") or 0), prepay_uzs=rec.get("prepay_uzs"),
                           created_at=rec.get("created_at"), client_order_id=rec.get("client_order_id"),
                           cid=rec.get("cid"), source=rec.get("source") or "site", lang=rec.get("lang") or "",
                           comment=cust.get("comment") or "", page_url=rec.get("page_url") or "",
                           marketing_opt_in=bool(rec.get("consent_marketing")),
                           telegram_user={"id": tg_uid} if tg_uid else None)
            added += 1
        return added, skipped


def default_path() -> Path:
    p = os.environ.get("ORDERS_DB_PATH")
    return Path(p) if p else DEFAULT_PATH


def prune_backups(folder: Path, keep_days: int = 30) -> int:
    """Удалить копии orders-*.sqlite.gz старше keep_days. Сколько удалено."""
    if not folder.is_dir():
        return 0
    cutoff = datetime.now().timestamp() - keep_days * 86400
    n = 0
    for p in folder.glob("orders-*.sqlite*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="База заказов (SQLite)")
    ap.add_argument("cmd", choices=["migrate", "stats", "export", "backup", "import-jsonl"])
    ap.add_argument("path", nargs="?", help="для import-jsonl: файл журнала (по умолчанию data/orders/orders.jsonl)")
    ap.add_argument("--db", default=str(default_path()))
    ap.add_argument("--out", help="файл для export/backup")
    ap.add_argument("--private", action="store_true", help="export: с закупочными данными")
    ap.add_argument("--customers", action="store_true", help="export: покупатели вместо заказов")
    args = ap.parse_args()
    db = OrdersDB(args.db)
    try:
        if args.cmd == "migrate":
            print(f"готово: {db.path} (схема {SCHEMA_VERSION})")
        elif args.cmd == "stats":
            print(json.dumps(db.stats(), ensure_ascii=False, indent=1))
        elif args.cmd == "export":
            stamp = datetime.now().strftime("%Y%m%d-%H%M")
            kind = "customers" if args.customers else ("orders-private" if args.private else "orders")
            out = Path(args.out) if args.out else db.path.parent / "export" / f"{kind}-{stamp}.csv"
            n = db.export_csv(out, include_private=args.private, customers=args.customers)
            print(f"{out}: {n} строк")
        elif args.cmd == "backup":
            out = Path(args.out) if args.out else db.path.parent / "backup" / f"orders-{datetime.now():%Y%m%d}.sqlite.gz"
            print(f"копия: {db.backup(out)}")
        elif args.cmd == "import-jsonl":
            src = Path(args.path) if args.path else db.path.parent / "orders.jsonl"
            added, skipped = db.import_jsonl(src)
            print(f"перенесено {added}, пропущено {skipped}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
