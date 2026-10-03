"""Проверка .github/workflows/update.yml без PyYAML и без GitHub (python tests/check_workflow.py).

Разбирает YAML небольшим парсером (блочные словари/списки, `|`-текст, [списки], кавычки, комментарии —
ровно то, что есть в нашем файле) и проверяет: структуру workflow, cron, шаги (у каждого run или uses,
версии actions), что упомянутые скрипты есть в репозитории, порядок шагов (память → сбор → сохранение →
публикация), что закрытые файлы не публикуются. Код выхода 0 — всё в порядке.
"""
from __future__ import annotations

import re
import sys
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
    if "products-admin.js" not in re.search(r"FORBIDDEN\s*=\s*\[(.*?)\]", deploy, re.S).group(1):
        errs.append("deploy.py: products-admin.js не в FORBIDDEN")
    if re.search(r"PUBLIC_FILES\s*=\s*\[[^\]]*products-admin", deploy):
        errs.append("deploy.py: products-admin.js в публикуемых файлах!")
    if "noindex" not in deploy:
        errs.append("deploy.py: нет noindex")
    return errs


def main() -> int:
    errs = check()
    wf = parse_yaml(WF.read_text(encoding="utf-8"))
    job = next(iter(wf["jobs"].values()))
    print(f"{WF.relative_to(ROOT)}: триггеры {list(wf['on'])}, шагов {len(job['steps'])}, "
          f"секреты {sorted(set(re.findall(r'secrets\.(\w+)', WF.read_text(encoding='utf-8'))))}")
    for e in errs:
        print("ОШИБКА:", e)
    print("Всё в порядке." if not errs else f"Ошибок: {len(errs)}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
