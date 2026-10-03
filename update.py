"""Регулярное обновление каталога (для Планировщика заданий Windows).

    python update.py                   # один проход: пересобрать турецкие магазины + свежие файлы YOOX
    python update.py --no-deploy       # то же, но не публиковать, даже если sync.auto_deploy = true
    python update.py --print-schedule  # напечатать команду schtasks для запуска каждые sync.every_hours часов
    python update.py --yoox-only       # только новые файлы YOOX (сервер, каждые 15 минут); нет файлов — тихо выйти
    python update.py --accept-drop     # передать run.py --accept-drop (распродажа правда закончилась)

Свой сервер (server/yurt-run.sh) и GitHub Actions вызывают update.py --no-deploy и публикуют сами.

Что делает один проход:
  1. run.py --only pcardin_tr,cacharel_tr,trendyol (+ yoox_import, если в «Загрузках» появился файл
     yoox_*.json новее data/raw_yoox_import.json). run.py сравнивает с прошлым запуском: распроданные
     помечаются «Нет в наличии», закончившиеся размеры — зачёркнуты, изменения — в data/changes/.
  2. deploy.py — только если в config.json → sync.auto_deploy = true (по умолчанию false).
Журнал — data/logs/update_ГГГГММДД_ччмм.log (старше 30 дней удаляются). Замок data/update.lock не даёт
двум проходам идти одновременно: второй сразу выходит.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import sync_state

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOGS = DATA / "logs"
AUTO_SOURCES = ["pcardin_tr", "cacharel_tr", "trendyol"]   # собираются сами, без участия человека
TASK_NAME = "YooxDeals Update"
LOG_KEEP_DAYS = 30

if sys.stdout is None:                     # pythonw.exe (Планировщик без окна): печатать некуда
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = sys.stdout
elif hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def load_cfg() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def sync_opts(cfg: dict) -> dict:
    out = {"keep_sold_out_days": 3, "auto_deploy": False, "every_hours": 6}
    out.update({k: v for k, v in (cfg.get("sync") or {}).items() if not str(k).startswith("_")})
    return out


def console_python() -> str:
    """python.exe рядом с pythonw.exe: дочерним процессам нужен stdout для журнала."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def fresh_yoox_files(cfg: dict) -> list[Path]:
    """Файлы кнопки «Сохранить YOOX», которых run.py ещё не видел (новее data/raw_yoox_import.json)."""
    so = (cfg.get("source_opts") or {}).get("yoox_import") or {}
    # YURT_YOOX_FOLDER — папка-«входящие» на сервере (server/setup.sh), важнее config.json
    folder = Path(os.environ.get("YURT_YOOX_FOLDER") or so.get("folder") or Path.home() / "Downloads").expanduser()
    raw = DATA / "raw_yoox_import.json"
    seen = raw.stat().st_mtime if raw.exists() else 0
    cutoff = time.time() - float(so.get("max_age_days", 3)) * 86400
    try:
        files = list(folder.glob("yoox_*.json"))
    except OSError:
        return []
    return sorted((f for f in files if f.stat().st_mtime > max(seen, cutoff)), key=lambda f: f.stat().st_mtime)


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")
        self.path = path

    def __call__(self, line: str = "") -> None:
        self.f.write(line + "\n")
        self.f.flush()
        try:
            print(line, flush=True)
        except Exception:
            pass

    def close(self) -> None:
        self.f.close()


