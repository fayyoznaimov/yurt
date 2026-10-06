"""Свой хост фото: отдаёт фото товаров по непрозрачным адресам, покупатель не видит доменов магазинов.

    python img_api.py                       # служба (yurt-img): 127.0.0.1:8788, наружу — через Caddy / Cloudflare Tunnel
    python img_api.py --check               # настройки, карта, папка кэша (ничего не качает)
    python img_api.py --prewarm --limit 2000  # заранее подготовить ПЕРВЫЕ фото Pierre Cardin / Cacharel / Trendyol
                                              # (фото YOOX — никогда: только по просмотрам покупателей)

Адрес фото: GET <IMG_BASE>/p/<токен>.<ширина>.webp, ширина 160 / 480 / 960. Токен (16 знаков) выдаёт сборка
(img_map.py, catalog_files.write_site), по нему ничего не узнать; что за ним стоит — только в закрытой карте
data/img_map.json (исходный адрес на CDN магазина или файл в site/).

Как отдаётся:
  * есть в кэше (IMG_CACHE/p/<токен>.<ширина>.webp) — сразу, Cache-Control на год (immutable). Caddy отдаёт такие
    файлы сам (try_files), сюда приходят только промахи;
  * нет — один раз скачивается оригинал (браузерный User-Agent и Referer его магазина, таймаут 15 с), из него
    делаются все три ширины: вписать в 3:4 с белыми полями, WebP качества 78 — и кладутся в кэш;
  * магазин ответил 403/429 — этот хост не трогаем BLOCK_MINUTES минут, без повторов: отдаётся нейтральная
    заглушка (без кэширования, чтобы потом пришло настоящее фото); другая ошибка — заглушка, токен не пробуем
    FAIL_MINUTES минут;
  * вежливость: не чаще 1 запроса в секунду к yoox.com и 2 в секунду к остальным хостам (на каждый хост);
    не успеваем за IMG_MAX_WAIT секунд — заглушка без кэширования. Скачивание бывает только по просмотру
    покупателя (или --prewarm для первых фото турецких магазинов) — никакого обхода.
  * хосты только из списка HOSTS (CDN наших магазинов); чужой адрес в карте не скачивается.

Переменные окружения (на сервере — /etc/yurt/yurt.env):
  IMG_API_PORT / IMG_API_BIND   8788 / 127.0.0.1
  IMG_CACHE                     папка кэша (по умолчанию data/img_cache; setup.sh — /opt/yurt/img-cache)
  IMG_MAP                       карта (по умолчанию data/img_map.json)
  IMG_SITE_DIR                  папка site/ (свои фото img/p/…)
  IMG_MAX_WAIT                  сколько секунд ждать очереди к хосту (8)
Нужен Pillow (pip install pillow; setup.sh ставит его сам).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import img_map

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PATH_RE = re.compile(r"(?:^|/)p/([a-z2-7]{16})\.(\d{2,4})\.webp$")
QUALITY = 78
MAX_BYTES = 15 * 1024 * 1024
TIMEOUT = 15
BLOCK_MINUTES = 30
FAIL_MINUTES = 30
LONG_CACHE = "public, max-age=31536000, immutable"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
# (окончание имени хоста, Referer, запросов в секунду на хост). У CDN Akinon имя вида 25d163-pcardin.akinoncloudcdn.com —
# для них окончание считается и после «-» (как channel.referer_for).
HOSTS = (
    ("yoox.com", "https://www.yoox.com/", 1.0),
    ("pcardin.akinoncloudcdn.com", "https://www.pierrecardin.com.tr/", 2.0),
    ("cacharel.akinoncloudcdn.com", "https://www.cacharel.com.tr/", 2.0),
    ("dsmcdn.com", "https://www.trendyol.com/", 2.0),
)
DASH_OK = ("akinoncloudcdn.com",)
NEVER_PREWARM = ("yoox",)        # фото YOOX — только по просмотрам покупателей, заранее не качаются никогда

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def host_rule(host: str) -> tuple[str, float] | None:
    """(Referer, запросов/с) для хоста фото; None — хост не из списка, не качаем."""
    h = (host or "").lower().split(":")[0]
    for suffix, referer, rate in HOSTS:
        if h == suffix or h.endswith("." + suffix) or (suffix.endswith(DASH_OK) and h.endswith("-" + suffix)):
            return referer, rate
    return None


def http_fetch(url: str, headers: dict, timeout: float) -> tuple[int, str, bytes]:
    """Скачать оригинал (requests): (HTTP-код, Content-Type, байты не больше MAX_BYTES)."""
    import requests
    with requests.get(url, headers=headers, timeout=timeout, stream=True) as r:
        body = r.raw.read(MAX_BYTES + 1, decode_content=True) if r.status_code == 200 else b""
        return r.status_code, r.headers.get("content-type") or "", body[:MAX_BYTES + 1]


class HostLimiter:
    """Не чаще rate запросов в секунду на хост: каждый запрос занимает свой слот; ждать дольше max_wait — отказ."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.next: dict[str, float] = {}
        self.lock = threading.Lock()

    def wait(self, host: str, rate: float, max_wait: float) -> bool:
        with self.lock:
            now = self.clock()
            slot = max(now, self.next.get(host, 0.0))
            if slot - now > max_wait:
                return False
            self.next[host] = slot + 1.0 / max(rate, 0.01)
        if slot > now:
            self.sleep(slot - now)
        return True


