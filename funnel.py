"""Воронка продаж ipakly: отчёт, анкета «Подобрать вещи моего размера», личные подборки, брошенные корзины,
вопрос «Всё подошло?» после выдачи и рекомендации друзей.

    python funnel.py --report [--days 7] [--no-send]   # отчёт: печать + админам бота (TELEGRAM_ADMIN_IDS)
    python funnel.py --digest [--dry]                  # личные подборки (таймер yurt-funnel@digest, 10:00)
    python funnel.py --carts [--dry]                   # напоминание о корзине (yurt-funnel@carts, каждый час)
    python funnel.py --followup [--dry]                # «Всё подошло?» через 7 дней после выдачи (yurt-funnel@followup)

--dry (или нет TELEGRAM_BOT_TOKEN) — ничего не отправляется и в базу не пишется: печатается, что ушло бы.
Каждое задание берёт свой замок data/funnel-<задание>.lock (общий data/server.lock не трогается).

Откуда данные:
  * события сайта — data/events/ГГГГ-ММ-ДД.jsonl (пишет order_api.py, POST /api/event; без IP и личных данных):
    {ts, day, s (сессия), src (метка источника), ev:[{t: visit|view|cart|co|order|order_tg|q, …}]};
  * заказы, пользователи бота, анкеты, корзины — база data/orders/orders.sqlite (orders_db.py, схема 3);
  * товары — собранный сайт site/ (channel.Site: индекс catalog_files; ключи размеров zk, иначе размеры карточки).

Отчёт: по каждой метке и всего — сессии, смотрели товар, корзина, оформление, заказы (заказ с сайта + запасной
заказ через Telegram order_tg), конверсия шагов, выручка заказов из базы (без отменённых) по метке последнего
касания; топ-10 просмотренных товаров без заказов; поиск без результатов; заказы в базе по источникам;
рекомендации (кто привёл покупателей). Тот же отчёт — команда /report у бота (только админы).

Анкета (в боте, плагин этого модуля): /start → «Подобрать вещи моего размера» (callback 'pf:go') или /settings:
пол → категории (мультивыбор из типов каталога) → размеры для выбранных категорий (буквенные XS–3XL,
IT 44–58 у мужчин / 36–50 у женщин, ворот рубашки 37–46, обувь EU 36–46, талия W26–W38) → бренды (топ каталога,
страницами, «Все бренды») → бюджет → согласие «не чаще раза в день». Сообщение анкеты правится на месте
(editMessageText). /stop (встроенная команда tg_bot) выключает подборки.

Подборка: для каждой включённой анкеты — товары в наличии, подходящие по полу/категории/размеру/бренду/бюджету,
новые с прошлой подборки (first_seen) или подешевевшие на ≥10% (последняя цена каждого кода — price_seen);
не больше 5 лучших по ranking.score; уже присланные не повторяются. Альбом фото (фото — как у канала: свой файл
или CDN турецких магазинов, YOOX никогда) и сообщение со списком и кнопками на товары (Mini App, если в
channel.json задан mini_app_url, иначе сайт #/catalog?p=КОД). Совпадений нет — ничего не шлём. Не чаще
1 сообщения в секунду в один чат и 25 в секунду всего; 403 (бот заблокирован) — подборки выключаются.
Каждое сообщение покупателю проходит проверку на утечку (channel.find_leak).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import catalog_files as cf
import channel
import orders_db
import ranking
import sync_state
import tg_bot

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
DATA = ROOT / "data"
EVENTS_DIR = DATA / "events"
TZ = timezone(timedelta(hours=5))                         # Ташкент, без летнего времени
CODE_RE = re.compile(r"^[A-Z0-9]{7}$")
DIGEST_MAX = 5
DROP_PCT = 10.0

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------- тексты (переопределяются server/order_messages.json)

TEXTS = {
    "picks_gender": "Подберём новинки вашего размера — займёт полминуты.\n\nДля кого подбираем?",
    "picks_types": "Что вам интересно? Можно выбрать несколько, затем «Готово».",
    "picks_sizes": "Ваш {group}. Можно выбрать несколько, затем «Дальше».",
    "picks_brands": "Любимые бренды — можно несколько. Или «Все бренды».",
    "picks_budget": "Бюджет на одну вещь?",
    "picks_done": "Готово! Буду присылать новинки вашего размера не чаще раза в день. /stop — отключить, /settings — изменить.",
    "picks_cancel": "Хорошо. Заполнить анкету можно в любой момент: /settings",
    "picks_stale": "Анкета устарела — начните заново: /settings",
    "picks_empty": "Каталог сейчас обновляется — попробуйте через несколько минут: /settings",
    "digest_head": "Новинки вашего размера:",
    "digest_drop": "цена снижена",
    "cart_reminder": "Вы оставили в корзине:\n{cart}\n\nОформить заказ?",
    "button_cart": "Оформить заказ",
}
GENDERS = {"m": ("men", "Мужское"), "w": ("women", "Женское")}
GENDER_RU = {"men": "мужское", "women": "женское"}
BUDGETS = [("1", "до 1,5 млн", 0, 1_500_000), ("2", "1,5–4 млн", 1_500_000, 4_000_000),
           ("3", "4–10 млн", 4_000_000, 10_000_000), ("0", "без ограничений", 0, None)]
LETTERS = ["XS", "S", "M", "L", "XL", "XXL", "3XL"]
SIZE_GROUPS = {                                            # ключи — как sizes_norm.filter_keys (столбец zk)
    "letters": ("буквенный размер одежды", LETTERS),
    "it_m": ("итальянский размер одежды (IT)", [str(x) for x in range(44, 60, 2)]),
    "it_w": ("итальянский размер одежды (IT)", [str(x) for x in range(36, 52, 2)]),
    "collar": ("размер рубашки по вороту", [f"ворот {x}" for x in range(37, 47)]),
    "jeans": ("размер джинсов и брюк по талии", [f"W{x}" for x in range(26, 39)]),
    "shoes": ("размер обуви (EU)", [str(x) for x in range(36, 47)]),
}
GROUP_ORDER = ["letters", "it_m", "it_w", "collar", "jeans", "shoes"]
SHOES = {"обувь"}
BOTTOMS = {"брюки", "джинсы", "шорты", "юбки"}
NO_SIZE = {"сумки", "аксессуары"}
TYPES_MAX = 12
BRANDS_MAX = 40
BRANDS_PAGE = 8


def _log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [funnel] {msg}", flush=True)


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def money(n) -> str:
    try:
        return f"{int(round(float(n))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "?"


def pct(a: int, b: int) -> str:
    return f"{a * 100 / b:.0f}%" if b else "—"


def local_naive(dt: datetime) -> str:
    """Местное время сервера без пояса — как created_at/status_at заказов (datetime.now())."""
    return dt.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def text_of(messages: dict, key: str) -> str:
    v = messages.get(key)
    return v if isinstance(v, str) and v.strip() else TEXTS.get(key, "")


# ---------------------------------------------------------------- размеры и подбор

def groups_for(type_, gender) -> list[str]:
    """Группы размеров для товара этого типа и пола ([] — размер не важен: сумки, аксессуары)."""
    it = "it_w" if gender == "women" else "it_m"
    if type_ in SHOES:
        return ["shoes"]
    if type_ in NO_SIZE or not type_:
        return []
    if type_ in BOTTOMS:
        return ["letters", it, "jeans"]
    if type_ == "рубашки" and gender != "women":
        return ["letters", it, "collar"]
    return ["letters", it]


def size_keys(row: dict) -> list[str]:
    """Ключи размеров товара: нормализованные zk (catalog_files), иначе показанные размеры."""
    z = row.get("_zk")
    if z is None:
        z = row.get("zk") if isinstance(row.get("zk"), list) else None
    return [str(x) for x in (z if z is not None else row.get("sizes") or [])]


def size_match(wanted: set[str], keys: list[str]) -> bool:
    """Есть ли у товара выбранный размер. Обувь «40½» / «40⅓» подходит к выбранному 40."""
    for k in keys:
        if k in wanted:
            return True
        m = re.match(r"^(\d+)[⅓½⅔]$", k)
        if m and m.group(1) in wanted:
            return True
    return False


def budget_range(code) -> tuple[int, int | None]:
    for c, _, lo, hi in BUDGETS:
        if c == str(code or "0"):
            return lo, hi
    return 0, None


def budget_label(code) -> str:
    return next((lbl for c, lbl, _, _ in BUDGETS if c == str(code or "0")), "без ограничений")


def matches(prefs: dict, row: dict) -> bool:
    """Товар подходит анкете: в наличии, пол, категория, бренд, бюджет, размер (если выбран для его группы)."""
    if row.get("in_stock") is False or not row.get("sizes"):
        return False
    g = prefs.get("gender")
    if g and row.get("gender") not in (g, None, ""):
        return False
    types = prefs.get("types") or []
    if types and row.get("type") not in types:
        return False
    brands = {ranking.norm_key(b) for b in prefs.get("brands") or []}
    if brands and ranking.norm_key(row.get("brand")) not in brands:
        return False
    lo, hi = budget_range(prefs.get("budget"))
    price = int(row.get("price_uzs") or 0)
    if price <= 0 or (lo > 0 and price <= lo) or (hi is not None and price > hi):
        return False
    sizes = prefs.get("sizes") or {}
    wanted = set()
    for grp in groups_for(row.get("type"), row.get("gender") or g):
        wanted.update(str(x) for x in sizes.get(grp) or [])
    if wanted and not size_match(wanted, size_keys(row)):
        return False
    return True


# ---------------------------------------------------------------- отправка: темп

class Throttle:
    """Не чаще 1 сообщения в секунду в один чат и не больше 25 в секунду всего (лимиты Telegram)."""

    def __init__(self, per_chat_s: float = 1.0, per_sec: int = 25, sleep: Callable = time.sleep,
                 clock: Callable = time.monotonic):
        self.per_chat_s, self.per_sec, self.sleep, self.clock = per_chat_s, per_sec, sleep, clock
        self.last: dict[str, float] = {}
        self.recent: deque = deque()

    def wait(self, chat) -> None:
        key = str(chat)
        now = self.clock()
        last = self.last.get(key)
        if last is not None and now - last < self.per_chat_s:
            self.sleep(self.per_chat_s - (now - last))
            now = self.clock()
        while self.recent and now - self.recent[0] >= 1.0:
            self.recent.popleft()
        while len(self.recent) >= self.per_sec:
            self.sleep(max(0.0, 1.0 - (now - self.recent[0])) or 0.01)
            now = self.clock()
            while self.recent and now - self.recent[0] >= 1.0:
                self.recent.popleft()
        self.recent.append(now)
        self.last[key] = now


# ---------------------------------------------------------------- отчёт

def read_events(events_dir: Path, since_ts: int, until_ts: int) -> list[dict]:
    """Строки событий с ts в [since_ts, until_ts). Битые строки пропускаются."""
    out = []
    d0 = datetime.fromtimestamp(since_ts, TZ).date()
    d1 = datetime.fromtimestamp(until_ts, TZ).date()
    day = d0
    while day <= d1:
        path = Path(events_dir) / f"{day.isoformat()}.jsonl"
        if path.is_file():
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("ts"), int) and since_ts <= rec["ts"] < until_ts \
                            and isinstance(rec.get("ev"), list) and isinstance(rec.get("s"), str):
                        out.append(rec)
        day += timedelta(days=1)
    return out


NO_TAG = "(без метки)"


def funnel_stats(records: list[dict]) -> dict:
    """По сессиям: {sessions: {s: {...}}, by_src: {метка: счётчики}, total, views, zero_q}."""
    sess: dict[str, dict] = {}
    views: Counter = Counter()
    view_sessions: dict[str, set] = defaultdict(set)
    zero_q: dict[str, set] = defaultdict(set)
    for rec in sorted(records, key=lambda r: r["ts"]):
        s = sess.setdefault(rec["s"], {"src": "", "view": False, "cart": False, "co": False, "order": 0, "order_tg": 0})
        if not s["src"] and rec.get("src"):
            s["src"] = str(rec["src"])
        for ev in rec["ev"]:
            if not isinstance(ev, dict):
                continue
            t = ev.get("t")
            if t == "view" and isinstance(ev.get("id"), str):
                s["view"] = True
                view_sessions[ev["id"]].add(rec["s"])
            elif t == "cart":
                s["cart"] = True
            elif t == "co":
                s["co"] = True
            elif t == "order":
                s["order"] += 1
            elif t == "order_tg":
                s["order_tg"] += 1
            elif t == "q" and ev.get("n") == 0 and isinstance(ev.get("q"), str):
                zero_q[re.sub(r"\s+", " ", ev["q"].strip().lower())].add(rec["s"])
    by_src: dict[str, Counter] = defaultdict(Counter)
    for s in sess.values():
        for key in (s["src"] or NO_TAG, "__total__"):
            c = by_src[key]
            c["sessions"] += 1
            c["view"] += s["view"]
            c["cart"] += s["cart"] or bool(s["co"] or s["order"])
            c["co"] += s["co"] or bool(s["order"])
            c["orders_site"] += s["order"]
            c["orders_tg"] += s["order_tg"]
    for pid, ss in view_sessions.items():
        views[pid] = len(ss)
    total = by_src.pop("__total__", Counter())
    return {"sessions": sess, "by_src": dict(by_src), "total": total, "views": views,
            "zero_q": Counter({q: len(ss) for q, ss in zero_q.items()})}


def _funnel_line(c: Counter) -> str:
    orders = c["orders_site"] + c["orders_tg"]
    steps = (f"сессий {c['sessions']} → смотрели товар {c['view']} ({pct(c['view'], c['sessions'])}) → "
             f"корзина {c['cart']} ({pct(c['cart'], c['view'])}) → оформление {c['co']} ({pct(c['co'], c['cart'])}) → "
             f"заказы {orders} ({pct(orders, c['co'])})")
    if c["orders_tg"]:
        steps += f" [сайт {c['orders_site']}, через Telegram {c['orders_tg']}]"
    return steps + f" · из сессий {pct(orders, c['sessions'])}"


def build_report(db: orders_db.OrdersDB | None, events_dir: Path, days: int = 7, now: datetime | None = None,
                 site_dir: Path | None = None) -> str:
    """Текст отчёта (обычный текст, по-русски) за последние days суток до now."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    st = funnel_stats(read_events(events_dir, int(start.timestamp()), int(now.timestamp()) + 1))
    orders = []
    if db is not None:
        orders = db.orders_between(local_naive(start), local_naive(now + timedelta(seconds=1)))
    live = [o for o in orders if o["status"] != "cancelled"]
    out = [f"📊 Воронка за {days} дн. ({start.astimezone(TZ):%d.%m %H:%M} — {now.astimezone(TZ):%d.%m %H:%M}, Ташкент)", ""]
    t = st["total"]
    if t["sessions"]:
        out.append("Всего: " + _funnel_line(t))
    else:
        out.append("Событий сайта за период нет (сайт ещё не присылает /api/event или нет посетителей).")
    out.append(f"Заказы в базе: {len(orders)} (отменено {len(orders) - len(live)}), выручка без отменённых: "
               f"{money(sum(int(o['total_uzs'] or 0) for o in live))} сум")
    rev_src: dict[str, list] = defaultdict(lambda: [0, 0])
    first_src: Counter = Counter()
    for o in live:
        key = o.get("src_last") or o.get("src_first") or NO_TAG
        rev_src[key][0] += 1
        rev_src[key][1] += int(o["total_uzs"] or 0)
        if o.get("src_first"):
            first_src[o["src_first"]] += 1
    if st["by_src"]:
        out += ["", "По источникам (метка первого захода сессии):"]
        for src, c in sorted(st["by_src"].items(), key=lambda kv: (-kv[1]["sessions"], kv[0])):
            line = f"• {src}: " + _funnel_line(c)
            if src in rev_src:
                line += f" · выручка {money(rev_src[src][1])} сум"
            out.append(line)
    top = [(pid, n) for pid, n in st["views"].most_common()
           if pid not in {str(it.get("id")) for o in orders for it in o["items"] if isinstance(it, dict)}][:10]
    if top:
        names = _product_names(site_dir, [p for p, _ in top])
        out += ["", "Смотрели, но не заказывали (топ-10):"]
        for i, (pid, n) in enumerate(top, 1):
            out.append(f"{i}. {pid}" + (f" · {names[pid]}" if names.get(pid) else "") + f" — {n} сесс.")
    zq = st["zero_q"].most_common(10)
    if zq:
        out += ["", "Поиск без результатов:"]
        out += [f"• «{q}» — {n}" for q, n in zq]
    if rev_src:
        out += ["", "Заказы в базе по источникам (последнее касание):"]
        for src, (n, rev) in sorted(rev_src.items(), key=lambda kv: (-kv[1][1], kv[0])):
            out.append(f"• {src} — {n} зак., {money(rev)} сум" +
                       (f" (впервые пришли отсюда: {first_src[src]})" if first_src.get(src) else ""))
    refs: dict[int, list] = {}
    for o in live:
        if o.get("ref_user_id"):
            r = refs.setdefault(int(o["ref_user_id"]), [o.get("ref_username") or "", o.get("ref_client_no"), 0, 0])
            r[2] += 1
            r[3] += int(o["total_uzs"] or 0)
    if refs:
        out += ["", "Рекомендации (кто привёл покупателей):"]
        for uid, (uname, cno, n, rev) in sorted(refs.items(), key=lambda kv: -kv[1][2]):
            who = " / ".join(x for x in ((f"@{uname}" if uname else ""), (f"клиент {cno}" if cno else "")) if x) or "клиент"
            out.append(f"• {who} — {n} зак., {money(rev)} сум")
    if db is not None:
        try:
            n_new = db.query("SELECT COUNT(*) FROM tg_users WHERE referred_at>=?", (local_naive(start),))[0][0]
            n_prefs = db.query("SELECT COUNT(*) FROM prefs WHERE opt_in_at IS NOT NULL AND stopped_at IS NULL")[0][0]
            out += ["", f"Пришли в бота по ссылкам друзей: {n_new} · подписаны на подборки: {n_prefs}"]
        except Exception:
            pass
    return "\n".join(out)