def run_step(log: Log, args: list[str]) -> int:
    log(f"$ {' '.join(args)}")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    t0 = time.time()
    proc = subprocess.Popen([console_python(), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            creationflags=flags)
    for line in proc.stdout:
        log(line.rstrip("\n"))
    rc = proc.wait()
    log(f"→ код выхода {rc}, {time.time() - t0:.0f} с")
    return rc


def cleanup_logs() -> None:
    cutoff = time.time() - LOG_KEEP_DAYS * 86400
    for f in LOGS.glob("update_*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def schedule_command(cfg: dict) -> str:
    every = max(1, int(sync_opts(cfg).get("every_hours") or 6))
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    script = ROOT / "update.py"
    if " " in str(pyw) or " " in str(script):
        tr = f'"\\"{pyw}\\" \\"{script}\\""'          # пути с пробелами — кавычки внутри /TR (для cmd.exe)
    else:
        tr = f'"{pyw} {script}"'
    when = f"/SC HOURLY /MO {every}" if every < 24 else f"/SC DAILY /MO {max(1, every // 24)}"
    return f'schtasks /Create /TN "{TASK_NAME}" /TR {tr} {when} /ST 07:00 /F'


def main() -> int:
    ap = argparse.ArgumentParser(description="Регулярное обновление каталога")
    ap.add_argument("--print-schedule", action="store_true", help="напечатать команду schtasks и выйти")
    ap.add_argument("--no-deploy", action="store_true", help="не публиковать сайт в этом проходе")
    ap.add_argument("--yoox-only", action="store_true",
                    help="только импорт новых файлов YOOX (если их нет — тихо выйти); для сервера, каждые 15 минут")
    ap.add_argument("--accept-drop", action="store_true",
                    help="передать run.py --accept-drop (распродажа правда закончилась)")
    args = ap.parse_args()
    cfg = load_cfg()

    if args.print_schedule:
        print("Зарегистрировать задание (одна команда, в «Командной строке» или PowerShell):")
        print()
        print("  " + schedule_command(cfg))
        print()
        print(f'Проверить сразу:  schtasks /Run /TN "{TASK_NAME}"')
        print(f'Посмотреть:       schtasks /Query /TN "{TASK_NAME}" /V /FO LIST')
        print(f'Удалить:          schtasks /Delete /TN "{TASK_NAME}" /F')
        print("Журналы проходов: data\\logs\\update_*.log")
        return 0

    if args.yoox_only and not ("yoox_import" in cfg.get("sources", []) and fresh_yoox_files(cfg)):
        print("Новых файлов YOOX нет — ничего не делаю.")
        return 0
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    lock = sync_state.Lock(DATA / "update.lock", "update.py", max_age_s=6 * 3600)
    if not lock.try_acquire():
        other = lock.info()
        print(f"Предыдущее обновление ещё идёт (pid {other.get('pid')}, с {other.get('started')}) — выхожу.")
        return 0
    log = Log(LOGS / f"update_{stamp}.log")
    try:
        opts = sync_opts(cfg)
        log(f"=== Обновление каталога {datetime.now():%d.%m.%Y %H:%M} ===")
        sources = [] if args.yoox_only else [s for s in AUTO_SOURCES if s in cfg.get("sources", [])]
        if "yoox_import" in cfg.get("sources", []):
            fresh = fresh_yoox_files(cfg)
            if fresh:
                sources.append("yoox_import")
                log(f"Новые файлы YOOX: {', '.join(f.name for f in fresh)}")
            else:
                log("Новых файлов YOOX нет — товары YOOX остаются как были (YOOX собирается только кнопкой).")
        if not sources:
            log("Нет источников для обновления (config.json → sources).")
            return 0
        rc = run_step(log, ["run.py", "--only", ",".join(sources), *(["--accept-drop"] if args.accept_drop else [])])
        if rc != 0:
            log("run.py завершился с ошибкой — сайт не публикую.")
            return rc
        if opts.get("auto_deploy") and not args.no_deploy:
            rc = run_step(log, ["deploy.py"])
        else:
            log("Публикация выключена (config.json → sync.auto_deploy = false). Выложить вручную: python deploy.py")
        return rc
    except Exception as e:
        log(f"ОШИБКА update.py: {e.__class__.__name__}: {e}")
        return 1
    finally:
        log(f"Журнал: {log.path}")
        log.close()
        lock.release()
        cleanup_logs()


if __name__ == "__main__":
    sys.exit(main())