def render(data: bytes, w: int) -> bytes:
    """Оригинал → WebP шириной w в пропорции 3:4: вписать целиком (ничего не обрезая) и добить белым."""
    from PIL import Image, ImageOps
    h = w * 4 // 3
    im = Image.open(io.BytesIO(data))
    try:
        im.draft("RGB", (w * 2, h * 2))           # большой JPEG декодируется сразу уменьшенным — в разы быстрее
    except (AttributeError, ValueError, OSError):
        pass
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, rgba)
    im = im.convert("RGB")
    im = ImageOps.contain(im, (w, h), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    canvas.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, "WEBP", quality=QUALITY, method=4)
    return buf.getvalue()


_PLACEHOLDERS: dict[int, bytes] = {}


def placeholder(w: int) -> bytes:
    """Нейтральная светлая заглушка 3:4 (WebP) — без логотипов и намёков на магазин."""
    if w not in _PLACEHOLDERS:
        from PIL import Image, ImageDraw
        h = w * 4 // 3
        im = Image.new("RGB", (w, h), (240, 241, 244))
        d = ImageDraw.Draw(im)
        r = max(4, w // 10)
        d.rounded_rectangle((w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r), radius=max(2, r // 3),
                            outline=(214, 217, 223), width=max(1, w // 160))
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=60)
        _PLACEHOLDERS[w] = buf.getvalue()
    return _PLACEHOLDERS[w]


class ImgService:
    """Логика без HTTP (так её проверяют тесты): get(токен, ширина) → (код, байты, Cache-Control, пометка)."""

    def __init__(self, cache_dir: Path, map_file: Path, site_dir: Path, fetch=http_fetch, clock=time.time,
                 limiter: HostLimiter | None = None, max_wait: float = 8.0, widths=img_map.WIDTHS):
        self.cache = Path(cache_dir)
        self.map_file = Path(map_file)
        self.site = Path(site_dir)
        self.fetch, self.clock = fetch, clock
        self.limiter = limiter or HostLimiter()
        self.max_wait = max_wait
        self.widths = tuple(int(x) for x in widths)
        self._items: dict[str, list] = {}
        self._map_sig = None
        self._map_checked = 0.0
        self.blocked: dict[str, float] = {}           # хост -> до какого времени не трогать (403/429)
        self.failed: dict[str, float] = {}            # токен -> до какого времени не пробовать
        self.locks: dict[str, threading.Lock] = {}
        self.lock = threading.Lock()
        self.render_sem = threading.BoundedSemaphore(int(os.environ.get("IMG_RENDER_THREADS") or 3))
        self.stats = {"hit": 0, "miss": 0, "fetched": 0, "placeholder": 0, "blocked": 0}

    # --- карта ---
    def items(self) -> dict[str, list]:
        now = time.monotonic()
        if now - self._map_checked >= 30 or not self._items:
            self._map_checked = now
            try:
                st = self.map_file.stat()
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                sig = None
            if sig != self._map_sig:
                self._map_sig = sig
                self._items = img_map.load_items(self.map_file) if sig else {}
        return self._items

    def cache_path(self, token: str, w: int) -> Path:
        return self.cache / "p" / f"{token}.{w}.webp"

    # --- главное ---
    def get(self, token: str, w: int) -> tuple[int, bytes, str, str]:
        if not img_map.is_token(token) or w not in self.widths:
            return 404, b"", "no-store", "bad"
        p = self.cache_path(token, w)
        if p.is_file():
            self.stats["hit"] += 1
            return 200, p.read_bytes(), LONG_CACHE, "hit"
        self.stats["miss"] += 1
        rec = self.items().get(token)
        if not rec:
            return 404, placeholder(w), "public, max-age=300", "unknown"
        with self.lock:
            tl = self.locks.setdefault(token, threading.Lock())
        with tl:                                       # одно скачивание на фото, даже если просят сразу три ширины
            if p.is_file():
                return 200, p.read_bytes(), LONG_CACHE, "hit"
            out = self._make(token, rec[0])
        with self.lock:
            self.locks.pop(token, None)
        if out is None:
            self.stats["placeholder"] += 1
            return 200, placeholder(w), "no-store", "placeholder"
        return 200, out[w], LONG_CACHE, "rendered"

    def _original(self, token: str, src: str) -> bytes | None:
        now = self.clock()
        if self.failed.get(token, 0) > now:
            return None
        if not src.startswith(("http://", "https://")):
            try:
                f = (self.site / src).resolve()
                if self.site.resolve() not in f.parents or not f.is_file():
                    raise OSError("нет файла")
                return f.read_bytes()
            except OSError:
                self.failed[token] = now + FAIL_MINUTES * 60
                return None
        host = (urlsplit(src).hostname or "").lower()
        rule = host_rule(host)
        if not rule:
            print(f"[img] хост {host} не из списка HOSTS — не качаю")
            self.failed[token] = now + FAIL_MINUTES * 60
            return None
        if self.blocked.get(host, 0) > now:
            self.stats["blocked"] += 1
            return None
        referer, rate = rule
        if not self.limiter.wait(host, rate, self.max_wait):
            return None                                 # очередь к хосту слишком длинная — в другой раз
        try:
            status, ctype, body = self.fetch(src, {"User-Agent": BROWSER_UA, "Referer": referer,
                                                   "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}, TIMEOUT)
        except Exception as e:                          # сеть, таймаут
            print(f"[img] {host}: {e.__class__.__name__}")
            self.failed[token] = self.clock() + FAIL_MINUTES * 60
            return None
        if status in (403, 429):
            self.blocked[host] = self.clock() + BLOCK_MINUTES * 60
            print(f"[img] {host} ответил {status} — {BLOCK_MINUTES} мин этот хост не трогаю, отдаю заглушки")
            return None
        if status != 200 or not body or len(body) > MAX_BYTES or ctype.startswith("text/"):
            self.failed[token] = self.clock() + FAIL_MINUTES * 60
            return None
        self.stats["fetched"] += 1
        return body

    def _make(self, token: str, src: str) -> dict[int, bytes] | None:
        data = self._original(token, src)
        if data is None:
            return None
        out = {}
        with self.render_sem:
            try:
                for w in self.widths:
                    out[w] = render(data, w)
            except Exception as e:                      # битая картинка, не изображение
                print(f"[img] не разобрать фото ({e.__class__.__name__})")
                self.failed[token] = self.clock() + FAIL_MINUTES * 60
                return None
        for w, b in out.items():
            p = self.cache_path(token, w)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_bytes(b)
            os.replace(tmp, p)
        return out

    # --- заранее: первые фото турецких магазинов ---
    def prewarm(self, limit: int = 2000, say=print) -> dict:
        """Первые фото (номер 0) товаров Pierre Cardin / Cacharel / Trendyol, которых ещё нет в кэше, — по порядку
        витрины, с теми же лимитами на хост. YOOX не трогается никогда. 403/429 — хост до конца прогрева пропускается."""
        done = skipped = 0
        stopped: set[str] = set()
        for token, rec in self.items().items():
            if done >= limit:
                break
            if len(rec) < 3 or rec[2] != 0 or not str(rec[0]).startswith(("http://", "https://")):
                continue
            host = (urlsplit(rec[0]).hostname or "").lower()
            if any(x in host for x in NEVER_PREWARM) or not host_rule(host) or host in stopped:
                continue
            if all(self.cache_path(token, w).is_file() for w in self.widths):
                skipped += 1
                continue
            if self.blocked.get(host, 0) > self.clock():
                stopped.add(host)
                say(f"[img] прогрев: {host} закрыт (403/429) — его пропускаю")
                continue
            if self._make(token, rec[0]) is not None:
                done += 1
                if done % 100 == 0:
                    say(f"[img] прогрев: готово {done}")
        say(f"[img] прогрев: сделано {done}, уже были {skipped}" + (f", закрыты: {', '.join(sorted(stopped))}" if stopped else ""))
        return {"done": done, "skipped": skipped, "stopped": sorted(stopped)}


# ---------------------------------------------------------------- HTTP

def make_handler(svc: ImgService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "img"
        sys_version = ""

        def log_message(self, fmt, *args):       # журнал доступа не ведём
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.rstrip("/").endswith("/health") or path == "/health":
                body = json.dumps({"ok": True, "map": len(svc.items()), "cache": str(svc.cache), **svc.stats}).encode()
                return self._send(200, body, "application/json; charset=utf-8", "no-store")
            m = PATH_RE.search(path)
            if not m:
                return self._send(404, b"not found", "text/plain; charset=utf-8", "no-store")
            try:
                code, body, cache, _tag = svc.get(m.group(1), int(m.group(2)))
            except Exception as e:                    # не роняем службу из-за одного фото
                print(f"[img] ошибка: {e.__class__.__name__}: {e}")
                code, body, cache = 200, placeholder(480), "no-store"
            if not body:
                return self._send(code, b"not found", "text/plain; charset=utf-8", "no-store")
            return self._send(code, body, "image/webp", cache)
    return Handler


def service_from_env() -> ImgService:
    cache = Path(os.environ.get("IMG_CACHE") or DATA / "img_cache")
    site = Path(os.environ.get("IMG_SITE_DIR") or ROOT / "site")
    return ImgService(cache, img_map.map_path(DATA), site, max_wait=float(os.environ.get("IMG_MAX_WAIT") or 8))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Свой хост фото (см. начало файла)")
    ap.add_argument("--bind", default=os.environ.get("IMG_API_BIND") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("IMG_API_PORT") or 8788))
    ap.add_argument("--check", action="store_true", help="показать настройки и выйти")
    ap.add_argument("--prewarm", action="store_true", help="подготовить первые фото турецких магазинов и выйти")
    ap.add_argument("--limit", type=int, default=2000, help="сколько фото подготовить за --prewarm")
    a = ap.parse_args(argv)
    svc = service_from_env()
    if a.check:
        st = img_map.settings()
        n = len(svc.items())
        print(f"IMG_BASE: {st['base'] or '(не задан — сайт показывает фото по старым адресам)'}")
        print(f"IMG_SECRET: {'задан' if st['secret'] else 'НЕТ'}; карта {svc.map_file}: {n} фото; кэш {svc.cache}")
        try:
            import PIL
            print(f"Pillow {PIL.__version__}")
        except ImportError:
            print("Pillow не установлен: pip install pillow")
            return 1
        return 0
    if a.prewarm:
        svc.prewarm(a.limit)
        return 0
    svc.cache.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(svc))
    srv.daemon_threads = True
    print(f"[img] слушаю {a.bind}:{a.port}, кэш {svc.cache}, карта {svc.map_file} ({len(svc.items())} фото)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
