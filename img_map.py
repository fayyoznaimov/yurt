"""Свой хост фото: непрозрачные токены вместо адресов магазинов + закрытая карта «токен → исходное фото».

Покупатель не должен видеть в адресах фото домены магазинов (yoox.com, akinoncloudcdn.com, dsmcdn.com). Когда задан
адрес своего хоста фото — переменная окружения IMG_BASE (или config.json → images.base), например
https://img.example.uz (или https://orders.example.uz/img, если поддомена нет), — сборка (catalog_files.write_site)
пишет в публичные данные вместо адреса каждого фото токен из 16 латинских букв и цифр:

    токен = base32(HMAC-SHA256(IMG_SECRET, "<код товара>\\n<номер фото>\\n<исходный адрес>"))[:16]

Сайт строит адрес сам: IMG_BASE + "/p/" + токен + "." + ширина + ".webp", ширина 160 (миниатюры), 480 (карточки),
960 (окно товара). По токену нельзя узнать ни магазин, ни адрес: нужен секрет IMG_SECRET (только в
/etc/yurt/yurt.env). Поменялся исходный адрес фото — меняется и токен (старые кэши не мешают).

Карта data/img_map.json (data/ в .gitignore, deploy.py её не публикует) — для img_api.py, который по токену берёт
оригинал, и для channel.py (фото в канал):

    {"v": 1, "base": "...", "updated_at": "...", "items": {"<токен>": ["<исходный адрес или путь в site/>",
                                                                       "<код товара>", <номер фото с 0>, <день>]}}

<день> — номер суток (unix-время // 86400), когда токен последний раз был в сборке; токены, которых нет в сборке
дольше KEEP_DAYS суток, из карты убираются (их кэш удаляет server/cleanup.py).
IMG_BASE не задан — всё как раньше (адреса фото в данных как есть), а токены из прошлых сборок (снимки распроданных)
переводятся обратно по карте.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

TOKEN_RE = re.compile(r"^[a-z2-7]{16}$")
WIDTHS = (160, 480, 960)
FULL_W = 960                      # ширина фото в окне товара (её адрес — «канонический», от него карточки и миниатюры)
MAP_NAME = "img_map.json"
KEEP_DAYS = 14                    # токен, которого нет в сборках дольше, убирается из карты


def settings(cfg: dict | None = None) -> dict:
    """{"base": адрес своего хоста фото без «/» на конце или "", "secret": IMG_SECRET или ""}.
    IMG_BASE из окружения важнее config.json → images.base. Секрет — только из окружения (в git его нет)."""
    base = os.environ.get("IMG_BASE") or str(((cfg or {}).get("images") or {}).get("base") or "")
    return {"base": base.strip().rstrip("/"), "secret": (os.environ.get("IMG_SECRET") or "").strip()}


def is_token(s) -> bool:
    return isinstance(s, str) and bool(TOKEN_RE.match(s))


def norm_src(u: str) -> str:
    u = u.strip()
    return "https:" + u if u.startswith("//") else u


def make_token(secret: str, pid: str, n: int, src: str) -> str:
    d = hmac.new(secret.encode("utf-8"), f"{pid}\n{n}\n{src}".encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(d[:10]).decode("ascii").lower()          # 80 бит → 16 знаков [a-z2-7]


def url_for(base: str, token: str, w: int = FULL_W) -> str:
    return f"{base.rstrip('/')}/p/{token}.{int(w)}.webp"


def today() -> int:
    return int(time.time() // 86400)


def load_items(path: Path) -> dict[str, list]:
    """Карта {токен: [источник, код, номер, день]}; файла нет / битый — пусто."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    return {t: r for t, r in items.items() if is_token(t) and isinstance(r, list) and r and isinstance(r[0], str)}


def map_path(data_dir: Path) -> Path:
    return Path(os.environ.get("IMG_MAP") or Path(data_dir) / MAP_NAME)


def detokenize(images, items: dict[str, list]) -> list[str]:
    """Токены → исходные адреса по карте (сборка без своего хоста, channel.py); неизвестный токен — выбрасывается."""
    out = []
    for u in images or []:
        if is_token(u):
            rec = items.get(u)
            if rec:
                out.append(rec[0])
        elif isinstance(u, str) and u:
            out.append(u)
    return out


class Tokenizer:
    """Токены фото для одной сборки. tokenize() — на каждую карточку, save() — записать карту (до файлов сайта)."""

    def __init__(self, base: str, secret: str, data_dir: Path, day: int | None = None):
        if not secret:
            raise ValueError("IMG_SECRET пуст")
        self.base, self.secret = base.rstrip("/"), secret
        self.path = map_path(data_dir)
        self.day = today() if day is None else day
        self.old = load_items(self.path)
        self.items: dict[str, list] = {}

    @classmethod
    def from_settings(cls, st: dict, data_dir: Path) -> "Tokenizer | None":
        if not st.get("base"):
            return None
        if not st.get("secret"):
            raise SystemExit("Задан свой хост фото (IMG_BASE), но нет секрета IMG_SECRET — без него токены фото "
                             "можно было бы подобрать. Впишите в /etc/yurt/yurt.env строку IMG_SECRET=<длинная случайная "
                             "строка> (setup.sh делает это сам) или уберите IMG_BASE.")
        return cls(st["base"], st["secret"], data_dir)

    def token(self, pid: str, n: int, src: str) -> str:
        src = norm_src(src)
        tok = make_token(self.secret, pid, n, src)
        self.items[tok] = [src, pid, n, self.day]
        return tok

    def tokenize(self, pid: str, images) -> list[str]:
        """Фото одной карточки → токены. Уже токен (снимок распроданного из прошлой сборки) — остаётся, если он
        есть в карте; неизвестный токен выбрасывается (показать его всё равно нечем)."""
        out = []
        for u in images or []:
            if not isinstance(u, str) or not u.strip():
                continue
            if is_token(u):
                rec = self.items.get(u) or self.old.get(u)
                if rec:
                    self.items[u] = [rec[0], rec[1], rec[2], self.day]
                    out.append(u)
                continue
            out.append(self.token(pid, len(out), u))
        return out

    def info(self) -> dict:
        """Что положить в manifest.json → img (сайт строит адреса фото по нему)."""
        return {"base": self.base, "w": list(WIDTHS), "full": FULL_W, "fmt": "webp"}

    def save(self) -> int:
        """Записать карту: токены этой сборки + недавние из прошлых (страница, открытая до публикации, ещё может их
        попросить). Права 600 — карта выдаёт источники. Возвращает число записей."""
        merged = {t: r for t, r in self.old.items()
                  if t not in self.items and len(r) > 3 and isinstance(r[3], int) and self.day - r[3] <= KEEP_DAYS}
        merged.update(self.items)
        data = {"v": 1, "base": self.base, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "items": merged}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        return len(merged)
