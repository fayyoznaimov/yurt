"""Собрать товары со всех источников, посчитать цену в сумах и обновить сайт.

    python run.py                          # все источники из config.json
    python run.py --only trendyol          # один источник (можно несколько: --only pcardin_tr,cacharel_tr)
    python run.py --offline                # не ходить на сайты, пересчитать из data/raw_*.json
    python run.py --only pcardin_tr --accept-drop   # принять резкое падение числа товаров (распродажа кончилась)

Сервер (server/yurt-run.sh) разносит сбор и сборку сайта по отдельным заданиям:
    python run.py --collect-only --only pcardin_tr,cacharel_tr          # только собрать в data/raw_*.json
    python run.py --collect-only --only trendyol --trendyol-mode pdp    # проверка размеров Trendyol по ярусам
    python run.py --collect-only --only trendyol --trendyol-mode listing  # ночной обход выдачи Trendyol
    python run.py --offline --if-changed     # собрать сайт, только если данные источников / config.json изменились
--collect-only не берёт общий замок data/run.lock (сбор идёт часами и не должен держать сборку сайта): у каждого
источника свой замок data/collect_<источник>.lock, сайт и data/state.json он не трогает. Сборку делает
run.py --offline — она видит новые data/raw_*.json и их итог (распроданные) по data/raw_*.meta.json.

Каждый запуск сравнивается с прошлым (sync_state.py, data/state.json): распроданные товары ещё
sync.keep_sold_out_days дней видны на сайте с пометкой «Нет в наличии», закончившиеся размеры
показываются зачёркнутыми (sizes_out), что изменилось — в data/changes/. Если источник упал или его
заблокировали, его товары НЕ пропадают: берутся прошлые данные data/raw_<источник>.json.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import catalog_files
import describe
import img_map
import ranking
import sizes_norm
import sync_state
from pricing import load_rates, sell_price_uzs
from sources import ADAPTERS
from sources.base import Product, Query

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "site"
PUB_IMG = "img/p"        # фото для сайта под нейтральными именами: site/img/p/<код IPAK>_<n>.jpg
# прежнее имя своих фото (до октября 2026) — <код>-<n>.jpg; такие файлы переименовываются, а не скачиваются заново
LEGACY_PUB_RE = re.compile(r"^([A-Z2-7]{7,12})-(\d{1,3})(\.[A-Za-z0-9]{2,5})$")
# адаптер -> код источника, который стоит в товарах (product.source) и в source_filters
PRODUCT_SOURCE = {"yoox_import": "yoox", "feed_yoox": "yoox"}   # feed_yoox — партнёрский фид (не включать вместе с yoox_import)
COMPACT_RAW = {"yoox_import", "feed_yoox"}   # большие data/raw_<источник>.json пишутся без отступов (в разы меньше)
YOOX_STALE_HIDE_DAYS = 7  # товар YOOX, который не подтверждался файлами кнопки дольше, на сайте не показываем
TOP_BRANDS = 25          # сколько брендов печатать в сводке, остальные — одной строкой
# Поля товара на публичном сайте (products.js / products.json). Только то, что видит покупатель:
# никаких магазинов-источников, ссылок, исходных названий, валют, закупочных цен и маржи.
# sizes — размеры в наличии; sizes_out — размеры, которые недавно были, а теперь закончились.
PUBLIC_FIELDS = ("id", "brand", "title", "type", "gender", "origin", "price_uzs", "discount_pct", "sizes",
                 "sizes_out", "size_system", "color", "composition", "details", "description", "images",
                 "in_stock", "fetched_at", "first_seen")
# first_seen — когда товар впервые появился на сайте (из data/state.json); по нему лента «Новинки».
# Закупочные данные по коду товара — в products-admin.js (не выкладывается, нужен для ?admin=1).
ADMIN_FIELDS = ("source", "source_item_id", "url", "title_original", "category_original", "price_now",
                "price_old", "currency", "cost_uzs", "margin_uzs")
# Настройки слежения (config.json → sync); чего нет в config — берётся отсюда.
SYNC_DEFAULTS = {"keep_sold_out_days": 3, "auto_deploy": False, "every_hours": 6,
                 "max_drop_pct": 50, "verify_limit": 120, "carry_days": 7}
STATUS_RU = {"ok": "обновлён", "partial": "собран не полностью", "stale": "НЕ ОБНОВИЛСЯ — прошлые данные",
             "nodata": "новых данных нет — прошлые", "busy": "занят другим заданием — прошлые данные",
             "failed": "ошибка, данных нет", "raw": "из data/raw (не собирался)"}
HOT_DAYS = 30            # «горячие» товары Trendyol: из заказов и корзин за столько дней (+ верх «Рекомендуем»)
VOLATILE_KEYS = ("fetched_at",)   # поля строки источника, которые меняются каждый сбор и сайт не меняют

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def product_source(adapter: str) -> str:
    return PRODUCT_SOURCE.get(adapter, adapter)


def _clean(d: dict | None) -> dict:
    """Ключи на «_» в config.json — пояснения для человека, не настройки."""
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def sync_cfg(cfg: dict) -> dict:
    out = dict(SYNC_DEFAULTS)
    out.update(_clean(cfg.get("sync")))
    return out


def filters_for(source: str, cfg: dict) -> dict:
    """Фильтры для товаров источника: filters, поверх них source_filters[source]."""
    f = _clean(cfg.get("filters"))
    f.update(_clean((cfg.get("source_filters") or {}).get(source)))
    return f


def _when(ts) -> str:
    dt = sync_state.parse_ts(ts)
    return dt.astimezone().strftime("%d.%m %H:%M") if dt else "?"


def _age_days(ts) -> float:
    dt = sync_state.parse_ts(ts)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400 if dt else 1e9


def raw_path(source: str) -> Path:
    return DATA / f"raw_{source}.json"


def meta_path(source: str) -> Path:
    return DATA / f"raw_{source}.meta.json"


def collect(source: str, cfg: dict, prev_rows: list[dict] | None = None, watch: set[str] | None = None,
            accept_drop: bool = False, extra: dict | None = None) -> tuple[list[dict], dict]:
    """Запускает один адаптер. Ошибка одного источника не роняет остальные и не стирает его товары.

    Возвращает (строки, info). info["status"]: ok — собрано; partial — собрано не всё (блокировка посреди
    обхода, пропущенные страницы, подозрительно мало товаров): чего нет в выдаче, остаётся как было;
    stale — адаптер упал, взяты прошлые данные; failed — упал, прошлых данных нет.
    Товары с нашего сайта (watch), которых нет в выдаче, перепроверяются по ссылке (verify адаптера):
    распроданными считаются только подтверждённые (info["gone_ids"])."""
    prev_rows = prev_rows if prev_rows is not None else load_raw(source)
    watch = set(watch or ())
    sc = sync_cfg(cfg)
    module_name, opts, _country = ADAPTERS[source]
    f = filters_for(product_source(source), cfg)
    so = dict(cfg.get("source_opts", {}).get(source, {}))
    so.update(extra or {})               # служебное от вызывающего: режим Trendyol, горячие id, свой хост фото…
    hot = {str(x) for x in (so.get("_hot") or ())}
    by_prev = {str(r["source_item_id"]): r for r in prev_rows}
    if source == "yoox_import" and watch:   # чтобы адаптер мог сказать, пропал ли товар из новых файлов YOOX
        so["_watch"] = {i: {k: by_prev[i].get(k) for k in ("brand", "gender", "price_now", "fetched_at")}
                        for i in watch if i in by_prev}
    query = Query(
        brands=f.get("brands", []), types=f.get("types", []), genders=f.get("genders", []),
        discount_min=f.get("discount_min", 0),
        max_pages=so.get("max_pages") or cfg.get("crawl", {}).get("max_pages", 3),
        source_opts=so,
    )
    info = {"status": "ok", "note": "", "gone_ids": [], "absence_means_gone": True, "verified": 0,
            "carried": 0, "collected_at": sync_state.iso(sync_state.now_utc())}
    t0 = time.time()
    try:
        module = importlib.import_module(module_name)
        if isinstance(getattr(module, "LAST_RUN", None), dict):
            module.LAST_RUN.clear()
        items: list[Product] = module.fetch(query, **opts)
        rows = [p.to_dict() for p in items]
    except Exception as e:
        traceback.print_exc(limit=3)
        if prev_rows:
            info.update(status="stale", note=f"{e.__class__.__name__}: {e}")
            newest = max((r.get("fetched_at") or "" for r in prev_rows), default="")
            print(f"[{source}] ВНИМАНИЕ: источник не обновился ({e}). Беру прошлые данные data/raw_{source}.json "
                  f"({len(prev_rows)} шт., собраны {_when(newest)}) — товары с сайта НЕ удаляю, данные устаревшие.")
            return prev_rows, info
        info.update(status="failed", note=f"{e.__class__.__name__}: {e}")
        print(f"[{source}] ОШИБКА, прошлых данных нет — источник пропущен: {e}")
        return [], info

    run = dict(getattr(module, "LAST_RUN", None) or {})
    if run.get("no_new_data") and not rows:
        info.update(status="nodata", note="новых данных нет")
        print(f"[{source}] новых данных нет — оставляю прошлые ({len(prev_rows)} шт.)")
        return prev_rows, info
    info["absence_means_gone"] = bool(run.get("absence_means_gone", True))
    gone_ids = [str(x) for x in (run.get("gone_ids") or [])]
    partial, reason = bool(run.get("partial")), str(run.get("reason") or "")
    n_fetched = len(rows)
    drop = float(sc["max_drop_pct"] or 0)
    if (not partial and not run.get("skipped") and not run.get("no_drop_check") and not accept_drop and drop > 0 and len(prev_rows) >= 20
            and n_fetched < len(prev_rows) * (1 - drop / 100)):
        partial = True
        reason = (f"собрано {n_fetched} вместо прежних {len(prev_rows)} — похоже на сбой или блокировку; если распродажа "
                  f"правда закончилась, запустите: python run.py --only {source} --accept-drop")

    # Товары с нашего сайта, которых нет в выдаче (или у которых пропали размеры), — перепроверяем по ссылке.
    by_new = {str(r["source_item_id"]): r for r in rows}
    verify = getattr(module, "verify", None)
    if callable(verify):
        info["absence_means_gone"] = False          # распроданы только подтверждённые
        order = sorted(watch, key=lambda i: (i not in hot, i))      # горячие (заказы, корзины, верх витрины) — первыми
        missing = [by_prev[i] for i in order if i not in by_new and i in by_prev]
        unsized = [by_new[i] for i in order
                   if i in by_new and not by_new[i].get("sizes") and (by_prev.get(i) or {}).get("sizes")]
        try:
            limit = int(so.get("verify_limit", sc["verify_limit"]) or 0)    # source_opts.<источник>.verify_limit
        except (TypeError, ValueError):
            limit = int(sc["verify_limit"] or 0)
        todo = (missing + unsized)[:limit]
        if todo:
            print(f"[{source}] перепроверяю по ссылкам {len(todo)} товаров с сайта "
                  f"(нет в выдаче: {len(missing)}, без размеров: {len(unsized)}"
                  + (f", лимит {limit}" if len(missing) + len(unsized) > limit else "") + ")")
            try:
                res = verify(todo, query, **opts) or {}
            except Exception as e:
                print(f"[{source}] перепроверка не удалась: {e}")
                res = {}
            n_gone = n_ok = 0
            n_dead = sum(1 for r in todo if str(r["source_item_id"]) in res and res[str(r["source_item_id"])] is None)
            # Защита для запусков без человека (GitHub Actions): если «распродано» сразу больше max_drop_pct %
            # товаров источника на сайте, скорее всего сайт отдаёт машине чужие страницы (гео/блокировка),
            # а не распродажа. Такие товары не помечаем; правда распродано — запустите с --accept-drop.
            suspicious = (not accept_drop and n_dead >= 10 and len(watch) and drop > 0
                          and n_dead > len(watch) * drop / 100)
            if suspicious:
                print(f"[{source}] ВНИМАНИЕ: перепроверка говорит «нет в наличии» у {n_dead} из {len(watch)} товаров "
                      f"с сайта — похоже на сбой/блокировку, распроданными не помечаю "
                      f"(если правда: python run.py --only {source} --accept-drop)")
                partial, reason = True, f"перепроверка: подозрительно много «нет в наличии» ({n_dead})"
            for r in todo:
                i = str(r["source_item_id"])
                if i not in res:
                    continue
                info["verified"] += 1
                if res[i] is None:
                    if suspicious:
                        continue
                    gone_ids.append(i)
                    by_new.pop(i, None)
                    n_gone += 1
                else:
                    by_new[i] = res[i]
                    n_ok += 1
            print(f"[{source}] перепроверено {info['verified']} из {len(todo)}: нет в наличии {n_gone}, "
                  f"в наличии {n_ok}, не удалось проверить {len(todo) - info['verified']}")
        # не перепроверенные, но есть в выдаче без размеров — прежние размеры (в выдаче их просто нет)
        for i in watch:
            if i in by_new and i not in gone_ids and not by_new[i].get("sizes") and (by_prev.get(i) or {}).get("sizes"):
                by_new[i] = dict(by_new[i], sizes=by_prev[i]["sizes"])
    if not info["absence_means_gone"]:
        # товары с сайта, которых нет в выдаче и распродажа которых не подтверждена, — оставляем как были,
        # но не дольше sync.carry_days с последнего раза, когда их видели
        try:
            carry = float(so.get("carry_days", sc["carry_days"]))           # source_opts.<источник>.carry_days
        except (TypeError, ValueError):
            carry = float(sc["carry_days"])
        for i in watch:
            if i not in by_new and i not in gone_ids and i in by_prev and _age_days(by_prev[i].get("fetched_at")) <= carry:
                by_new[i] = by_prev[i]
                info["carried"] += 1
    if partial:
        info["absence_means_gone"] = False
        kept_before = len(by_new)
        for i, r in by_prev.items():
            if i not in by_new and i not in gone_ids:
                by_new[i] = r
        info["carried"] += len(by_new) - kept_before
        info.update(status="partial", note=reason)
        print(f"[{source}] ВНИМАНИЕ: собрано не всё ({reason}). Товары, которых нет в этой выдаче, оставляю "
              f"как были ({len(by_new) - kept_before} шт.), распроданными их не считаю.")
    rows = list(by_new.values())
    # бренд убрали из списка — его прошлые товары тоже уходят, а не висят «из прошлых»
    bkey = lambda s: re.sub(r"[\W_]+", "", str(s or "").lower())
    wanted = {bkey(b) for b in f.get("brands", [])}
    if wanted:
        n_before = len(rows)
        rows = [r for r in rows if bkey(r.get("brand")) in wanted]
        if len(rows) < n_before:
            print(f"[{source}] убраны товары брендов не из списка: {n_before - len(rows)}")
    info["gone_ids"] = sorted(set(gone_ids))

    write_raw(source, rows)
    meta = {"source": source, "status": info["status"], "collected_at": info["collected_at"], "note": info["note"],
            "fetched": n_fetched, "rows": len(rows), "verified": info["verified"], "carried": info["carried"],
            "absence_means_gone": info["absence_means_gone"], "gone_ids": info["gone_ids"],
            "raw_sha1": sync_state.file_sha1(raw_path(source))}
    sync_state.write_json(meta_path(source), meta, indent=1)
    with_disc = sum(1 for r in rows if r.get("discount_pct"))
    print(f"[{source}] собрано {n_fetched} товаров, в данных {len(rows)} (со скидкой {with_disc}) за {time.time() - t0:.0f} с"
          + (f"; из прошлых оставлено {info['carried']}" if info["carried"] else "")
          + (f"; распродано (подтверждено) {len(info['gone_ids'])}" if info["gone_ids"] else ""))
    return rows, info


def load_raw(source: str) -> list[dict]:
    p = raw_path(source)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except ValueError as e:
        print(f"[{source}] data/raw_{source}.json не читается ({e}) — считаю пустым")
        return []


def load_raw_consistent(source: str) -> tuple[list[dict], str | None, dict]:
    """(строки, sha1 файла, meta) для сборки — из одного и того же содержимого файла. Сбор идёт отдельным заданием и
    может как раз переписывать raw и meta: если meta не про этот файл, читаем ещё раз (до 3 попыток)."""
    p = raw_path(source)
    data, sha, meta = b"", None, {}
    for attempt in range(3):
        meta = sync_state.read_json(meta_path(source), {})
        meta = meta if isinstance(meta, dict) else {}
        try:
            data = p.read_bytes()
        except OSError:
            return [], None, meta
        sha = hashlib.sha1(data).hexdigest()
        if not meta or meta.get("raw_sha1") in (None, sha):
            break
        if attempt < 2:
            time.sleep(0.5)
    try:
        rows = json.loads(data.decode("utf-8")) if data else []
    except ValueError as e:
        print(f"[{source}] data/raw_{source}.json не читается ({e}) — считаю пустым")
        rows = []
    return (rows if isinstance(rows, list) else []), sha, meta


def write_raw(source: str, rows: list[dict]) -> None:
    """data/raw_<источник>.json через временный файл; большие (COMPACT_RAW) — без отступов."""
    if source in COMPACT_RAW:
        sync_state.write_json(raw_path(source), rows, compact=True)
    else:
        sync_state.write_json(raw_path(source), rows, indent=1)


def compact_raw(source: str, state: dict) -> int:
    """Прежний data/raw_<источник>.json с отступами (до COMPACT_RAW) -> без отступов, один раз. Отпечатки
    (raw_sha1 в meta и в state) переносятся на новый файл, только если совпадали со старым, — так сборка не
    примет переписанный файл за новые данные источника. Возвращает, на сколько байт файл стал меньше."""
    p = raw_path(source)
    try:
        with p.open("rb") as f:
            head = f.read(2)
        st = p.stat()
    except OSError:
        return 0
    if len(head) < 2 or head[:1] != b"[" or head[1:2] not in (b"\n", b"\r", b" "):
        return 0
    old_sha = sync_state.file_sha1(p)
    rows = load_raw(source)
    if not rows:
        return 0
    write_raw(source, rows)
    # время файла — прежнее: update.py по нему решает, какие файлы кнопки YOOX run.py уже видел
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    new_sha = sync_state.file_sha1(p)
    meta = sync_state.read_json(meta_path(source), None)
    if isinstance(meta, dict) and meta.get("raw_sha1") == old_sha:
        meta["raw_sha1"] = new_sha
        sync_state.write_json(meta_path(source), meta, indent=1)
    rec = state["sources"].get(product_source(source))
    if isinstance(rec, dict) and rec.get("raw_sha1") == old_sha:
        rec["raw_sha1"] = new_sha
        sync_state.save(DATA, state)
    return st.st_size - p.stat().st_size


def trendyol_official_flags(cfg: dict) -> tuple[dict[str, bool], dict[str, bool]]:
    """Бейдж официального продавца Trendyol: ({id товара: да/нет}, {id продавца: да/нет}). Источники — листинг
    (data/trendyol_listing.json → items[id]._official, _merchant_id) и кэш страниц товаров
    (data/trendyol_pdp_cache.json → [id].official). В data/raw_trendyol.json этого поля нет."""
    so = (cfg.get("source_opts") or {}).get("trendyol") or {}

    def path(key: str, default: str) -> Path:
        p = Path(so.get(key) or default)
        return p if p.is_absolute() else ROOT / p

    by_item: dict[str, bool] = {}
    by_merchant: dict[str, bool] = {}
    pdp = sync_state.read_json(path("pdp_cache_file", "data/trendyol_pdp_cache.json"), {})
    for cid, e in (pdp.items() if isinstance(pdp, dict) else []):
        if isinstance(e, dict) and isinstance(e.get("official"), bool):
            by_item[str(cid)] = e["official"]
    listing = sync_state.read_json(path("listing_file", "data/trendyol_listing.json"), {})
    items = listing.get("items") if isinstance(listing, dict) else None
    for cid, rec in (items.items() if isinstance(items, dict) else []):
        if not isinstance(rec, dict) or not isinstance(rec.get("_official"), bool):
            continue
        by_item[str(cid)] = rec["_official"]          # листинг свежее и полнее кэша
        if rec.get("_merchant_id") is not None:
            by_merchant.setdefault(str(rec["_merchant_id"]), rec["_official"])
    return by_item, by_merchant


def is_official(row: dict, by_item: dict[str, bool], by_merchant: dict[str, bool]) -> bool:
    """Карточка Trendyol от официального продавца? Неизвестно — нет (обещаем «только официальные продавцы»)."""
    v = by_item.get(str(row.get("source_item_id")))
    if v is None:
        m = re.search(r"[?&]merchantId=(\d+)", str(row.get("url") or ""))
        v = by_merchant.get(m.group(1)) if m else None
    return bool(v)


def brand_key(s: str) -> str:
    """«Dolce & Gabbana» и «Dolce&Gabbana», «EA7» и «Ea7» — один бренд."""
    return re.sub(r"[\W_]+", "", (s or "").lower())


def _bound(v, upper: bool = False):
    """Граница фильтра из config.json: пусто / null (и 0 для верхней) — без ограничения."""
    if v in (None, ""):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if upper and v <= 0 else v


def _between(v, lo, hi) -> bool:
    return _between_raw(v, _bound(lo), _bound(hi, upper=True))


def _between_raw(v: float, lo, hi) -> bool:
    """Пустая граница или 0 у верхней — без ограничения."""
    return (lo is None or v >= lo) and (not hi or v <= hi)


def passes(p: dict, f: dict) -> bool:
    if f.get("brands") and brand_key(p["brand"]) not in {brand_key(b) for b in f["brands"]}:
        return False
    if f.get("types") and p.get("type") not in f["types"]:
        return False
    if f.get("genders") and p.get("gender") not in f["genders"]:
        return False
    if (p.get("discount_pct") or 0) < (f.get("discount_min") or 0):
        return False
    # цена в валюте источника (EUR у YOOX, TRY у турецких магазинов)
    if not _between(p["price_now"], f.get("price_min"), f.get("price_max")):
        return False
    if not _between(p["price_uzs"], f.get("price_uzs_min"), f.get("price_uzs_max")):
        return False
    if f.get("only_with_sizes") and not p.get("sizes"):
        return False
    return p.get("in_stock", True)


def hidden_rows(rows: list[dict], cfg: dict) -> set[tuple[str, str]]:
    """Что не показывать на сайте, хотя товар есть в данных источника: (источник, id в магазине).
    Trendyol при source_opts.trendyol.official_only — всё не от официального продавца (и при сборке --offline);
    YOOX — что файлы кнопки не подтверждали дольше source_opts.yoox_import.stale_hide_days дней (0 — не скрывать).
    Эти товары остаются в data/raw_* и в выдаче источника: для памяти (sync_state) они «сняты фильтрами»,
    а не распроданы."""
    so = cfg.get("source_opts") or {}
    out: set[tuple[str, str]] = set()
    ty = [r for r in rows if r.get("source") == "trendyol"]
    if ty and (so.get("trendyol") or {}).get("official_only"):
        by_item, by_merchant = trendyol_official_flags(cfg)
        bad = [r for r in ty if not is_official(r, by_item, by_merchant)]
        out.update((r["source"], str(r["source_item_id"])) for r in bad)
        brands: dict[str, int] = {}
        for r in bad:
            brands[r.get("brand") or "?"] = brands.get(r.get("brand") or "?", 0) + 1
        print(f"[trendyol] только официальные продавцы: {len(ty) - len(bad)} из {len(ty)}"
              + (f"; скрыто {len(bad)}: " + ", ".join(f"{b} {n}" for b, n in sorted(brands.items(), key=lambda x: -x[1]))
                 if bad else ""))
    yx = [r for r in rows if r.get("source") == "yoox"]
    if yx:
        try:
            days = float((so.get("yoox_import") or {}).get("stale_hide_days", YOOX_STALE_HIDE_DAYS) or 0)
        except (TypeError, ValueError):
            days = float(YOOX_STALE_HIDE_DAYS)
        stale = [r for r in yx if days > 0 and _age_days(r.get("fetched_at")) > days]
        out.update((r["source"], str(r["source_item_id"])) for r in stale)
        print(f"[yoox] не подтверждались файлами кнопки дольше {days:g} дн. — скрыто с сайта: {len(stale)} из {len(yx)}"
              if days > 0 else f"[yoox] скрытие устаревших выключено (stale_hide_days = 0)")
    return out


def stats(items: list[dict]) -> dict | None:
    """Сводка: число товаров, цены в сумах, скидки (средняя — только по товарам со скидкой)."""
    if not items:
        return None
    prices = [i["price_uzs"] for i in items]
    disc = [i["discount_pct"] for i in items if i.get("discount_pct")]
    return {"count": len(items), "price_min": min(prices), "price_max": max(prices),
            "with_discount": len(disc), "discount_max": max(disc, default=0),
            "discount_avg": round(sum(disc) / len(disc), 1) if disc else 0}



ONE_SIZE = {"--", "-", "ONESIZE", "ONE SIZE", "OS", "TU", "UNI", "UNICA", "STD", "STANDART", "TEK EBAT"}


def clean_sizes(sizes) -> list[str]:
    """Размеры как у магазина; «--»/ONESIZE и т.п. — это «Единый размер» (перевод, не выдумка)."""
    out = []
    for s in sizes or []:
        s = str(s).strip()
        if not s:
            continue
        s = "Единый размер" if s.upper() in ONE_SIZE else s
        if s not in out:
            out.append(s)
    return out


def public_id(source: str, item_id: str, n: int = 7) -> str:
    """Нейтральный код товара для покупателя: первые 7 символов base32(sha1("источник:id")).
    Не меняется между запусками и ничего не говорит о магазине (никаких YX-/TY-/PC-/CC-)."""
    digest = hashlib.sha1(f"{source}:{item_id}".encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii")[:n]


def pub_image_name(code: str, n: int, suffix: str = ".jpg") -> str:
    """Имя своего фото на сайте: <код IPAK>_<n>.jpg (код — public_id). По имени не узнать ни магазин, ни его
    номер товара."""
    return f"{code}_{n}{(suffix or '.jpg').lower()}"


def _adopt(dst: Path, old: Path) -> bool:
    """Переименовать уже выложенную копию old в dst (фото не копируется и не скачивается заново).
    Если dst уже есть — лишняя старая копия удаляется."""
    if old == dst or not old.is_file():
        return False
    try:
        if dst.exists():
            old.unlink()
        else:
            os.replace(old, dst)
        return True
    except OSError:
        return False


def legacy_pub_name(name: str) -> str | None:
    """«ABCDEFG-2.jpg» (прежнее имя своего фото) -> «ABCDEFG_2.jpg»; другое имя -> None."""
    m = LEGACY_PUB_RE.match(name)
    return pub_image_name(m.group(1), int(m.group(2)), m.group(3)) if m else None


def migrate_published_names() -> int:
    """Один раз (и после копии из gh-pages, где ещё старые имена): site/img/p/<код>-<n>.jpg -> <код>_<n>.jpg.
    Файлы переименовываются на месте — ничего не скачивается и не копируется заново. Возвращает число файлов."""
    folder = SITE / PUB_IMG
    moved = 0
    for f in (sorted(folder.iterdir()) if folder.is_dir() else []):
        new = legacy_pub_name(f.name) if f.is_file() else None
        if new and _adopt(folder / new, f):
            moved += 1
    return moved


def publish_images(code: str, images: list[str], used: set[str]) -> list[str]:
    """Локальные фото (site/img/yoox/<id в магазине>_1.jpg) -> site/img/p/<код>_<n>.jpg: в адресе фото на сайте
    не должно быть ни имени магазина, ни его номера товара. Жёсткая ссылка, если можно, иначе копия.
    Уже выложенная копия под прежним именем (<код>-<n>.jpg или имя файла магазина) переименовывается.
    Внешние ссылки (CDN магазинов) остаются как есть."""
    out = []
    folder = SITE / PUB_IMG
    for n, img in enumerate(images, 1):
        if img.startswith(("http://", "https://", "//")):
            out.append(img)
            continue
        src = SITE / img
        suffix = src.suffix.lower() or ".jpg"
        name = pub_image_name(code, n, suffix)
        dst = folder / name
        if not dst.exists():
            for old in (f"{code}-{n}{suffix}", src.name):
                if (folder / old) != src and _adopt(dst, folder / old):
                    break
        if not src.exists():
            # оригинала нет (в облаке нет site/img/yoox), но копия уже выложена — берём её (её дал gh-pages)
            if dst.is_file():
                used.add(name)
                out.append(f"{PUB_IMG}/{name}")
            continue
        used.add(name)
        if not (dst.exists() and dst.stat().st_size == src.stat().st_size):
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst.unlink()
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)
        out.append(f"{PUB_IMG}/{name}")
    return out


_TOKMAP: dict | None = None


def _token_map() -> dict:
    """Карта своего хоста фото (data/img_map.json) — один раз за запуск."""
    global _TOKMAP
    if _TOKMAP is None:
        _TOKMAP = img_map.load_items(img_map.map_path(DATA))
    return _TOKMAP


def keep_published_images(images: list[str], used: set[str]) -> list[str]:
    """Фото карточки распроданного товара (уже лежат в site/img/p): не удалять и не ссылаться на пропавшие.
    Ссылки прошлой сборки на прежние имена (<код>-<n>.jpg) переводятся на новые."""
    out = []
    folder = SITE / PUB_IMG
    for img in images or []:
        if img.startswith(("http://", "https://", "//")):
            out.append(img)
            continue
        if img_map.is_token(img):         # снимок со своего хоста фото: токен; его файл в img/p (если есть) — нужен
            rec = _token_map().get(img)
            src = rec[0] if rec else ""
            if src.startswith(PUB_IMG + "/") and (folder / src.split("/")[-1]).is_file():
                used.add(src.split("/")[-1])
            out.append(img)
            continue
        if not img.startswith(PUB_IMG + "/"):
            continue
        name = img.split("/")[-1]
        new = legacy_pub_name(name)
        if new:
            _adopt(folder / new, folder / name)
            name = new
        if (folder / name).is_file():
            used.add(name)
            out.append(f"{PUB_IMG}/{name}")
    return out


def _utc(ts: str | None) -> str | None:
    """Время сбора в одном формате для всех источников: «2026-10-03T09:44:14Z»."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ts


