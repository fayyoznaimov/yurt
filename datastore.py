"""Память каталога между запусками в облаке (GitHub Actions): зашифрованный архив в ветке `data`.

    python datastore.py init-key        # один раз: создать ключ шифрования (~/.yurt/data_key.txt)
    python datastore.py pull            # data/ ← ветка data (+ прошлые карточки site/data/ из gh-pages)
    python datastore.py push            # ветка data ← data/ (только если данные изменились)
    python datastore.py push --dry-run  # собрать коммит, но не отправлять
    python datastore.py status          # что лежит в ветке data
    python datastore.py summary         # статусы источников (в GitHub Actions — в сводку запуска)
    python datastore.py selftest        # проверить упаковку + шифрование + распаковку на этом компьютере

Зачем: run.py помнит прошлый запуск (data/state.json, data/sold_out.json, data/raw_*.json …), а машина
GitHub каждый раз новая и пустая. Эти файлы git-ignored и НЕ должны быть видны всем: репозиторий публичный,
а в них ссылки на магазины и закупочные цены (как и в site/products-admin.js). Поэтому они хранятся в ветке
`data` одним архивом tar.gz, зашифрованным AES-256 (openssl, ключ — секрет DATA_KEY в GitHub и файл
~/.yurt/data_key.txt у владельца). Без ключа архив — набор случайных байт.

Ветка `data` всегда из ОДНОГО коммита (перезаписывается), чтобы репозиторий не рос на ~10 МБ за запуск.
В коммите: bundle.tar.gz.enc (сейчас), bundle.prev.tar.gz.enc (прошлый — на случай отката), manifest.json
(когда и кем записан, размеры, без содержимого), README.md. Запись — с --force-with-lease: если ветку за это
время обновил кто-то другой (облако или push_yoox.py), отправка не пройдёт и ничего не затрётся.

Ключ: переменная окружения DATA_KEY, иначе файл из DATA_KEY_FILE, иначе ~/.yurt/data_key.txt.
Нужен openssl: на машинах GitHub есть, на Windows — из Git for Windows (ищется рядом с git.exe).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SITE = ROOT / "site"
BRANCH = "data"
BUNDLE = "bundle.tar.gz.enc"
PREV_BUNDLE = "bundle.prev.tar.gz.enc"
MANIFEST = "manifest.json"
PULL_INFO = DATA / ".datastore.json"       # какой коммит ветки data взят последним (для --force-with-lease)
KEY_FILE = Path.home() / ".yurt" / "data_key.txt"
CHANGES_KEEP = 60                           # сколько последних data/changes/*.json хранить в архиве
BACKUPS_KEEP = 2
OPENSSL_ARGS = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256"]
BRANCH_README = """# Данные каталога (зашифрованы)

