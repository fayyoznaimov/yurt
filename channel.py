"""Канал Telegram: отобранные товары из каталога сайта — сначала полуавтоматом (продавец нажимает «Опубликовать»).

    python channel.py --plan [N]         # пробный прогон: N (20) лучших с разбором оценки и точной подписью; ничего не шлёт
    python channel.py --propose N        # продавцу в личку N готовых постов с кнопками «Опубликовать» / «Пропустить»
    python channel.py --post             # автомат: один товар в канал (следующий слот дня)
    python channel.py --post-id КОД      # конкретный товар в канал (--force — мимо ворот и дедупа; дважды — никогда)
    python channel.py --sync             # посты за 14 дней: продано → «⛔ Продано» без кнопок; пропали все ходовые
                                         # размеры (2 запуска подряд) → новая строка «Размеры: …»
    python channel.py --status           # что опубликовано сегодня и что ждёт решения продавца

Отправка в Telegram — ТОЛЬКО если в окружении CHANNEL_ENABLED=1, TELEGRAM_BOT_TOKEN и CHANNEL_ID (@ник канала или
-100…). Иначе всё то же самое печатается «пробно»: в сеть ничего не уходит, состояние не меняется.
Личка продавца для --propose: CHANNEL_REVIEW_CHAT_ID, иначе первый id из TELEGRAM_ADMIN_IDS, иначе чат заказов.
Кнопки «Опубликовать/Пропустить» нажимают админы бота (TELEGRAM_ADMIN_IDS); если список пуст — и
CHANNEL_REVIEW_CHAT_ID, когда это личный id (> 0). --status предупреждает, если CHANNEL_ENABLED=1 без админов.

Откуда товары: собранный сайт (site/data: индекс целиком, подробности — только для выбранных). Закрытые файлы
site/admin читаются ТОЛЬКО ради ключа дедупликации (бренд | исходное название | цвет — в состоянии хранится лишь
хэш) и никогда не попадают ни в подписи, ни в печать.

Ворота (channel.json → gates): есть ходовой размер (ranking.py) и размеров ≥ 2, скидка ≥ 40%, цена 1,2–15 млн сум,
фото ≥ 2, в наличии; плюс хотя бы одно фото, которое можно отправить (свой файл site/img/p или фото с CDN
турецких магазинов). Фото YOOX по ссылке не скачиваются никогда — только свои файлы из ручного сбора.
Оценка: ranking.score() + свежесть (first_seen) + экономия в сумах. Квоты на день (по Ташкенту): ≤2 одного бренда,
≤2 одного типа, пол по плану М-Ж-М-М-Ж, ≤1 вещи дороже 10 млн; ключ дедупликации закрыт 14 дней; один код —
в канал только один раз.

Кнопки предложений (callback 'ch:pub:<КОД>' / 'ch:skip:<КОД>') обрабатывает бот заказов (tg_bot.py, служба
yurt-orders): модуль подключается сам (TG_BOT_PLUGINS, по умолчанию «channel»), getUpdates здесь не вызывается.
Фото уходят файлом (multipart), в состоянии хранится file_id. Перед КАЖДОЙ отправкой — проверка на утечку:
yoox, trendyol, akinon, dsmcdn, €, ₺, TL, EUR, TRY, себестоимость, маржа и т. п. — такое не отправляется
(запись в data/logs/channel_leaks.log).

Состояние — data/channel_state.json (запись атомарная), замок — data/channel.lock (свой, не data/server.lock).
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import requests

import catalog_files as cf
import ranking
import sync_state
import tg_bot

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
DATA = ROOT / "data"
CONFIG_PATH = ROOT / "channel.json"
STATE_PATH = DATA / "channel_state.json"
LOCK_PATH = DATA / "channel.lock"
LOG_DIR = DATA / "logs"
API_URL = "https://api.telegram.org/bot{token}/{method}"
DEFAULT_SITE_URL = "https://fayyoznaimov.github.io/yurt/"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
CODE_RE = re.compile(r"^[A-Z0-9]{7}$")
MIN_IMG_BYTES = 1500              # меньше — заглушка вместо фото
MAX_IMG_BYTES = 10 * 1024 * 1024  # предел Telegram для sendPhoto
SOLD_MARK = "\n\n⛔ Продано"
PLACEHOLDER_USERS = {"your_username", "username", "yourname", "example"}

# Фото по ссылке скачиваются только с CDN турецких магазинов, с браузерным UA и Referer магазина (как адаптеры).
# Хосты YOOX не скачиваются НИКОГДА (никакого автоматического доступа к YOOX) — только свои файлы site/img/p.
DOWNLOAD_HOSTS = (                # (окончание имени хоста, Referer)
    ("cdn.dsmcdn.com", "https://www.trendyol.com/"),
    ("pcardin.akinoncloudcdn.com", "https://www.pierrecardin.com.tr/"),
    ("cacharel.akinoncloudcdn.com", "https://www.cacharel.com.tr/"),
)
NEVER_FETCH = ("yoox", "ynap")

# Проверка на утечку источника и закупки перед любой отправкой (подпись, текст, кнопки, имена файлов).
LEAK_SUBSTRINGS = ("yoox", "trendyol", "akinon", "dsmcdn", "€", "₺", "себестоим", "марж", "закуп", "возврат",
                   "cost_uzs", "margin_uzs", "source_item_id", "price_now", "title_original")
LEAK_TOKENS = (
    re.compile(r"(?<![a-z0-9_])(?:tl|eur|try)(?![a-z0-9_])", re.I),          # «TL», «EUR», «try» отдельным словом
    re.compile(r"(?<![a-z0-9_])\d[\d\s.,]*(?:tl|eur|try)(?![a-z0-9_])", re.I),  # «1500TL», «99,90 EUR»
)
SEND_METHODS = {"sendPhoto", "sendMediaGroup", "sendMessage", "editMessageCaption", "editMessageText",
                "editMessageReplyMarkup", "copyMessage", "forwardMessage"}

DEFAULTS: dict = {
    "site_url": "",
    "mini_app_url": "",
    "contact_url": "",
    "gates": {"min_discount": 40, "price_min_uzs": 1_200_000, "price_max_uzs": 15_000_000, "min_sizes": 2,
              "need_popular_size": True, "min_photos": 2, "min_sendable_photos": 1, "in_stock": True},
    "score": {"rank": 1.0, "fresh": 0.15, "fresh_days": 7, "saving": 0.15, "saving_cap_uzs": 10_000_000},
    "quotas": {"per_brand": 2, "per_type": 2, "gender_plan": ["М", "Ж", "М", "М", "Ж"], "gender_fallback": True,
               "expensive_uzs": 10_000_000, "max_expensive": 1, "dedupe_days": 14},
    "caption": {"footer": "Оригинал · предоплата 50% · доставка до 10 дней", "max_len": 1000,
                "order_button": "Заказать", "contact_button": "Написать нам", "round_old_to": 10_000},
    "photos": {"album": False, "album_max": 4},
    "propose": {"count": 8, "ttl_days": 3, "skip_days": 30},
    "sync": {"days": 14, "confirm_runs": 2, "max_edits": 20},
    "send_pause_s": 1.5,
    "tz_offset_hours": 5,
}
GENDER_PLAN = {"м": "men", "m": "men", "men": "men", "муж": "men",
               "ж": "women", "w": "women", "f": "women", "women": "women", "жен": "women"}
GENDER_RU = {"men": "М", "women": "Ж"}


class LeakBlocked(RuntimeError):
    """В отправке нашлось запрещённое слово — ничего не отправлено."""


# ---------------------------------------------------------------- мелочи

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def money(n) -> str:
    """4140000 → «4 140 000»."""
    return f"{int(round(float(n))):,}".replace(",", " ")


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def round_to(x: float, step: int) -> int:
    return int((x + step / 2) // step * step) if step > 0 else int(round(x))


def old_price(price: int, disc, step: int = 10_000) -> int | None:
    """Цена до скидки по проценту скидки, округлённая до step; None — скидки нет."""
    try:
        d = float(disc)
    except (TypeError, ValueError):
        return None
    if not (0 < d < 100) or not price:
        return None
    old = round_to(price / (1 - d / 100), step)
    return old if old > price else None


def disc_label(disc) -> int:
    return int(float(disc) + 0.5)


def sort_sizes(sizes) -> list[str]:
    uniq = list(dict.fromkeys(str(s).strip() for s in sizes or [] if str(s).strip()))
    return sorted(uniq, key=sync_state.size_key)


def title_without_brand(title, brand) -> str:
    """«Женский тренч Tod's» + «Tod's» → «Женский тренч» (бренд и так стоит в начале подписи)."""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    b = str(brand or "").strip()
    if b:
        t2 = re.sub(r"(?<!\w)" + re.escape(b) + r"(?!\w)", " ", t, flags=re.I)
        t2 = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s+", " ", t2)).strip(" -–—,·")
        if t2:
            t = t2
    return t[:1].upper() + t[1:]


def dedupe_key(brand, title_original, color, title=None, price=None) -> str:
    """Хэш ключа «бренд | исходное название (без цвета после запятой и цифр) | цвет». Нет закрытых данных —
    «бренд | название на сайте | цвет | цена». Само исходное название нигде не хранится."""
    if title_original:
        t = re.sub(r"\d+", " ", str(title_original).split(",")[0])
        raw = f"{ranking.norm_key(brand)}|{ranking.norm_key(t)}|{ranking.norm_key(color)}"
    else:
        raw = f"{ranking.norm_key(brand)}|{ranking.norm_key(title)}|{ranking.norm_key(color)}|{int(price or 0)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def find_leak(*objs) -> str | None:
    """Первое запрещённое слово в текстах/кнопках/адресах (или None)."""
    for obj in objs:
        for s in _strings(obj):
            low = s.lower()
            for sub in LEAK_SUBSTRINGS:
                if sub in low:
                    return sub
            for rx in LEAK_TOKENS:
                m = rx.search(s)
                if m:
                    return m.group(0).strip()
    return None


def host_of(url: str) -> str:
    u = "https:" + url if url.startswith("//") else url
    try:
        return (urlsplit(u).hostname or "").lower()
    except ValueError:
        return ""


def referer_for(host: str) -> str | None:
    """Referer для скачивания фото с этого хоста; None — с этого хоста не качаем (YOOX и неизвестные)."""
    if not host or any(x in host for x in NEVER_FETCH):
        return None
    for suffix, ref in DOWNLOAD_HOSTS:
        if host == suffix or host.endswith("." + suffix) or host.endswith("-" + suffix):
            return ref
    return None


def fetch_url(url: str, headers: dict, timeout: float = 20) -> tuple[int, str, bytes]:
    """Скачать фото: (HTTP-код, content-type, байты). Без перенаправлений (никуда, кроме этого хоста)."""
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)
    return r.status_code, r.headers.get("content-type", ""), r.content


def load_config(path: Path | None = None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        user = {}
    for k, v in (user or {}).items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update({kk: vv for kk, vv in v.items() if not str(kk).startswith("_")})
        else:
            cfg[k] = v
    return cfg


def parse_plan(plan) -> list[str]:
    out = [GENDER_PLAN.get(str(x).strip().lower()) for x in plan or []]
    return [g for g in out if g] or ["men", "women", "men", "men", "women"]


def tg_username(s) -> str:
    u = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", str(s or "").strip()).lstrip("@").strip("/")
    return u if re.fullmatch(r"[A-Za-z0-9_]{4,32}", u) and u.lower() not in PLACEHOLDER_USERS else ""


# ---------------------------------------------------------------- Telegram

class TG:
    """Bot API: JSON или multipart (фото файлом). Проверка на утечку — перед каждой отправкой.
    dry=True — ничего не отправляет, печатает, что ушло бы (и тоже проверяет на утечку).
    http(method, params, files) → (result | None, raw) — подмена для тестов."""

    def __init__(self, token: str = "", http: Callable | None = None, dry: bool = False,
                 log: Callable = print, sleep: Callable = time.sleep):
        self.token = (token or "").strip()
        self.http = http
        self.dry = dry
        self.log = log
        self.sleep = sleep
        self.last_error: dict = {}
        self._n = 0

    def call(self, method: str, params: dict, files: dict | None = None):
        params = {k: v for k, v in params.items() if v is not None}
        if method in SEND_METHODS:
            leak = find_leak(_scan_view(params), [f[0] for f in (files or {}).values()])
            if leak:
                raise LeakBlocked(leak)
        photos = [params.get("photo")] + [m.get("media") for m in params.get("media") or [] if isinstance(m, dict)]
        if any(isinstance(p, str) and p.strip().lower().startswith(("http:", "https:", "//")) for p in photos):
            self.last_error = {"description": "фото по ссылке не отправляем — только файлом или file_id"}
            self.log(self.last_error["description"])
            return None
        if self.dry:
            return self._dry(method, params, files)
        if self.http:
            res, raw = self.http(method, params, files)
        elif not self.token:
            res, raw = None, {"description": "нет TELEGRAM_BOT_TOKEN"}
        else:
            res, raw = self._post(method, params, files)
        self.last_error = {} if res is not None else (raw or {})
        return res

    def _post(self, method: str, params: dict, files: dict | None):
        url = API_URL.format(token=self.token, method=method)
        for attempt in range(2):
            try:
                if files:
                    data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
                            for k, v in params.items()}
                    r = requests.post(url, data=data, files=files, timeout=90)
                else:
                    r = requests.post(url, json=params, timeout=30)
            except requests.RequestException as ex:
                return None, {"ok": False, "description": ex.__class__.__name__}
            try:
                data = r.json()
            except ValueError:
                data = {"ok": False, "error_code": r.status_code, "description": r.text[:200]}
            if data.get("ok"):
                return data.get("result"), data
            if r.status_code == 429 and attempt == 0:        # лимит Telegram: подождать, сколько просит, один раз
                self.sleep(min(int((data.get("parameters") or {}).get("retry_after") or 3), 30))
                continue
            return None, data
        return None, {"ok": False, "description": "429"}

    def _dry(self, method: str, params: dict, files: dict | None):
        self._n += 1
        chat = params.get("chat_id")
        text = params.get("caption") or params.get("text") or ""
        if method == "sendMediaGroup":
            media = params.get("media") or []
            text = (media[0].get("caption") if media else "") or ""
            self.log(f"[пробно] {method} → {chat}: альбом из {len(media)} фото")
        else:
            self.log(f"[пробно] {method} → {chat}" + (f" (сообщение {params['message_id']})" if params.get("message_id") else ""))
        if text:
            self.log("  | " + text.replace("\n", "\n  | "))
        kb = (params.get("reply_markup") or {}).get("inline_keyboard") if isinstance(params.get("reply_markup"), dict) else None
        if kb is not None:
            btns = [f"[{b.get('text')}{' → ' + b['url'] if b.get('url') else ''}]" for row in kb for b in row]
            self.log("  кнопки: " + (" ".join(btns) if btns else "(убрать)"))
        fake = {"message_id": -self._n, "chat": {"id": chat}, "photo": [{"file_id": f"dry-{self._n}"}]}
        if method == "sendMediaGroup":
            return [dict(fake, message_id=-self._n * 100 - i) for i in range(len(params.get("media") or []))]
        return fake if method.startswith("send") else True


def _scan_view(params: dict) -> dict:
    """Что проверять на утечку: всё, кроме служебных идентификаторов (chat_id, message_id, file_id фото —
    случайные строки Telegram, в них может случайно оказаться «-TRY-»)."""
    view = {k: v for k, v in params.items() if k not in ("chat_id", "message_id", "photo", "media")}
    if isinstance(params.get("photo"), str) and params["photo"].startswith(("http://", "https://")):
        view["photo"] = params["photo"]                    # ссылку проверяем (хотя по ссылке мы фото не шлём)
    if isinstance(params.get("media"), list):
        view["media"] = [{k: v for k, v in m.items() if k != "media" or str(v).startswith(("http://", "https://"))}
                         for m in params["media"] if isinstance(m, dict)]
    return view


def _file_id(msg) -> str | None:
    try:
        return (msg.get("photo") or [])[-1]["file_id"]
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------- каталог сайта

class Site:
    """Индекс каталога целиком (компактный), подробности — по корзинам для выбранных товаров.
    Закрытые файлы — только исходное название для ключа дедупликации."""

    def __init__(self, site: Path):
        self.dir = Path(site)
        self.manifest = cf.load_manifest(self.dir)
        self.rows: dict[str, dict] = {}
        self._det: dict[int, dict] = {}
        self._admin = None
        self.legacy = False
        if self.manifest:
            m = self.manifest
            for s in m["shards"]:
                for r in cf.decode_index(cf.unwrap((self.dir / cf.DATA_DIR / s["file"]).read_bytes()), m):
                    self.rows.setdefault(r["id"], r)
        elif (self.dir / "products.json").is_file():
            self.legacy = True
            for pid, p in cf.PublicCatalog(self.dir).items():
                imgs = [u for u in p.get("images") or [] if isinstance(u, str) and u]
                self.rows[pid] = dict(p, _n_img=len(imgs))

    def detail(self, pid: str) -> dict:
        if not self.manifest:
            return self.rows.get(pid) or {}
        d = self.manifest["detail"]
        b = cf.fnv1a(pid) & (d["shards"] - 1)
        if b not in self._det:
            try:
                self._det[b] = cf.decode_detail(cf.unwrap((self.dir / cf.DATA_DIR / d["files"][b]).read_bytes()))
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                self._det[b] = {}
        return self._det[b].get(pid) or {}

    def images(self, pid: str) -> list[str]:
        imgs = self.detail(pid).get("images") or (self.rows.get(pid) or {}).get("images") or []
        return [u for u in imgs if isinstance(u, str) and u]

    def original_title(self, pid: str) -> str | None:
        """Только исходное название (для ключа дедупликации); остальное из закрытых файлов не берётся."""
        if self._admin is None:
            try:
                self._admin = cf.AdminCatalog(self.dir)
            except (OSError, ValueError):
                self._admin = {}
        try:
            rec = self._admin.get(pid) if self._admin else None
        except (OSError, ValueError):
            rec = None
        t = (rec or {}).get("title_original") if isinstance(rec, dict) else None
        return str(t) if t else None


# ---------------------------------------------------------------- отбор

@dataclass
class Cand:
    row: dict
    rank: float
    parts: dict
    fresh: float
    age_days: float | None
    saving_uzs: int
    saving: float
    total: float
    key: str | None = None
    photos: list = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.row["id"]


@dataclass
class DayCounts:
    slot: int = 0
    brands: Counter = field(default_factory=Counter)
    types: Counter = field(default_factory=Counter)
    expensive: int = 0
    keys: set = field(default_factory=set)

    def add(self, brand, type_, price, key, expensive_uzs) -> None:
        self.slot += 1
        self.brands[ranking.norm_key(brand)] += 1
        self.types[type_ or "другое"] += 1
        if int(price or 0) > expensive_uzs:
            self.expensive += 1
        if key:
            self.keys.add(key)


class Picker:
    """Лучший кандидат, который проходит квоты дня, дедуп и проверку фото; accept() — занять слот."""

    def __init__(self, ch: "Channel", pool: list[Cand], counts: DayCounts, blocked_keys: set[str]):
        self.ch, self.pool, self.counts, self.blocked = ch, list(pool), counts, blocked_keys
        q = ch.cfg["quotas"]
        self.per_brand, self.per_type = int(q["per_brand"]), int(q["per_type"])
        self.expensive_uzs, self.max_expensive = int(q["expensive_uzs"]), int(q["max_expensive"])
        self.plan = parse_plan(q.get("gender_plan"))
        self.fallback = bool(q.get("gender_fallback", True))
        self.dropped: Counter = Counter()

    def want_gender(self) -> str:
        return self.plan[self.counts.slot % len(self.plan)]

    def _quota_ok(self, c: Cand) -> bool:
        r = self.counts
        if r.brands[ranking.norm_key(c.row.get("brand"))] >= self.per_brand:
            return False
        if r.types[c.row.get("type") or "другое"] >= self.per_type:
            return False
        if int(c.row.get("price_uzs") or 0) > self.expensive_uzs and r.expensive >= self.max_expensive:
            return False
        return True

    def _drop(self, c: Cand, why: str) -> None:
        self.pool.remove(c)
        self.dropped[why] += 1

    def next(self) -> Cand | None:
        want = self.want_gender()
        for strict in ((True, False) if self.fallback else (True,)):
            i = 0
            while i < len(self.pool):
                c = self.pool[i]
                g = c.row.get("gender")
                if not self._quota_ok(c) or (strict and g in ("men", "women") and g != want):
                    i += 1
                    continue
                if c.key is None:
                    c.key = self.ch.key_for(c.row)
                if c.key in self.blocked or c.key in self.counts.keys:
                    self._drop(c, "похожий уже был (дедуп 14 дней)")
                    continue
                c.photos = self.ch.sendable_photos(c.id)
                if len(c.photos) < int(self.ch.cfg["gates"]["min_sendable_photos"]):
                    self._drop(c, "нет фото для отправки (своего файла нет, по ссылке нельзя)")
                    continue
                return c
        return None

    def accept(self, c: Cand) -> None:
        if c in self.pool:
            self.pool.remove(c)
        self.counts.add(c.row.get("brand"), c.row.get("type"), c.row.get("price_uzs"), c.key, self.expensive_uzs)

    def reject(self, c: Cand, why: str) -> None:
        if c in self.pool:
            self._drop(c, why)


# ---------------------------------------------------------------- канал

class Channel:
    def __init__(self, site: Path | None = None, cfg: dict | None = None, config_path: Path | None = None,
                 state_path: Path | None = None, lock_path: Path | None = None, log_dir: Path | None = None,
                 env: dict | None = None, http: Callable | None = None, fetch: Callable | None = None,
                 now: datetime | Callable | None = None, ranker: ranking.Ranker | None = None,
                 log: Callable | None = None, sleep: Callable | None = None, dry: bool | None = None):
        self.site_dir = Path(site or SITE)
        self.cfg = cfg if cfg is not None else load_config(config_path)
        self.state_path = Path(state_path or STATE_PATH)
        self.lock_path = Path(lock_path or LOCK_PATH)
        self.log_dir = Path(log_dir or LOG_DIR)
        self.env = os.environ if env is None else env
        self.fetch = fetch or fetch_url
        self._now_src = now
        self.ranker = ranker or ranking.Ranker.from_config()
        self.log = log or (lambda m: print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [channel] {m}", flush=True))
        self.sleep = sleep or time.sleep
        self.token = str(self.env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self.channel_id = str(self.env.get("CHANNEL_ID") or "").strip()
        self.enabled = (str(self.env.get("CHANNEL_ENABLED") or "").strip() == "1"
                        and bool(self.token) and bool(self.channel_id))
        is_dry = (not self.enabled) if dry is None else (dry or not self.enabled)
        self.tg = TG(self.token, http=http, dry=is_dry, log=self.log, sleep=self.sleep)
        self.tz = timezone(timedelta(hours=float(self.cfg.get("tz_offset_hours", 5))))
        self._site: Site | None = None
        self.blocked_hosts: set[str] = set()
        self.downloads = 0
        self.leaks: list[tuple[str, str]] = []
        self.last_fail = ""                                # photo | telegram | leak — почему не ушёл последний пост

    # ------------------------------------------------------------ окружение и состояние

    @property
    def dry(self) -> bool:
        return self.tg.dry

    def now(self) -> datetime:
        n = self._now_src() if callable(self._now_src) else self._now_src
        return n or _now_utc()

    def local_date(self, ts) -> str | None:
        dt = _parse_ts(ts) if not isinstance(ts, datetime) else ts
        return dt.astimezone(self.tz).date().isoformat() if dt else None

    @property
    def site(self) -> Site:
        if self._site is None:
            self._site = Site(self.site_dir)
        return self._site

    def review_chat(self) -> str:
        v = str(self.env.get("CHANNEL_REVIEW_CHAT_ID") or "").strip()
        if v:
            return v
        ids = sorted(tg_bot.parse_admin_ids(self.env.get("TELEGRAM_ADMIN_IDS")), key=lambda x: (x < 0, x))
        if ids:
            return str(ids[0])
        return str(self.env.get("TELEGRAM_ORDERS_CHAT_ID") or self.env.get("TELEGRAM_CHAT_ID") or "").strip()

    def mode_line(self) -> str:
        if self.enabled and not self.dry:
            return f"режим: ПУБЛИКАЦИЯ в {self.channel_id}"
        miss = [k for k, ok in (("CHANNEL_ENABLED=1", str(self.env.get("CHANNEL_ENABLED") or "") == "1"),
                                ("TELEGRAM_BOT_TOKEN", bool(self.token)), ("CHANNEL_ID", bool(self.channel_id))) if not ok]
        return "режим: ПРОБНЫЙ — ничего не отправляю" + (f" (нет {', '.join(miss)})" if miss else "")

    def load_state(self) -> dict:
        st = sync_state.read_json(self.state_path, {}) if self.state_path.exists() else {}
        if not isinstance(st, dict):
            st = {}
        st.setdefault("version", 1)
        for k in ("posts", "keys", "proposals", "skipped", "blocked"):
            if not isinstance(st.get(k), dict):
                st[k] = {}
        return st

    def save_state(self, st: dict) -> None:
        if self.dry:
            return                                         # пробный прогон состояние не меняет
        self._prune(st)
        st["updated_at"] = _iso(self.now())
        sync_state.write_json(self.state_path, st, indent=1)

    def _prune(self, st: dict) -> None:
        now = self.now()
        dd = int(self.cfg["quotas"]["dedupe_days"])
        for k, ts in list(st["keys"].items()):
            t = _parse_ts(ts)
            if not t or now - t > timedelta(days=dd * 2):
                del st["keys"][k]
        for code, p in st["posts"].items():                 # старые посты — только отметка «был» (дважды нельзя)
            t = _parse_ts(p.get("at"))
            if t and now - t > timedelta(days=60) and "caption" in p:
                for f in ("caption", "markup", "file_ids", "sizes_line", "links_line", "sizes", "message_ids"):
                    p.pop(f, None)
        for name, days in (("proposals", 30), ("skipped", int(self.cfg["propose"]["skip_days"])), ("blocked", 30)):
            for code, v in list(st[name].items()):
                t = _parse_ts(v.get("at") if isinstance(v, dict) else v)
                if not t or now - t > timedelta(days=days):
                    del st[name][code]

    @contextmanager
    def locked(self, wait_s: float = 120, every_s: float = 2):
        """Свой замок data/channel.lock (в пробном режиме не нужен: состояние только читается)."""
        if self.dry:
            yield True
            return
        lock = sync_state.Lock(self.lock_path, "channel.py", max_age_s=3600)
        ok = lock.acquire(wait_s=wait_s, every_s=every_s, say=self.log)
        try:
            yield ok
        finally:
            if ok:
                lock.release()

    def _pause(self) -> None:
        """Пауза между отправками (лимит Telegram ~20 сообщений в минуту в один чат); пробно — без пауз."""
        if not self.dry:
            self.sleep(float(self.cfg.get("send_pause_s") or 0))

    def _leak(self, code: str, marker: str, where: str) -> None:
        self.leaks.append((code, marker))
        line = f"{_iso(self.now())} {code} {where}: найдено «{marker}» — НЕ отправлено"
        self.log("УТЕЧКА: " + line)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_dir / "channel_leaks.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------ ссылки и подпись

    def site_url(self) -> str:
        u = (str(self.cfg.get("site_url") or "").strip() or str(self.env.get("SHOP_URL") or "").strip()
             or str(self.env.get("PUBLIC_URL") or "").strip() or DEFAULT_SITE_URL)
        return u.split("#", 1)[0].rstrip("/") + "/"

    def order_url(self, code: str) -> str:
        mini = str(self.cfg.get("mini_app_url") or "").strip()
        if mini:
            return f"{mini}{'&' if '?' in mini else '?'}startapp=p_{code}"
        return f"{self.site_url()}#/catalog?p={code}"

    def contact_url(self) -> str:
        u = str(self.cfg.get("contact_url") or "").strip()
        if u.startswith(("https://", "http://", "tg://")) and "your_username" not in u.lower():
            return u
        cand = [u, self.env.get("SELLER_TELEGRAM")]
        try:
            cp = self.site_dir / "content.json"
            c = json.loads(cp.read_text(encoding="utf-8")) if cp.is_file() else {}
            cand += [c.get("orders_telegram_username"), (c.get("contacts") or {}).get("telegram")]
        except (OSError, ValueError, AttributeError):
            pass
        for x in cand:
            name = tg_username(x)
            if name:
                return f"https://t.me/{name}"
        return ""

    def post_link(self, message_id) -> str:
        c = self.channel_id
        if c.startswith("@") and message_id and int(message_id) > 0:
            return f"https://t.me/{c[1:]}/{message_id}"
        if c.startswith("-100") and message_id and int(message_id) > 0:
            return f"https://t.me/c/{c[4:]}/{message_id}"
        return ""

    def render(self, row: dict, album: bool = False) -> dict:
        """Подпись (HTML, ≤ max_len) и кнопки. album — ссылки в подписи (у альбома кнопок не бывает)."""
        cc = self.cfg["caption"]
        code = row["id"]
        brand = str(row.get("brand") or "").strip()
        title = title_without_brand(row.get("title"), brand)
        price = int(row.get("price_uzs") or 0)
        # Только наша цена и процент скидки — как на сайте. «Старую цену» не показываем: она была бы вычислена
        # из процента (цена / (1 − скидка)), а по такой цене ipakly никогда не продавал.
        price_line = f"<b>{money(price)} сум</b>"
        try:
            disc = disc_label(row.get("discount_pct") or 0) if 0 < float(row.get("discount_pct") or 0) < 100 else 0
        except (TypeError, ValueError):
            disc = 0
        if disc > 0:
            price_line += f" −{disc}%"
        sizes = sort_sizes(row.get("sizes"))
        order = self.order_url(code)
        contact = self.contact_url()
        links_line = ""
        markup = None
        if album:
            links_line = f'<a href="{html.escape(order, quote=True)}">{esc(cc["order_button"])}</a>'
            if contact:
                links_line += f' · <a href="{html.escape(contact, quote=True)}">{esc(cc["contact_button"])}</a>'
        else:
            row_btns = [{"text": cc["order_button"], "url": order}]
            if contact:
                row_btns.append({"text": cc["contact_button"], "url": contact})
            markup = {"inline_keyboard": [row_btns]}
        footer = esc(cc.get("footer") or "")
        max_len = int(cc.get("max_len") or 1000)

        def build(t: str, sz: list[str], more: bool) -> tuple[str, str]:
            sl = "Размеры: " + ", ".join(esc(s) for s in sz) + (" …" if more else "")
            head = " — ".join(x for x in (f"<b>{esc(brand)}</b>" if brand else "", esc(t)) if x)
            text = f"{head}\n\n{price_line}\n{sl}\n\n{footer}" + (f"\n\n{links_line}" if links_line else "")
            return text, sl

        text, sizes_line = build(title, sizes, False)
        k = len(sizes)
        while len(text) > max_len and k > 1:               # длинная строка размеров — первые k и «…»
            k -= 1
            text, sizes_line = build(title, sizes[:k], True)
        t = title
        while len(text) > max_len and len(t) > 10:
            t = t[: max(10, len(t) - 20)].rstrip() + "…"
            text, sizes_line = build(t, sizes[:k], k < len(sizes))
        return {"caption": text, "markup": markup, "sizes_line": sizes_line, "links_line": links_line,
                "sizes": sizes, "price": price, "discount": disc}

    # ------------------------------------------------------------ ворота, оценка, фото

    def gate(self, row: dict) -> str | None:
        """Причина отказа (или None, если товар годится в канал)."""
        g = self.cfg["gates"]
        if g.get("in_stock", True) and row.get("in_stock") is False:
            return "нет в наличии"
        sizes = [str(s).strip() for s in row.get("sizes") or [] if str(s).strip()]
        if len(sizes) < int(g["min_sizes"]):
            return "размеров меньше 2"
        if g.get("need_popular_size", True) and not ranking.has_popular_size(sizes, row.get("gender"), row.get("type")):
            return "нет ходового размера"
        if float(row.get("discount_pct") or 0) < float(g["min_discount"]):
            return "скидка меньше порога"
        p = int(row.get("price_uzs") or 0)
        if p < int(g["price_min_uzs"]) or p > int(g["price_max_uzs"]):
            return "цена вне диапазона"
        n = row.get("_n_img")
        if n is None:
            n = len([u for u in row.get("images") or [] if u])
        if int(n) < int(g["min_photos"]):
            return "фото меньше 2"
        return None

    def score(self, row: dict) -> Cand:
        now = self.now()
        s = self.cfg["score"]
        rank = self.ranker.score(row, now)
        parts = self.ranker.parts(row, now)
        fs = _parse_ts(row.get("first_seen"))
        age = (now - fs).total_seconds() / 86400 if fs else None
        fresh = _clamp(1 - max(age, 0) / float(s["fresh_days"])) if age is not None and float(s["fresh_days"]) > 0 else 0.0
        price = int(row.get("price_uzs") or 0)
        old = old_price(price, row.get("discount_pct"), int(self.cfg["caption"].get("round_old_to") or 10_000))
        saving_uzs = max((old or price) - price, 0)
        saving = _clamp(saving_uzs / float(s["saving_cap_uzs"])) if float(s["saving_cap_uzs"]) > 0 else 0.0
        total = float(s["rank"]) * rank + float(s["fresh"]) * fresh + float(s["saving"]) * saving
        return Cand(row, rank, parts, fresh, age, saving_uzs, saving, round(total, 6))

    def key_for(self, row: dict) -> str:
        return dedupe_key(row.get("brand"), self.site.original_title(row["id"]), row.get("color"),
                          title=row.get("title"), price=row.get("price_uzs"))

    def sendable_photos(self, pid: str) -> list[str]:
        """Фото, которые можно отправить: свой файл в site/ или CDN турецкого магазина (YOOX — никогда по ссылке)."""
        out = []
        root = self.site_dir.resolve()
        for u in self.site.images(pid):
            if u.startswith(("http://", "https://", "//")):
                h = host_of(u)
                if referer_for(h) and h not in self.blocked_hosts:
                    out.append(u)
                continue
            try:
                p = (self.site_dir / u).resolve()
            except (OSError, ValueError):
                continue
            if root in p.parents and p.is_file():
                out.append(u)
        return out

    def photo_bytes(self, ref: str) -> bytes | None:
        """Байты фото: свой файл или скачивание (браузерный UA + Referer магазина). 403/429 — хост до конца
        запуска больше не трогаем, без повторов."""
        if not ref.startswith(("http://", "https://", "//")):
            p = (self.site_dir / ref).resolve()
            if self.site_dir.resolve() not in p.parents:
                return None
            try:
                b = p.read_bytes()
            except OSError:
                return None
            return b if MIN_IMG_BYTES <= len(b) <= MAX_IMG_BYTES else None
        h = host_of(ref)
        ref_hdr = referer_for(h)
        if not ref_hdr or h in self.blocked_hosts:
            return None
        url = "https:" + ref if ref.startswith("//") else ref
        try:
            status, ctype, body = self.fetch(url, {"User-Agent": BROWSER_UA, "Referer": ref_hdr,
                                                   "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
        except Exception as ex:                            # сеть: это фото пропускаем
            self.log(f"фото не скачано ({ex.__class__.__name__})")
            return None
        self.downloads += 1
        if status in (403, 429):
            self.blocked_hosts.add(h)
            self.log(f"фото: магазин ответил {status} — больше не скачиваю с этого хоста в этом запуске")
            return None
        if status != 200 or not str(ctype).startswith("image") or not (MIN_IMG_BYTES <= len(body or b"") <= MAX_IMG_BYTES):
            return None
        return body

    def candidates(self, st: dict, exclude: set[str] | None = None) -> tuple[list[Cand], Counter]:
        """Прошедшие ворота и не исключённые (публиковались, ждут решения, пропущены), по убыванию оценки."""
        now = self.now()
        stats: Counter = Counter()
        excl = set(exclude or ())
        excl |= set(st["posts"])
        ttl = timedelta(days=int(self.cfg["propose"]["ttl_days"]))
        for code, pr in st["proposals"].items():
            t = _parse_ts(pr.get("at"))
            if pr.get("status") == "pending" and t and now - t <= ttl:
                excl.add(code)
        skip = timedelta(days=int(self.cfg["propose"]["skip_days"]))
        for name in ("skipped", "blocked"):
            for code, v in st[name].items():
                t = _parse_ts(v.get("at") if isinstance(v, dict) else v)
                if t and now - t <= skip:
                    excl.add(code)
        out = []
        for pid, row in self.site.rows.items():
            why = self.gate(row)
            if why:
                stats[why] += 1
                continue
            if pid in excl:
                stats["уже был / ждёт решения / пропущен"] += 1
                continue
            out.append(self.score(row))
        out.sort(key=lambda c: (-c.total, -(c.row.get("_r") or 0), c.id))
        stats["прошли ворота"] = len(out)
        return out, stats

    def day_counts(self, st: dict) -> DayCounts:
        """Что уже занято сегодня (по Ташкенту): посты дня и сегодняшние предложения, ждущие решения."""
        today = self.local_date(self.now())
        exp = int(self.cfg["quotas"]["expensive_uzs"])
        dc = DayCounts()
        for p in st["posts"].values():
            if self.local_date(p.get("at")) == today:
                dc.add(p.get("brand"), p.get("type"), p.get("price"), p.get("key"), exp)
        for code, pr in st["proposals"].items():
            if pr.get("status") == "pending" and self.local_date(pr.get("at")) == today and code not in st["posts"]:
                dc.add(pr.get("brand"), pr.get("type"), pr.get("price"), pr.get("key"), exp)
        return dc

    def blocked_keys(self, st: dict) -> set[str]:
        now = self.now()
        days = timedelta(days=int(self.cfg["quotas"]["dedupe_days"]))
        return {k for k, ts in st["keys"].items() if (t := _parse_ts(ts)) and now - t < days}

    def picker(self, st: dict, exclude: set[str] | None = None) -> tuple[Picker, Counter]:
        pool, stats = self.candidates(st, exclude)
        return Picker(self, pool, self.day_counts(st), self.blocked_keys(st)), stats

    # ------------------------------------------------------------ отправка

    def _media(self, refs: list[str], file_ids: list[str]) -> list:
        """Список фото для отправки: file_id (уже загружено) или (имя, байты, тип) для multipart."""
        out: list = list(file_ids)
        for ref in refs[len(file_ids):]:
            if self.dry:
                out.append(("photo.jpg", b"", "image/jpeg"))      # пробно: ничего не читаем и не скачиваем
                continue
            b = self.photo_bytes(ref)
            if b:
                ext = os.path.splitext(urlsplit(ref).path)[1].lower() if ref.startswith(("http", "//")) else Path(ref).suffix.lower()
                ctype = mimetypes.types_map.get(ext, "image/jpeg")
                out.append((f"photo{len(out) + 1}{ext if ext in ('.jpg', '.jpeg', '.png', '.webp') else '.jpg'}", b, ctype))
        return out

    def send_photo(self, chat_id, media_item, caption: str, markup: dict | None):
        params = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML", "reply_markup": markup}
        if isinstance(media_item, str):
            return self.tg.call("sendPhoto", dict(params, photo=media_item))
        return self.tg.call("sendPhoto", params, files={"photo": media_item})

    def send_album(self, chat_id, media: list, caption: str):
        items, files = [], {}
        for i, m in enumerate(media):
            if isinstance(m, str):
                ent = {"type": "photo", "media": m}
            else:
                files[f"p{i}"] = m
                ent = {"type": "photo", "media": f"attach://p{i}"}
            if i == 0:
                ent.update(caption=caption, parse_mode="HTML")
            items.append(ent)
        return self.tg.call("sendMediaGroup", {"chat_id": chat_id, "media": items}, files=files or None)

    def publish(self, st: dict, row: dict, refs: list[str], file_ids: list[str] | None = None,
                key: str | None = None) -> dict | None:
        """В канал: фото (альбом или одно) + подпись + кнопки. Запись в состояние. None — не получилось."""
        code = row["id"]
        ph = self.cfg["photos"]
        album = bool(ph.get("album"))
        want = max(1, int(ph.get("album_max") or 4)) if album else 1
        self.last_fail = ""
        media = self._media(refs[:want], list(file_ids or [])[:want])
        if not media:
            self.last_fail = "photo"
            self.log(f"{code}: нет фото, которое можно отправить — пропуск")
            return None
        use_album = album and len(media) >= 2
        r = self.render(row, album=use_album)
        try:
            if use_album:
                res = self.send_album(self.channel_id or "@канал", media, r["caption"])
                msgs = res if isinstance(res, list) else []
            else:
                res = self.send_photo(self.channel_id or "@канал", media[0], r["caption"], r["markup"])
                msgs = [res] if isinstance(res, dict) else []
        except LeakBlocked as ex:
            self.last_fail = "leak"
            self._leak(code, str(ex), "пост в канал")
            st["blocked"][code] = {"at": _iso(self.now()), "marker": str(ex)}
            return None
        if not msgs:
            self.last_fail = "telegram"
            self.log(f"{code}: Telegram не принял пост ({str(self.tg.last_error.get('description') or '?')[:160]})")
            return None
        key = key or self.key_for(row)
        now = _iso(self.now())
        post = {"at": now, "chat_id": str(self.channel_id), "message_id": msgs[0].get("message_id"),
                "message_ids": [m.get("message_id") for m in msgs], "album": use_album,
                "brand": row.get("brand"), "type": row.get("type"), "gender": row.get("gender"),
                "price": r["price"], "key": key, "caption": r["caption"], "markup": r["markup"],
                "sizes_line": r["sizes_line"], "links_line": r["links_line"], "sizes": r["sizes"],
                "file_ids": [f for f in (_file_id(m) for m in msgs) if f], "status": "live", "miss": 0, "pop_gone": 0}
        st["posts"][code] = post
        st["keys"][key] = now
        self.log(f"{code}: опубликовано ({row.get('brand')}, {money(r['price'])} сум)"
                 + (f" — {self.post_link(post['message_id'])}" if self.post_link(post["message_id"]) else ""))
        return post

    # ------------------------------------------------------------ режимы

    def plan(self, n: int = 20, out: Callable = print) -> list[Cand]:
        """Пробный прогон: что ушло бы сейчас (квоты дня соблюдены), с разбором оценки и точной подписью."""
        st = self.load_state()
        pk, stats = self.picker(st)
        out(f"Каталог: {len(self.site.rows)} товаров; {self.mode_line()}")
        out("Ворота: " + ", ".join(f"{k} — {v}" for k, v in stats.most_common()))
        dc = pk.counts
        if dc.slot:
            out(f"Сегодня уже занято слотов: {dc.slot} (бренды {dict(dc.brands)}, типы {dict(dc.types)})")
        picked = []
        while len(picked) < n:
            want = pk.want_gender()
            c = pk.next()
            if c is None:
                break
            pk.accept(c)
            picked.append((c, want))
        if pk.dropped:
            out("Отсеяно при отборе: " + ", ".join(f"{k} — {v}" for k, v in pk.dropped.most_common()))
        if len(picked) < n:
            out(f"Квоты дня позволяют только {len(picked)} из {n}.")
        album = bool(self.cfg["photos"].get("album"))
        for i, (c, want) in enumerate(picked, 1):
            row = c.row
            p = c.parts
            local = sum(1 for u in c.photos if not u.startswith(("http", "//")))
            out("")
            out(f"{i:2}. {c.id} · {row.get('brand')} · {row.get('type') or '—'} · {GENDER_RU.get(row.get('gender'), '—')}"
                f" (слот {GENDER_RU[want]}) · {money(row.get('price_uzs') or 0)} сум −{disc_label(row.get('discount_pct') or 0)}%"
                f" · фото {row.get('_n_img')} (к отправке {len(c.photos)}, своих {local})")
            out(f"    итог {c.total:.3f} = рейтинг {c.rank:.3f} [скидка {p['disc']:.2f}, бренд {p['brand']:.2f}, "
                f"размеры {p['size']:.2f}, цена {p['price']:.2f}, сезон {p['season']:.2f}, фото {p['photos']:.2f}]"
                f" + свежесть {c.fresh:.2f}×{self.cfg['score']['fresh']}"
                f" ({'?' if c.age_days is None else f'{c.age_days:.1f} дн.'})"
                f" + экономия {c.saving:.2f}×{self.cfg['score']['saving']} ({money(c.saving_uzs)} сум)")
            r = self.render(row, album=album and len(c.photos) >= 2)
            leak = find_leak(r["caption"], r["markup"])
            if leak:
                out(f"    УТЕЧКА «{leak}» — такой пост НЕ будет отправлен")
            out("    " + r["caption"].replace("\n", "\n    "))
            if r["markup"]:
                out("    кнопки: " + " ".join(f"[{b['text']} → {b['url']}]" for b in r["markup"]["inline_keyboard"][0]))
        return [c for c, _ in picked]

    def propose(self, n: int) -> int:
        """Продавцу в личку n готовых постов с кнопками. Возвращает, сколько отправлено."""
        chat = self.review_chat()
        self.last_fail = ""
        if not self.dry and not chat:
            self.last_fail = "config"
            self.log("Некуда слать предложения: задайте CHANNEL_REVIEW_CHAT_ID (или TELEGRAM_ADMIN_IDS)")
            return 0
        with self.locked() as ok:
            if not ok:
                self.log("Другой запуск channel.py ещё идёт — пропускаю")
                return 0
            st = self.load_state()
            pk, stats = self.picker(st)
            self.log(f"{self.mode_line()}; кандидатов {stats['прошли ворота']}, предлагаю {n}")
            sent = tries = 0
            while sent < n and tries < n * 4:
                tries += 1
                c = pk.next()
                if c is None:
                    break
                res = self._send_proposal(st, c, chat or "продавец")
                if res is True:
                    pk.accept(c)
                    sent += 1
                    self.save_state(st)
                    self._pause()
                elif res == "Telegram не принял":            # чат недоступен / бот заблокирован — не повторяем
                    break
                else:
                    pk.reject(c, res or "не отправлено")
            if sent < n:
                self.log(f"Отправлено {sent} из {n}" + (f"; отсеяно: {dict(pk.dropped)}" if pk.dropped else ""))
            self.save_state(st)
        return sent

    def _send_proposal(self, st: dict, c: Cand, chat) -> bool | str:
        code = c.id
        media = self._media(c.photos[:1], [])
        if not media:
            return "фото не получено"
        r = self.render(c.row, album=bool(self.cfg["photos"].get("album")) and len(c.photos) >= 2)
        info = (f"\n\n<i>Предложение для канала · оценка {c.total:.2f} · свежесть {c.fresh:.2f} · "
                f"экономия {money(c.saving_uzs)} сум</i>")
        kb = {"inline_keyboard": [[{"text": "Опубликовать", "callback_data": f"ch:pub:{code}"},
                                   {"text": "Пропустить", "callback_data": f"ch:skip:{code}"}],
                                  [{"text": "Открыть на сайте", "url": f"{self.site_url()}#/catalog?p={code}"}]]}
        caption = r["caption"] + info
        if len(caption) > 1024:
            caption = r["caption"]
        try:
            msg = self.send_photo(chat, media[0], caption, kb)
        except LeakBlocked as ex:
            self._leak(code, str(ex), "предложение продавцу")
            st["blocked"][code] = {"at": _iso(self.now()), "marker": str(ex)}
            return "утечка"
        if not isinstance(msg, dict):
            self.last_fail = "telegram"
            self.log(f"{code}: предложение не отправлено ({str(self.tg.last_error.get('description') or '?')[:160]})")
            return "Telegram не принял"
        st["proposals"][code] = {"at": _iso(self.now()), "status": "pending", "chat_id": str(chat),
                                 "message_id": msg.get("message_id"), "file_ids": [f for f in [_file_id(msg)] if f],
                                 "brand": c.row.get("brand"), "type": c.row.get("type"),
                                 "gender": c.row.get("gender"), "price": c.row.get("price_uzs"), "key": c.key,
                                 "score": c.total}
        return True

    def post_auto(self) -> dict | None:
        """Один слот: лучший товар по квотам дня — сразу в канал."""
        with self.locked() as ok:
            if not ok:
                self.log("Другой запуск channel.py ещё идёт — пропускаю")
                return None
            st = self.load_state()
            pk, stats = self.picker(st)
            self.log(f"{self.mode_line()}; кандидатов {stats['прошли ворота']}, слот {GENDER_RU[pk.want_gender()]}")
            for _ in range(10):
                c = pk.next()
                if c is None:
                    break
                post = self.publish(st, c.row, c.photos, key=c.key)
                if post:
                    self.save_state(st)
                    return post
                if self.last_fail == "telegram":              # бот не админ канала, неверный CHANNEL_ID… — не долбим
                    break
                pk.reject(c, "не отправлено")
            self.save_state(st)
            self.log("Сегодня публиковать нечего (квоты дня или нет подходящих товаров)")
        return None

    def post_id(self, code: str, force: bool = False) -> dict | None:
        code = code.strip().upper()
        with self.locked() as ok:
            if not ok:
                self.log("Другой запуск channel.py ещё идёт — пропускаю")
                return None
            st = self.load_state()
            if code in st["posts"]:
                self.log(f"{code}: уже публиковался {st['posts'][code].get('at')} — дважды не публикую")
                return None
            row = self.site.rows.get(code)
            if row is None:
                self.log(f"{code}: такого товара нет в каталоге сайта")
                return None
            if row.get("in_stock") is False:
                self.log(f"{code}: нет в наличии")
                return None
            why = self.gate(row)
            if why and not force:
                self.log(f"{code}: не проходит ворота ({why}); --force — опубликовать всё равно")
                return None
            key = self.key_for(row)
            if key in self.blocked_keys(st) and not force:
                self.log(f"{code}: похожий товар уже был в канале за {self.cfg['quotas']['dedupe_days']} дней; --force — всё равно")
                return None
            refs = self.sendable_photos(code)
            post = self.publish(st, row, refs, key=key)
            self.save_state(st)
            return post

    def sync(self) -> Counter:
        """Посты за sync.days: продано → «⛔ Продано» и без кнопок; все ходовые размеры пропали 2 запуска подряд →
        новая строка размеров. Пропал из каталога — тоже 2 запуска подряд (разовое мигание не трогаем)."""
        res: Counter = Counter()
        sc = self.cfg["sync"]
        confirm = max(1, int(sc.get("confirm_runs") or 2))
        max_edits = int(sc.get("max_edits") or 20)
        with self.locked() as ok:
            if not ok:
                self.log("Другой запуск channel.py ещё идёт — пропускаю")
                return res
            st = self.load_state()
            now = self.now()
            rows = self.site.rows
            edits = 0
            for code, p in st["posts"].items():
                t = _parse_ts(p.get("at"))
                if p.get("status") != "live" or not t or now - t > timedelta(days=int(sc["days"])) or "caption" not in p:
                    continue
                res["проверено"] += 1
                row = rows.get(code)
                if row is None:
                    p["miss"] = int(p.get("miss") or 0) + 1
                    sold = p["miss"] >= confirm
                else:
                    p["miss"] = 0
                    sold = row.get("in_stock") is False
                if sold:
                    if edits >= max_edits:
                        res["отложено (лимит правок)"] += 1
                        continue
                    edits += 1
                    caption = p["caption"]
                    if p.get("links_line"):
                        caption = caption.replace("\n\n" + p["links_line"], "")
                    caption += SOLD_MARK
                    st_ = self._edit(code, p, caption, keep_markup=False)
                    if st_ == "ok":
                        p["status"], p["sold_at"], p["caption"] = "sold", _iso(now), caption
                        res["продано"] += 1
                    elif st_ == "gone":
                        p["status"] = "gone"
                        res["пост удалён"] += 1
                    else:
                        res["ошибка правки"] += 1
                    self._pause()
                    continue
                if row is None:
                    res["нет в каталоге (жду подтверждения)"] += 1
                    continue
                pop = [s for s in p.get("sizes") or [] if ranking.popular_size(s, row.get("gender"), row.get("type"))]
                cur = set(str(s).strip() for s in row.get("sizes") or [])
                if pop and not any(s in cur for s in pop):
                    p["pop_gone"] = int(p.get("pop_gone") or 0) + 1
                    if p["pop_gone"] < confirm:
                        res["ходовые размеры пропали (жду подтверждения)"] += 1
                        continue
                    if edits >= max_edits:
                        res["отложено (лимит правок)"] += 1
                        continue
                    edits += 1
                    sizes = sort_sizes(row.get("sizes"))
                    new_line = "Размеры: " + ", ".join(esc(s) for s in sizes)
                    caption = p["caption"].replace(p.get("sizes_line") or "\u0000", new_line, 1)
                    st_ = self._edit(code, p, caption, keep_markup=True)
                    if st_ == "ok":
                        p.update(caption=caption, sizes_line=new_line, sizes=sizes, pop_gone=0, sizes_updated_at=_iso(now))
                        res["строка размеров обновлена"] += 1
                    elif st_ == "gone":
                        p["status"] = "gone"
                        res["пост удалён"] += 1
                    else:
                        res["ошибка правки"] += 1
                    self._pause()
                else:
                    p["pop_gone"] = 0
            self.save_state(st)
        self.log(f"{self.mode_line()}; синхронизация: " + (", ".join(f"{k} — {v}" for k, v in res.items()) or "постов нет"))
        return res

    def _edit(self, code: str, p: dict, caption: str, keep_markup: bool) -> str:
        """'ok' | 'gone' (сообщения больше нет) | 'error'."""
        params = {"chat_id": p.get("chat_id") or self.channel_id, "message_id": p.get("message_id"),
                  "caption": caption, "parse_mode": "HTML"}
        if not p.get("album"):
            params["reply_markup"] = p.get("markup") if keep_markup and p.get("markup") else {"inline_keyboard": []}
        try:
            res = self.tg.call("editMessageCaption", params)
        except LeakBlocked as ex:
            self._leak(code, str(ex), "правка поста")
            return "error"
        if res is not None:
            return "ok"
        desc = str(self.tg.last_error.get("description") or "").lower()
        if "not modified" in desc:
            return "ok"
        if any(x in desc for x in ("message to edit not found", "message not found", "message_id_invalid",
                                   "message can't be edited")):
            return "gone"
        self.log(f"{code}: правка не прошла ({desc[:160]})")
        return "error"

    # ------------------------------------------------------------ кнопки предложений (из бота)

    def _mark_proposal(self, msg: dict, text: str, url: str = "") -> None:
        chat = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")
        if not (chat and mid):
            return
        btn = {"text": text, "url": url} if url else {"text": text, "callback_data": "ch:done"}
        try:
            self.tg.call("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid,
                                                    "reply_markup": {"inline_keyboard": [[btn]]}})
        except LeakBlocked as ex:
            self._leak("-", str(ex), "кнопки предложения")

    def publish_proposal(self, code: str, msg: dict | None = None) -> str:
        msg = msg or {}
        if not self.enabled:
            return "Канал выключен: нужны CHANNEL_ENABLED=1 и CHANNEL_ID"
        with self.locked(wait_s=15, every_s=0.5) as ok:
            if not ok:
                return "Канал занят другим запуском — нажмите ещё раз через минуту"
            st = self.load_state()
            if code in st["posts"]:
                self._mark_proposal(msg, "✅ Уже в канале", self.post_link(st["posts"][code].get("message_id")))
                return "Уже опубликовано"
            pr = st["proposals"].get(code) or {}
            t = _parse_ts(pr.get("at"))
            if t and self.now() - t > timedelta(days=int(self.cfg["propose"]["ttl_days"])):
                pr["status"] = "expired"
                self.save_state(st)
                self._mark_proposal(msg, "⌛ Устарело")
                return "Предложение устарело — дождитесь новых"
            row = self.site.rows.get(code)
            if row is None or row.get("in_stock") is False:
                if pr:
                    pr["status"] = "sold"
                    self.save_state(st)
                self._mark_proposal(msg, "⛔ Уже продано")
                return "Товар уже продан"
            why = self.gate(row)
            if why:
                self._mark_proposal(msg, f"✋ Не подходит: {why}")
                return f"Условия изменились: {why}"
            key = self.key_for(row)
            if key in self.blocked_keys(st):
                self._mark_proposal(msg, "✋ Похожий уже был")
                return "Похожий товар уже был в канале за 14 дней"
            refs = self.sendable_photos(code)
            if not refs and not pr.get("file_ids"):
                return "Нет фото для отправки"
            if not refs:
                refs = ["(file_id)"]
            post = self.publish(st, row, refs, file_ids=pr.get("file_ids") or [], key=key)
            if not post:
                self.save_state(st)
                return "Не получилось опубликовать — подробности в журнале"
            if pr:
                pr["status"] = "published"
                pr["decided_at"] = _iso(self.now())
            self.save_state(st)
            self._mark_proposal(msg, "✅ Опубликовано", self.post_link(post.get("message_id")))
            return "Опубликовано в канале"

    def skip_proposal(self, code: str, msg: dict | None = None) -> str:
        with self.locked(wait_s=15, every_s=0.5) as ok:
            if not ok:
                return "Канал занят другим запуском — нажмите ещё раз через минуту"
            st = self.load_state()
            now = _iso(self.now())
            st["skipped"][code] = {"at": now}
            pr = st["proposals"].get(code)
            if pr and pr.get("status") == "pending":
                pr["status"] = "skipped"
                pr["decided_at"] = now
            self.save_state(st)
        self._mark_proposal(msg or {}, "⏭ Пропущено")
        return "Пропущено — этот товар больше не предложу"

    def status_text(self) -> str:
        st = self.load_state()
        today = self.local_date(self.now())
        posts_today = [c for c, p in st["posts"].items() if self.local_date(p.get("at")) == today]
        pending = [c for c, p in st["proposals"].items() if p.get("status") == "pending"]
        live = sum(1 for p in st["posts"].values() if p.get("status") == "live")
        sold = sum(1 for p in st["posts"].values() if p.get("status") == "sold")
        warn = tg_bot.channel_admin_warning(self.env)
        return (f"Канал: {self.mode_line()}\nСегодня опубликовано: {len(posts_today)}"
                + (f" ({', '.join(posts_today)})" if posts_today else "")
                + f"\nЖдут решения: {len(pending)}\nВсего постов: {len(st['posts'])} (в продаже {live}, продано {sold})"
                + (f"\nВНИМАНИЕ: {warn}" if warn else ""))


# ---------------------------------------------------------------- подключение к боту заказов (tg_bot.py)

def _bot_http(bot) -> Callable | None:
    h = getattr(bot, "http", None)
    if not h:
        return None
    return lambda method, params, files=None: h(method, params)


def _channel_for_bot(bot) -> Channel:
    token = getattr(bot, "token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    env = dict(os.environ)
    if token:
        env["TELEGRAM_BOT_TOKEN"] = token
    return Channel(env=env, http=_bot_http(bot), log=getattr(bot, "log", None))


def on_callback(bot, cq: dict):
    """callback 'ch:pub:<КОД>' / 'ch:skip:<КОД>' / 'ch:done' (только админы — проверяет tg_bot)."""
    parts = str(cq.get("data") or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "done":
        return "Уже обработано"
    code = parts[2].strip().upper() if len(parts) > 2 else ""
    if action not in ("pub", "skip") or not CODE_RE.match(code):
        return "Неверная кнопка"
    msg = cq.get("message") or {}
    try:
        ch = _channel_for_bot(bot)
        if action == "pub":
            return ch.publish_proposal(code, msg)
        return ch.skip_proposal(code, msg)
    except sync_state.LockStuck:                            # SystemExit — tg_bot его не ловит
        return "Канал занят зависшим запуском — посмотрите журнал"


def on_command(bot, message: dict, args: str):
    """/channel — состояние канала (только админы)."""
    chat = (message.get("chat") or {}).get("id")
    text = _channel_for_bot(bot).status_text()
    if find_leak(text):                                    # проверка и для служебного ответа
        text = "Состояние канала: подробности в журнале службы"
    bot.send(chat, text)


def _register(mod) -> None:
    mod.register_callback("ch:", on_callback, admin_only=True)
    mod.register_command("channel", on_command, admin_only=True)


_register(tg_bot)


def tg_register(bot) -> None:
    """Вызывает tg_bot.load_plugins с работающим ботом. Если бот запущен как `python tg_bot.py --run`
    (модуль __main__, а не tg_bot), регистрируемся и в его реестре."""
    mod = sys.modules.get(type(bot).__module__)
    if mod is not None and mod is not tg_bot and hasattr(mod, "register_callback"):
        _register(mod)


# ---------------------------------------------------------------- запуск

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Канал Telegram: отбор и публикация товаров")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", nargs="?", const=20, type=int, metavar="N", help="пробный прогон: N лучших (20)")
    g.add_argument("--propose", nargs="?", const=0, type=int, metavar="N",
                   help="прислать продавцу N постов с кнопками (без N — propose.count из channel.json)")
    g.add_argument("--post", action="store_true", help="опубликовать один товар (следующий слот)")
    g.add_argument("--post-id", metavar="КОД", help="опубликовать конкретный товар")
    g.add_argument("--sync", action="store_true", help="отметить проданное, обновить размеры")
    g.add_argument("--status", action="store_true", help="состояние канала")
    ap.add_argument("--force", action="store_true", help="--post-id: мимо ворот и дедупа (дважды — всё равно нет)")
    ap.add_argument("--site", help="папка сайта (по умолчанию site/)")
    ap.add_argument("--config", help="файл настроек (по умолчанию channel.json)")
    args = ap.parse_args(argv)
    try:
        ch = Channel(site=Path(args.site) if args.site else None,
                     config_path=Path(args.config) if args.config else None,
                     dry=True if args.plan is not None or args.status else None)
        if ch.site.manifest is None and not ch.site.legacy:
            print(f"Каталог не найден в {ch.site_dir} (нужен собранный сайт: python run.py)")
            return 1
        if args.plan is not None:
            ch.plan(max(1, args.plan))
            return 0
        if args.status:
            print(ch.status_text())
            return 0
        if args.propose is not None:                      # нечего предложить — не ошибка; ошибка Telegram/настроек — 1
            ch.propose(args.propose if args.propose > 0 else max(1, int(ch.cfg["propose"].get("count") or 8)))
            return 1 if ch.last_fail in ("telegram", "config") else 0
        if args.post:
            ch.post_auto()
            return 1 if ch.last_fail == "telegram" else 0
        if args.post_id:
            return 0 if ch.post_id(args.post_id, force=args.force) else 1
        if args.sync:
            r = ch.sync()
            return 1 if r.get("ошибка правки") else 0
    except sync_state.LockStuck as ex:
        print(str(ex))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