def cleanup_images(used: set[str]) -> int:
    """Удаляет из site/img/p фото товаров, которых больше нет на сайте."""
    folder = SITE / PUB_IMG
    removed = 0
    for f in (folder.glob("*") if folder.exists() else []):
        if f.is_file() and f.name not in used:
            f.unlink()
            removed += 1
    return removed


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """plural(3, ("бренд", "бренда", "брендов")) -> "бренда"."""
    a, b = n % 100, n % 10
    return forms[2] if 10 < a < 20 else forms[0] if b == 1 else forms[1] if 1 < b < 5 else forms[2]


def rank_report(public: list[dict], first: int = ranking.WINDOW) -> str:
    """Строка сводки: что видно первым в «Рекомендуем» (первые first по r)."""
    top = sorted(public, key=lambda p: -(p.get("r") or 0))[:first]
    if not top:
        return "Порядок «Рекомендуем»: товаров нет"
    brands: dict[str, int] = {}
    for p in top:
        brands[p.get("brand") or "?"] = brands.get(p.get("brand") or "?", 0) + 1
    b, k = max(brands.items(), key=lambda x: (x[1], x[0]))
    titles = len({(p.get("brand"), p.get("title")) for p in top})
    return (f"Порядок «Рекомендуем» (ranking.py): в первых {len(top)} — {len(brands)} "
            f"{plural(len(brands), ('бренд', 'бренда', 'брендов'))}, разных названий {titles}; "
            f"больше всего {b} — {k}")