def _product_names(site_dir: Path | None, ids: list[str]) -> dict[str, str]:
    try:
        rows = channel.Site(Path(site_dir or SITE)).rows
    except Exception:
        return {}
    out = {}
    for pid in ids:
        r = rows.get(pid)
        if r:
            out[pid] = " — ".join(x for x in (str(r.get("brand") or ""), channel.title_without_brand(r.get("title"), r.get("brand"))) if x)
    return out


def split_text(text: str, limit: int = 4000) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        cand = (cur + "\n" + line) if cur else line
        if len(cand) > limit and cur:
            parts.append(cur)
            cur = line[:limit]
        else:
            cur = cand[:limit]
    if cur:
        parts.append(cur)
    return parts


# ---------------------------------------------------------------- задания

class Funnel:
    """Задания по таймеру. Покупателям — через channel.TG (проверка на утечку перед каждой отправкой, фото файлом),
    админам — через tg_bot.Bot. http(method, params, files=None) — подмена Bot API в тестах."""

    def __init__(self, db: orders_db.OrdersDB, token: str | None = None, http: Callable | None = None,
                 site_dir: Path | None = None, events_dir: Path | None = None, env: dict | None = None,
                 now: datetime | Callable | None = None, sleep: Callable = time.sleep, clock: Callable = time.monotonic,
                 log: Callable | None = None, dry: bool = False, cfg: dict | None = None, fetch: Callable | None = None,
                 messages_path: Path | None = None, ranker: ranking.Ranker | None = None):
        self.env = os.environ if env is None else env
        self.db = db
        self.log = log or _log
        self.token = (token if token is not None else str(self.env.get("TELEGRAM_BOT_TOKEN") or "")).strip()
        self.dry = dry or not self.token
        self.site_dir = Path(site_dir or SITE)
        self.events_dir = Path(events_dir or EVENTS_DIR)
        self._now_src = now
        self.sleep = sleep
        self.cfg = cfg if cfg is not None else channel.load_config()
        self.fetch = fetch
        self.ranker = ranker
        bot_http = (lambda m, p: http(m, p)) if http else None
        self.bot = tg_bot.Bot(self.token, db=db, admin_ids=tg_bot.parse_admin_ids(self.env.get("TELEGRAM_ADMIN_IDS")),
                              orders_chat=(self.env.get("TELEGRAM_ORDERS_CHAT_ID") or self.env.get("TELEGRAM_CHAT_ID") or ""),
                              shop_url=str(self.env.get("SHOP_URL") or ""), bot_username=str(self.env.get("BOT_USERNAME") or ""),
                              seller_telegram=str(self.env.get("SELLER_TELEGRAM") or ""),
                              messages_path=messages_path or tg_bot.MESSAGES_PATH, http=bot_http, log=self.log)
        self.tg = channel.TG(self.token, http=http, dry=self.dry, log=self.log, sleep=sleep)
        self.throttle = Throttle(sleep=sleep, clock=clock)
        self._ch: channel.Channel | None = None
        self.stats: Counter = Counter()

    def now(self) -> datetime:
        n = self._now_src() if callable(self._now_src) else self._now_src
        return n or datetime.now(timezone.utc)

    @property
    def ch(self) -> channel.Channel:
        """Канальные помощники: каталог (Site), фото (свой файл / CDN, YOOX никогда), ссылка на товар."""
        if self._ch is None:
            env = {k: v for k, v in self.env.items() if k != "CHANNEL_ENABLED"}
            self._ch = channel.Channel(site=self.site_dir, cfg=self.cfg, env=env, fetch=self.fetch, now=self.now,
                                       ranker=self.ranker or ranking.Ranker({}, 0.5), log=self.log, sleep=self.sleep,
                                       dry=True)
        return self._ch

    def rows(self) -> dict[str, dict]:
        return self.ch.site.rows

    def messages(self) -> dict:
        return self.bot.messages()

    def _send(self, chat, method: str, params: dict, files: dict | None = None, what: str = ""):
        """Отправка покупателю: темп, проверка на утечку (LeakBlocked → None), 403 → отметка в базе."""
        if not self.dry:
            self.throttle.wait(chat)
        try:
            res = self.tg.call(method, dict(params, chat_id=chat), files=files)
        except channel.LeakBlocked as ex:
            self.stats["утечка — не отправлено"] += 1
            self.log(f"{what}: НЕ отправлено — найдено «{ex}» ({tg_bot.uid_tag(chat)})")
            return None
        if res is None and int((self.tg.last_error or {}).get("error_code") or 0) == 403:
            self.stats["заблокировали бота"] += 1
            if not self.dry:
                self.db.mark_blocked(chat)
        return res

    def blocked(self) -> bool:
        return int((self.tg.last_error or {}).get("error_code") or 0) == 403

    # ------------------------------------------------------------ отчёт

    def report(self, days: int = 7, send: bool = True, out: Callable = print) -> str:
        text = build_report(self.db, self.events_dir, days, self.now(), self.site_dir)
        out(text)
        if send and not self.dry:
            chats = sorted(self.bot.admin_ids) or ([self.bot.orders_chat] if self.bot.orders_chat else [])
            if not chats:
                self.log("отчёт: некому отправить (TELEGRAM_ADMIN_IDS / TELEGRAM_ORDERS_CHAT_ID пусты)")
            for chat in chats:
                for part in split_text(text):
                    self.throttle.wait(chat)
                    if self.bot.send(chat, part) is None:
                        self.log(f"отчёт: не отправлен {tg_bot.uid_tag(chat)} "
                                 f"({str(self.bot.last_error.get('description') or '')[:100]})")
                        break
        return text

    # ------------------------------------------------------------ подборки

    def digest(self) -> Counter:
        now = self.now()
        now_s = orders_db.utc_iso(now)
        rows = self.rows()
        if not rows:
            self.log("подборки: каталог не найден — пропуск")
            return self.stats
        if not self.dry:
            n = self.db.update_prices({pid: r.get("price_uzs") for pid, r in rows.items() if r.get("in_stock") is not False},
                                      now_s, DROP_PCT)
            self.stats["подешевели на ≥10%"] = n
        drops = self.db.price_drops()
        first_days = float(self.env.get("FUNNEL_DIGEST_FIRST_DAYS") or 3)
        ranker = self.ranker or ranking.Ranker.from_config()
        for prefs in self.db.active_prefs():
            uid = prefs["telegram_user_id"]
            since = prefs.get("last_digest_at")
            if not since:
                try:
                    opt = datetime.fromisoformat(str(prefs["opt_in_at"]).replace("Z", "+00:00"))
                except ValueError:
                    opt = now
                since = orders_db.utc_iso(opt - timedelta(days=first_days))
            sent = self.db.sent_ids(uid)
            picks = []
            for pid, r in rows.items():
                if pid in sent or not matches(prefs, r):
                    continue
                new = str(r.get("first_seen") or "") > since
                dr = drops.get(pid)
                drop = bool(dr and str(dr.get("dropped_at") or "") > since and int(r.get("price_uzs") or 0) <= int(dr.get("price_uzs") or 0))
                if new or drop:
                    picks.append((ranker.score(r, now), str(r.get("first_seen") or ""), pid, "drop" if drop and not new else "new"))
            if not picks:
                self.stats["без совпадений"] += 1
                continue
            picks.sort(key=lambda x: x[2])                 # при равной оценке — новее раньше, затем код
            picks.sort(key=lambda x: x[1], reverse=True)
            picks.sort(key=lambda x: -x[0])
            chosen = [(rows[pid], why) for _, _, pid, why in picks[:DIGEST_MAX]]
            ok = self.send_digest(uid, chosen, drops)
            if ok:
                self.stats["подборок отправлено"] += 1
                if not self.dry:
                    self.db.add_sent(uid, [r["id"] for r, _ in chosen], now_s)
                    self.db.set_last_digest(uid, now_s)
            elif self.blocked():
                self.log(f"подборка: {tg_bot.uid_tag(uid)} заблокировал бота — подборки выключены")
            else:
                self.stats["не отправлено"] += 1
        self.log("подборки: " + (", ".join(f"{k} {v}" for k, v in self.stats.items()) or "нет включённых анкет"))
        return self.stats

    def digest_text(self, chosen: list[tuple[dict, str]], drops: dict) -> tuple[str, dict]:
        m = self.messages()
        lines = [esc(text_of(m, "digest_head"))]
        buttons = []
        for i, (r, why) in enumerate(chosen, 1):
            brand = str(r.get("brand") or "").strip()
            title = channel.title_without_brand(r.get("title"), brand)
            price = f"{money(r.get('price_uzs'))} сум"
            try:
                d = float(r.get("discount_pct") or 0)
            except (TypeError, ValueError):
                d = 0
            if 0 < d < 100:
                price += f" −{channel.disc_label(d)}%"
            if why == "drop":
                price += f" · {esc(text_of(m, 'digest_drop'))}"
            sizes = channel.sort_sizes(r.get("sizes"))
            sz = ", ".join(sizes[:10]) + (" …" if len(sizes) > 10 else "")
            lines.append(f"\n{i}. <b>{esc(brand)}</b> — {esc(title)}\n{price}\nРазмеры: {esc(sz)}")
            buttons.append([{"text": f"{i}. {brand or title}"[:60], "url": self.ch.order_url(r["id"])}])
        footer = esc(self.cfg.get("caption", {}).get("footer") or "")
        if footer:
            lines.append("\n" + footer)
        return "\n".join(lines), {"inline_keyboard": buttons}

    def send_digest(self, uid: int, chosen: list[tuple[dict, str]], drops: dict) -> bool:
        text, markup = self.digest_text(chosen, drops)
        media = []
        if not self.dry and str(self.env.get("FUNNEL_DIGEST_PHOTOS") or "1") != "0":
            for i, (r, _) in enumerate(chosen, 1):
                for ref in self.ch.sendable_photos(r["id"])[:2]:
                    b = self.ch.photo_bytes(ref)
                    if b:
                        media.append((i, (f"photo{i}.jpg", b, "image/jpeg")))
                        break
        what = f"подборка {tg_bot.uid_tag(uid)}"
        if len(media) >= 2:
            items, files = [], {}
            for j, (i, f) in enumerate(media):
                files[f"p{j}"] = f
                items.append({"type": "photo", "media": f"attach://p{j}",
                              "caption": f"{i}. {chosen[i - 1][0].get('brand') or ''}".strip()})
            if self._send(uid, "sendMediaGroup", {"media": items}, files=files, what=what) is None:
                return False
        elif len(media) == 1 and len(text) <= 1024:
            return self._send(uid, "sendPhoto", {"caption": text, "parse_mode": "HTML", "reply_markup": markup},
                              files={"photo": media[0][1]}, what=what) is not None
        return self._send(uid, "sendMessage", {"text": text, "parse_mode": "HTML", "reply_markup": markup,
                                               "disable_web_page_preview": True}, what=what) is not None

    # ------------------------------------------------------------ корзины

    def carts(self) -> Counter:
        now = self.now()
        hours = float(self.env.get("FUNNEL_CART_HOURS") or 24)
        max_days = float(self.env.get("FUNNEL_CART_MAX_DAYS") or 7)
        due = self.db.carts_due(local_naive(now - timedelta(hours=hours)), local_naive(now - timedelta(days=max_days)))
        if not due:
            self.log("корзины: напоминать некому")
            return self.stats
        rows = self.rows()
        m = self.messages()
        base = tg_bot.shop_link(self.bot.shop_url)
        btn = tg_bot.shop_button(text_of(m, "button_cart"), base + "#/cart") if base else None
        markup = {"inline_keyboard": [[btn]]} if btn else None
        for cart in due:
            uid = cart["telegram_user_id"]
            lines = []
            for it in cart["items"]:
                r = rows.get(str(it.get("id") or ""))
                if not isinstance(it, dict) or not r or r.get("in_stock") is False:
                    continue
                name = " — ".join(x for x in (str(r.get("brand") or "").strip(),
                                              channel.title_without_brand(r.get("title"), r.get("brand"))) if x)
                s = f"• {name}"
                if it.get("size"):
                    s += f", размер {it['size']}"
                if int(it.get("qty") or 1) > 1:
                    s += f" × {int(it['qty'])}"
                lines.append(s)
            if not lines:                                  # всё из корзины распродано — не напоминаем
                self.stats["корзина распродана"] += 1
                if not self.dry:
                    self.db.mark_cart_reminded(uid, local_naive(now))
                continue
            text = self.bot.render(text_of(m, "cart_reminder"), None, cart="\n".join(lines))
            res = self._send(uid, "sendMessage", {"text": text, "reply_markup": markup, "disable_web_page_preview": True},
                             what=f"корзина {tg_bot.uid_tag(uid)}")
            if res is not None or self.blocked():          # одно напоминание на корзину (заблокировал — тоже всё)
                if not self.dry:
                    self.db.mark_cart_reminded(uid, local_naive(now))
                if res is not None:
                    self.stats["напомнили"] += 1
            else:
                self.stats["не отправлено"] += 1
        self.log("корзины: " + ", ".join(f"{k} {v}" for k, v in self.stats.items() if v))
        return self.stats

    # ------------------------------------------------------------ после выдачи

    def followup(self) -> Counter:
        now = self.now()
        days = float(self.env.get("FUNNEL_FOLLOWUP_DAYS") or 7)
        m = self.messages()
        for order in self.db.followups_due(local_naive(now - timedelta(days=days))):
            uid = order["telegram_user_id"]
            no = order["order_no"]
            u = self.db.tg_user(uid)
            if u and u.get("blocked_at"):
                if not self.dry:
                    self.db.mark_followup(no, "blocked", local_naive(now))
                continue
            text = self.bot.render(m.get("followup_ask") or tg_bot.DEFAULT_MESSAGES["followup_ask"], order)
            kb = {"inline_keyboard": [[
                {"text": m.get("button_followup_ok") or tg_bot.DEFAULT_MESSAGES["button_followup_ok"], "callback_data": f"fu:ok:{no}"},
                {"text": m.get("button_followup_help") or tg_bot.DEFAULT_MESSAGES["button_followup_help"], "callback_data": f"fu:help:{no}"}]]}
            res = self._send(uid, "sendMessage", {"text": text, "reply_markup": kb}, what=f"заказ {no}: «Всё подошло?»")
            if res is not None:
                self.stats["спросили"] += 1
                if not self.dry:
                    self.db.mark_followup(no, "asked", local_naive(now))
                    self.db.add_event(no, "followup asked", by="funnel")
            elif self.blocked():
                if not self.dry:
                    self.db.mark_followup(no, "blocked", local_naive(now))
            else:
                self.stats["не отправлено"] += 1
        self.log("после выдачи: " + (", ".join(f"{k} {v}" for k, v in self.stats.items() if v) or "спрашивать некого"))
        return self.stats


