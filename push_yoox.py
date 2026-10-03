"""Отправить свежие файлы YOOX (кнопка «Сохранить YOOX» → «Загрузки») туда, где каталог собирается сам.

Запускается на вашем компьютере после того, как кнопка сохранила yoox_*.json.

Свой сервер (основной вариант, server/setup.sh):
    python push_yoox.py --setup yurt@203.0.113.10      # один раз: ключ SSH и адрес сервера
    python push_yoox.py                                # отправить новые файлы во «входящие» сервера
    python push_yoox.py --dry-run                      # только показать, какие файлы ушли бы
    python push_yoox.py --check                        # проверить связь с сервером
    python push_yoox.py --migrate                      # один раз: перенести на сервер data/ и фото YOOX с компьютера

  Файлы копируются по SSH (scp, только по ключу — пароли не используются) в папку «входящие» сервера
  (YURT_YOOX_FOLDER, по умолчанию /opt/yurt/inbox). Сервер проверяет её каждые 15 минут
  (yurt-yoox.timer); с --now (по умолчанию включено) импорт и публикация запускаются сразу.
  Что уже отправлено — в ~/.yurt/pushed.json, повторно не отправляется.

GitHub Actions (запасной вариант, .github/workflows/update.yml):
    python push_yoox.py --github             # data ← ветка data; импорт YOOX здесь; ветка data ← data; публикация
    python push_yoox.py --github --dry-run   # всё, кроме отправки в GitHub
    python push_yoox.py --github --seed      # первый раз: создать ветку data из текущих данных этого компьютера

Настройки — ~/.yurt/push.json (создаёт --setup):
    {"transport": "ssh", "host": "yurt@203.0.113.10", "port": 22, "key": "~/.ssh/yurt_ed25519",
     "inbox": "/opt/yurt/inbox", "app": "/opt/yurt/app", "run_now": true}
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
YURT_DIR = Path.home() / ".yurt"
SETTINGS = YURT_DIR / "push.json"
PUSHED = YURT_DIR / "pushed.json"
DEFAULTS = {"transport": "ssh", "port": 22, "key": str(Path.home() / ".ssh" / "yurt_ed25519"),
            "inbox": "/opt/yurt/inbox", "app": "/opt/yurt/app", "env_file": "/etc/yurt/yurt.env", "run_now": True}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS.is_file():
        s.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    return s


def yoox_opts() -> dict:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    return (cfg.get("source_opts") or {}).get("yoox_import") or {}


def candidate_files() -> list[Path]:
    so = yoox_opts()
    folder = Path(so.get("folder") or Path.home() / "Downloads").expanduser()
    cutoff = time.time() - float(so.get("max_age_days", 3)) * 86400
    files = [f for f in folder.glob("yoox_*.json") if f.is_file() and f.stat().st_mtime >= cutoff]
    return sorted(files, key=lambda f: f.stat().st_mtime)


def tool(name: str) -> str:
    """ssh/scp/ssh-keygen: встроенный OpenSSH Windows, иначе из Git for Windows, иначе из PATH."""
    for cand in (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / f"{name}.exe",):
        if cand.is_file():
            return str(cand)
    exe = shutil.which(name)
    if not exe:
        raise SystemExit(f"Не найден {name}. Windows: Параметры → Приложения → Дополнительные компоненты → «Клиент OpenSSH».")
    return exe


def ssh_base(s: dict, prog: str) -> list[str]:
    port_flag = "-P" if prog == "scp" else "-p"
    return [tool(prog), "-i", str(Path(s["key"]).expanduser()), port_flag, str(s["port"]),
            "-o", "BatchMode=yes",                     # никаких запросов пароля: только ключ
            "-o", "StrictHostKeyChecking=accept-new",  # ключ сервера запоминается при первом подключении
            "-o", "ConnectTimeout=20"]


# ---------- свой сервер (SSH) ----------

def cmd_setup(args) -> int:
    YURT_DIR.mkdir(exist_ok=True)
    s = load_settings()
    s.update(transport="ssh", host=args.setup)
    if args.port:
        s["port"] = args.port
    if args.inbox:
        s["inbox"] = args.inbox
    key = Path(s["key"]).expanduser()
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([tool("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", "yurt push_yoox", "-f", str(key)],
                       check=True)
        print(f"Создан ключ {key} (закрытый — никому не отдавайте) и {key}.pub")
    SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Настройки сохранены: {SETTINGS}")
    print("\nПубличный ключ этого компьютера (его можно показывать) — добавьте на сервер:")
    print("  " + Path(str(key) + ".pub").read_text(encoding="utf-8").strip())
    print("\nНа сервере: сохраните эту строку в файл, например /root/pc.pub, и выполните")
    print("  sudo OWNER_PUBKEY_FILE=/root/pc.pub bash /opt/yurt/app/server/setup.sh")
    print("Проверка отсюда:  python push_yoox.py --check")
    return 0


def remote_run_cmd(s: dict, job: str) -> str:
    return (f"set -a; . {shlex.quote(s['env_file'])}; set +a; "
            f"{shlex.quote(s['app'])}/server/yurt-run.sh {job}")


def cmd_check(s: dict) -> int:
    r = subprocess.run([*ssh_base(s, "ssh"), s["host"], f"ls -ld {shlex.quote(s['inbox'])} && hostname"],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    print("Связь с сервером есть." if r.returncode == 0 else "Связи нет (адрес, ключ на сервере, порт?).")
    return r.returncode


def push_ssh(s: dict, args) -> int:
    if not s.get("host"):
        raise SystemExit("Сервер не настроен: python push_yoox.py --setup пользователь@адрес")
    pushed = json.loads(PUSHED.read_text(encoding="utf-8")) if PUSHED.is_file() else {}
    todo = [f for f in candidate_files() if pushed.get(f.name) != [f.stat().st_size, int(f.stat().st_mtime)]]
    if not todo:
        print("Новых файлов yoox_*.json нет (уже отправлены или старше max_age_days). Нажмите кнопку «Сохранить YOOX».")
        return 0
    print(f"Отправляю на {s['host']}:{s['inbox']}: " + ", ".join(f.name for f in todo))
    if args.dry_run:
        print("--dry-run: ничего не отправлено.")
        return 0
    inbox = s["inbox"].rstrip("/")
    # сначала в подпапку .incoming, потом переложить во «входящие» — сервер не возьмёт недокачанный файл
    r = subprocess.run([*ssh_base(s, "ssh"), s["host"], f"mkdir -p {shlex.quote(inbox)}/.incoming"])
    if r.returncode != 0:
        raise SystemExit("Не удалось подключиться к серверу (python push_yoox.py --check).")
    r = subprocess.run([*ssh_base(s, "scp"), "-q", *[str(f) for f in todo], f"{s['host']}:{inbox}/.incoming/"])
    if r.returncode != 0:
        raise SystemExit("Копирование не удалось — ничего не помечено как отправленное.")
    mv = " && ".join(f"mv -f {shlex.quote(inbox + '/.incoming/' + f.name)} {shlex.quote(inbox + '/' + f.name)}"
                     for f in todo)
    r = subprocess.run([*ssh_base(s, "ssh"), s["host"], mv])
    if r.returncode != 0:
        raise SystemExit("Файлы скопированы, но не переложены во «входящие» — повторите команду.")
    for f in todo:
        pushed[f.name] = [f.stat().st_size, int(f.stat().st_mtime)]
    YURT_DIR.mkdir(exist_ok=True)
    PUSHED.write_text(json.dumps(pushed, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Отправлено файлов: {len(todo)}.")
    if s.get("run_now", True) and not args.no_run:
        print("Запускаю импорт на сервере (идёт несколько минут)…")
        r = subprocess.run([*ssh_base(s, "ssh"), s["host"], remote_run_cmd(s, "yoox")])
        if r.returncode != 0:
            print("Импорт на сервере завершился с ошибкой — сервер попробует ещё раз сам через 15 минут.")
            return r.returncode
    else:
        print("Сервер возьмёт их в течение 15 минут (yurt-yoox.timer).")
    return 0


def cmd_migrate(s: dict, args) -> int:
    """Один раз: перенести на сервер память и данные этого компьютера, чтобы сервер начал не с нуля."""
    if not s.get("host"):
        raise SystemExit("Сервер не настроен: python push_yoox.py --setup пользователь@адрес")
    data, site = ROOT / "data", ROOT / "site"
    files = [p for p in [data / "state.json", data / "sold_out.json", data / "fx.json", *sorted(data.glob("raw_*.json"))]
             if p.is_file()]
    site_files = [p for p in (site / "products.json", site / "products-admin.js") if p.is_file()]
    img = site / "img" / "yoox"
    app = s["app"].rstrip("/")
    print(f"На {s['host']}:{app}: data/ ← {len(files)} файлов, site/ ← {len(site_files)}"
          + (f", site/img/yoox ← {sum(1 for _ in img.iterdir())} фото" if img.is_dir() else ""))
    if args.dry_run:
        print("--dry-run: ничего не отправлено.")
        return 0
    steps = [[*ssh_base(s, "ssh"), s["host"], f"mkdir -p {shlex.quote(app)}/data {shlex.quote(app)}/site/img"],
             [*ssh_base(s, "scp"), "-q", *map(str, files), f"{s['host']}:{app}/data/"],
             [*ssh_base(s, "scp"), "-q", *map(str, site_files), f"{s['host']}:{app}/site/"] if site_files else None,
             [*ssh_base(s, "scp"), "-q", "-r", str(img), f"{s['host']}:{app}/site/img/"] if img.is_dir() else None]
    for cmd in filter(None, steps):
        if subprocess.run(cmd).returncode != 0:
            raise SystemExit("Перенос не удался (python push_yoox.py --check).")
    print("Перенесено. Первый сбор на сервере: sudo systemctl start yurt-job@update.service")
    return 0


# ---------- GitHub Actions (ветка data) ----------

def run_py(*argv: str) -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    print("$ python " + " ".join(argv))
    return subprocess.run([sys.executable, *argv], cwd=ROOT, env=env).returncode


def push_github(args) -> int:
    import datastore
    yoox_raw = ROOT / "data" / "raw_yoox_import.json"
    if not args.seed:
        if datastore.main(["pull", "--site-from-branch"]) != 0:
            return 1
    before = datastore.content_hash([yoox_raw]) if yoox_raw.is_file() else None
    if not args.no_import:
        if run_py("run.py", "--only", "yoox_import") != 0:
            print("run.py завершился с ошибкой — в GitHub ничего не отправляю.")
            return 1
        after = datastore.content_hash([yoox_raw]) if yoox_raw.is_file() else None
        if after == before and not args.seed:
            print("Новых данных YOOX нет (нажмите кнопку «Сохранить YOOX») — в GitHub ничего не отправляю.")
            return 0
    push = ["push", "--by", "local"] + (["--seed"] if args.seed else []) + (["--dry-run"] if args.dry_run else [])
    if datastore.main(push) != 0:
        return 1
    if args.dry_run or args.no_deploy:
        print("Сайт не публикую (--dry-run / --no-deploy).")
        return 0
    # публикуем сразу: и чтобы новые товары появились, и чтобы облако получило их фото (img/p в gh-pages)
    return run_py("deploy.py")


def main() -> int:
    ap = argparse.ArgumentParser(description="Отправить файлы YOOX на сервер или в GitHub")
    ap.add_argument("--setup", metavar="ПОЛЬЗОВАТЕЛЬ@АДРЕС", help="один раз: ключ SSH и адрес сервера")
    ap.add_argument("--port", type=int, help="порт SSH сервера (для --setup)")
    ap.add_argument("--inbox", help="папка «входящие» на сервере (для --setup), по умолчанию /opt/yurt/inbox")
    ap.add_argument("--check", action="store_true", help="проверить связь с сервером")
    ap.add_argument("--migrate", action="store_true",
                    help="один раз: перенести на сервер память и данные этого компьютера (data/, фото YOOX)")
    ap.add_argument("--no-run", action="store_true", help="не запускать импорт на сервере сразу (подождать таймер)")
    ap.add_argument("--github", action="store_true", help="вариант GitHub Actions: через ветку data")
    ap.add_argument("--seed", action="store_true", help="с --github: первый раз создать ветку data из своих данных")
    ap.add_argument("--no-import", action="store_true", help="с --github: не импортировать, только отправить данные")
    ap.add_argument("--no-deploy", action="store_true", help="с --github: не публиковать сайт")
    ap.add_argument("--dry-run", action="store_true", help="ничего не отправлять, только показать")
    args = ap.parse_args()
    if args.setup:
        return cmd_setup(args)
    s = load_settings()
    if args.check:
        return cmd_check(s)
    if args.migrate:
        return cmd_migrate(s, args)
    if args.github or s.get("transport") == "github":
        return push_github(args)
    return push_ssh(s, args)


if __name__ == "__main__":
    sys.exit(main())