def stats_line(st: dict) -> str:
    s = f"{st['count']:>4} шт · {st['price_min']:,} – {st['price_max']:,} сум"
    if st["with_discount"]:
        s += f" · скидка до {st['discount_max']:.0f}% (в среднем {st['discount_avg']:.0f}%)"
    no_disc = st["count"] - st["with_discount"]
    if no_disc:
        s += f" · без скидки {no_disc}"
    return s


def previous_site():
    """Прошлые карточки сайта и закупочные данные по коду — снимки для распроданных товаров.
    Каталог частями (site/data/, site/admin/) или старые products.json / products-admin.js — catalog_files;
    подробности и закупка читаются только для тех кодов, к которым обратятся (распроданные)."""
    pub, adm = {}, {}
    try:
        pub = catalog_files.PublicCatalog(SITE)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
        print(f"Прошлые карточки сайта не прочитаны ({e.__class__.__name__}: {e}) — снимков распроданных не будет")
    try:
        adm = catalog_files.AdminCatalog(SITE)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Прошлые закрытые данные не прочитаны ({e.__class__.__name__}: {e})")
    return pub, adm


# ---------- отдельные задания сервера: сбор без сборки, сборка только при изменениях ----------

def content_fp(rows: list[dict]) -> str:
    """Отпечаток данных источника без полей, которые меняются каждый сбор (fetched_at): если он тот же —
    сайт из-за этого источника пересобирать незачем."""
    clean = sorted((json.dumps({k: v for k, v in r.items() if k not in VOLATILE_KEYS}, ensure_ascii=False,
                               sort_keys=True) for r in rows))
    h = hashlib.sha1()
    for line in clean:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def config_sha(path: Path) -> str | None:
    return sync_state.file_sha1(path)