# ---------------------------------------------------------------- анкета в боте (плагин tg_bot)

_SUMMARY: dict = {"sig": None, "dir": None, "data": None}


def catalog_summary(site_dir: Path) -> dict:
    """Для анкеты: типы и бренды по полу (только в наличии). Кэш до следующей сборки каталога."""
    sig = cf.public_signature(site_dir)
    if _SUMMARY["data"] is not None and _SUMMARY["sig"] == sig and _SUMMARY["dir"] == str(site_dir):
        return _SUMMARY["data"]
    types = {"men": Counter(), "women": Counter()}
    brands = {"men": defaultdict(Counter), "women": defaultdict(Counter)}
    try:
        rows = channel.Site(Path(site_dir)).rows if sig else {}
    except Exception as ex:
        _log(f"анкета: каталог не прочитан ({ex.__class__.__name__})")
        rows = {}
    for r in rows.values():
        if r.get("in_stock") is False or not r.get("type"):
            continue
        for g in ((r["gender"],) if r.get("gender") in ("men", "women") else ("men", "women")):
            types[g][r["type"]] += 1
            brands[g][r["type"]][str(r.get("brand") or "")] += 1
    data = {"types": {g: [t for t, _ in c.most_common(TYPES_MAX)] for g, c in types.items()},
            "brands": {g: {t: dict(c) for t, c in by.items()} for g, by in brands.items()}}
    _SUMMARY.update(sig=sig, dir=str(site_dir), data=data)
    return data


