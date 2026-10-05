"""Память между запусками run.py: что было на сайте, что распродано, какие размеры закончились.

data/state.json — по публичному коду товара:
    {first_seen, last_seen, sizes_last, price_uzs_last, price_src_last, currency, in_stock, sold_out_since,
     sizes_seen (все размеры, что когда-либо были), sizes_seen_at {размер: когда видели последний раз},
     on_site, off_reason, source, item_id, brand, title}
  и по источникам: {raw_sha1, status, collected_at, note} — чтобы понять, обновились ли данные источника.
data/sold_out.json — снимки карточек распроданных товаров (как они были на сайте): их ещё
  sync.keep_sold_out_days дней показываем с пометкой «Нет в наличии», потом убираем.
data/changes/ГГГГММДД_ччмм.json — что изменилось за запуск (новые, распроданные, размеры, цены).

Распроданным товар считается, только если его источник в этот раз действительно обновился и источник
уверен, что товара нет (см. run.py: перепроверка по ссылке для Турции, охват поисков для YOOX).
Не уверены — товар не помечается распроданным.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SIZES_OUT_DAYS = 14       # в sizes_out — только размеры, которые видели за последние N дней
SIZES_OUT_MAX = 12        # и не больше стольких
FORGET_DAYS = 45          # записи о товарах, которых давно нет на сайте, забываем
EXAMPLES = 10             # сколько примеров печатать в сводке
LETTER_SIZES = ["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "XXXL", "3XL", "4XL", "5XL"]
NO_SIZE = {"one size", "tek ebat", "std", "standart", "unica", "tu"}


# ---------- мелочи ----------

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def file_sha1(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data, indent: int | None = None, compact: bool = False) -> None:
    """Запись через временный файл: при сбое посередине старый файл остаётся целым.
    compact — без отступов и пробелов после «,» и «:» (большие файлы данных)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact
            else json.dumps(data, ensure_ascii=False, indent=indent))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def size_key(s: str):
    u = re.sub(r"\s+", "", str(s).upper())
    if u in LETTER_SIZES:
        return (0, LETTER_SIZES.index(u), u)
    m = re.match(r"^\d+(?:[.,]\d+)?", u)
    if m:
        return (1, float(m.group(0).replace(",", ".")), u)
    return (2, 0, u)


def informative(sizes) -> bool:
    """Есть ли в списке настоящие размеры (а не пусто / «one size»)."""
    return any(str(s).strip().lower() not in NO_SIZE for s in (sizes or []))


def brief(entry_or_pub: dict, pid: str) -> dict:
    return {"id": pid, "brand": entry_or_pub.get("brand"), "title": entry_or_pub.get("title")}


# ---------- блокировка (два запуска одновременно не пишут одни и те же файлы) ----------

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":            # os.kill(pid, 0) на Windows УБИВАЕТ процесс — только через WinAPI
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k32.GetLastError() == 5             # нет доступа — значит, процесс есть
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


PROC_DIR = Path("/proc")
BOOT_ID_PATH = PROC_DIR / "sys" / "kernel" / "random" / "boot_id"


def boot_id() -> str:
    """Идентификатор текущей загрузки системы (Linux: /proc/sys/kernel/random/boot_id); нет — пусто."""
    try:
        return BOOT_ID_PATH.read_text(encoding="ascii").strip()[:64]
    except (OSError, ValueError):
        return ""


def stat_starttime(raw: str) -> str:
    """Поле 22 (starttime) строки /proc/<pid>/stat. Имя процесса в скобках может содержать пробелы и «)»,
    поэтому считаем поля после ПОСЛЕДНЕЙ «)». Не разобрать — пусто."""
    try:
        rest = raw[raw.rindex(")") + 2:].split()           # после «pid (имя) » идёт поле 3 (state) — индекс 0
        v = rest[22 - 3]
    except (ValueError, IndexError):
        return ""
    return v if v.isdigit() else ""