def sources_changed(cfg: dict, state: dict, config_path: Path) -> list[str]:
    """Что изменилось с прошлой сборки сайта: источники (data/raw_*.json — по содержимому, без fetched_at) и
    config.json. Пусто — пересобирать сайт незачем."""
    out = []
    if (state.get("build") or {}).get("config_sha1") != config_sha(config_path):
        out.append("config.json")
    for s in cfg["sources"]:
        rec = state["sources"].get(product_source(s)) or {}
        sha = sync_state.file_sha1(raw_path(s))
        if not sha or sha == rec.get("raw_sha1"):
            continue
        if rec.get("content_fp") and rec["content_fp"] == content_fp(load_raw(s)):
            continue                                       # поменялось только время сбора
        out.append(s)
    return out


def orders_db_path() -> Path:
    p = os.environ.get("ORDERS_DB_PATH")
    return Path(p) if p else DATA / "orders" / "orders.sqlite"


def demand_ids(state: dict, source: str, days: float = HOT_DAYS, db: Path | None = None) -> set[str]:
    """id товаров источника (в магазине) из заказов и корзин Mini App за days дней (data/orders/orders.sqlite,
    только чтение). Базы нет (компьютер продавца, GitHub Actions) — пусто."""
    import sqlite3
    db = db or orders_db_path()
    if not db.is_file():
        return set()
    by_pid = {pid: e for pid, e in state["items"].items() if isinstance(e, dict)}
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    out: set[str] = set()

    def take(items) -> None:
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            if it.get("source") == source and it.get("source_item_id"):
                out.add(str(it["source_item_id"]))
                continue
            e = by_pid.get(str(it.get("id") or ""))
            if e and e.get("source") == source and e.get("item_id"):
                out.add(str(e["item_id"]))
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        print(f"[{source}] база заказов не открылась ({e}) — горячие только по витрине")
        return out
    try:
        for sql in ("SELECT items_json FROM orders WHERE created_at >= ?", "SELECT items_json FROM carts WHERE updated_at >= ?"):
            try:
                for (raw,) in con.execute(sql, (cutoff,)):
                    try:
                        take(json.loads(raw or "[]"))
                    except ValueError:
                        continue
            except sqlite3.Error:
                continue                                   # старая база без таблицы carts
    finally:
        con.close()
    return out