def _check(mark: bool, label: str) -> str:
    return ("✓ " if mark else "") + label


def _draft_groups(draft: dict) -> list[str]:
    g = draft.get("gender")
    types = draft.get("types") or draft.get("opts") or []
    got = {x for t in types for x in groups_for(t, g)}
    return [x for x in GROUP_ORDER if x in got]


def picks_screen(bot, draft: dict, site_dir: Path) -> tuple[str, dict]:
    """Текст и кнопки текущего шага анкеты."""
    m = bot.messages()
    step = draft.get("step")
    kb: list[list[dict]] = []
    if step == "gender":
        kb = [[{"text": lbl, "callback_data": f"pf:g:{k}"} for k, (_, lbl) in GENDERS.items()]]
        text = text_of(m, "picks_gender")
    elif step == "types":
        opts = draft.get("opts") or []
        row: list[dict] = []
        for i, t in enumerate(opts):
            row.append({"text": _check(t in draft.get("types", []), t[:1].upper() + t[1:]), "callback_data": f"pf:t:{i}"})
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append([{"text": "Готово", "callback_data": "pf:td"}])
        text = text_of(m, "picks_types")
    elif step == "sizes":
        grp = draft["groups"][draft.get("gi", 0)]
        label, keys = SIZE_GROUPS[grp]
        chosen = set((draft.get("sizes") or {}).get(grp) or [])
        row = []
        per = 4 if grp != "collar" else 5
        for i, k in enumerate(keys):
            row.append({"text": _check(k in chosen, k.replace("ворот ", "")), "callback_data": f"pf:z:{i}"})
            if len(row) == per:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append([{"text": "Дальше", "callback_data": "pf:zd"}])
        text = bot.render(text_of(m, "picks_sizes"), None, group=label)
    elif step == "brands":
        opts = draft.get("bopts") or []
        page = int(draft.get("page") or 0)
        pages = max(1, (len(opts) + BRANDS_PAGE - 1) // BRANDS_PAGE)
        page = min(max(page, 0), pages - 1)
        row = []
        for i in range(page * BRANDS_PAGE, min(len(opts), (page + 1) * BRANDS_PAGE)):
            row.append({"text": _check(opts[i] in draft.get("brands", []), opts[i])[:40], "callback_data": f"pf:b:{i}"})
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        if pages > 1:
            kb.append([{"text": "◀", "callback_data": f"pf:bp:{(page - 1) % pages}"},
                       {"text": f"{page + 1}/{pages}", "callback_data": f"pf:bp:{page}"},
                       {"text": "▶", "callback_data": f"pf:bp:{(page + 1) % pages}"}])
        kb.append([{"text": "Все бренды", "callback_data": "pf:ba"}, {"text": "Готово", "callback_data": "pf:bd"}])
        text = text_of(m, "picks_brands")
    elif step == "budget":
        kb = [[{"text": lbl, "callback_data": f"pf:u:{c}"}] for c, lbl, _, _ in BUDGETS]
        text = text_of(m, "picks_budget")
    else:
        return text_of(m, "picks_stale"), {"inline_keyboard": []}
    kb.append([{"text": "Отмена", "callback_data": "pf:x"}])
    return text, {"inline_keyboard": kb}


def prefs_summary(prefs: dict) -> str:
    sizes = [k for grp in GROUP_ORDER for k in (prefs.get("sizes") or {}).get(grp) or []]
    return "\n".join([
        f"Для кого: {GENDER_RU.get(prefs.get('gender'), '—')}",
        "Категории: " + (", ".join(prefs.get("types") or []) or "все"),
        "Размеры: " + (", ".join(dict.fromkeys(sizes)) or "любые"),
        "Бренды: " + (", ".join(prefs.get("brands") or []) or "все"),
        f"Бюджет: {budget_label(prefs.get('budget'))}",
    ])


def _new_draft(db, uid) -> dict:
    old = db.get_prefs(uid) if db is not None else None
    d = {"step": "gender", "types": [], "sizes": {}, "brands": []}
    if old and old.get("gender"):                          # /settings: прежние ответы отмечены
        d.update(types=list(old.get("types") or []), sizes=dict(old.get("sizes") or {}),
                 brands=list(old.get("brands") or []), prev_gender=old.get("gender"))
    return d


def start_picks(bot, chat_id, uid) -> None:
    """Новое сообщение с первым шагом анкеты (кнопка «Подобрать…» или /settings)."""
    db = bot.db
    if db is None:
        bot.send(chat_id, "Сейчас не получается — попробуйте позже.")
        return
    draft = _new_draft(db, uid)
    text, kb = picks_screen(bot, draft, _site(bot))
    res = bot.send(chat_id, text, reply_markup=kb)
    if res is not None:
        draft["mid"] = res.get("message_id") if isinstance(res, dict) else None
        db.save_draft(uid, draft)


def _site(bot) -> Path:
    return Path(getattr(bot, "site_dir", None) or SITE)


def _edit(bot, cq: dict, text: str, kb: dict | None) -> None:
    msg = cq.get("message") or {}
    bot.call("editMessageText", chat_id=(msg.get("chat") or {}).get("id"), message_id=msg.get("message_id"),
             text=text, reply_markup=kb or {"inline_keyboard": []}, disable_web_page_preview=True)


def on_picks(bot, cq: dict):
    """callback 'pf:…' — шаги анкеты (любой пользователь, только своя анкета)."""
    data = str(cq.get("data") or "")
    uid = (cq.get("from") or {}).get("id")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    db = bot.db
    if db is None or not uid:
        return "Попробуйте позже"
    m = bot.messages()
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    if action == "go":
        if hasattr(bot, "_touch"):
            bot._touch(cq.get("from") or {})
        start_picks(bot, chat_id, uid)
        return None
    prefs = db.get_prefs(uid) or {}
    draft = prefs.get("draft") or None
    if not isinstance(draft, dict) or (draft.get("mid") and msg.get("message_id") and draft["mid"] != msg.get("message_id")):
        _edit(bot, cq, text_of(m, "picks_stale"), None)
        return None
    step = draft.get("step")
    site = _site(bot)
    if action == "x":
        db.save_draft(uid, None)
        _edit(bot, cq, text_of(m, "picks_cancel"), None)
        return None
    if action == "g" and arg in GENDERS:
        gender = GENDERS[arg][0]
        summ = catalog_summary(site)
        opts = summ["types"].get(gender) or []
        if not opts:
            _edit(bot, cq, text_of(m, "picks_empty"), None)
            return None
        if draft.get("prev_gender") and draft["prev_gender"] != gender:
            draft.update(types=[], sizes={}, brands=[])
        draft.update(step="types", gender=gender, opts=opts, types=[t for t in draft.get("types", []) if t in opts])
    elif action == "t" and step == "types":
        try:
            t = draft["opts"][int(arg)]
        except (ValueError, IndexError, KeyError):
            return "Неверная кнопка"
        draft["types"] = [x for x in draft["types"] if x != t] if t in draft["types"] else draft["types"] + [t]
    elif action == "td" and step == "types":
        groups = _draft_groups(draft)
        draft["sizes"] = {g: v for g, v in (draft.get("sizes") or {}).items() if g in groups}
        if groups:
            draft.update(step="sizes", groups=groups, gi=0)
        else:
            _to_brands(draft, site)
    elif action == "z" and step == "sizes":
        grp = draft["groups"][draft.get("gi", 0)]
        try:
            k = SIZE_GROUPS[grp][1][int(arg)]
        except (ValueError, IndexError):
            return "Неверная кнопка"
        cur = list((draft.setdefault("sizes", {})).get(grp) or [])
        cur = [x for x in cur if x != k] if k in cur else cur + [k]
        order = SIZE_GROUPS[grp][1]
        draft["sizes"][grp] = sorted(cur, key=order.index)
    elif action == "zd" and step == "sizes":
        if draft.get("gi", 0) + 1 < len(draft["groups"]):
            draft["gi"] = draft.get("gi", 0) + 1
        else:
            _to_brands(draft, site)
    elif action == "b" and step == "brands":
        try:
            b = draft["bopts"][int(arg)]
        except (ValueError, IndexError, KeyError):
            return "Неверная кнопка"
        draft["brands"] = [x for x in draft["brands"] if x != b] if b in draft["brands"] else draft["brands"] + [b]
    elif action == "bp" and step == "brands":
        try:
            draft["page"] = int(arg)
        except ValueError:
            return None
    elif action in ("ba", "bd") and step == "brands":
        if action == "ba":
            draft["brands"] = []
        draft["step"] = "budget"
    elif action == "u" and step == "budget" and arg in {c for c, _, _, _ in BUDGETS}:
        db.activate_prefs(uid, draft["gender"], draft.get("types") or [], draft.get("sizes") or {},
                          draft.get("brands") or [], arg)
        saved = db.get_prefs(uid) or {}
        bot.log(f"анкета подборок сохранена ({tg_bot.uid_tag(uid)})")
        _edit(bot, cq, text_of(m, "picks_done") + "\n\n" + prefs_summary(saved), None)
        return "Сохранено"
    else:
        return "Анкета устарела — /settings"
    db.save_draft(uid, draft)
    text, kb = picks_screen(bot, draft, site)
    _edit(bot, cq, text, kb)
    return None


def _to_brands(draft: dict, site: Path) -> None:
    summ = catalog_summary(site)
    by = summ["brands"].get(draft.get("gender")) or {}
    c: Counter = Counter()
    for t in (draft.get("types") or draft.get("opts") or []):
        c.update(by.get(t) or {})
    opts = [b for b, _ in c.most_common(BRANDS_MAX) if b]
    draft.update(step="brands", bopts=opts, page=0, brands=[b for b in draft.get("brands", []) if b in opts])


def cmd_settings(bot, message: dict, args: str) -> None:
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return
    start_picks(bot, chat.get("id"), (message.get("from") or {}).get("id"))


def cmd_report(bot, message: dict, args: str) -> None:
    """/report [дней] — отчёт воронки (только админы)."""
    try:
        days = max(1, min(90, int((args or "7").split()[0])))
    except ValueError:
        days = 7
    text = build_report(bot.db, Path(getattr(bot, "events_dir", None) or EVENTS_DIR), days, site_dir=_site(bot))
    for part in split_text(text):
        bot.send((message.get("chat") or {}).get("id"), part)


def _register(mod) -> None:
    mod.register_callback("pf:", on_picks, admin_only=False)
    mod.register_command("settings", cmd_settings)
    mod.register_command("report", cmd_report, admin_only=True)


_register(tg_bot)


def tg_register(bot) -> None:
    """tg_bot.load_plugins: если бот запущен как `python tg_bot.py --run` (модуль __main__), регистрируемся и там."""
    mod = sys.modules.get(type(bot).__module__)
    if mod is not None and mod is not tg_bot and hasattr(mod, "register_callback"):
        _register(mod)


# ---------------------------------------------------------------- запуск

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Воронка: отчёт, подборки, корзины, вопрос после выдачи")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--report", action="store_true", help="отчёт воронки (печать + админам)")
    g.add_argument("--digest", action="store_true", help="личные подборки по анкетам")
    g.add_argument("--carts", action="store_true", help="напоминание о брошенных корзинах")
    g.add_argument("--followup", action="store_true", help="«Всё подошло?» через 7 дней после выдачи")
    ap.add_argument("--days", type=int, default=7, help="--report: за сколько суток (по умолчанию 7)")
    ap.add_argument("--no-send", action="store_true", help="--report: только напечатать")
    ap.add_argument("--dry", action="store_true", help="ничего не отправлять и не записывать")
    ap.add_argument("--site", default=str(SITE), help="папка сайта (по умолчанию site/)")
    args = ap.parse_args(argv)
    mode = "report" if args.report else "digest" if args.digest else "carts" if args.carts else "followup"
    lock = sync_state.Lock(DATA / f"funnel-{mode}.lock", f"funnel.py --{mode}", max_age_s=3 * 3600)
    if not lock.acquire(wait_s=0, say=_log):
        _log(f"--{mode}: уже идёт другой запуск — выхожу")
        return 0
    try:
        db = orders_db.OrdersDB(orders_db.default_path())
        try:
            f = Funnel(db, site_dir=Path(args.site), dry=args.dry)
            if f.dry and mode != "report":
                _log("пробный режим (--dry или нет TELEGRAM_BOT_TOKEN): ничего не отправляю и не записываю")
            if mode == "report":
                f.report(max(1, args.days), send=not args.no_send)
            elif mode == "digest":
                f.digest()
            elif mode == "carts":
                f.carts()
            else:
                f.followup()
        finally:
            db.close()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