def process_start(pid: int) -> str:
    """Время старта процесса pid как метка «способ:значение» — чтобы отличить наш процесс от чужого, получившего
    тот же номер после перезагрузки. Linux: поле 22 /proc/<pid>/stat (такты с загрузки), иначе psutil
    (create_time), иначе Windows (GetProcessTimes). Нельзя узнать (нет процесса, нет доступа) — пусто."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    if (PROC_DIR / "self" / "stat").exists():
        try:
            st = stat_starttime((PROC_DIR / str(pid) / "stat").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return ""
        return ("proc:" + st) if st else ""
    try:
        import psutil                                      # необязательная зависимость
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return f"psutil:{psutil.Process(pid).create_time():.3f}"
        except Exception:                                  # NoSuchProcess, AccessDenied, ZombieProcess
            return ""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ""
            try:
                ft = [wintypes.FILETIME() for _ in range(4)]
                if not k32.GetProcessTimes(h, *(ctypes.byref(x) for x in ft)):
                    return ""
                return f"win:{(ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime}"
            finally:
                k32.CloseHandle(h)
        except (OSError, AttributeError, ValueError):
            return ""
    return ""


class LockStuck(SystemExit):
    """Замок держит живой процесс дольше max_age_s: сами не снимаем (он, может быть, ещё пишет файлы) — выходим
    с понятным сообщением. Наследник SystemExit: без обработки программа завершится с этим текстом."""


class Lock:
    """Файл-замок: {"pid", "started", "what", "boot_id", "pid_start"}. Замок умершего процесса снимается.
    Умершим считается и замок, у которого pid вроде бы жив, но это уже другой процесс: система перезагружалась
    (boot_id другой) или у процесса с этим pid другое время старта (pid_start) — после перезагрузки номер часто
    достаётся другой программе. Замок ЖИВОГО процесса (тот же boot_id и то же время старта, или сравнить нечем —
    старый замок без этих полей) не снимается никогда: пока он моложе max_age_s — ждём / «занято», старше —
    LockStuck (выход с сообщением, что делать). Файл без pid (другой запуск как раз его создаёт) считается
    занятым первые FRESH_S секунд."""

    FRESH_S = 60

    @staticmethod
    def _owner_gone(other: dict, pid: int) -> bool:
        """True — процесса, создавшего замок, точно нет (даже если pid сейчас занят кем-то другим)."""
        then, now = str(other.get("boot_id") or ""), boot_id()
        if then and now and then != now:
            return True                                    # перезагрузка: все процессы того запуска умерли
        if not pid_alive(pid):
            return True
        start_then = str(other.get("pid_start") or "")
        if start_then:
            start_now = process_start(pid)
            if start_now and start_now.split(":", 1)[0] == start_then.split(":", 1)[0] and start_now != start_then:
                return True                                # тот же номер, но другой процесс
        return False

    def __init__(self, path: Path, what: str, max_age_s: float = 6 * 3600):
        self.path, self.what, self.max_age_s, self.held = path, what, max_age_s, False

    def info(self) -> dict:
        data = read_json(self.path, {}) if self.path.exists() else {}
        return data if isinstance(data, dict) else {}

    def _stuck_message(self, other: dict, pid: int, age: float) -> str:
        return (f"Замок {self.path.name} держит работающий процесс pid {pid} ({other.get('what') or '?'}, "
                f"с {other.get('started') or '?'}) уже {age / 3600:.1f} ч — дольше {self.max_age_s / 3600:g} ч. "
                f"Сам не снимаю: процесс жив и, может быть, ещё пишет файлы. Если он завис — завершите его "
                f"(Windows: taskkill /PID {pid} /F; Linux: kill {pid}) и запустите снова: замок умершего процесса "
                f"снимается сам. Если pid {pid} — вовсе не этот запуск (номер достался другой программе), "
                f"удалите файл {self.path}.")

    def try_acquire(self) -> bool:
        """True — замок наш; False — занят живым процессом; LockStuck — живой процесс держит его дольше max_age_s."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                other = self.info()
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue                           # замок только что сняли — пробуем ещё раз
                try:
                    pid = int(other.get("pid") or 0)
                except (TypeError, ValueError):
                    pid = 0
                if pid <= 0 and age < self.FRESH_S:
                    return False                       # файл только создан, pid ещё не записан
                if pid > 0 and not self._owner_gone(other, pid):
                    if age > self.max_age_s:
                        raise LockStuck(self._stuck_message(other, pid, age))
                    return False
                if self.info() != other:
                    continue                           # пока смотрели, замок сменился — посмотреть заново
                try:
                    self.path.unlink()                 # замок от упавшего запуска (процесса уже нет)
                except FileNotFoundError:
                    pass
                except OSError:
                    return False
                continue
            me = os.getpid()
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": me, "started": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                           "what": self.what, "boot_id": boot_id(), "pid_start": process_start(me)},
                          f, ensure_ascii=False)
            self.held = True
            return True
        return False

    def acquire(self, wait_s: float = 0, every_s: float = 15, say=print) -> bool:
        deadline = time.time() + wait_s
        told = False
        while True:
            if self.try_acquire():
                return True
            if time.time() >= deadline:
                return False
            if not told:
                o = self.info()
                say(f"Уже идёт другой запуск ({o.get('what') or '?'}, pid {o.get('pid')}, с {o.get('started')}) — жду…")
                told = True
            time.sleep(every_s)

    def release(self) -> None:
        if self.held:
            try:
                if int(self.info().get("pid") or 0) == os.getpid():
                    self.path.unlink()
            except OSError:
                pass
            self.held = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