def hot_ids(cfg: dict, state: dict, source: str = "trendyol") -> set[str]:
    """«Горячие» товары источника: в заказах и корзинах за HOT_DAYS дней + первые pdp_hot_top товаров этого источника
    в порядке «Рекомендуем» (столбец r каталога сайта). Перепроверяются чаще всех (source_opts.trendyol.pdp_hot_hours)."""
    so = (cfg.get("source_opts") or {}).get(source) or {}
    out = demand_ids(state, source)
    n_demand = len(out)
    try:
        top = int(so.get("pdp_hot_top", 1000) or 0)
    except (TypeError, ValueError):
        top = 1000
    ranked = 0
    if top > 0:
        try:
            ranks = catalog_files.PublicCatalog(SITE).ranks()
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            ranks = {}
        items = state["items"]
        for pid in sorted(ranks, key=lambda p: -ranks[p]):
            e = items.get(pid) or {}
            if e.get("source") == source and e.get("on_site") and e.get("in_stock", True) and e.get("item_id"):
                out.add(str(e["item_id"]))
                ranked += 1
                if ranked >= top:
                    break
    print(f"[{source}] горячие товары: {len(out)} (из заказов и корзин {n_demand}, верх «Рекомендуем» {ranked})")
    return out


def collect_lock_name(source: str, extra: dict) -> str:
    return "trendyol-listing" if source == "trendyol" and extra.get("_mode") == "listing" else source


