"""Выложить сайт на GitHub Pages (ветка gh-pages репозитория из origin).

    python deploy.py                 # рабочая копия gh-pages — ~/yurt-pages
    python deploy.py --out _pages    # своя папка (так делает GitHub Actions: там уже checkout gh-pages)

Перед публикацией берётся свежая ветка gh-pages из origin (её же обновляет облачный запуск),
поэтому публикация с компьютера и из облака не мешают друг другу.

Публикуется только публичная часть site/: index.html, каталог частями data/ (manifest.json и ровно те части
индекса data/i/ и подробностей data/d/, что в нём перечислены — устаревшие части в gh-pages не попадают),
products.js — маленькое оглавление (window.DEALS_MANIFEST, для старых браузеров), тексты продавца
content.js / content.json (content.js пересобирается из content.json), логотипы брендов brands.json /
brands.js / img/brands/ (brands.js пересобирается из brands.json) и скачанные фото img/p/.
Старый products.json (весь каталог одним файлом, для открытия с диска) не публикуется — сайту он не нужен.
Если раскладки data/ ещё нет (run.py старой версии), публикуются products.js / products.json как раньше.
Закрытое НЕ публикуется никогда: products-admin.js и site/admin/** (закупочные цены, маржа, ссылки на
магазины), старые папки фото с названием источника; перед коммитом это проверяется ещё раз, в том числе
по содержимому файлов данных. На страницу добавляется <meta name="robots" content="noindex">.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import catalog_files

ROOT = Path(__file__).parent
SITE = ROOT / "site"
OUT = Path(os.environ.get("DEPLOY_OUT") or Path.home() / "yurt-pages")   # рабочая копия ветки gh-pages
PUBLIC_FILES = ["index.html", "products.js"]
LEGACY_FILES = ["products.json"]                   # только если раскладки data/ нет
OPTIONAL_FILES = ["content.js", "content.json",     # тексты продавца (content.py), если есть
                  "brands.js", "brands.json"]       # логотипы брендов (brands.py), если есть
PUBLIC_DIRS = ["brand", "img/p", "img/brands"]
FORBIDDEN = ["products-admin.js", "admin"]         # закрытые данные: файл и папка site/admin/**

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd or OUT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def main() -> None:
    global OUT
    ap = argparse.ArgumentParser(description="Выложить сайт на GitHub Pages (ветка gh-pages)")
    ap.add_argument("--out", help="рабочая копия ветки gh-pages (по умолчанию ~/yurt-pages)")
    args = ap.parse_args()
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
    remote = git("remote", "get-url", "origin", cwd=ROOT)

    if not (OUT / ".git").exists():
        OUT.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "gh-pages")
        git("remote", "add", "origin", remote)
        try:
            git("fetch", "-q", "origin", "gh-pages")
            git("reset", "-q", "--soft", "origin/gh-pages")
        except SystemExit:
            pass   # ветки ещё нет — будет первая публикация
    else:
        # ветку могли обновить из облака (GitHub Actions) или с другого компьютера — строим поверх неё
        try:
            git("fetch", "-q", "origin", "gh-pages")
            git("reset", "-q", "--soft", "FETCH_HEAD")
        except SystemExit:
            pass

    # чистим всё, кроме .git, и копируем публичные файлы заново
    for item in OUT.iterdir():
        if item.name == ".git":
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    for f in PUBLIC_FILES:
        shutil.copy2(SITE / f, OUT / f)
    if data_files:
        # каталог частями: только перечисленное в манифесте; products.js — оглавление (без 25+ МБ старого формата)
        for f in data_files:
            (OUT / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SITE / f, OUT / f)
        manifest = (SITE / catalog_files.DATA_DIR / "manifest.json").read_text(encoding="utf-8")
        (OUT / "products.js").write_text(catalog_files.stub_js(manifest), encoding="utf-8")
    else:
        for f in LEGACY_FILES:
            if (SITE / f).exists():
                shutil.copy2(SITE / f, OUT / f)
    for f in OPTIONAL_FILES:
        if (SITE / f).exists():
            shutil.copy2(SITE / f, OUT / f)
    for d in PUBLIC_DIRS:
        if (SITE / d).exists():
            shutil.copytree(SITE / d, OUT / d)
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    page = (OUT / "index.html").read_text(encoding="utf-8")
    if 'name="robots"' not in page:
        page = page.replace("<head>", '<head>\n<meta name="robots" content="noindex, nofollow">', 1)
        (OUT / "index.html").write_text(page, encoding="utf-8")

    leaked = [f for f in FORBIDDEN if (OUT / f).exists()]
    leaked += [p.relative_to(OUT).as_posix() for p in OUT.rglob("*")
               if ".git" not in p.parts and (p.name.startswith("products-admin") or "admin" in p.relative_to(OUT).parts[:1])]
    # и по содержимому: в публичных данных не должно быть закупочных полей
    for p in [OUT / "products.js", OUT / "products.json", *(OUT / catalog_files.DATA_DIR).rglob("*.js*")]:
        if p.is_file() and any(m in p.read_bytes() for m in catalog_files.ADMIN_LEAK_MARKERS):
            leaked.append(p.relative_to(OUT).as_posix())
    if leaked:
        raise SystemExit(f"Стоп: в публикацию попали закрытые данные {sorted(set(leaked))[:10]}")

    git("add", "-A")
    if not git("status", "--porcelain"):
        print("Изменений нет — сайт уже актуален.")
        return
    git("commit", "-q", "-m", f"Обновление каталога {datetime.now():%d.%m.%Y %H:%M}")
    git("push", "-q", "-u", "origin", "gh-pages")
    user_repo = remote.rstrip("/").removesuffix(".git").split("github.com/")[-1]
    user, repo = user_repo.split("/", 1)
    print(f"Опубликовано: https://{user.lower()}.github.io/{repo}/ (обновится через 1–2 минуты)")


if __name__ == "__main__":
    main()