Служебная ветка: память сборщика между запусками в GitHub Actions (`datastore.py`).
`bundle.tar.gz.enc` — архив data/ и закрытых файлов сайта, зашифрован AES-256 (openssl enc -pbkdf2).
Ключ — секрет репозитория DATA_KEY. Ветка перезаписывается каждым запуском (всегда один коммит).
"""

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


class StoreError(RuntimeError):
    pass


# ---------- что входит в архив ----------

def bundle_files(root: Path = ROOT) -> list[Path]:
    """Файлы, которые нужны run.py в следующий раз. Пути — относительно root."""
    data = root / "data"
    out: list[Path] = []
    for name in ("state.json", "sold_out.json", "fx.json", "notify_state.json"):
        if (data / name).is_file():
            out.append(data / name)
    out += sorted(p for p in data.glob("raw_*.json") if p.is_file())        # и raw_*.meta.json
    changes = sorted((data / "changes").glob("*.json")) if (data / "changes").is_dir() else []
    out += changes[-CHANGES_KEEP:]
    admin = root / "site" / "products-admin.js"     # закупочные данные распроданных карточек (?admin=1)
    if admin.is_file():
        out.append(admin)
    adir = root / "site" / "admin"                  # то же, когда каталог большой: по частям (catalog_files.py)
    if adir.is_dir():
        out += sorted(p for p in adir.glob("*.js") if p.is_file())
    return out


def allowed_member(name: str) -> bool:
    if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
        return False
    return (name.startswith("data/") or name == "site/products-admin.js"
            or (name.startswith("site/admin/") and name.count("/") == 2 and name.endswith(".js")))


def content_hash(files: list[Path], root: Path = ROOT) -> str:
    """Отпечаток содержимого (имена + байты, без дат) — понять, изменилось ли что-нибудь."""
    h = hashlib.sha256()
    for f in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        h.update(f.relative_to(root).as_posix().encode() + b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


# ---------- ключ и openssl ----------

def load_key() -> str:
    key = os.environ.get("DATA_KEY", "").strip()
    if key:
        return key
    path = Path(os.environ.get("DATA_KEY_FILE") or KEY_FILE).expanduser()
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise StoreError(f"Нет ключа шифрования: задайте переменную DATA_KEY или создайте {KEY_FILE} "
                     f"(python datastore.py init-key). В GitHub — секрет репозитория DATA_KEY.")


def find_openssl() -> str:
    exe = shutil.which("openssl")
    if exe:
        return exe
    git = shutil.which("git")
    cands = []
    if git:   # ...\Git\cmd\git.exe -> ...\Git\usr\bin\openssl.exe
        base = Path(git).resolve().parent.parent
        cands += [base / "usr" / "bin" / "openssl.exe", base / "mingw64" / "bin" / "openssl.exe"]
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), r"C:\Program Files"):
        if pf:
            cands += [Path(pf) / "Git" / "usr" / "bin" / "openssl.exe", Path(pf) / "Git" / "mingw64" / "bin" / "openssl.exe"]
    for c in cands:
        if c.is_file():
            return str(c)
    raise StoreError("Не найден openssl. На Windows он входит в Git for Windows (C:\\Program Files\\Git\\usr\\bin).")


def _openssl(src: Path, dst: Path, key: str, decrypt: bool) -> None:
    env = dict(os.environ, YURT_DATA_KEY=key)
    args = [find_openssl(), *OPENSSL_ARGS, *(["-d"] if decrypt else ["-salt"]),
            "-pass", "env:YURT_DATA_KEY", "-in", str(src), "-out", str(dst)]
    r = subprocess.run(args, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        what = "расшифровать (неверный ключ DATA_KEY?)" if decrypt else "зашифровать"
        raise StoreError(f"openssl не смог {what}: {r.stderr.strip()[:300]}")


# ---------- упаковка ----------

def pack(dst: Path, key: str, root: Path = ROOT) -> dict:
    files = bundle_files(root)
    if not any(f.name == "state.json" for f in files):
        raise StoreError(f"В {root / 'data'} нет state.json — упаковывать нечего (сначала python run.py).")
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "bundle.tar.gz"
        with tarfile.open(plain, "w:gz", compresslevel=6) as tar:
            for f in files:
                tar.add(f, arcname=f.relative_to(root).as_posix(), recursive=False)
        _openssl(plain, dst, key, decrypt=False)
        plain_size = plain.stat().st_size
    return {"files": {f.relative_to(root).as_posix(): f.stat().st_size for f in files},
            "content_sha256": content_hash(files, root), "plain_size": plain_size,
            "bundle_size": dst.stat().st_size, "bundle_sha256": hashlib.sha256(dst.read_bytes()).hexdigest()}


def unpack(src: Path, key: str, root: Path = ROOT, backup: bool = True) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "bundle.tar.gz"
        _openssl(src, plain, key, decrypt=True)
        try:
            tar = tarfile.open(plain, "r:gz")
        except tarfile.TarError as e:
            raise StoreError(f"архив повреждён или ключ не тот: {e}") from e
        with tar:
            members = tar.getmembers()
            bad = [m.name for m in members if not (m.isfile() and allowed_member(m.name))]
            if bad:
                raise StoreError(f"в архиве посторонние файлы: {bad[:5]}")
            names = [m.name for m in members]
            if backup:
                _backup([root / n for n in names if (root / n).is_file()], root)
            tar.extractall(root, members=members, filter="data")
    return names


def _backup(files: list[Path], root: Path) -> None:
    """Перед тем как затереть свои локальные файлы облачными — копия в data/backup_local/<время>/."""
    if not files:
        return
    base = root / "data" / "backup_local"
    dst = base / datetime.now().strftime("%Y%m%d_%H%M%S")
    for f in files:
        t = dst / f.relative_to(root)
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
    olds = sorted(p for p in base.iterdir() if p.is_dir())
    for p in olds[:-BACKUPS_KEEP]:
        shutil.rmtree(p, ignore_errors=True)
    print(f"Локальные файлы сохранены в {dst.relative_to(root)} (на всякий случай)")


# ---------- git ----------

def git(*args: str, cwd: Path = ROOT, input_bytes: bytes | None = None, check: bool = True,
        env: dict | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, input=input_bytes, capture_output=True,
                       env=dict(os.environ, **(env or {})))
    if check and r.returncode != 0:
        raise StoreError(f"git {' '.join(args[:3])}…: {r.stderr.decode('utf-8', 'replace').strip()[:400]}")
    return r


def git_out(*args: str, **kw) -> str:
    return git(*args, **kw).stdout.decode("utf-8", "replace").strip()


def fetch_branch(remote: str, branch: str) -> str | None:
    """Скачивает ветку (один коммит) и возвращает его sha; None — ветки нет."""
    if not git_out("ls-remote", "--heads", remote, branch):
        return None
    git("fetch", "-q", "--no-tags", "--depth=1", remote, f"refs/heads/{branch}")
    return git_out("rev-parse", "FETCH_HEAD")


def tree_entries(commit: str) -> dict[str, str]:
    out = {}
    for line in git_out("ls-tree", commit).splitlines():
        meta, name = line.split("\t", 1)
        out[name] = meta.split()[2]
    return out


def read_blob(sha: str) -> bytes:
    return git("cat-file", "blob", sha).stdout


def committer_env() -> dict:
    env = {}
    if not git_out("config", "user.name", check=False):
        env.update(GIT_AUTHOR_NAME="yurt-bot", GIT_COMMITTER_NAME="yurt-bot")
    if not git_out("config", "user.email", check=False):
        env.update(GIT_AUTHOR_EMAIL="yurt-bot@users.noreply.github.com",
                   GIT_COMMITTER_EMAIL="yurt-bot@users.noreply.github.com")
    return env


# ---------- команды ----------

def cmd_init_key(args) -> int:
    path = Path(os.environ.get("DATA_KEY_FILE") or KEY_FILE).expanduser()
    if path.exists() and not args.force:
        print(f"Ключ уже есть: {path}. Новый — только с --force (старые архивы новым ключом не открыть).")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    print(f"Ключ записан в {path}")
    print("Дальше: GitHub → репозиторий → Settings → Secrets and variables → Actions → New repository secret,")
    print("  Name: DATA_KEY, Secret: содержимое этого файла (одна строка). Никому не показывайте и не коммитьте.")
    return 0


def cmd_pull(args) -> int:
    key = load_key()
    sha = fetch_branch(args.remote, BRANCH)
    if not sha:
        raise StoreError(f"В {args.remote} нет ветки «{BRANCH}». Первый раз её создаёт: python push_yoox.py --seed")
    entries = tree_entries(sha)
    if BUNDLE not in entries:
        raise StoreError(f"В ветке {BRANCH} нет {BUNDLE}")
    manifest = json.loads(read_blob(entries[MANIFEST])) if MANIFEST in entries else {}
    DATA.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        enc = Path(tmp) / BUNDLE
        enc.write_bytes(read_blob(entries[BUNDLE]))
        names = unpack(enc, key, ROOT, backup=not args.no_backup)
    PULL_INFO.write_text(json.dumps({"commit": sha, "pulled_at": _now(),
                                     "content_sha256": manifest.get("content_sha256")}, indent=1), encoding="utf-8")
    print(f"Данные из ветки {BRANCH} ({sha[:8]}, записаны {manifest.get('written_at', '?')} "
          f"[{manifest.get('by', '?')}]): {len(names)} файлов")
    if args.site_from:
        _site_from_dir(Path(args.site_from))
    elif args.site_from_branch:
        _site_from_branch(args.remote)
    return 0


def _site_from_dir(pages: Path) -> None:
    """Опубликованный сайт (рабочая копия gh-pages) -> site/: прошлые карточки и уже выложенные фото.
    Каталог частями — data/ (манифест + части), старый формат — products.json."""
    src = pages / "products.json"
    if src.is_file():
        shutil.copy2(src, SITE / "products.json")
    if (pages / "data" / "manifest.json").is_file():
        if (SITE / "data").exists():
            shutil.rmtree(SITE / "data")
        shutil.copytree(pages / "data", SITE / "data")
    pdir = pages / "img" / "p"
    if pdir.is_dir():
        dst = SITE / "img" / "p"
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in pdir.iterdir():
            if f.is_file() and not (dst / f.name).exists():
                shutil.copy2(f, dst / f.name)
                n += 1
        print(f"Из опубликованного сайта: каталог (data/ или products.json), фото img/p (+{n})")


def _site_from_branch(remote: str) -> None:
    """Прошлые карточки из gh-pages (data/ частями или products.json): run.py берёт из них «Нет в наличии»."""
    sha = fetch_branch(remote, "gh-pages")
    if not sha:
        return
    entries = tree_entries(sha)
    if "data" in entries:
        files = {}
        for line in git_out("ls-tree", "-r", sha, "--", "data").splitlines():
            meta, name = line.split("\t", 1)
            files[name] = meta.split()[2]
        if "data/manifest.json" in files:
            if (SITE / "data").exists():
                shutil.rmtree(SITE / "data")
            for name, blob in files.items():
                (SITE / name).parent.mkdir(parents=True, exist_ok=True)
                (SITE / name).write_bytes(read_blob(blob))
            print(f"site/data/ взят из опубликованного сайта (gh-pages): {len(files)} файлов")
    if "products.json" in entries:
        SITE.mkdir(exist_ok=True)
        (SITE / "products.json").write_bytes(read_blob(entries["products.json"]))
        print("site/products.json взят из опубликованного сайта (gh-pages)")


def cmd_push(args) -> int:
    key = load_key()
    pulled = json.loads(PULL_INFO.read_text(encoding="utf-8")) if PULL_INFO.is_file() else {}
    remote_sha = fetch_branch(args.remote, BRANCH) if not args.offline_remote else None
    expected = pulled.get("commit")
    if remote_sha and not expected and not args.seed:
        raise StoreError(f"Ветка {BRANCH} уже есть, а данные не брались из неё (python datastore.py pull). "
                         "Чтобы не затереть облачную память, отправка отменена.")
    if remote_sha and expected and remote_sha != expected and not args.force:
        raise StoreError(f"Ветку {BRANCH} уже обновили ({remote_sha[:8]}, а взяли {expected[:8]}). "
                         "Сначала python datastore.py pull (или push_yoox.py ещё раз).")
    prev_entries = tree_entries(remote_sha) if remote_sha else {}
    prev_manifest = json.loads(read_blob(prev_entries[MANIFEST])) if MANIFEST in prev_entries else {}

    with tempfile.TemporaryDirectory() as tmp:
        enc = Path(tmp) / BUNDLE
        info = pack(enc, key)
        if prev_manifest.get("content_sha256") == info["content_sha256"] and not args.force:
            print(f"Данные не изменились с прошлой записи ({prev_manifest.get('written_at')}) — ветку {BRANCH} не трогаю.")
            return 0
        manifest = {"written_at": _now(), "by": args.by, "content_sha256": info["content_sha256"],
                    "bundle_sha256": info["bundle_sha256"], "bundle_size": info["bundle_size"],
                    "plain_size": info["plain_size"], "files": info["files"],
                    "cipher": "openssl " + " ".join(OPENSSL_ARGS), "previous_written_at": prev_manifest.get("written_at")}
        blob = git_out("hash-object", "-w", str(enc))
        mblob = git_out("hash-object", "-w", "--stdin",
                        input_bytes=json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))
        rblob = git_out("hash-object", "-w", "--stdin", input_bytes=BRANCH_README.encode("utf-8"))
    lines = [f"100644 blob {blob}\t{BUNDLE}", f"100644 blob {mblob}\t{MANIFEST}", f"100644 blob {rblob}\tREADME.md"]
    if BUNDLE in prev_entries:
        lines.append(f"100644 blob {prev_entries[BUNDLE]}\t{PREV_BUNDLE}")
    tree = git_out("mktree", input_bytes=("\n".join(sorted(lines, key=lambda l: l.split("\t")[1])) + "\n").encode())
    msg = f"data: {manifest['written_at']} [{args.by}]"
    commit = git_out("commit-tree", tree, "-m", msg, env=committer_env())
    mb = info["bundle_size"] / 1e6
    print(f"Архив: {len(info['files'])} файлов, {info['plain_size'] / 1e6:.1f} МБ сжато, {mb:.1f} МБ зашифровано; коммит {commit[:8]}")
    if mb > 90:
        raise StoreError(f"Архив {mb:.0f} МБ — больше предела GitHub (100 МБ на файл).")
    if args.dry_run:
        print(f"--dry-run: ветку {BRANCH} не отправляю. Было бы: git push {args.remote} {commit[:8]}:refs/heads/{BRANCH}")
        return 0
    lease = f"--force-with-lease=refs/heads/{BRANCH}:{remote_sha or ''}"
    r = git("push", "-q", lease, args.remote, f"{commit}:refs/heads/{BRANCH}", check=False)
    if r.returncode != 0:
        raise StoreError("ветку data не удалось обновить (её только что обновил кто-то ещё?): "
                         + r.stderr.decode("utf-8", "replace").strip()[:400])
    PULL_INFO.write_text(json.dumps({"commit": commit, "pulled_at": _now(),
                                     "content_sha256": info["content_sha256"]}, indent=1), encoding="utf-8")
    print(f"Ветка {BRANCH} обновлена: {commit[:8]}")
    return 0


def cmd_status(args) -> int:
    sha = fetch_branch(args.remote, BRANCH)
    if not sha:
        print(f"Ветки {BRANCH} в {args.remote} нет.")
        return 1
    entries = tree_entries(sha)
    m = json.loads(read_blob(entries[MANIFEST])) if MANIFEST in entries else {}
    print(f"Ветка {BRANCH}: {sha[:8]}, записана {m.get('written_at')} [{m.get('by')}], "
          f"архив {m.get('bundle_size', 0) / 1e6:.1f} МБ, файлов {len(m.get('files') or {})}")
    for name, size in sorted((m.get("files") or {}).items()):
        if not name.startswith("data/changes/"):
            print(f"  {name:<40} {size / 1e6:8.2f} МБ")
    pulled = json.loads(PULL_INFO.read_text(encoding="utf-8")) if PULL_INFO.is_file() else {}
    if pulled:
        same = "совпадает" if pulled.get("commit") == sha else "УСТАРЕЛ — сделайте pull"
        print(f"Локально взят коммит {str(pulled.get('commit'))[:8]} ({pulled.get('pulled_at')}): {same}")
    return 0


def cmd_summary(args) -> int:
    """Статусы источников по data/raw_*.meta.json. В GitHub Actions — таблица в сводке запуска и предупреждения."""
    rows, warn = [], []
    now = time.time()
    for meta in sorted(DATA.glob("raw_*.meta.json")):
        m = json.loads(meta.read_text(encoding="utf-8"))
        src = m.get("source") or meta.name[4:-10]
        ts = m.get("collected_at") or ""
        try:
            age_h = (now - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()) / 3600
        except ValueError:
            age_h = float("nan")
        rows.append(f"| {src} | {m.get('status')} | {ts} | {age_h:.0f} ч | {m.get('fetched')} | {m.get('rows')} | "
                    f"{len(m.get('gone_ids') or [])} | {str(m.get('note') or '')[:80]} |")
        auto = src in ("pcardin_tr", "cacharel_tr", "trendyol")
        if m.get("status") in ("stale", "failed", "partial"):
            warn.append(f"{src}: {m.get('status')} — {m.get('note') or ''}")
        elif auto and age_h == age_h and age_h > args.stale_hours:
            warn.append(f"{src}: данные не обновлялись {age_h:.0f} ч")
    table = ["| источник | статус | собран (UTC) | возраст | собрано | в данных | распродано | заметка |",
             "|---|---|---|---|---|---|---|---|", *rows]
    print("\n".join(table))
    gh = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write("### Источники\n\n" + "\n".join(table) + "\n\n")
            if warn:
                f.write("**Внимание:**\n\n" + "\n".join(f"- {w}" for w in warn) + "\n")
    for w in warn:
        print(f"::warning title=Источник не обновился::{w}" if gh else f"ВНИМАНИЕ: {w}")
    return 0


def cmd_selftest(args) -> int:
    """Упаковать текущие data/, зашифровать временным ключом, расшифровать в пустую папку и сравнить."""
    key = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory() as tmp:
        enc = Path(tmp) / BUNDLE
        t0 = time.time()
        info = pack(enc, key)
        t1 = time.time()
        dest = Path(tmp) / "restore"
        dest.mkdir()
        names = unpack(enc, key, dest, backup=False)
        t2 = time.time()
        restored = [dest / n for n in names]
        ok = content_hash(restored, dest) == info["content_sha256"]
        try:
            unpack(enc, key + "x", Path(tmp) / "wrong", backup=False)
            wrong_ok = False
        except StoreError:
            wrong_ok = True
    print(f"Файлов {len(info['files'])}, исходно {sum(info['files'].values()) / 1e6:.1f} МБ → "
          f"архив {info['plain_size'] / 1e6:.1f} МБ → зашифровано {info['bundle_size'] / 1e6:.1f} МБ; "
          f"упаковка {t1 - t0:.1f} с, распаковка {t2 - t1:.1f} с")
    print(f"Содержимое после распаковки совпадает: {'да' if ok else 'НЕТ'}; чужой ключ отвергнут: {'да' if wrong_ok else 'НЕТ'}")
    return 0 if ok and wrong_ok else 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Память каталога в ветке data (зашифровано)")
    ap.add_argument("--remote", default="origin", help="git remote (по умолчанию origin)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init-key", help="создать ключ шифрования")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("pull", help="взять данные из ветки data")
    p.add_argument("--site-from", help="папка с рабочей копией gh-pages: взять каталог (data/ или products.json) и img/p")
    p.add_argument("--site-from-branch", action="store_true", help="взять каталог (data/ или products.json) из ветки gh-pages")
    p.add_argument("--no-backup", action="store_true")
    p = sub.add_parser("push", help="записать данные в ветку data")
    p.add_argument("--by", default="local", help="кто пишет: ci / local (для manifest.json)")
    p.add_argument("--dry-run", action="store_true", help="собрать коммит, но не отправлять")
    p.add_argument("--seed", action="store_true", help="первая запись (или поверх ветки, не делая pull)")
    p.add_argument("--force", action="store_true", help="записать, даже если ветку обновили / данные те же")
    p.add_argument("--offline-remote", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("status", help="что лежит в ветке data")
    p = sub.add_parser("summary", help="статусы источников")
    p.add_argument("--stale-hours", type=float, default=30)
    sub.add_parser("selftest", help="проверить упаковку и шифрование")
    args = ap.parse_args(argv)
    handlers = {"init-key": cmd_init_key, "pull": cmd_pull, "push": cmd_push, "status": cmd_status,
                "summary": cmd_summary, "selftest": cmd_selftest}
    try:
        return handlers[args.cmd](args)
    except StoreError as e:
        print(f"ОШИБКА: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