def lock_busy(name: str) -> bool:
    lk = sync_state.Lock(DATA / f"collect_{name}.lock", "проверка", max_age_s=12 * 3600)
    try:
        if lk.try_acquire():
            lk.release()
            return False
    except sync_state.LockStuck:
        pass
    return True


def source_extras(source: str, cfg: dict, state: dict, trendyol_mode: str = "auto") -> dict:
    """Служебные настройки адаптеру от run.py (ключи на «_»): режим Trendyol, горячие и «на сайте» id, свой хост фото."""
    extra: dict = {}
    if source == "trendyol":
        extra["_mode"] = trendyol_mode
        if trendyol_mode != "listing":
            extra["_warm"] = sync_state.watched(state, "trendyol")
            extra["_hot"] = hot_ids(cfg, state, "trendyol")
            if lock_busy("trendyol-listing"):
                extra["_listing_running"] = True
                if trendyol_mode == "auto":
                    extra["_mode"] = "pdp"                 # выдачу сейчас обходит ночное задание — берём сохранённую
    if source == "yoox_import" and img_map.settings(cfg)["base"]:
        extra["_own_img_host"] = True                      # фото YOOX отдаёт свой хост по просмотрам — не скачиваем
    return extra


def collect_locked(source: str, cfg: dict, prev_rows: list[dict], watch: set[str], accept_drop: bool,
                   extra: dict) -> tuple[list[dict], dict]:
    """collect() под замком источника data/collect_<источник>.lock: одно и то же не собирают два задания сразу.
    Занят — прошлые данные (status busy), сбор пропускается."""
    name = collect_lock_name(source, extra)
    lk = sync_state.Lock(DATA / f"collect_{name}.lock", f"run.py сбор {name}", max_age_s=12 * 3600)
    try:
        got = lk.try_acquire()
    except sync_state.LockStuck as e:
        print(f"[{source}] {e}")
        got = False
    if not got:
        o = lk.info()
        print(f"[{source}] сбор {name} уже идёт (pid {o.get('pid')}, с {o.get('started')}) — пропускаю, данные прошлые")
        return prev_rows, {"status": "busy", "note": f"идёт другое задание ({o.get('what') or '?'})", "gone_ids": [],
                           "absence_means_gone": False, "verified": 0, "carried": 0}
    try:
        return collect(source, cfg, prev_rows, watch, accept_drop, extra)
    finally:
        lk.release()


def collect_only(cfg: dict, sources: set[str], args) -> int:
    """Только сбор (без сборки сайта и без run.lock): data/raw_<источник>.json + .meta.json. Память сайта
    (data/state.json) читается, но не пишется — её обновит сборка run.py --offline."""
    state = sync_state.load(DATA)
    order = [s for s in cfg["sources"] if s in sources] + sorted(s for s in sources if s not in cfg["sources"])
    for s in order:
        t0 = time.time()
        extra = source_extras(s, cfg, state, args.trendyol_mode)
        _, info = collect_locked(s, cfg, load_raw(s), sync_state.watched(state, product_source(s)),
                                 args.accept_drop, extra)
        print(f"[{s}] сбор: {STATUS_RU.get(info['status'], info['status'])}"
              + (f" ({info.get('note')})" if info.get("note") else "") + f", {time.time() - t0:.0f} с", flush=True)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="код источника из sources/__init__.py; несколько — через запятую")
    ap.add_argument("--offline", action="store_true", help="не ходить на сайты, взять data/raw_*.json")
    ap.add_argument("--accept-drop", action="store_true",
                    help="принять резкое падение числа товаров у источника (распродажа правда закончилась)")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--collect-only", action="store_true",
                    help="только собрать источники (--only) в data/raw_*.json, сайт не собирать (сервер: отдельные задания)")
    ap.add_argument("--trendyol-mode", choices=("auto", "listing", "pdp"), default="auto",
                    help="Trendyol: auto — как раньше; listing — только обход выдачи; pdp — только проверка размеров")
    ap.add_argument("--if-changed", action="store_true",
                    help="с --offline: собирать сайт, только если данные источников или config.json изменились")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sc = sync_cfg(cfg)
    DATA.mkdir(exist_ok=True)
    sources = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else set(cfg["sources"])
    unknown = sources - set(ADAPTERS)
    if unknown:
        raise SystemExit(f"Неизвестные источники: {', '.join(sorted(unknown))}. Есть: {', '.join(ADAPTERS)}")
    if args.collect_only:
        if args.offline:
            raise SystemExit("--collect-only и --offline вместе не имеют смысла")
        raise SystemExit(collect_only(cfg, sources, args))

    # два run.py одновременно испортили бы state.json и файлы сайта — второй ждёт первого
    lock = sync_state.Lock(DATA / "run.lock", "run.py " + " ".join(sys.argv[1:]), max_age_s=4 * 3600)
    if not lock.acquire(wait_s=45 * 60):      # живой замок старше 4 ч — sync_state.LockStuck с подсказкой
        o = lock.info()
        raise SystemExit(f"Другой запуск run.py (pid {o.get('pid')}, {o.get('what') or '?'}, с {o.get('started')}) "
                         f"не закончился за 45 минут — выхожу. Замок живого процесса не снимаю; если он завис, "
                         f"завершите его (taskkill /PID {o.get('pid')} /F) — замок снимется сам.")
    try:
        build(cfg, sc, sources, args)
    finally:
        lock.release()


