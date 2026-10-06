"""Выложить сайт на GitHub Pages (ветка gh-pages репозитория из origin).

    python deploy.py                        # рабочая копия gh-pages — ~/yurt-pages
    python deploy.py --out _pages           # своя папка (так делает GitHub Actions: там уже checkout gh-pages)
    python deploy.py --out tmp --dry-run    # собрать и проверить папку, коммит — только локально, без fetch/push
    python deploy.py --make-og              # перерисовать site/brand/og.png (картинка превью ссылки; нужен Pillow)

Ветка gh-pages каждый раз — ОДИН «сиротский» коммит (без родителей), он отправляется с --force: фото и части
каталога, которые ушли с сайта, не копятся в истории, и репозиторий не растёт с каждой публикацией.
Публикация с компьютера и из облака (GitHub Actions) не мешают друг другу: каждая выкладывает сайт целиком
из своего site/, последняя побеждает. Если опубликованное дерево совпадает с новым — ничего не отправляется.

Публикуется только публичная часть site/: index.html, каталог частями data/ (manifest.json и ровно те части
индекса data/i/ и подробностей data/d/, что в нём перечислены — устаревшие части в gh-pages не попадают),
products.js — маленькое оглавление (window.DEALS_MANIFEST, для старых браузеров), тексты продавца
content.js / content.json (content.js пересобирается из content.json), логотипы brand/ (в т.ч. og.png),
логотипы брендов brands.json / brands.js / img/brands/ (brands.js пересобирается из brands.json) и фото img/p/.
Старый products.json (весь каталог одним файлом, для открытия с диска) не публикуется — сайту он не нужен.
Если раскладки data/ ещё нет (run.py старой версии), публикуются products.js / products.json как раньше.

Закрытое НЕ публикуется никогда: products-admin.js и site/admin/** (закупочные цены, маржа, ссылки на
магазины), заказы и покупатели (любой путь с orders / customers / channel_state, файлы *.sqlite*, *.jsonl);
перед коммитом это проверяется ещё раз, в том числе по содержимому файлов данных — при находке публикация
останавливается. На страницу добавляются <meta name="robots" content="noindex"> и теги превью ссылки
(Open Graph / Twitter): заголовок и описание из content.json, картинка brand/og.png по абсолютному адресу
PUBLIC_URL (переменная окружения, по умолчанию https://fayyoznaimov.github.io/yurt/).
Если контакт для заказов не настроен (заглушка t.me/your_username, пустые contacts.telegram и
orders_telegram_username) — громкое предупреждение; сайт всё равно публикуется.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import catalog_files

ROOT = Path(__file__).parent
SITE = ROOT / "site"
OUT = Path(os.environ.get("DEPLOY_OUT") or Path.home() / "yurt-pages")   # рабочая копия ветки gh-pages
BRANCH = "gh-pages"
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "https://fayyoznaimov.github.io/yurt/").rstrip("/") + "/"
PUBLIC_FILES = ["index.html", "products.js"]
LEGACY_FILES = ["products.json"]                   # только если раскладки data/ нет
OPTIONAL_FILES = ["content.js", "content.json",     # тексты продавца (content.py), если есть
                  "brands.js", "brands.json"]       # логотипы брендов (brands.py), если есть
PUBLIC_DIRS = ["brand", "img/p", "img/brands"]
FORBIDDEN = ["products-admin.js", "admin"]         # закрытые данные: файл и папка site/admin/**
FORBIDDEN_IN_PATH = ["orders", "customers", "channel_state"]   # заказы, покупатели, состояние канала — нигде
FORBIDDEN_SUFFIXES = (".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal", ".jsonl")
PLACEHOLDER_CONTACT = "your_username"
OG_IMAGE = "brand/og.png"                          # 1200×630, рисует python deploy.py --make-og
OG_SIZE = (1200, 630)
OG_DESC_MAX = 200
OG_FALLBACK_DESC = "Оригинальная брендовая одежда из Италии и Турции. Предоплата 50%, доставка до 10 дней."
OG_FOOTER = "Оригиналы · предоплата 50% · доставка до 10 дней"
# тексты превью видят все, кто получил ссылку: ни источников, ни закупочных валют
UNSAFE_TEXT = re.compile(r"yoox|trendyol|akinon|dsmcdn|€|₺|\bTL\b|\bEUR\b|\bTRY\b|возврат", re.I)
META_RE = re.compile(r'[ \t]*<meta\s+(?:property|name)\s*=\s*["\'](?:og:|twitter:)[^>]*>[ \t]*\r?\n?', re.I)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd or OUT, capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ, **env) if env else None)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def committer_env() -> dict:
    """Имя для коммита, если в git его не задали (свежий сервер): yurt-bot."""
    env = {}
    for key, var, default in (("user.name", "NAME", "yurt-bot"),
                              ("user.email", "EMAIL", "yurt-bot@users.noreply.github.com")):
        try:
            if git("config", key):
                continue
        except SystemExit:
            pass
        env[f"GIT_AUTHOR_{var}"] = env[f"GIT_COMMITTER_{var}"] = default
    return env


# ---------- тексты и теги превью ссылки ----------

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean(value) -> str:
    """Одна строка без лишних пробелов; текст с названием источника / валютой закупки — пустой."""
    s = " ".join(str(value or "").split())
    if s and UNSAFE_TEXT.search(s):
        print(f"  ! текст для превью пропущен (название источника или валюта закупки): {s[:60]}…")
        return ""
    return s


def _cut(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:—-") + "…"


def og_texts(site: Path) -> tuple[str, str, str, str]:
    """(название, заголовок, описание, подзаголовок) из content.json; запасное — manifest site и общие слова."""
    c = _read_json(site / "content.json")
    s = (catalog_files.load_manifest(site) or {}).get("site") or {}
    name = _clean(c.get("name")) or _clean(s.get("name")) or "ipakly"
    tagline = _clean(c.get("tagline"))
    title = _clean(c.get("title"))
    if not title and tagline:
        low = tagline[0].lower() + tagline[1:] if tagline[1:2].islower() else tagline
        title = f"{name} — {low}"
    title = _cut(title or _clean(s.get("title")) or name, 90)
    desc = next((t for t in (_clean(c.get(k)) for k in ("hero_text", "tagline", "announcement")) if t), "")
    return name, title, _cut(desc or OG_FALLBACK_DESC, OG_DESC_MAX), tagline or OG_FALLBACK_DESC.split(".")[0]


def _attr(value: str) -> str:
    """Значение для атрибута в двойных кавычках (апостроф в «Tod's» оставляем как есть)."""
    return html.escape(value, quote=False).replace('"', "&quot;")


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        return struct.unpack(">II", head[16:24])
    return None


def inject_meta(page: str, out: Path, site: Path) -> str:
    """robots noindex + Open Graph / Twitter. Старые og:/twitter: теги страницы заменяются (адреса — абсолютные)."""
    name, title, desc, _ = og_texts(site)
    page, replaced = META_RE.subn("", page)
    if replaced:
        print(f"  (в index.html было {replaced} тегов og:/twitter: — заменены тегами публикации)")
    tags = [("property", "og:type", "website"), ("property", "og:site_name", name),
            ("property", "og:locale", "ru_RU"), ("property", "og:url", PUBLIC_URL),
            ("property", "og:title", title), ("property", "og:description", desc)]
    img = out / OG_IMAGE
    size = png_size(img)
    if size:
        url = f"{PUBLIC_URL}{OG_IMAGE}?v={hashlib.sha256(img.read_bytes()).hexdigest()[:8]}"
        tags += [("property", "og:image", url), ("property", "og:image:type", "image/png"),
                 ("property", "og:image:width", str(size[0])), ("property", "og:image:height", str(size[1])),
                 ("property", "og:image:alt", name), ("name", "twitter:card", "summary_large_image"),
                 ("name", "twitter:image", url)]
        if size != OG_SIZE:
            print(f"  ! {OG_IMAGE}: {size[0]}×{size[1]}, а для превью нужно {OG_SIZE[0]}×{OG_SIZE[1]}")
    else:
        print(f"  ! нет site/{OG_IMAGE} — превью ссылки будет без картинки (python deploy.py --make-og)")
        tags.append(("name", "twitter:card", "summary"))
    tags += [("name", "twitter:title", title), ("name", "twitter:description", desc)]
    lines = [f'<meta {attr}="{key}" content="{_attr(val)}">' for attr, key, val in tags]
    if not re.search(r'<meta\s+name=["\']description["\']', page, re.I):
        lines.insert(0, f'<meta name="description" content="{_attr(desc)}">')
    if 'name="robots"' not in page:
        lines.insert(0, '<meta name="robots" content="noindex, nofollow">')
    block = "\n".join(lines) + "\n"
    # сразу после <title> (в начале файла: некоторые сборщики превью читают только первые килобайты)
    m = re.search(r"</title>[ \t]*\r?\n?", page, re.I) or re.search(r"<head[^>]*>[ \t]*\r?\n?", page, re.I)
    if not m:
        return block + page
    return page[:m.end()] + ("" if page[m.end() - 1:m.end()] == "\n" else "\n") + block + page[m.end():]


# ---------- контакт для заказов ----------

def contact_problems(site: Path, root: Path) -> list[str]:
    probs = []
    murl = str(((catalog_files.load_manifest(site) or {}).get("site") or {}).get("contact_url") or "")
    curl = str((_read_json(root / "config.json").get("site") or {}).get("contact_url") or "")
    c = _read_json(site / "content.json")
    contacts = c.get("contacts") if isinstance(c.get("contacts"), dict) else {}
    tg = str(contacts.get("telegram") or "").strip()
    otg = str(c.get("orders_telegram_username") or "").strip()
    if PLACEHOLDER_CONTACT in murl:
        probs.append(f"в каталоге (data/manifest.json → site.contact_url) заглушка {murl}: поправьте "
                     "config.json → site.contact_url и пересоберите (python run.py --offline)")
    elif PLACEHOLDER_CONTACT in curl:
        probs.append(f"config.json → site.contact_url — заглушка {curl}")
    if PLACEHOLDER_CONTACT in tg or PLACEHOLDER_CONTACT in otg:
        probs.append("content.json: в contacts.telegram / orders_telegram_username осталась заглушка your_username")
    elif not tg and not otg:
        probs.append("content.json: contacts.telegram и orders_telegram_username пустые — "
                     "впишите ник продавца в Telegram (без @)")
    return probs


def loud_warning(problems: list[str]) -> None:
    bar = "!" * 78
    print("\n".join([bar, "!!! ВНИМАНИЕ: контакт для заказов не настроен — покупатель не сможет написать продавцу.",
                     *(f"!!!   - {p}" for p in problems),
                     "!!! Сайт всё равно публикуется. Исправьте контакт и опубликуйте ещё раз.", bar]), flush=True)
    if os.environ.get("GITHUB_ACTIONS"):
        print("::warning title=Контакт для заказов не настроен::" + "; ".join(problems), flush=True)


# ---------- проверка закрытых данных ----------

def private_leaks(out: Path) -> list[str]:
    leaked = [f for f in FORBIDDEN if (out / f).exists()]
    for p in out.rglob("*"):
        rel = p.relative_to(out)
        if ".git" in rel.parts:
            continue
        low = rel.as_posix().lower()
        if (p.name.startswith("products-admin") or rel.parts[0] == "admin"
                or any(w in low for w in FORBIDDEN_IN_PATH) or low.endswith(FORBIDDEN_SUFFIXES)):
            leaked.append(rel.as_posix())
    # и по содержимому: в публичных данных не должно быть закупочных полей
    for p in [out / "products.js", out / "products.json", *(out / catalog_files.DATA_DIR).rglob("*.js*")]:
        if p.is_file() and any(m in p.read_bytes() for m in catalog_files.ADMIN_LEAK_MARKERS):
            leaked.append(p.relative_to(out).as_posix())
    return sorted(set(leaked))


# ---------- сборка папки и публикация ----------

def build_output(out: Path, data_files: list[str]) -> None:
    """Чистит всё, кроме .git, и копирует публичные файлы заново."""
    out.mkdir(parents=True, exist_ok=True)
    for item in out.iterdir():
        if item.name == ".git":
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    for f in PUBLIC_FILES:
        shutil.copy2(SITE / f, out / f)
    if data_files:
        # каталог частями: только перечисленное в манифесте; products.js — оглавление (без 25+ МБ старого формата)
        for f in data_files:
            (out / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SITE / f, out / f)
        manifest = (SITE / catalog_files.DATA_DIR / "manifest.json").read_text(encoding="utf-8")
        (out / "products.js").write_text(catalog_files.stub_js(manifest), encoding="utf-8")
    else:
        for f in LEGACY_FILES:
            if (SITE / f).exists():
                shutil.copy2(SITE / f, out / f)
    for f in OPTIONAL_FILES:
        if (SITE / f).exists():
            shutil.copy2(SITE / f, out / f)
    for d in PUBLIC_DIRS:
        if (SITE / d).exists():
            shutil.copytree(SITE / d, out / d)
    (out / ".nojekyll").write_text("", encoding="utf-8")
    page = (out / "index.html").read_text(encoding="utf-8")
    (out / "index.html").write_text(inject_meta(page, out, SITE), encoding="utf-8", newline="")


def ensure_repo(out: Path, remote: str | None) -> None:
    if not (out / ".git").exists():
        git("init", "-q", "-b", BRANCH)
    if remote and "origin" not in git("remote").split():
        git("remote", "add", "origin", remote)


def publish(out: Path, remote: str | None, dry_run: bool) -> None:
    """Один коммит без родителей поверх собранной папки и git push --force в gh-pages."""
    ensure_repo(out, remote)
    git("add", "-A")
    tree = git("write-tree")
    published, with_history = None, False
    if not dry_run:
        try:
            git("fetch", "-q", "--no-tags", "--depth=1", "origin", BRANCH)
            published = git("rev-parse", "FETCH_HEAD^{tree}")
            with_history = "\nparent " in "\n" + git("cat-file", "-p", "FETCH_HEAD")
        except SystemExit:
            published = None      # ветки ещё нет — будет первая публикация (нет сети — скажет push)
    if published == tree and not with_history:
        print("Изменений нет — сайт уже актуален.")
        return
    if published == tree:
        print("Сайт не изменился, но в gh-pages старая история коммитов — заменяю одним коммитом.")
    commit = git("commit-tree", tree, "-m", f"Сайт {datetime.now():%d.%m.%Y %H:%M}", env=committer_env())
    git("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    git("update-ref", f"refs/heads/{BRANCH}", commit)
    if dry_run:
        print(f"--dry-run: в {out} собран коммит {commit[:8]} (без родителей), никуда не отправляю. "
              f"Было бы: git push --force origin {commit[:8]}:refs/heads/{BRANCH}")
        return
    git("push", "-q", "--force", "origin", f"{commit}:refs/heads/{BRANCH}")
    compact_local(out)
    print(f"Опубликовано: {PUBLIC_URL} (обновится через 1–2 минуты)")


def compact_local(out: Path) -> None:
    """Старые сиротские коммиты остаются в локальной .git, пока их не подчистить: когда она больше сайта
    вдвое (+64 МБ) — reflog expire + gc. На GitHub ветка всегда из одного коммита и так."""
    try:
        site_bytes = sum(p.stat().st_size for p in out.rglob("*")
                         if p.is_file() and ".git" not in p.relative_to(out).parts)
        info = dict(line.split(": ", 1) for line in git("count-objects", "-v").splitlines() if ": " in line)
        used = sum(int(info.get(k) or 0) for k in ("size", "size-pack", "size-garbage")) * 1024
        if used > 2 * site_bytes + 64 * 2 ** 20:
            print(f"Подчищаю старые версии в {out / '.git'}: {used / 1e6:.0f} МБ при сайте {site_bytes / 1e6:.0f} МБ")
            git("reflog", "expire", "--expire=now", "--all")
            git("gc", "-q", "--prune=now")
    except (SystemExit, OSError, ValueError) as e:
        print(f"(не удалось подчистить {out / '.git'}: {e})")


# ---------- картинка превью ----------

_THREADS = (   # нити знака из site/brand/ipakly-logo.svg (viewBox 0 0 64 64); S-кривая развёрнута в C
    (((9, 45), (16, 17), (33, 15), (32, 32)), ((32, 32), (31, 49), (47, 51), (55, 21))),
    (((12.5, 47.5), (19.5, 22), (35, 21), (35.5, 34)), ((35.5, 34), (36, 47), (48.5, 50.5), (56.5, 26))),
)
_DOTS = ((9, 45), (55, 21))


def _bezier(seg, n: int):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg
    for i in range(n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
        yield a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3


def _font(names: list[str], size: int):
    from PIL import ImageFont
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    raise SystemExit(f"Не найден ни один шрифт из {names}")


def make_og_image(dst: Path | None = None, site: Path | None = None) -> Path:
    """site/brand/og.png 1200×630: кобальтовый фон, знак-нить и ipakly как в ipakly-logo.svg, подзаголовок."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise SystemExit("Нужен Pillow: python -m pip install pillow")
    site = site or SITE
    dst = dst or site / OG_IMAGE
    name, _, _, tagline = og_texts(site)
    k = 2                                                  # рисуем вдвое крупнее и уменьшаем — сглаживание
    W, H = OG_SIZE[0] * k, OG_SIZE[1] * k
    bg, white = (0x27, 0x47, 0xC9, 255), (255, 255, 255)
    img = Image.new("RGBA", (W, H), bg)

    def layer(draw_fn, alpha: int):
        nonlocal img
        lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_fn(ImageDraw.Draw(lay), white + (alpha,))
        img = Image.alpha_composite(img, lay)

    def thread(d, fill, segs, s, ox, oy, width):
        # штамп кружками вдоль кривой: ровная толщина и круглые концы (stroke-linecap: round)
        r = width / 2
        for seg in segs:
            poly = sum(((seg[i + 1][0] - seg[i][0]) ** 2 + (seg[i + 1][1] - seg[i][1]) ** 2) ** .5 for i in range(3))
            for x, y in _bezier(seg, max(16, int(poly * s / max(0.5, r * 0.3)))):
                cx, cy = ox + x * s, oy + y * s
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)

    # фон: та же нить крупно и едва заметно — «шёлк» в правом верхнем углу, не заходя на текст
    big, bx, by = 11 * k, 700 * k, -190 * k
    layer(lambda d, f: thread(d, f, _THREADS[0], big, bx, by, 3 * k), 34)
    layer(lambda d, f: thread(d, f, _THREADS[1], big, bx, by, 2 * k), 20)
    # знак и надпись: геометрия ipakly-logo.svg (220×64) в масштабе s
    s, x0, y0 = 3.0 * k, 96 * k, 120 * k
    layer(lambda d, f: thread(d, f, _THREADS[1], s, x0, y0, 1.6 * s), 97)       # stroke-opacity .38
    def mark(d, f):
        thread(d, f, _THREADS[0], s, x0, y0, 3.2 * s)
        for cx, cy in _DOTS:
            r = 3.6 * s
            d.ellipse((x0 + cx * s - r, y0 + cy * s - r, x0 + cx * s + r, y0 + cy * s + r), fill=f)
    layer(mark, 255)
    word = _font(["TenorSans-Regular.ttf", "BOD_R.TTF", "Didot.ttc", "Bodoni 72.ttc", "georgia.ttf",
                  "DejaVuSerif.ttf"], round(36 * s))
    def wordmark(d, f):
        # строчными, как в логотипе (ipakly): регистр — как в content.json name, разрядка как у шрифта
        x = x0 + 78 * s
        for ch in name:
            d.text((x, y0 + 44 * s), ch, font=word, fill=f, anchor="ls")
            x += word.getlength(ch) + 0.5 * s
    layer(wordmark, 255)
    ui = ["GolosText-Regular.ttf", "segoeui.ttf", "DejaVuSans.ttf"]
    ui_light = ["GolosText-Regular.ttf", "segoeuil.ttf", "segoeui.ttf", "DejaVuSans.ttf"]
    tag_font = _font(ui_light, 50 * k)
    tag = tagline
    while tag_font.getlength(tag) > W - 2 * x0 and len(tag) > 10:
        tag = _cut(tag, len(tag) - 2)
    layer(lambda d, f: d.text((x0, 400 * k), tag, font=tag_font, fill=f, anchor="ls"), 240)
    layer(lambda d, f: d.line((x0, 446 * k, x0 + 72 * k, 446 * k), fill=f, width=2 * k), 150)
    foot = _font(ui, 28 * k)
    layer(lambda d, f: d.text((x0, 498 * k), OG_FOOTER, font=foot, fill=f, anchor="ls"), 205)
    out = img.resize(OG_SIZE, Image.LANCZOS).convert("RGB")
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, optimize=True)
    print(f"Картинка превью: {dst} ({OG_SIZE[0]}×{OG_SIZE[1]}, {dst.stat().st_size // 1024} КБ)")
    return dst


# ---------- main ----------

def main(argv: list[str] | None = None) -> None:
    global OUT
    ap = argparse.ArgumentParser(description="Выложить сайт на GitHub Pages (ветка gh-pages)")
    ap.add_argument("--out", help="рабочая копия ветки gh-pages (по умолчанию ~/yurt-pages)")
    ap.add_argument("--dry-run", action="store_true",
                    help="собрать и проверить папку, коммит только локально — без fetch и push")
    ap.add_argument("--make-og", action="store_true", help=f"перерисовать site/{OG_IMAGE} и выйти (нужен Pillow)")
    args = ap.parse_args(argv)
    if args.make_og:
        make_og_image()
        return
    if args.out:
        OUT = Path(args.out).resolve()
    for f in PUBLIC_FILES:
        if not (SITE / f).exists():
            raise SystemExit(f"Нет site/{f} — сначала запустите python run.py")
    data_files = catalog_files.published_files(SITE)
    missing = [f for f in data_files if not (SITE / f).is_file()]
    if missing:
        raise SystemExit(f"Каталог data/ неполный (нет {missing[:3]}…) — запустите python run.py ещё раз")
    if (SITE / "content.json").exists():
        import content
        content.build()                       # content.js — по свежему content.json
    if (SITE / "brands.json").exists():
        import brands
        brands.build(quiet=True)              # brands.js — по свежему brands.json
    try:
        remote = git("remote", "get-url", "origin", cwd=ROOT)
    except SystemExit:
        if not args.dry_run:
            raise
        remote = None

    build_output(OUT, data_files)
    leaked = private_leaks(OUT)
    if leaked:
        raise SystemExit(f"Стоп: в публикацию попали закрытые данные {leaked[:10]}")
    problems = contact_problems(SITE, ROOT)
    if problems:
        loud_warning(problems)
    publish(OUT, remote, args.dry_run)


if __name__ == "__main__":
    main()