# ---------- состояние ----------

def load(data_dir: Path) -> dict:
    st = read_json(data_dir / "state.json", {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("version", 1)
    st.setdefault("items", {})
    st.setdefault("sources", {})
    st["_sold"] = read_json(data_dir / "sold_out.json", {})
    if not isinstance(st["_sold"], dict):
        st["_sold"] = {}
    return st


def save(data_dir: Path, st: dict) -> None:
    sold = st.pop("_sold", {})
    try:
        st["updated_at"] = iso(now_utc())
        write_json(data_dir / "state.json", st)
        write_json(data_dir / "sold_out.json", sold)
    finally:
        st["_sold"] = sold


def watched(st: dict, source: str) -> set[str]:
    """id товаров источника (в магазине), которые сейчас на нашем сайте в наличии."""
    return {str(e.get("item_id")) for e in st["items"].values()
            if e.get("source") == source and e.get("on_site") and e.get("in_stock", True) and e.get("item_id")}


def _sizes_out(e: dict, sizes: list[str], now: datetime) -> list[str]:
    cutoff = now - timedelta(days=SIZES_OUT_DAYS)
    have = set(sizes)
    out = [s for s, t in (e.get("sizes_seen_at") or {}).items()
           if s not in have and str(s).strip().lower() not in NO_SIZE and (parse_ts(t) or cutoff) >= cutoff]
    return sorted(out, key=size_key)[:SIZES_OUT_MAX]


def update(st: dict, *, live: list[tuple[dict, dict]], sources: dict, prev_public: dict, prev_admin: dict,
           keep_days: float, now: datetime | None = None) -> tuple[list[dict], dict, dict]:
    """Сравнивает текущую выдачу с прошлым состоянием и обновляет его.

    live      — [(публичная карточка, строка источника)] товаров, которые сейчас на сайте в наличии;
                в карточку дописывается sizes_out.
    sources   — {код источника в товаре: {"changed", "absence_means_gone", "gone_ids", "present", "status"}}
                present — {id в магазине: строка} всё, что есть в данных источника (до фильтров).
    prev_public / prev_admin — прошлые карточки сайта по коду (снимок для распроданных).
    Возвращает (карточки распроданных для сайта, их закупочные данные, изменения).
    """
    now = now or now_utc()
    items: dict = st["items"]
    sold: dict = st["_sold"]
    baseline = not items
    ch = {k: [] for k in ("new", "returned", "sold_out", "sizes_gone", "sizes_back", "price",
                          "removed_filtered", "dropped_unconfirmed", "sold_out_expired")}
    ch["price_uzs_only"] = 0
    live_ids = set()

    for pub, row in live:
        pid = pub["id"]
        live_ids.add(pid)
        sizes = list(pub.get("sizes") or [])
        e = items.get(pid)
        if e is None:
            e = items[pid] = {"first_seen": iso(now), "sizes_seen": [], "sizes_seen_at": {}}
            if not baseline:
                ch["new"].append(brief(pub, pid))
        elif e.get("sold_out_since") or e.get("in_stock") is False:
            ch["returned"].append(brief(pub, pid))
            sold.pop(pid, None)
        elif not e.get("on_site", True):
            ch["new"].append(dict(brief(pub, pid), note="снова на сайте"))
        else:
            prev = e.get("sizes_last") or []
            if informative(prev) and informative(sizes):
                gone = [s for s in prev if s not in sizes]
                back = [s for s in sizes if s not in prev]
                if gone:
                    ch["sizes_gone"].append(dict(brief(pub, pid), sizes=gone, now=sizes))
                if back:
                    ch["sizes_back"].append(dict(brief(pub, pid), sizes=back, now=sizes))
            src_old, src_new = e.get("price_src_last"), row.get("price_now")
            uzs_old, uzs_new = e.get("price_uzs_last"), pub.get("price_uzs")
            if src_old is not None and src_new is not None and abs(float(src_old) - float(src_new)) > 0.009:
                ch["price"].append(dict(brief(pub, pid), uzs_old=uzs_old, uzs_new=uzs_new, src_old=src_old,
                                        src_new=src_new, currency=row.get("currency")))
            elif uzs_old is not None and uzs_old != uzs_new:
                ch["price_uzs_only"] += 1          # цена в магазине та же — изменился курс / наценка
        e.update(last_seen=iso(now), price_uzs_last=pub.get("price_uzs"), price_src_last=row.get("price_now"),
                 currency=row.get("currency"), in_stock=True, sold_out_since=None, on_site=True, off_reason=None,
                 source=row.get("source"), item_id=str(row.get("source_item_id")),
                 brand=pub.get("brand"), title=pub.get("title"))
        if sizes or not e.get("sizes_last"):
            e["sizes_last"] = sizes
        seen_at = e.setdefault("sizes_seen_at", {})
        seen = e.setdefault("sizes_seen", [])
        for s in sizes:
            seen_at[s] = iso(now)
            if s not in seen:
                seen.append(s)
        e["sizes_seen"] = sorted(seen, key=size_key)
        pub["sizes_out"] = _sizes_out(e, sizes, now) if informative(sizes) else []

    keep = timedelta(days=float(keep_days))
    for pid, e in list(items.items()):
        if pid in live_ids:
            continue
        if e.get("sold_out_since"):
            since = parse_ts(e["sold_out_since"]) or now
            if e.get("on_site") and now - since > keep:
                e.update(on_site=False, off_reason="sold_out_expired")
                sold.pop(pid, None)
                ch["sold_out_expired"].append(brief(e, pid))
            continue
        if not e.get("on_site"):
            continue
        si = sources.get(e.get("source")) or {}
        row = (si.get("present") or {}).get(str(e.get("item_id")))
        gone = False
        if si.get("changed"):
            if str(e.get("item_id")) in (si.get("gone_ids") or set()):
                gone = True
            elif row is not None and row.get("in_stock") is False:
                gone = True
            elif row is None and si.get("absence_means_gone"):
                gone = True
        if gone:
            e.update(in_stock=False, sold_out_since=iso(now), last_seen=e.get("last_seen"))
            snap = prev_public.get(pid)
            if snap:
                sold[pid] = {"since": iso(now), "public": snap, "admin": prev_admin.get(pid) or {}}
            else:
                e["on_site"] = False               # карточки нет — показать нечего
                e["off_reason"] = "sold_out_no_snapshot"
            ch["sold_out"].append(dict(brief(e, pid), sizes_last=e.get("sizes_last") or []))
        elif row is not None:
            e.update(on_site=False, off_reason="filtered")   # есть в магазине, но не проходит фильтры
            ch["removed_filtered"].append(brief(e, pid))
        else:
            e.update(on_site=False, off_reason="unconfirmed")  # данных нет, но и распродажа не подтверждена
            ch["dropped_unconfirmed"].append(dict(brief(e, pid), source=e.get("source")))

    # карточки распроданных для сайта
    out_pub, out_admin = [], {}
    for pid, rec in list(sold.items()):
        e = items.get(pid)
        if not e or not e.get("sold_out_since") or not e.get("on_site") or pid in live_ids:
            sold.pop(pid, None)
            continue
        pub = dict(rec.get("public") or {})
        if not pub:
            continue
        last = [s for s in (e.get("sizes_last") or pub.get("sizes") or []) if informative([s])]
        recent = _sizes_out(e, [], now)
        pub.update(in_stock=False, sizes=[], sizes_out=sorted(set(last) | set(recent), key=size_key)[:SIZES_OUT_MAX])
        out_pub.append(pub)
        if rec.get("admin"):
            out_admin[pid] = dict(rec["admin"], sold_out_since=e["sold_out_since"])

    # забываем давно ушедшие товары
    forget = now - timedelta(days=FORGET_DAYS)
    for pid, e in list(items.items()):
        if not e.get("on_site") and (parse_ts(e.get("last_seen")) or now) < forget:
            items.pop(pid, None)
            sold.pop(pid, None)

    ch["baseline"] = baseline
    ch["counts"] = {k: (len(v) if isinstance(v, list) else v) for k, v in ch.items() if k not in ("baseline",)}
    return out_pub, out_admin, ch


def any_changes(ch: dict) -> bool:
    return any(v for k, v in ch["counts"].items())


def write_changes(data_dir: Path, ch: dict, sources_report: dict, now: datetime | None = None) -> Path:
    now = now or now_utc()
    folder = data_dir / "changes"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = folder / f"{stamp}.json"
    n = 2
    while path.exists():
        path = folder / f"{stamp}_{n}.json"
        n += 1
    data = {"generated_at": iso(now), "sources": sources_report, "counts": ch["counts"]}
    data.update({k: v for k, v in ch.items() if k not in ("counts", "baseline")})
    write_json(path, data, indent=1)
    return path


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    a, b = n % 100, n % 10
    return forms[2] if 10 < a < 20 else forms[0] if b == 1 else forms[1] if 1 < b < 5 else forms[2]


def summary_lines(ch: dict, keep_days: float, n_sold_shown: int) -> list[str]:
    c = ch["counts"]
    if ch.get("baseline"):
        return ["Изменения: первый запуск с памятью — запомнил, что сейчас на сайте; сравнение начнётся со следующего."]
    if not any_changes(ch):
        return ["Изменения: с прошлого обновления ничего не изменилось."]
    out = ["Изменения с прошлого обновления:"]
    out.append(f"  новых товаров: {c['new']}; распродано: {c['sold_out']}; вернулись в продажу: {c['returned']}")
    out.append(f"  размеры закончились у {c['sizes_gone']} {_plural(c['sizes_gone'], ('товара', 'товаров', 'товаров'))}, "
               f"появились снова у {c['sizes_back']}; цена в магазине изменилась у {c['price']}"
               + (f" (только в сумах, из-за курса/наценки — у {c['price_uzs_only']})" if c["price_uzs_only"] else ""))
    extra = []
    if c["removed_filtered"]:
        extra.append(f"сняты фильтрами (скидка/цена/размеры): {c['removed_filtered']}")
    if c["dropped_unconfirmed"]:
        extra.append(f"убраны без подтверждения распродажи (нет свежих данных): {c['dropped_unconfirmed']}")
    if c["sold_out_expired"]:
        extra.append(f"распроданные старше {keep_days:g} дн. убраны с сайта: {c['sold_out_expired']}")
    if extra:
        out.append("  " + "; ".join(extra))
    if n_sold_shown:
        out.append(f"  на сайте с пометкой «Нет в наличии»: {n_sold_shown} (держатся {keep_days:g} дн.)")
    ex = []
    label = {"sold_out": "распродано", "sizes_gone": "нет размеров", "returned": "снова в продаже",
             "sizes_back": "размеры вернулись", "price": "цена", "new": "новый"}
    for k in ("sold_out", "sizes_gone", "returned", "sizes_back", "price", "new"):
        for x in ch[k][:4]:
            tail = ""
            if k in ("sizes_gone", "sizes_back"):
                tail = " — " + ", ".join(x["sizes"])
            elif k == "price":
                tail = f" — {x['src_old']:g} → {x['src_new']:g} {x.get('currency') or ''}".rstrip()
            ex.append(f"    {label[k]}: {x['id']} · {x.get('brand') or ''} · {(x.get('title') or '')[:48]}{tail}")
    if ex:
        out.append("  Примеры:")
        out += ex[:EXAMPLES]
    return out