def build(cfg: dict, sc: dict, sources: set[str], args) -> None:
    state = sync_state.load(DATA)
    config_path = Path(getattr(args, "config", None) or ROOT / "config.json")
    if args.offline and getattr(args, "if_changed", False):
        what = sources_changed(cfg, state, config_path)
        if not what:
            print("Данные источников и config.json не менялись с прошлой сборки — сайт не пересобираю.")
            return
        print("Изменилось с прошлой сборки: " + ", ".join(what))
    rows: list[dict] = []
    src_info: dict[str, dict] = {}       # по коду источника в товаре (yoox, trendyol, …)
    for s in cfg["sources"]:
        psrc = product_source(s)
        if s in COMPACT_RAW and (args.offline or s not in sources):
            saved = compact_raw(s, state)
            if saved > 0:
                print(f"[{s}] data/raw_{s}.json переписан без отступов: меньше на {saved / 1e6:.1f} МБ")
        prev, sha, meta = load_raw_consistent(s)
        if s in sources and not args.offline:
            got, info = collect_locked(s, cfg, prev, sync_state.watched(state, psrc), args.accept_drop,
                                       source_extras(s, cfg, state, getattr(args, "trendyol_mode", "auto")))
            info["changed"] = info["status"] in ("ok", "partial")
            # ok/partial — collect() только что записал raw; иначе строки — прошлые (из того, что прочитали выше)
            info["raw_sha1"] = sync_state.file_sha1(raw_path(s)) if info["changed"] else sha
        else:
            got = prev
            info = {"status": "raw", "note": "", "gone_ids": [], "absence_means_gone": False, "raw_sha1": sha}
            last = (state["sources"].get(psrc) or {}).get("raw_sha1")
            # данные источника поменялись с прошлой сборки (собраны другим запуском или правлены руками)?
            info["changed"] = bool(sha and last and sha != last)
            if info["changed"]:
                if meta.get("raw_sha1") == sha:          # файл записан run.py — верим его выводам
                    info["absence_means_gone"] = bool(meta.get("absence_means_gone"))
                    info["gone_ids"] = list(meta.get("gone_ids") or [])
                else:                                    # правлен вручную — файл и есть новая выдача
                    info["absence_means_gone"] = True
        info["gone_ids"] = set(info.get("gone_ids") or [])
        info["present"] = {str(r["source_item_id"]): r for r in got}
        info["adapter"] = s
        info["content_fp"] = content_fp(got)
        src_info[psrc] = info
        rows += got

    # дубли внутри источника
    rows = list({(r["source"], r["source_item_id"]): r for r in rows}.values())

    need = {r["currency"] for r in rows} or {"EUR"}
    if cfg["pricing"].get("cargo_usd_per_kg"):
        need.add("USD")                      # карго считается в долларах за кг
    rates = load_rates(need, cfg["pricing"].get("fx_manual"))
    for r in rows:
        country = ADAPTERS[r["source"]][2]
        r.update(sell_price_uzs(r["price_now"], r["currency"], country, rates, cfg["pricing"], r.get("type")))
        r["country"] = country

    # у каждого источника свои фильтры: filters + source_filters[источник]
    eff = {s: filters_for(s, cfg) for s in {r["source"] for r in rows}}
    hide = hidden_rows(rows, cfg)
    kept = sorted((r for r in rows if (r["source"], str(r["source_item_id"])) not in hide and passes(r, eff[r["source"]])),
                  key=lambda r: (-(r.get("discount_pct") or 0), r["price_uzs"]))

    by_brand: dict[str, list] = {}
    by_source: dict[str, list] = {}
    for r in kept:
        by_brand.setdefault(r["brand"], []).append(r)
        by_source.setdefault(r["source"], []).append(r)
    seen_by_source: dict[str, int] = {}
    for r in rows:
        seen_by_source[r["source"]] = seen_by_source.get(r["source"], 0) + 1
    summary = {
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "rates": {k: round(v, 2) for k, v in rates.items()},
        "total": stats(kept),
        "by_source": {s: stats(v) for s, v in sorted(by_source.items())},
        "by_brand": {b: stats(v) for b, v in sorted(by_brand.items())},
        "filters": eff,
    }

    # Публичные файлы — только то, что видит покупатель (PUBLIC_FIELDS): без магазинов, ссылок, исходных
    # названий и закупочных цен. Всё это — в products-admin.js по нейтральному коду товара; этот файл
    # НЕ выкладывается на хостинг (нужен только для ?admin=1).
    describe.MISSING.clear()
    live, admin = [], {}
    used_imgs: set[str] = set()
    renamed = migrate_published_names()      # прежние имена своих фото -> <код>_<n>.jpg (на месте, без скачивания)
    if renamed:
        print(f"Свои фото: {renamed} файлов в site/{PUB_IMG} переименованы в <код>_<n>.jpg")
    for r in kept:
        n = 7
        code = public_id(r["source"], r["source_item_id"], n)
        while code in admin:          # совпадение 7 символов почти невозможно, но на всякий случай
            n += 1
            code = public_id(r["source"], r["source_item_id"], n)
        d = describe.describe(r)
        pub = {
            "id": code, "brand": r["brand"], "title": d["title"], "type": r.get("type"), "gender": r.get("gender"),
            "origin": r["country"], "price_uzs": r["price_uzs"], "discount_pct": r.get("discount_pct"),
            "sizes": clean_sizes(r.get("sizes")), "sizes_out": [], "size_system": d["size_system"], "color": d["color"],
            "composition": d["composition"], "details": d["details"], "description": d["description"],
            "images": publish_images(code, r.get("images") or [], used_imgs),
            "in_stock": r.get("in_stock", True), "fetched_at": _utc(r.get("fetched_at")),
        }
        live.append((pub, r))
        orig = {"title_original": r.get("title"), "category_original": r.get("category")}
        admin[code] = {k: orig[k] if k in orig else r.get(k) for k in ADMIN_FIELDS}

    # Сравнение с прошлым запуском: распроданные, закончившиеся размеры (sizes_out), цены.
    prev_pub, prev_adm = previous_site()
    sold_pub, sold_adm, changes = sync_state.update(
        state, live=live, sources=src_info, prev_public=prev_pub, prev_admin=prev_adm,
        keep_days=float(sc["keep_sold_out_days"]))
    for pub, _ in live:
        pub["first_seen"] = (state["items"].get(pub["id"]) or {}).get("first_seen")
    public = [{k: pub.get(k) for k in PUBLIC_FIELDS} for pub, _ in live]
    for pub in sold_pub:              # распроданные — в конце, с пометкой in_stock=false
        if pub.get("id") in admin:
            continue
        pub["images"] = keep_published_images(pub.get("images") or [], used_imgs)
        title, brand = str(pub.get("title") or ""), str(pub.get("brand") or "").strip()
        if brand and title != brand and title.endswith(" " + brand):    # снимок старой сборки: «Рубашка Boss»
            pub["title"] = title[: -len(brand)].strip()                  # -> «Рубашка» (бренд на карточке отдельно)
        public.append({k: pub.get(k, [] if k == "sizes_out" else None) for k in PUBLIC_FIELDS})
        if pub["id"] in sold_adm:
            admin[pub["id"]] = sold_adm[pub["id"]]
    removed = cleanup_images(used_imgs)
    # для фильтров и поиска витрины (не поля карточки): ключи размеров — по размерам после clean_sizes
    # (sizes_norm.py; показываемый список sizes не меняется — заказ проверяется по нему), основы слов для
    # поиска — из цвета, состава и пунктов карточки (describe.keywords). catalog_files пишет их в столбцы zk / kw.
    for pub in public:
        pub["zk"] = sizes_norm.filter_keys(pub.get("sizes") or [], pub.get("type"), pub.get("gender"))
        pub["kw"] = describe.keywords(pub)

    sources_report = {s: {"status": i["status"], "note": i.get("note") or "", "adapter": i["adapter"],
                          "collected_at": i.get("collected_at"), "verified": i.get("verified", 0),
                          "carried": i.get("carried", 0), "confirmed_gone": len(i["gone_ids"])}
                      for s, i in src_info.items()}
    # публичная сводка: без курсов, фильтров и разбивки по источникам
    public_summary = {"generated_at": summary["generated_at"], "total": summary["total"],
                      "by_brand": summary["by_brand"]}
    # служебное для режима ?admin=1 — курсы, сводка по источникам, действующие фильтры, свежесть источников
    admin["_meta"] = {"generated_at": summary["generated_at"], "rates": summary["rates"],
                      "by_source": summary["by_source"], "filters": eff, "sources": sources_report,
                      "sold_out_shown": len(public) - len(live)}

    # Порядок «Рекомендуем» (ranking.py): оценка товара + перемешивание брендов -> столбец r в индексе каталога
    # (больше — раньше; распроданные — в конце). По r catalog_files строит порядок витрины и «голову».
    ranker = ranking.Ranker.from_config(cfg)
    for pub, r_ in zip(public, ranker.ranks(public)):
        pub["r"] = r_
    rank_line = rank_report(public)

    # Каталог частями: site/data/ (манифест, индекс, подробности; см. catalog_files.py), закрытое — site/admin/.
    # При ≤ catalog_files.LEGACY_MAX товаров дополнительно products.js / products.json / products-admin.js
    # целиком (index.html двойным щелчком); больше — products.js только с оглавлением частей.
    # Свой хост фото (IMG_BASE в окружении или config.json → images.base): в публичных данных вместо адресов фото —
    # непрозрачные токены (img_map.py), карта токен → исходный адрес — только в data/img_map.json (не публикуется).
    SITE.mkdir(exist_ok=True)
    img = img_map.Tokenizer.from_settings(img_map.settings(cfg), DATA)
    if img:
        print(f"Фото: свой хост {img.base} — в данных сайта токены вместо адресов магазинов")
    layout = catalog_files.write_site(SITE, public_summary, cfg.get("site", {}), public, admin, img=img)

    # память о запуске: после файлов сайта, чтобы при сбое состояние не убежало вперёд сайта
    for s, i in src_info.items():
        rec = state["sources"].setdefault(s, {})
        # отпечаток именно того файла, из которого собран сайт (сбор другим заданием мог уже записать новый)
        rec.update(raw_sha1=i.get("raw_sha1") or sync_state.file_sha1(raw_path(i["adapter"])), status=i["status"],
                   note=i.get("note") or "", content_fp=i.get("content_fp"))
        if i["status"] in ("ok", "partial"):
            rec["collected_at"] = i.get("collected_at")
    state["build"] = {"config_sha1": config_sha(config_path), "at": sync_state.iso(sync_state.now_utc())}
    sync_state.save(DATA, state)
    changes_file = None
    if not changes["baseline"] and (sync_state.any_changes(changes)
                                    or any(i["status"] in ("stale", "failed", "partial") for i in src_info.values())):
        changes_file = sync_state.write_changes(DATA, changes, sources_report)

    print(f"\nВсего товаров: {len(rows)}, подошло под фильтры: {len(kept)}"
          + (f"; распроданных на сайте с пометкой: {len(public) - len(live)}" if len(public) > len(live) else ""))
    print("Курсы ЦБ:", ", ".join(f"1 {k} = {v:,.2f} сум" for k, v in summary["rates"].items()))
    print("По источникам (подошло из собранного):")
    for s in sorted(seen_by_source):
        st = summary["by_source"].get(s)
        i = src_info.get(s) or {}
        flag = "" if i.get("status") in ("ok", "raw", None) else f"  [{STATUS_RU.get(i['status'], i['status'])}]"
        print(f"  {s:<14} {len(by_source.get(s, [])):>5} из {seen_by_source[s]:<5}" + (f" · {stats_line(st)}" if st else "") + flag)
    stale = [s for s, i in src_info.items() if i["status"] in ("stale", "failed")]
    if stale:
        print("ВНИМАНИЕ, не обновились (на сайте прошлые данные): "
              + ", ".join(f"{s} ({src_info[s].get('note') or '?'})" for s in stale))
    if by_brand:
        print(f"По брендам ({len(by_brand)}):")
        top = sorted(by_brand, key=lambda b: (-len(by_brand[b]), b.lower()))
        for b in top[:TOP_BRANDS]:
            print(f"  {b[:24]:<24} {stats_line(summary['by_brand'][b])}")
        rest = top[TOP_BRANDS:]
        if rest:
            print(f"  …ещё {len(rest)} {plural(len(rest), ('бренд', 'бренда', 'брендов'))} "
                  f"({sum(len(by_brand[b]) for b in rest)} шт)")
    n_pub = len(public)
    n_cg = sum(1 for p in public if describe.color_group(p.get("color")) >= 0)
    print(f"Заголовки: разных {len({p['title'] for p in public})} на {n_pub} карточек; группа цвета у {n_cg}, "
          f"ключи размеров у {sum(1 for p in public if p.get('zk'))}, слова для поиска у "
          f"{sum(1 for p in public if p.get('kw'))} (в среднем {sum(len(p.get('kw') or []) for p in public) / max(1, n_pub):.1f})")
    print(f"Карточки: цвет переведён у {sum(1 for p in public if p['color'])} из {n_pub}, "
          f"состав — у {sum(1 for p in public if p['composition'])}, "
          f"с пунктами описания — {sum(1 for p in public if p['details'])}; "
          f"своих фото в site/{PUB_IMG}: {len(used_imgs)}" + (f" (удалено старых {removed})" if removed else ""))
    miss = describe.missing_report()
    if miss:
        print("Не переведено (добавьте в словари describe.py), самые частые:")
        print("\n".join(miss))
    print()
    print("\n".join(sync_state.summary_lines(changes, float(sc["keep_sold_out_days"]), len(public) - len(live))))
    print(f"Файлы каталога: индекс {layout['index_files']} ч. ({layout['index_bytes'] / 1e6:.1f} МБ, сжато "
          f"{layout['index_gz'] / 1e6:.1f} МБ; первая часть — {layout['head_count']} товаров), подробности "
          f"{layout['detail_files']} файлов ({layout['detail_gz'] / 1e6:.1f} МБ сжато), закрытое site/admin/ — "
          f"{layout['admin_files']} файлов" + ("; products.js/json целиком — тоже" if layout["legacy"] else
                                             "; products.js — только оглавление (каталог больше "
                                             f"{catalog_files.LEGACY_MAX:,} товаров)")
          + (f"; удалено устаревших частей: {layout['stale_removed']}" if layout["stale_removed"] else ""))
    print(rank_line)
    if changes_file:
        print(f"Подробно: {changes_file.relative_to(ROOT)}")
    try:                              # тексты продавца: site/content.json -> site/content.js
        import content
        content.build()
    except (SystemExit, Exception) as e:   # сломанный content.json не должен ронять обновление товаров
        print(f"ВНИМАНИЕ: тексты сайта не обновлены — {e}")
    print(f"\nСайт обновлён: {SITE / 'index.html'}")


if __name__ == "__main__":
    main()
