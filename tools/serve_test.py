"""Локальный тестовый сервер для site/ — браузер не обращается к магазинам (только для проверки, не для продакшена).

    python tools/serve_test.py                       # http://127.0.0.1:8800/
    python tools/serve_test.py --port 8810 --img hosts --delay-data 300 --delay-img 800
    python tools/serve_test.py --placeholder         # вместо своих фото — серые заглушки
    python tools/serve_test.py --dead cdn-b          # фото с хостов, где в имени есть «cdn-b», отвечают 404

Открывать: http://127.0.0.1:8800/?reducedmotion=1 (в скрытой панели браузера — всегда с ?reducedmotion=1:
без него переходы между страницами ждут кадра и «застревают»).

Зачем: в данных каталога фото товаров — прямые ссылки на сервера магазинов. Обычный `python -m http.server`
отдал бы их браузеру как есть, и браузер скачал бы фото с магазинов (а это автоматический доступ, которого быть не
должно). Этот сервер отдаёт те же файлы site/, но:

  * в файлах с адресами фото (части каталога data/, products.*, brands.*, admin/) внешние адреса
    https://<хост>/… заменяются на локальные (адреса t.me, telegram.org, шрифтов не трогаются):
      --img path   (по умолчанию) → /__img/<хост>/…  (тот же адрес, что у сайта);
      --img hosts  → http://img-<хэш хоста>.localhost:<порт>/…  (у каждого хоста свой адрес: так проверяются
                     preconnect, варианты размеров фото и «мёртвый хост»);
    на такой запрос сервер отвечает своим фото из site/img/p (для одного адреса — всегда одно и то же) или серой
    заглушкой (--placeholder); хосты из --dead отвечают 404;
  * у HTML заголовок Content-Security-Policy: img-src 'self' data: blob: (+ *.localhost в режиме hosts) — даже если
    какой-то внешний адрес картинки не заменился, браузер его не запросит;
  * режимы данных по началу адреса: /old/ — части индекса без столбцов r/cg/zk/kw (как старая сборка);
    /legacy/ — без манифеста, весь каталог одним products.json (до 25 000 товаров) с новыми полями;
    /legacyold/ — то же без новых полей;
  * задержка (медленная сеть): --delay-data MS — файлы data/, --delay-img MS — фото; на ходу: /__delay?data=300&img=800;
  * Telegram Mini App без Telegram: ?tgmock=pre|late|fail|empty — первым скриптом страницы подставляется
    tools/tg_mock.js (подмена Telegram.WebApp, журнал вызовов в window.__tg; режимы и параметры — в начале tg_mock.js).
    Пример: /?tgmock=pre&tgsp=p_ABCDEFG&tgscheme=dark&reducedmotion=1;
  * приём заказов без сервера заказов: /__mode?endpoint=1[&bot=имя_бота][&tg=ник_для_заказов][&fail=1] — content.js / content.json
    отдаются с order_endpoint на этот сервер, POST /api/order записывает тело в память и отвечает как order_api.py
    (fail=1 — первая попытка получает 500); /__orders — записанные тела, /__orders?check=1 — они же через
    order_api.validate; /__mode без параметров — всё выключить;
  * воронка (при /__mode?endpoint=1 сайт шлёт сюда же): POST /api/event (пачки событий {s, src, ev:[…]}) и
    POST /api/cart (брошенная корзина Mini App {tg_init_data, items}) — печатаются в консоль и копятся в памяти;
    /__events, /__carts — что пришло (у событий — пометка pii, если в пачке есть имя/телефон/cid/данные Telegram),
    ?clear=1 — очистить; /__mode их тоже очищает;
  * /__log — какие фото запрашивались (хост и путь), /__log?clear=1 — очистить.
  * --imgtok — как будто включён свой хост фото (IMG_BASE, img_map.py): в частях каталога и products.* адреса фото
    заменяются токенами (16 знаков, из хэша адреса), в манифест добавляется "img": {"base": "/__imgh", …}; сайт сам
    строит адреса /__imgh/p/<токен>.<160|480|960>.webp, а этот сервер отвечает на них своим фото из site/img/p или
    заглушкой (как img_api.py, но без скачивания). В /__log у таких запросов host = "imgh" и ширина w.

Слушает только 127.0.0.1. Файлы site/ не меняет. Нужен только Python 3.10+ (без пакетов).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
HERE = Path(__file__).resolve().parent
MOCK_JS = HERE / "tg_mock.js"

# эти адреса не картинки магазинов — оставляем как есть
KEEP_HOSTS = {"t.me", "telegram.me", "telegram.org", "www.w3.org", "fonts.googleapis.com", "fonts.gstatic.com"}
EXT_URL = re.compile(r"(https?:)?//([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?=[/?#\"'\\]|$)")
MODES = ("old", "legacyold", "legacy")            # /old/…, /legacy/…, /legacyold/…
# где лежат адреса фото товаров (content.* не трогаем: там ссылки продавца, фото товаров нет)
REWRITE_PREFIXES = ("data/", "admin/", "products", "brands", "sample-products")
INDEX_EXTRA = ("r", "cg", "zk", "kw")             # столбцы индекса, которых нет у старых сборок
LEGACY_MAX = 25000
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
         ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8", ".woff2": "font/woff2"}
PLACEHOLDER = (b'<svg xmlns="http://www.w3.org/2000/svg" width="464" height="591" viewBox="0 0 464 591">'
               b'<rect width="464" height="591" fill="#d9dbe0"/><path d="M182 250h100v90H182z" fill="#b9bcc4"/></svg>')

OPT = {"port": 8800, "img": "path", "placeholder": False, "dead": [], "delay_data": 0, "delay_img": 0, "imgtok": False}
IMGH = "/__imgh"                                  # база «своего хоста фото» в режиме --imgtok
IMGH_RE = re.compile(r"^/__imgh/p/([a-z2-7]{16})\.(\d{2,4})\.webp$")
STATE = {"endpoint": False, "bot": "", "tg": "", "fail": False, "orders": [], "attempts": 0, "events": [], "carts": []}
# в событиях воронки не должно быть персональных данных: такие ключи (на любой глубине) — пометка pii в /__events
PII_KEYS = {"name", "phone", "telegram", "cid", "customer", "tg_init_data", "initdata", "user", "username", "email"}
PII_RE = re.compile(r"\+?\d[\d\s()-]{8,}\d|@[A-Za-z0-9_]{4,}|tgWebAppData|hash=|auth_date")


def pii_issues(obj, path: str = "") -> list[str]:
    """Ключи и значения в событиях, похожие на персональные данные (для проверки сайта)."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in PII_KEYS:
                out.append(f"{path}{k}: ключ")
            out += pii_issues(v, f"{path}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += pii_issues(v, f"{path}{i}.")
    elif isinstance(obj, str) and PII_RE.search(obj):
        out.append(f"{path.rstrip('.')}: «{obj[:40]}»")
    return out
LOG: list[dict] = []
HOST_OF_LABEL: dict[str, str] = {}               # img-<хэш>.localhost -> исходный хост (для журнала и --dead)
CACHE: dict[tuple, bytes] = {}
LOCK = threading.Lock()
PHOTOS: list[Path] = []


# ---------------------------------------------------------------- замена адресов

def host_label(host: str) -> str:
    return "img-" + hashlib.md5(host.lower().encode("utf-8")).hexdigest()[:8]


def is_dead(host: str) -> bool:
    h = host.lower()
    return any(s and s.lower() in h for s in OPT["dead"])


def rewrite(text: str) -> str:
    """Внешние адреса -> локальные (см. описание в начале файла)."""
    def sub(m: re.Match) -> str:
        scheme, host = m.group(1), m.group(2)
        if host.lower() in KEEP_HOSTS:
            return m.group(0)
        if not scheme and (m.start() == 0 or text[m.start() - 1] not in "\"'"):
            return m.group(0)                     # «//» без схемы — только адрес в кавычках, не комментарий
        if OPT["img"] == "hosts":
            label = host_label(host)
            with LOCK:
                HOST_OF_LABEL[label] = host
            return f"http://{label}.localhost:{OPT['port']}"
        return "/__img/" + host
    return EXT_URL.sub(sub, text)


def unwrap(text: str) -> tuple[str, str, str]:
    """Часть каталога: __DS("i/0",\\n{json}\\n); -> (голова, json, хвост)."""
    if text.startswith("__DS("):
        a, b = text.index("\n") + 1, text.rindex("\n)")
        return text[:a], text[a:b], text[b:]
    return "", text, ""


# ---------------------------------------------------------------- режим --imgtok (свой хост фото)

def fake_token(url: str) -> str:
    """Токен как у img_map (16 знаков [a-z2-7]), но из хэша адреса: секрета здесь нет и не нужно."""
    import base64
    return base64.b32encode(hashlib.md5(url.encode("utf-8")).digest()[:10]).decode("ascii").lower()


def img_info() -> dict:
    return {"base": IMGH, "w": [160, 480, 960], "full": 960, "fmt": "webp"}


def tok_list(urls) -> list:
    return [fake_token(u) for u in urls or [] if isinstance(u, str) and u]


def tok_manifest(m: dict) -> dict:
    m["img"] = img_info()
    return m


def tok_index(sh: dict, man: dict) -> dict:
    """Часть индекса: первое фото (ipd/ip + im + imgsuf/is) → токен в im, префиксы пустые."""
    pre = sh.get("ipd") or (man.get("dict") or {}).get("imgpre") or [None]
    suf = (man.get("dict") or {}).get("imgsuf") or [None]
    n = sh.get("n", 0)
    im = []
    for i in range(n):
        if sh["ni"][i]:
            im.append(fake_token((pre[sh["ip"][i]] or "") + sh["im"][i] + (suf[sh["is"][i]] or "")))
        else:
            im.append("")
    sh["ipd"], sh["ip"], sh["im"], sh["is"] = [None], [0] * n, im, [0] * n
    return sh


def tok_detail(obj: dict) -> dict:
    pre, suf = obj.get("pre") or [""], obj.get("suf") or [""]
    for pid, rec in (obj.get("items") or {}).items():
        flat = rec[3] or []
        urls = [(pre[flat[j]] or "") + flat[j + 1] + (suf[flat[j + 2]] or "") for j in range(0, len(flat), 3)]
        rec[3] = [x for t in tok_list(urls) for x in (0, t, 0)]
    obj["pre"], obj["suf"] = [""], [""]
    return obj


def tok_whole(data: dict) -> dict:
    """products.json / window.DEALS: фото товаров → токены, + img."""
    for p in data.get("products") or []:
        if isinstance(p, dict):
            p["images"] = tok_list(p.get("images"))
    data["img"] = img_info()
    return data


def manifest_now() -> dict:
    return json.loads((SITE / "data" / "manifest.json").read_text(encoding="utf-8"))


def tokenize_file(text: str, rel: str) -> str:
    dumps = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    if rel == "data/manifest.json":
        return dumps(tok_manifest(json.loads(text)))
    if rel.startswith("data/i/"):
        head, body, tail = unwrap(text)
        return head + dumps(tok_index(json.loads(body), manifest_now())) + tail
    if rel.startswith("data/d/"):
        head, body, tail = unwrap(text)
        return head + dumps(tok_detail(json.loads(body))) + tail
    if rel == "products.json":
        return dumps(tok_whole(json.loads(text)))
    if rel == "products.js":
        for key in ("window.DEALS_MANIFEST = ", "window.DEALS = "):
            i = text.find(key)
            if i >= 0:
                j = text.rindex(";")
                obj = json.loads(text[i + len(key):j])
                obj = tok_manifest(obj) if "MANIFEST" in key else tok_whole(obj)
                return text[:i + len(key)] + dumps(obj) + text[j:]
    return text


def strip_index(text: str) -> str:
    head, body, tail = unwrap(text)
    sh = json.loads(body)
    for k in INDEX_EXTRA:
        sh.pop(k, None)
    return head + json.dumps(sh, ensure_ascii=False, separators=(",", ":")) + tail


def strip_manifest(text: str) -> str:
    m = json.loads(text)
    for k in ("zk", "kw", "szk", "ckw", "cgroup"):
        (m.get("dict") or {}).pop(k, None)
    for k in ("colors", "zk"):
        (m.get("facets") or {}).pop(k, None)
    return json.dumps(m, ensure_ascii=False)


def legacy_products(with_new: bool) -> bytes:
    """Весь каталог одним products.json (как у сборок до частей), собранный из частей индекса."""
    key = ("legacy", with_new, OPT["img"], OPT["imgtok"], (SITE / "data" / "manifest.json").stat().st_mtime_ns)
    if key in CACHE:
        return CACHE[key]
    man = json.loads((SITE / "data" / "manifest.json").read_text(encoding="utf-8"))
    D = man["dict"]
    out: list[dict] = []
    for s in man["shards"]:
        sh = json.loads(unwrap((SITE / "data" / s["file"]).read_text(encoding="utf-8"))[1])
        pre = sh.get("ipd") or D.get("imgpre") or [None]
        td = sh.get("td")
        outs = set(sh.get("out") or [])
        for i in range(sh["n"]):
            p = {"id": sh["id"][i], "brand": D["brand"][sh["b"][i]], "title": td[sh["t"][i]] if td else sh["t"][i],
                 "type": D["type"][sh["ty"][i]], "gender": D["gender"][sh["g"][i]], "origin": D["origin"][sh["o"][i]],
                 "price_uzs": sh["p"][i] * man["price_unit"],
                 "discount_pct": None if sh["d"][i] is None else sh["d"][i] / man["disc_scale"],
                 "sizes": [D["size"][x] for x in sh["s"][i]], "size_system": D["sys"][sh["ss"][i]],
                 "color": D["color"][sh["c"][i]], "in_stock": i not in outs,
                 "images": [(pre[sh["ip"][i]] or "") + sh["im"][i] + (D["imgsuf"][sh["is"][i]] or "")] if sh["ni"][i] else []}
            if with_new and "r" in sh:
                p["r"] = sh["r"][i]
                p["cg"] = sh["cg"][i]
                z = sh["zk"][i]
                nums: list[int] = []
                for x in (sh["s"][i] if z == 0 else []):
                    nums += [k for k in D["szk"][x] if k not in nums]
                p["zk"] = [D["zk"][k] for k in sorted(nums if z == 0 else z)]
                p["kw"] = [D["kw"][k] for k in (D["ckw"][sh["c"][i]] + sh["kw"][i])]
            out.append(p)
            if len(out) >= LEGACY_MAX:
                break
        if len(out) >= LEGACY_MAX:
            break
    data = {"summary": man.get("summary"), "site": man.get("site"), "products": out}
    if OPT["imgtok"]:
        data = tok_whole(data)
    body = rewrite(json.dumps(data, ensure_ascii=False)).encode("utf-8")
    CACHE[key] = body
    return body


def file_body(fp: Path, rel: str, mode: str) -> bytes:
    """Текстовый файл site/ с заменой адресов (и урезанием столбцов в режиме /old/); кэш по времени изменения."""
    key = (str(fp), fp.stat().st_mtime_ns, mode, OPT["img"], OPT["port"], OPT["imgtok"])
    hit = CACHE.get(key)
    if hit is not None:
        return hit
    text = fp.read_text(encoding="utf-8")
    if mode == "old":
        if rel.startswith("data/i/"):
            text = strip_index(text)
        elif rel == "data/manifest.json":
            text = strip_manifest(text)
    if OPT["imgtok"]:
        text = tokenize_file(text, rel)
    body = rewrite(text).encode("utf-8")
    with LOCK:
        if len(CACHE) > 400:
            CACHE.clear()
        CACHE[key] = body
    return body


def photo_for(path: str) -> tuple[bytes, str]:
    if OPT["placeholder"]:
        return PLACEHOLDER, "image/svg+xml"
    global PHOTOS
    if not PHOTOS:
        folder = SITE / "img" / "p"
        PHOTOS = sorted(f for f in folder.glob("*") if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")) \
            if folder.is_dir() else []
    if not PHOTOS:
        return PLACEHOLDER, "image/svg+xml"
    f = PHOTOS[int(hashlib.md5(path.encode("utf-8")).hexdigest(), 16) % len(PHOTOS)]
    try:
        return f.read_bytes(), TYPES.get(f.suffix.lower(), "image/jpeg")
    except OSError:
        return PLACEHOLDER, "image/svg+xml"


def content_data() -> dict:
    d = json.loads((SITE / "content.json").read_text(encoding="utf-8-sig"))
    if STATE["endpoint"]:
        d["order_endpoint"] = f"http://127.0.0.1:{OPT['port']}/api/order"
    if STATE["bot"]:
        d["orders_bot_username"] = STATE["bot"]
    if STATE["tg"]:
        d["orders_telegram_username"] = STATE["tg"]      # ник продавца — только для проверки кнопок Telegram
        d["contacts"] = dict(d.get("contacts") or {}, telegram=STATE["tg"])
    return d


def check_orders() -> list:
    """Записанные тела заказов через order_api.validate (без сети)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import order_api                          # noqa: WPS433 — только по запросу /__orders?check=1
    except Exception as e:                        # noqa: BLE001
        return [{"error": f"order_api не импортируется: {e.__class__.__name__}: {e}"}]
    out = []
    for o in STATE["orders"]:
        try:
            v = order_api.validate(o["body"])
            keep = {k: v.get(k) for k in ("cid", "client_order_id", "consent", "consent_marketing", "city", "items")}
            keep["tg_init_data"] = (v.get("tg_init_data") or "")[:40]
            # воронка: что прислал сайт и что оставила проверка order_api (None — поле не пропущено)
            keep["src_sent"] = {k: o["body"].get(k) for k in ("src_first", "src_last", "ref") if k in o["body"]}
            keep["src_valid"] = {k: v.get(k) for k in ("src_first", "src_last", "ref")}
            keep["customer"] = {k: (v.get("customer") or {}).get(k) for k in ("name", "phone", "telegram", "city")}
            out.append({"attempt": o["attempt"], "ok": True, "valid": keep})
        except Exception as e:                    # noqa: BLE001 — ValueError с текстом ошибки и любые другие
            out.append({"attempt": o["attempt"], "ok": False, "error": f"{e.__class__.__name__}: {e}"})
    return out


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "serve_test"

    def log_message(self, fmt, *args):            # тихо
        pass

    def send(self, code: int, body: bytes | str, ctype: str = "text/plain; charset=utf-8", extra: dict | None = None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, data, code: int = 200, extra: dict | None = None):
        self.send(code, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8", extra)

    def csp(self) -> dict:
        extra = f" http://*.localhost:{OPT['port']}" if OPT["img"] == "hosts" else ""
        return {"Content-Security-Policy": "img-src 'self' data: blob:" + extra}

    def image(self, host: str, path: str):
        with LOCK:
            if len(LOG) < 5000:
                LOG.append({"host": host, "path": path})
        if OPT["delay_img"]:
            time.sleep(OPT["delay_img"] / 1000)
        if is_dead(host):
            return self.send(404, "dead host (--dead)")
        body, ctype = photo_for(path)
        return self.send(200, body, ctype)

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        self.send(204, b"", extra={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
                                   "Access-Control-Allow-Headers": "Content-Type"})

    def do_GET(self):
        u = urlsplit(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host.startswith("img-") and host.endswith(".localhost"):          # режим --img hosts
            label = host[: -len(".localhost")]
            return self.image(HOST_OF_LABEL.get(label, label), u.path + ("?" + u.query if u.query else ""))
        if path.startswith("/__img/"):                                        # режим --img path
            img_host, _, img_path = path[len("/__img/"):].partition("/")
            return self.image(img_host, "/" + img_path + ("?" + u.query if u.query else ""))
        if path.startswith(IMGH + "/"):                                       # режим --imgtok: «свой хост фото»
            m = IMGH_RE.match(path)
            if not m or int(m.group(2)) not in (160, 480, 960):
                return self.send(404, "not found")
            with LOCK:
                if len(LOG) < 5000:
                    LOG.append({"host": "imgh", "path": path[len(IMGH):], "token": m.group(1), "w": int(m.group(2))})
            if OPT["delay_img"]:
                time.sleep(OPT["delay_img"] / 1000)
            if is_dead("imgh"):
                return self.send(404, "dead host (--dead)")
            body, ctype = photo_for(m.group(1))
            return self.send(200, body, ctype)
        if path == "/__log":
            with LOCK:
                data = list(LOG)
                if q.get("clear", [""])[0] == "1":
                    LOG.clear()
            return self.send_json(data)
        if path == "/__delay":
            for k in ("data", "img"):
                if k in q:
                    OPT["delay_" + k] = max(0, int(q[k][0] or 0))
            return self.send_json({"data": OPT["delay_data"], "img": OPT["delay_img"]})
        if path == "/__mode":
            with LOCK:
                STATE["endpoint"] = q.get("endpoint", [""])[0] == "1"
                STATE["bot"] = q.get("bot", [""])[0]
                STATE["tg"] = q.get("tg", [""])[0]
                STATE["fail"] = q.get("fail", [""])[0] == "1"
                STATE["attempts"] = 0
                STATE["orders"].clear()
                STATE["events"].clear()
                STATE["carts"].clear()
            return self.send_json({k: v for k, v in STATE.items() if k not in ("orders", "events", "carts")})
        if path in ("/__events", "/__carts"):
            key = path[3:]
            with LOCK:
                data = list(STATE[key])
                if q.get("clear", [""])[0] == "1":
                    STATE[key].clear()
            return self.send_json(data)
        if path == "/__orders":
            return self.send_json(check_orders() if q.get("check", [""])[0] == "1" else STATE["orders"])
        if path == "/__tg/mock.js":
            return self.send(200, MOCK_JS.read_bytes(), TYPES[".js"])

        mode = ""
        for m in MODES:
            if path == "/" + m or path.startswith("/" + m + "/"):
                mode, path = m, path[len(m) + 1:] or "/"
                break
        if path.endswith("/"):
            path += "index.html"
        rel = path.lstrip("/")
        delay = OPT["delay_data"] if rel.startswith("data/") else OPT["delay_img"] if rel.startswith("img/") else 0
        if delay:
            time.sleep(delay / 1000)
        if mode in ("legacy", "legacyold"):
            if rel in ("data/manifest.json", "products.js"):
                return self.send(404, "нет (режим /legacy/)")
            if rel == "products.json":
                try:
                    return self.send(200, legacy_products(mode == "legacy"), TYPES[".json"])
                except (OSError, ValueError, KeyError, IndexError) as e:
                    return self.send(500, f"products.json не собран: {e.__class__.__name__}: {e}")
        if rel == "index.html":
            html = (SITE / "index.html").read_text(encoding="utf-8")
            if "tgmock" in q:
                html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n<script src="/__tg/mock.js"></script>', 1)
            return self.send(200, html, TYPES[".html"], self.csp())
        if rel == "content.json" and (STATE["endpoint"] or STATE["bot"] or STATE["tg"]):
            return self.send_json(content_data())
        if rel == "content.js" and (STATE["endpoint"] or STATE["bot"] or STATE["tg"]):
            return self.send(200, "window.SITE_CONTENT = " + json.dumps(content_data(), ensure_ascii=False) + ";\n",
                             TYPES[".js"])
        try:
            fp = (SITE / rel).resolve()
        except (OSError, ValueError):
            return self.send(404, "not found")
        if SITE.resolve() not in fp.parents or not fp.is_file():
            return self.send(404, "not found")
        ext = fp.suffix.lower()
        ctype = TYPES.get(ext, "application/octet-stream")
        if ext in (".js", ".json") and rel.startswith(REWRITE_PREFIXES):
            return self.send(200, file_body(fp, rel, mode), ctype)
        return self.send(200, fp.read_bytes(), ctype, self.csp() if ext == ".html" else None)

    def do_POST(self):
        u = urlsplit(self.path)
        cors = {"Access-Control-Allow-Origin": "*"}
        if u.path not in ("/api/order", "/api/event", "/api/cart"):
            return self.send_json({}, 404, cors)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 1 << 20)).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            return self.send_json({"ok": False, "error": "bad json"}, 400, cors)
        if u.path == "/api/event":
            return self.funnel_event(body, cors)
        if u.path == "/api/cart":
            return self.funnel_cart(body, cors)
        with LOCK:
            STATE["attempts"] += 1
            STATE["orders"].append({"attempt": STATE["attempts"], "body": body, "ctype": self.headers.get("Content-Type")})
            fail = STATE["fail"] and STATE["attempts"] == 1
        if fail:
            return self.send_json({"ok": False, "error": "внутренняя ошибка, напишите нам в Telegram"}, 500, cors)
        if not isinstance(body, dict) or not body.get("consent"):
            return self.send_json({"ok": False, "error": "consent: нужно согласие на обработку данных"}, 400, cors)
        linked = bool(body.get("tg_init_data")) and STATE.get("bot") == "linked_bot"
        bot = STATE.get("bot") or ""
        no = "YR-261005-TEST"
        return self.send_json({"ok": True, "order_no": no, "duplicate": False, "notified": True, "total_uzs": 0,
                               "prepay_uzs": 0, "unavailable": [], "telegram_linked": linked,
                               "bot_url": f"https://t.me/{bot}?start=o_{no}" if bot else ""}, 200, cors)

    # ---- заглушки воронки: печатают, что пришло, и копят в памяти (/__events, /__carts)
    def funnel_event(self, body, cors: dict):
        ev = body.get("ev") if isinstance(body, dict) else None
        ok = isinstance(body, dict) and isinstance(body.get("s"), str) and bool(re.fullmatch(r"[0-9a-f]{12}", body["s"])) \
            and isinstance(body.get("src"), str) and isinstance(ev, list) and 0 < len(ev) <= 50
        issues = pii_issues(body)
        rec = {"at": round(time.time(), 1), "ctype": self.headers.get("Content-Type"), "origin": self.headers.get("Origin"),
               "valid": ok, "pii": issues, "body": body}
        with LOCK:
            if len(STATE["events"]) < 2000:
                STATE["events"].append(rec)
        print(f"[event] s={body.get('s') if isinstance(body, dict) else '?'} src={body.get('src') if isinstance(body, dict) else '?'!r} "
              f"n={len(ev) if isinstance(ev, list) else '?'} valid={ok}" + (f" PII: {issues}" if issues else "")
              + "\n        " + json.dumps(ev, ensure_ascii=False)[:600], flush=True)
        return self.send_json({"ok": ok}, 200 if ok else 400, cors)

    def funnel_cart(self, body, cors: dict):
        items = body.get("items") if isinstance(body, dict) else None
        init = body.get("tg_init_data") if isinstance(body, dict) else None
        ok = isinstance(init, str) and 0 < len(init) <= 4096 and isinstance(items, list) and len(items) <= 30 and all(
            isinstance(i, dict) and set(i) == {"id", "size", "qty"} for i in items)
        rec = {"at": round(time.time(), 1), "ctype": self.headers.get("Content-Type"), "valid": ok,
               "tg_init_data_len": len(init) if isinstance(init, str) else None, "items": items}
        with LOCK:
            if len(STATE["carts"]) < 500:
                STATE["carts"].append(rec)
        print(f"[cart] valid={ok} tg_init_data={rec['tg_init_data_len']} симв. items={json.dumps(items, ensure_ascii=False)}", flush=True)
        return self.send_json({"ok": ok}, 200 if ok else 400, cors)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Тестовый сервер site/ без обращений к магазинам (см. начало файла).")
    ap.add_argument("port_pos", nargs="?", type=int, help=argparse.SUPPRESS)       # python tools/serve_test.py 8804
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--img", choices=("path", "hosts"), default="path",
                    help="path — фото по /__img/<хост>/…; hosts — у каждого хоста свой img-….localhost")
    ap.add_argument("--placeholder", action="store_true", help="серые заглушки вместо своих фото из site/img/p")
    ap.add_argument("--dead", action="append", default=[], metavar="ПОДСТРОКА",
                    help="фото с хостов, в имени которых есть эта подстрока, отвечают 404 (можно несколько раз)")
    ap.add_argument("--delay-data", type=int, default=0, metavar="МС", help="задержка файлов data/")
    ap.add_argument("--delay-img", type=int, default=0, metavar="МС", help="задержка фото")
    ap.add_argument("--imgtok", action="store_true",
                    help="как со своим хостом фото: токены вместо адресов, фото по /__imgh/p/<токен>.<ширина>.webp")
    a = ap.parse_args(argv)
    OPT.update(port=a.port_pos or a.port, img=a.img, placeholder=a.placeholder, dead=a.dead,
               delay_data=a.delay_data, delay_img=a.delay_img, imgtok=a.imgtok)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    srv = ThreadingHTTPServer(("127.0.0.1", OPT["port"]), Handler)
    n = len(list((SITE / "img" / "p").glob("*"))) if (SITE / "img" / "p").is_dir() else 0
    print(f"http://127.0.0.1:{OPT['port']}/?reducedmotion=1   (site: {SITE})", flush=True)
    print(f"фото: {'заглушки' if OPT['placeholder'] or not n else f'свои из site/img/p ({n})'}, режим --img {OPT['img']}"
          + (", токены своего хоста фото (/__imgh/p/…)" if OPT["imgtok"] else "")
          + (f", мёртвые хосты: {', '.join(OPT['dead'])}" if OPT["dead"] else "")
          + (f", задержка data {OPT['delay_data']} мс / фото {OPT['delay_img']} мс"
             if OPT["delay_data"] or OPT["delay_img"] else ""), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    os.chdir(SITE)
    main()
