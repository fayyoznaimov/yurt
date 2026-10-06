"""Еженедельная уборка на сервере (server/yurt-run.sh cleanup).

    python server/cleanup.py            # удалить
    python server/cleanup.py --dry-run  # только показать, что было бы удалено

Что удаляется:
  * data/logs/*.log старше 30 дней;
  * data/changes/*.json старше 90 дней;
  * data/events/*.jsonl (счётчик воронки) старше 90 дней;
  * data/backup_local/* старше 7 дней;
  * во «входящих» YOOX (только папка из YURT_YOOX_FOLDER, «Загрузки» на Windows не трогаем) файлы
    yoox_*.json старше 14 дней — run.py их уже не берёт (source_opts.yoox_import.max_age_days);
  * site/img/yoox/* — скачанные фото товаров, которых больше нет в data/raw_yoox_import.json, старше 30 дней.
    (Фото сайта site/img/p run.py убирает сам при каждой сборке.)
  * кэш своего хоста фото (IMG_CACHE/p/*.webp, img_api.py): фото, токена которых больше нет в карте
    data/img_map.json, старше 30 дней. Карты нет — кэш не трогаем.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DAY = 86400

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def old(p: Path, days: float) -> bool:
    try:
        return time.time() - p.stat().st_mtime > days * DAY
    except OSError:
        return False


def img_cache_victims(days: float = 30) -> list[Path]:
    """Фото кэша своего хоста (IMG_CACHE/p/<токен>.<ширина>.webp), токена которых нет в карте, старше days дней."""
    sys.path.insert(0, str(ROOT))
    import img_map
    cache = Path(os.environ.get("IMG_CACHE") or DATA / "img_cache") / "p"
    mp = img_map.map_path(DATA)
    if not cache.is_dir() or not mp.is_file():
        return []
    items = img_map.load_items(mp)
    if not items:
        return []                     # пустая/битая карта — ничего не удаляем
    return [p for p in cache.glob("*.webp") if p.name.split(".")[0] not in items and old(p, days)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    victims: list[Path] = []
    victims += [p for p in (DATA / "logs").glob("*.log") if old(p, 30)]
    victims += [p for p in (DATA / "changes").glob("*.json") if old(p, 90)]
    victims += [p for p in (DATA / "events").glob("*.jsonl") if old(p, 90)]
    if (DATA / "backup_local").is_dir():
        victims += [p for p in (DATA / "backup_local").iterdir() if old(p, 7)]
    inbox = os.environ.get("YURT_YOOX_FOLDER")
    if inbox and Path(inbox).is_dir():
        victims += [p for p in Path(inbox).glob("yoox_*.json") if old(p, 14)]

    img_dir = ROOT / "site" / "img" / "yoox"
    raw = DATA / "raw_yoox_import.json"
    if img_dir.is_dir() and raw.is_file():
        try:
            rows = json.loads(raw.read_text(encoding="utf-8"))
            keep = {Path(i).name for r in rows for i in (r.get("images") or []) if not str(i).startswith(("http", "//"))}
            victims += [p for p in img_dir.iterdir() if p.is_file() and p.name not in keep and old(p, 30)]
        except (OSError, ValueError) as e:
            print(f"data/raw_yoox_import.json не читается ({e}) — фото YOOX не трогаю")
    victims += img_cache_victims()

    size = 0
    for p in victims:
        size += sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
        if not args.dry_run:
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    verb = "Было бы удалено" if args.dry_run else "Удалено"
    print(f"{verb}: {len(victims)} файлов/папок, {size / 1e6:.1f} МБ")
    for p in victims[:20]:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
