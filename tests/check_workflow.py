"""Проверка .github/workflows/update.yml и публикации без PyYAML и без GitHub (python tests/check_workflow.py).

Разбирает YAML небольшим парсером (блочные словари/списки, `|`-текст, [списки], кавычки, комментарии —
ровно то, что есть в нашем файле) и проверяет: структуру workflow, cron, шаги (у каждого run или uses,
версии actions), что упомянутые скрипты есть в репозитории, порядок шагов (память → сбор → сохранение →
публикация), что закрытые файлы не публикуются, что заказы не попадают ни в git (.gitignore), ни в архив
ветки data (datastore.py). Затем пробная публикация: deploy.py собирает маленький выдуманный сайт во временную
папку, git fetch/push подменены (в сеть — ни шагу) — проверяются теги превью, og.png, отсутствие закрытых
файлов, предупреждение о контакте и что gh-pages — всегда один коммит без родителей. Код выхода 0 — всё в порядке.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "update.yml"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ---------- мини-парсер YAML ----------

def _strip_comment(s: str) -> str:
    out, q = [], None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in _split_flow(v[1:-1])] if v[1:-1].strip() else []
    if v.startswith("{") and v.endswith("}"):
        return {k.strip(): _scalar(x) for k, x in (p.split(":", 1) for p in _split_flow(v[1:-1]))}
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return {"true": True, "false": False, "null": None, "~": None}.get(v, v)


def _split_flow(s: str) -> list[str]:
    parts, depth, cur, q = [], 0, "", None
    for ch in s:
        if q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


class YamlError(ValueError):
    pass


def parse_yaml(text: str):
    if "\t" in text:
        n = text[:text.index("\t")].count("\n") + 1
        raise YamlError(f"строка {n}: табуляция (в YAML только пробелы)")
    raw = text.splitlines()
    lines = []          # (номер, отступ, текст)
    i = 0
    while i < len(raw):
        s = _strip_comment(raw[i])
        if s.strip():
            lines.append((i + 1, len(s) - len(s.lstrip(" ")), s.strip(), raw[i]))
        i += 1
    pos = 0

    def block_scalar(ind_parent: int, start_line: int) -> str:
        nonlocal pos
        out = []
        j = start_line          # номер строки (1-based) с ключом
        k = j
        while k < len(raw):
            line = raw[k]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= ind_parent:
                break
            out.append(line)
            k += 1
        while pos < len(lines) and lines[pos][0] <= k:
            pos += 1
        return "\n".join(x.strip() for x in out).strip() + "\n"

    def parse_block(ind: int):
        nonlocal pos
        if pos >= len(lines):
            return None
        if lines[pos][2].startswith("- ") or lines[pos][2] == "-":
            return parse_seq(lines[pos][1])
        return parse_map(lines[pos][1])

    def parse_map(ind: int, first: str | None = None, first_no: int | None = None) -> dict:
        nonlocal pos
        d = {}
        pending = [(first_no, first)] if first is not None else []
        while pending or pos < len(lines):
            if pending:
                no, s = pending.pop()
            else:
                no, li, s, _ = lines[pos]
                if li < ind:
                    break
                if li > ind:
                    raise YamlError(f"строка {no}: лишний отступ")
                if s.startswith("- "):
                    break
                pos += 1
            m = re.match(r"^([^:]+?|\"[^\"]+\"|'[^']+'):(?:\s+(.*))?$", s)
            if not m:
                raise YamlError(f"строка {no}: ожидалось «ключ: значение», а здесь {s!r}")
            key, val = _scalar(m.group(1)), (m.group(2) or "").strip()
            if key in d:
                raise YamlError(f"строка {no}: ключ {key!r} повторяется")
            if val in ("|", "|-", ">", ">-"):
                d[key] = block_scalar(ind, no)
            elif val:
                d[key] = _scalar(val)
            elif pos < len(lines) and (lines[pos][1] > ind or (lines[pos][1] == ind and lines[pos][2].startswith("- "))):
                d[key] = parse_block(lines[pos][1])
            else:
                d[key] = None
        return d

    def parse_seq(ind: int) -> list:
        nonlocal pos
        out = []
        while pos < len(lines):
            no, li, s, _ = lines[pos]
            if li < ind or not (s.startswith("- ") or s == "-"):
                if li > ind:
                    raise YamlError(f"строка {no}: лишний отступ в списке")
                break
            pos += 1
            item = s[2:].strip() if s != "-" else ""
            if not item:
                out.append(parse_block(lines[pos][1]) if pos < len(lines) and lines[pos][1] > ind else None)
            elif re.match(r"^[^\[{\"'][^:]*:(\s|$)", item):
                out.append(parse_map(ind + 2, item, no))
            else:
                out.append(_scalar(item))
        return out

    result = parse_block(0)
    if pos != len(lines):
        raise YamlError(f"строка {lines[pos][0]}: не разобрано")
    return result


# ---------- проверки ----------

def check(path: Path = WF) -> list[str]:
    errs: list[str] = []
    text = path.read_text(encoding="utf-8")
    if "\r\n" in text:
        errs.append("файл с CRLF — лучше LF (.gitattributes это обеспечит при коммите)")
    try:
        wf = parse_yaml(text)
    except YamlError as e:
        return [f"YAML: {e}"]
    if text.count("${{") != text.count("}}"):
        errs.append("несбалансированные ${{ … }}")
    for k in ("name", "on", "permissions", "jobs"):
        if k not in wf:
            errs.append(f"нет ключа верхнего уровня {k!r}")
    on = wf.get("on") or {}
    for c in (on.get("schedule") or []):
        cron = str((c or {}).get("cron", ""))
        if len(cron.split()) != 5:
            errs.append(f"cron {cron!r}: нужно 5 полей")
    disp = (on.get("workflow_dispatch") or {}).get("inputs") or {}
    for name, inp in disp.items():
        if inp.get("type") == "choice" and inp.get("default") not in (inp.get("options") or []):
            errs.append(f"input {name}: default не из options")
    perms = wf.get("permissions") or {}
    if perms.get("contents") != "write":
        errs.append("permissions.contents должно быть write (ветки data и gh-pages)")
    jobs = wf.get("jobs") or {}
    if not jobs:
        errs.append("нет jobs")
    for jname, job in jobs.items():
        steps = job.get("steps") or []
        if not job.get("runs-on"):
            errs.append(f"{jname}: нет runs-on")
        names = [s.get("name") for s in steps if isinstance(s, dict) and s.get("name")]
        if len(names) != len(set(names)):
            errs.append(f"{jname}: повторяются имена шагов")
        order = []
        for n, st in enumerate(steps, 1):
            if not isinstance(st, dict):
                errs.append(f"{jname} шаг {n}: не словарь")
                continue
            if bool(st.get("run")) == bool(st.get("uses")):
                errs.append(f"{jname} шаг {n} ({st.get('name')}): нужен ровно один из run / uses")
            uses = str(st.get("uses") or "")
            if uses and not re.search(r"@v\d+|@[0-9a-f]{40}$", uses):
                errs.append(f"{jname} шаг {n}: {uses} без версии")
            run = str(st.get("run") or "")
            for script in re.findall(r"python\s+([\w/.-]+\.py)", run):
                if not (ROOT / script).is_file():
                    errs.append(f"{jname} шаг {n}: нет файла {script}")
            for key, pat in (("pull", r"datastore\.py pull"), ("collect", r"update\.py|run\.py"),
                             ("save", r"datastore\.py push"), ("deploy", r"deploy\.py")):
                if re.search(pat, run):
                    order.append(key)
        want = ["pull", "collect", "save", "deploy"]
        seq = [k for k in order if k in want]
        firsts = [seq.index(k) for k in want if k in seq]
        if firsts != sorted(firsts) or len(firsts) != 4:
            errs.append(f"{jname}: порядок шагов должен быть {want}, а он {seq}")
    deploy = (ROOT / "deploy.py").read_text(encoding="utf-8")
    forbidden = re.search(r"FORBIDDEN\s*=\s*\[(.*?)\]", deploy, re.S).group(1)
    if "products-admin.js" not in forbidden:
        errs.append("deploy.py: products-admin.js не в FORBIDDEN")
    if '"admin"' not in forbidden:
        errs.append("deploy.py: папка site/admin/ (закрытые части) не в FORBIDDEN")
    if re.search(r"PUBLIC_DIRS\s*=\s*\[[^\]]*admin", deploy):
        errs.append("deploy.py: site/admin/ в публикуемых папках!")
    if re.search(r"PUBLIC_FILES\s*=\s*\[[^\]]*products-admin", deploy):
        errs.append("deploy.py: products-admin.js в публикуемых файлах!")
    if "noindex" not in deploy:
        errs.append("deploy.py: нет noindex")
    if "commit-tree" not in deploy or '"--force"' not in deploy:
        errs.append("deploy.py: gh-pages должна публиковаться одним коммитом без родителей (commit-tree + push --force)")
    for word in ("orders", "customers", "channel_state"):
        if not re.search(r"FORBIDDEN_IN_PATH\s*=\s*\[[^\]]*\"" + word + '"', deploy):
            errs.append(f"deploy.py: путь с {word!r} не в FORBIDDEN_IN_PATH")
    suffixes = re.search(r"FORBIDDEN_SUFFIXES\s*=\s*\((.*?)\)", deploy, re.S)
    for suf in (".sqlite", ".sqlite-wal", ".sqlite-shm", ".jsonl"):
        if not suffixes or f'"{suf}"' not in suffixes.group(1):
            errs.append(f"deploy.py: *{suf} не в FORBIDDEN_SUFFIXES")
    errs += check_private_storage()
    return errs


def check_private_storage() -> list[str]:
    """Заказы и покупатели: явные строки в .gitignore, datastore их не архивирует и не распаковывает."""
    errs = []
    ignore = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for line in ("data/orders/", "*.sqlite", "*.sqlite-wal", "*.sqlite-shm", "data/channel_state.json",
                 "data/analysis/"):
        if line not in ignore:
            errs.append(f".gitignore: нет строки {line}")
    sys.path.insert(0, str(ROOT))
    import datastore
    for name in ("data/orders/orders.jsonl", "data/orders/orders.sqlite", "data/orders/export/x.csv",
                 "data/orders.sqlite", "data/x.sqlite-wal", "data/x.sqlite-shm", "data/customers.jsonl"):
        if datastore.allowed_member(name):
            errs.append(f"datastore.allowed_member пропускает {name}")
    for name in ("data/state.json", "data/raw_trendyol.json", "site/admin/a1.js", "site/products-admin.js"):
        if not datastore.allowed_member(name):
            errs.append(f"datastore.allowed_member не пропускает нужный {name}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel in ("data/state.json", "data/raw_x.json", "data/orders/orders.jsonl", "data/orders/orders.sqlite",
                    "data/orders/orders.sqlite-wal", "data/changes/1.json", "site/admin/a1.js"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("{}", encoding="utf-8")
        got = {p.relative_to(root).as_posix() for p in datastore.bundle_files(root)}
        if any(datastore.is_private(n) or "orders" in n for n in got):
            errs.append(f"datastore.bundle_files берёт заказы: {sorted(got)}")
        if not {"data/state.json", "data/raw_x.json", "site/admin/a1.js"} <= got:
            errs.append(f"datastore.bundle_files потерял нужные файлы: {sorted(got)}")
    return errs


# ---------- пробная публикация deploy.py (без сети) ----------

def _rmtree(path: Path) -> None:
    def retry(func, p, _exc):        # объекты .git только для чтения — на Windows иначе не удалить
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)


def _fake_site(root: Path, telegram: str = "", contact_url: str = "https://t.me/your_username") -> Path:
    site = root / "site"
    files = {
        "index.html": "<!doctype html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n"
                      "<title>Брендовая одежда</title>\n<meta property=\"og:title\" content=\"старый\">\n"
                      "</head>\n<body>каталог</body>\n</html>\n",
        "products.js": "window.DEALS = [];\n",
        "content.json": json.dumps({"name": "IPAK", "tagline": "Брендовая одежда из Италии и Турции",
                                    "hero_text": "Boss, Emporio Armani и ещё десятки брендов со скидками до 80%.",
                                    "contacts": {"telegram": telegram}, "orders_telegram_username": ""},
                                   ensure_ascii=False),
        "data/manifest.json": json.dumps({"layout": 1, "site": {"name": "IPAK", "contact_url": contact_url},
                                          "shards": [{"file": "i/0.aaaa.js"}], "detail": {"files": ["d/00.bbbb.js"]}}),
        "data/i/0.aaaa.js": '__DS("i/0",\n{"v":1,"id":["1"]}\n);\n',
        "data/d/00.bbbb.js": '__DS("d/00",\n{"v":1,"items":{}}\n);\n',
        "admin/00.js": '{"cost_uzs": 1, "price_now": 2}',             # закрытое — не публикуется
        "products-admin.js": 'window.ADMIN = {"cost_uzs": 1};',
        "img/p/1-1.jpg": "jpg",
        "img/brands/boss.svg": "<svg/>",
    }
    for rel, text in files.items():
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        (site / rel).write_text(text, encoding="utf-8")
    shutil.copytree(ROOT / "site" / "brand", site / "brand")             # настоящие логотипы и og.png
    (root / "config.json").write_text(json.dumps({"site": {"contact_url": contact_url}}), encoding="utf-8")
    return site


def check_deploy_run() -> list[str]:
    errs: list[str] = []
    sys.path.insert(0, str(ROOT))
    import brands
    import content
    import deploy
    saved = {k: getattr(deploy, k) for k in ("ROOT", "SITE", "OUT", "git")}
    saved_builds = (content.build, brands.build)
    real_git = deploy.git
    state = {"pushed": None, "calls": [], "out": None}

    def fake_git(*args, cwd=None, env=None):
        state["calls"].append(args)
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/example/shop.git"
        if args[0] == "fetch":            # «ветка на GitHub» — то, что подменённый push отправил последним
            if not state["pushed"]:
                raise SystemExit("git fetch: ветки ещё нет (подмена)")
            (state["out"] / ".git" / "FETCH_HEAD").write_text(state["pushed"] + "\n", encoding="utf-8")
            return ""
        if args[0] in ("push", "pull", "clone", "ls-remote"):
            if args[0] == "push":
                state["pushed"] = args[-1].split(":")[0]
            return ""
        return real_git(*args, cwd=cwd, env=env)

    def run(argv: list[str]) -> tuple[str, str | None]:
        buf = io.StringIO()
        fail = None
        with contextlib.redirect_stdout(buf):
            try:
                deploy.main(argv)
            except SystemExit as e:
                fail = str(e)
        return buf.getvalue(), fail

    def published_commits(out: Path) -> tuple[int, str]:
        g = lambda *a: subprocess.run(["git", *a], cwd=out, capture_output=True, text=True).stdout.strip()
        return int(g("rev-list", "--count", "refs/heads/gh-pages") or 0), g("cat-file", "-p", "refs/heads/gh-pages")

    tmp = Path(tempfile.mkdtemp(prefix="deploy_check_"))
    try:
        deploy.git = fake_git
        content.build = brands.build = lambda *a, **k: None   # не трогать настоящие site/content.js, brands.js
        root = tmp / "root"
        deploy.ROOT, deploy.SITE = root, _fake_site(root)
        out = state["out"] = tmp / "out"

        # 1. первая публикация
        log, fail = run(["--out", str(out)])
        if fail:
            return [f"deploy.py упал на чистом сайте: {fail}\n{log}"]
        page = (out / "index.html").read_text(encoding="utf-8")
        want = {"og:title": "IPAK — брендовая одежда из Италии и Турции", "og:type": "website",
                "twitter:card": "summary_large_image"}
        for key, val in want.items():
            if f'="{key}" content="{val}"' not in page:
                errs.append(f"index.html: нет {key} = {val!r}")
        img = re.search(r'property="og:image" content="([^"]+)"', page)
        if not img or not img.group(1).startswith(deploy.PUBLIC_URL + "brand/og.png"):
            errs.append(f"index.html: og:image не абсолютный адрес brand/og.png: {img and img.group(1)}")
        if not img or not img.group(1).startswith("https://"):
            errs.append("index.html: og:image не https")
        desc = re.search(r'property="og:description" content="([^"]+)"', page)
        if not desc or deploy.UNSAFE_TEXT.search(desc.group(1)):
            errs.append(f"index.html: og:description пустой или с названием источника: {desc and desc.group(1)}")
        if page.count('property="og:title"') != 1 or "старый" in page:
            errs.append("index.html: старые og-теги страницы не заменены")
        if 'name="robots" content="noindex' not in page:
            errs.append("index.html: нет noindex")
        if deploy.png_size(out / "brand" / "og.png") != (1200, 630):
            errs.append("og.png не опубликован или не 1200×630")
        leaked = [p.relative_to(out).as_posix() for p in out.rglob("*") if ".git" not in p.relative_to(out).parts
                  and re.search(r"admin|orders|customers|channel_state|\.sqlite|\.jsonl", p.relative_to(out).as_posix())]
        if leaked:
            errs.append(f"в публикации закрытые файлы: {leaked}")
        if "ВНИМАНИЕ: контакт для заказов не настроен" not in log or "your_username" not in log:
            errs.append("нет громкого предупреждения о заглушке контакта")
        pushes = [c for c in state["calls"] if c[0] == "push"]
        if len(pushes) != 1 or "--force" not in pushes[0] or not pushes[0][-1].endswith(":refs/heads/gh-pages"):
            errs.append(f"push в gh-pages не --force: {pushes}")
        n, head = published_commits(out)
        if n != 1 or "\nparent " in head:
            errs.append(f"после первой публикации в gh-pages {n} коммитов (нужен 1 без родителей)")

        # 2. ничего не изменилось — ничего не отправляется
        state["calls"].clear()
        log, fail = run(["--out", str(out)])
        if fail or "Изменений нет" not in log or any(c[0] == "push" for c in state["calls"]):
            errs.append(f"повторная публикация без изменений что-то отправила: {fail or log[-300:]}")

        # 2б. в gh-pages старая история (коммит с родителем), сайт тот же — всё равно заменяется одним коммитом
        g = lambda *a: subprocess.run(["git", *a], cwd=out, capture_output=True, text=True).stdout.strip()
        tree = g("rev-parse", state["pushed"] + "^{tree}")
        with_parent = g("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree,
                        "-p", state["pushed"], "-m", "old history")
        state["pushed"] = with_parent
        state["calls"].clear()
        log, fail = run(["--out", str(out)])
        n, head = published_commits(out)
        if fail or state["pushed"] == with_parent or n != 1 or "\nparent " in head:
            errs.append(f"старая история gh-pages не схлопнута: {fail or log[-300:]}")

        # 3. изменение — снова один коммит без родителей (история не копится)
        (deploy.SITE / "img" / "p" / "2-1.jpg").write_text("jpg2", encoding="utf-8")
        state["calls"].clear()
        first = state["pushed"]
        log, fail = run(["--out", str(out)])
        n, head = published_commits(out)
        if fail or state["pushed"] == first or n != 1 or "\nparent " in head:
            errs.append(f"вторая публикация: {fail or ''} коммитов {n}, новый {state['pushed'] != first}")

        # 4. --dry-run: без fetch и push
        state["calls"].clear()
        log, fail = run(["--out", str(tmp / "dry"), "--dry-run"])
        if fail or any(c[0] in ("fetch", "push") for c in state["calls"]) or "--dry-run" not in log:
            errs.append(f"--dry-run ходил в сеть или упал: {fail}")

        # 5. закрытое в публичных папках — публикация останавливается
        for rel, text in (("brand/orders.jsonl", "{}"), ("img/p/customers.json", "{}"),
                          ("brand/channel_state.json", "{}"), ("img/p/db.sqlite", "x"),
                          ("img/p/db.sqlite-wal", "x"), ("img/p/db.sqlite-shm", "x"),
                          ("data/i/0.aaaa.js", '{"cost_uzs": 5}')):
            p = deploy.SITE / rel
            old = p.read_text(encoding="utf-8") if p.exists() else None
            p.write_text(text, encoding="utf-8")
            state["calls"].clear()
            log, fail = run(["--out", str(out)])
            if not fail or "Стоп" not in fail or any(c[0] == "push" for c in state["calls"]):
                errs.append(f"site/{rel} не остановил публикацию")
            p.write_text(old, encoding="utf-8") if old is not None else p.unlink()

        # 6. контакт настроен — предупреждения нет
        _rmtree(root)
        deploy.SITE = _fake_site(root, telegram="ipak_shop", contact_url="https://t.me/ipak_shop")
        log, fail = run(["--out", str(tmp / "dry2"), "--dry-run"])
        if fail or "ВНИМАНИЕ" in log:
            errs.append(f"предупреждение о контакте при настроенном контакте: {fail or log[-300:]}")
    finally:
        for k, v in saved.items():
            setattr(deploy, k, v)
        content.build, brands.build = saved_builds
        _rmtree(tmp)
    return errs


def main() -> int:
    errs = check()
    wf = parse_yaml(WF.read_text(encoding="utf-8"))
    job = next(iter(wf["jobs"].values()))
    print(f"{WF.relative_to(ROOT)}: триггеры {list(wf['on'])}, шагов {len(job['steps'])}, "
          f"секреты {sorted(set(re.findall(r'secrets\.(\w+)', WF.read_text(encoding='utf-8'))))}")
    deploy_errs = check_deploy_run()
    print("Пробная публикация deploy.py (без сети): " + ("в порядке" if not deploy_errs else "ОШИБКИ"))
    errs += deploy_errs
    for e in errs:
        print("ОШИБКА:", e)
    print("Всё в порядке." if not errs else f"Ошибок: {len(errs)}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
