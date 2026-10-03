"""YOOX через кнопку-закладку «Сохранить YOOX» (файл yoox-knopka.html).

Автоматический браузер YOOX блокирует, а у человека в обычном Chrome сайт работает.
Поэтому страницы открываете вы, а закладка сохраняет встроенные в страницу данные
каталога (__NEXT_DATA__) в файл yoox_ГГГГММДД_ччммсс.json в папку «Загрузки».
Этот адаптер читает такие файлы и разбирает товары тем же кодом, что и sources/yoox.py.

Формат файла: {"saved_at", "page_url", "dept", "searches": [...], "pages": [{"url", "dept", "search_name", "results"}]}.
Раздел (donna / uomo) берётся у каждой страницы: pages[i].dept, иначе из pages[i].url,
иначе общий dept / page_url файла. Один товар в нескольких файлах — побеждает самый новый файл.

Настройки (config.json → source_opts.yoox_import):
  folder          папка с файлами; пусто — «Загрузки» текущего пользователя
  max_age_days    3 — файлы старше не берём: цены и наличие на YOOX быстро меняются
  download_images true — скачать главное фото в site/img/yoox/, чтобы сайт меньше зависел от YOOX
  max_images      200 — скольким товарам за запуск обеспечить своё главное фото (по скидке, потом
                  по цене); у остальных фото остаются ссылками на yoox.com
  known_brands    как писать бренды на сайте: «EA7», а не «Ea7»

Распродано ли (для run.py / sync_state.py). YOOX нельзя перепроверить по ссылке, поэтому товар считается
пропавшим, только если его нет в САМОМ НОВОМ файле, поиск которого точно охватывал этот товар:
страница бренда (…/boss_d) того же раздела (uomo/donna), цена товара в диапазоне price= поиска и либо
поиск пролистан до конца, либо (сортировка «сначала дешёвые») цена товара ниже самой высокой цены на
последней собранной странице. Поиски по категориям (футболки 50–70 € и т. п.) ничего не доказывают.
Нет более нового охватывающего файла — товар не пропал. Итог — в LAST_RUN["gone_ids"].
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import requests

from .base import BROWSER_UA, Product, Query
from .yoox import IMG_DIR, IMG_REL, main_result, parse_hit

DEPT_GENDER = {"donna": "women", "uomo": "men"}
GENDER_LABEL = {"women": "женское", "men": "мужское", None: "без раздела"}
MAX_FAILS = 5            # столько ошибок подряд при скачивании фото — дальше не качаем
MIN_IMG_BYTES = 1500     # меньше — заглушка YOOX (прозрачный PNG 1×1 вместо фото)
# Итог последнего fetch() для run.py: отсутствие товара в файлах само по себе ничего не значит
# (absence_means_gone = False), пропавшие — только gone_ids (см. _covering_search).
LAST_RUN: dict = {}


def _dept_of(url) -> str | None:
    url = unquote(str(url or ""))
    m = re.search(r"/(donna|uomo)(?:[/?#]|$)", url)
    if m:
        return m.group(1)
    m = re.search(r"(?:dept\[0\]|area)=(women|men)\b", url)
    return {"women": "donna", "men": "uomo"}.get(m.group(1)) if m else None


def _gender(page: dict, payload: dict) -> str | None:
    """Раздел страницы: pages[i].dept → pages[i].url → dept файла → page_url файла."""
    dept = (page.get("dept") or _dept_of(page.get("url"))
            or payload.get("dept") or _dept_of(payload.get("page_url")))
    return DEPT_GENDER.get(str(dept or "").lower())


def _bkey(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").lower())


def _epoch(ts) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _search_scope(url) -> dict | None:
    """Что доказывает поиск: только страница бренда (…/boss_d, …/karl lagerfeld_md) без фильтра брендов.
    {"lo", "hi", "asc"} — диапазон цены из price=lo:hi и сортировка «сначала дешёвые»."""
    try:
        u = urlsplit(unquote(str(url or "")))
    except ValueError:
        return None
    if not re.search(r"_m?d/?$", u.path) or re.search(r"brand(\[|%5B)", u.query, re.I):
        return None
    q = parse_qs(u.query)
    lo, hi = 0.0, float("inf")
    m = re.match(r"^\s*([\d.]*)\s*:\s*([\d.]*)\s*$", (q.get("price") or [""])[0])
    if m:
        lo = float(m.group(1)) if m.group(1) else 0.0
        hi = float(m.group(2)) if m.group(2) else float("inf")
    return {"lo": lo, "hi": hi, "asc": (q.get("sortBy") or [""])[0].lower() == "priceasc"}


def _covering_search(fc: dict, brand_key: str, gender: str | None, price: float | None) -> str | None:
    """Имя поиска файла fc, который точно должен был показать товар (бренд, раздел, цена), иначе None."""
    for name, sc in fc["searches"].items():
        if not sc["scope"] or sc["gender"] != gender or brand_key not in sc["brands"] or not sc["pages"]:
            continue
        if price is None or not (sc["scope"]["lo"] <= price <= sc["scope"]["hi"]):
            continue
        pages = sorted(sc["pages"])
        contiguous = pages == list(range(len(pages)))      # страницы подряд с первой
        complete = contiguous and sc["nb_pages"] is not None and len(pages) >= sc["nb_pages"]
        window = contiguous and sc["scope"]["asc"] and sc["price_max"] is not None and price < sc["price_max"]
        if complete or window:
            return name
    return None


def _download_images(products: list[Product], limit: int) -> None:
    """Главное фото товара -> site/img/yoox/<id>_1.jpg, не больше `limit` новых скачиваний за запуск.
    Уже скачанные файлы используются и лимит не тратят; остальные фото — ссылки на yoox.com."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA, "Referer": "https://www.yoox.com/"})
    got = cached = failed = fails_in_row = attempts = 0
    stop = None
    for i, p in enumerate(products):
        local = []
        for n, url in enumerate(p.images, 1):
            name = f"{p.source_item_id}_{n}.jpg"
            path = IMG_DIR / name
            if path.exists() and path.stat().st_size >= MIN_IMG_BYTES:
                local.append(f"{IMG_REL}/{name}")
                cached += 1
                continue
            if n > 1 or attempts >= limit or stop:
                local.append(url)
                continue
            attempts += 1
            try:
                r = s.get(url, timeout=20)
                if r.status_code in (403, 429):
                    stop = f"HTTP {r.status_code}"
                if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
                    raise RuntimeError(f"HTTP {r.status_code}")
                if len(r.content) < MIN_IMG_BYTES:
                    raise RuntimeError("заглушка вместо фото")
                path.write_bytes(r.content)
                local.append(f"{IMG_REL}/{name}")
                got += 1
                fails_in_row = 0
            except Exception as e:
                failed += 1
                fails_in_row += 1
                local.append(url)          # не скачалось — оставляем ссылку на YOOX
                if fails_in_row >= MAX_FAILS:
                    stop = stop or f"{MAX_FAILS} ошибок подряд ({e})"
            time.sleep(0.3)
        p.images = local
    remote = sum(1 for p in products if p.images and p.images[0].startswith("http"))
    print(f"[yoox_import] фото: скачано {got}, уже были {cached}, не скачалось {failed}"
          + (f"; скачивание остановлено: {stop}" if stop else "")
          + (f"; главное фото по ссылке на yoox.com: {remote} шт." if remote else ""))


def fetch(query: Query, **opts) -> list[Product]:
    LAST_RUN.clear()
    # число товаров зависит от того, какие поиски нажаты, — резкое падение не значит сбой
    LAST_RUN.update(absence_means_gone=False, no_drop_check=True)
    so = query.source_opts
    # YURT_YOOX_FOLDER — папка-«входящие» на сервере (server/setup.sh), важнее config.json
    folder = Path(os.environ.get("YURT_YOOX_FOLDER") or so.get("folder") or Path.home() / "Downloads").expanduser()
    max_age = float(so.get("max_age_days", 3))
    files = sorted(folder.glob("yoox_*.json"), key=lambda f: f.stat().st_mtime)
    if not files:
        print(f"[yoox_import] в папке {folder} нет файлов yoox_*.json — откройте распродажу на YOOX "
              f"и нажмите закладку «Сохранить YOOX» (инструкция: yoox-knopka.html)")
        LAST_RUN["no_new_data"] = True       # run.py оставит прошлые товары YOOX как есть
        return []
    cutoff = time.time() - max_age * 86400
    fresh = [f for f in files if f.stat().st_mtime >= cutoff]
    if len(fresh) < len(files):
        print(f"[yoox_import] пропущено старых файлов (> {max_age:g} дн.): {len(files) - len(fresh)}")
    if not fresh:
        print(f"[yoox_import] свежих файлов (не старше {max_age:g} дн.) нет — нажмите закладку «Сохранить YOOX»")
        LAST_RUN["no_new_data"] = True
        return []

    # как писать бренд на сайте: «EA7», «Dolce&Gabbana» — из filters.brands и known_brands
    canon = {_bkey(b): b for b in list(query.brands) + list(so.get("known_brands") or [])}
    wanted = {_bkey(b) for b in query.brands}
    any_brand = replace(query, brands=[])          # бренд проверяем сами, после исправления написания

    by_id: dict[str, Product] = {}      # более новый файл перезаписывает старый
    src_file: dict[str, int] = {}       # из какого файла (номер в file_cov) товар в by_id
    file_cov: list[dict] = []           # по файлам: время, все id в файле, охват поисков
    gone: dict[str, str] = {}           # id -> почему считаем пропавшим
    for f in fresh:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[yoox_import] {f.name}: не читается ({e}), пропускаю")
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
            print(f"[yoox_import] {f.name}: не похоже на файл кнопки «Сохранить YOOX», пропускаю")
            continue
        saved_at = payload.get("saved_at")
        fc = {"name": f.name, "t": _epoch(saved_at) or f.stat().st_mtime, "ids": set(), "searches": {}}
        latest: dict[str, Product | None] = {}     # что сказал про товар этот файл
        n_pages = n_hits = 0
        per_gender: dict[str | None, int] = {}
        per_search: dict[str, list[int]] = {}      # поиск кнопки -> [товаров, подошло]
        for page in payload.get("pages") or []:
            if not isinstance(page, dict):
                continue
            gender = _gender(page, payload)
            if query.genders and gender not in query.genders:
                continue
            res = main_result(page)
            n_pages += 1
            sname = str(page.get("search_name") or "")
            ps = per_search.setdefault(sname, [0, 0])
            sc = fc["searches"].setdefault(sname, {"scope": _search_scope(page.get("url")), "gender": gender,
                                                   "brands": set(), "pages": set(), "nb_pages": None,
                                                   "price_max": None})
            if res and isinstance(res.get("page"), int):
                sc["pages"].add(res["page"])
                if isinstance(res.get("nbPages"), int):
                    sc["nb_pages"] = res["nbPages"]
            for h in (res or {}).get("hits") or []:
                n_hits += 1
                ps[0] += 1
                vid = str(h.get("variantId") or h.get("objectID") or "").strip()
                if vid:
                    fc["ids"].add(vid)
                try:
                    p = parse_hit(h, gender, any_brand, {})
                except Exception:
                    p = None
                if p:
                    raw_brand = p.brand
                    p.brand = canon.get(_bkey(raw_brand), raw_brand)
                    sc["brands"].add(_bkey(p.brand))
                    sc["price_max"] = max(sc["price_max"] or 0.0, p.price_now)
                    if wanted and _bkey(p.brand) not in wanted:
                        p = None
                if not p:
                    latest.setdefault(vid, None)       # продан / б/у / не подходит — старую запись убираем
                    continue
                if saved_at:
                    p.fetched_at = saved_at
                # «Giorgio Armani Pullover, bordeaux» -> «Pullover, bordeaux»: бренд и так над названием
                short = p.title[len(raw_brand):].strip(" ,") if p.title.startswith(raw_brand) else p.title
                p.title = short[:1].upper() + short[1:] if short else p.title
                ps[1] += 1
                if not latest.get(p.source_item_id):
                    per_gender[gender] = per_gender.get(gender, 0) + 1
                latest[p.source_item_id] = p
        fi = len(file_cov)
        file_cov.append(fc)
        for vid, p in latest.items():
            if p:
                by_id[vid] = p
                src_file[vid] = fi
                gone.pop(vid, None)
            elif vid and by_id.pop(vid, None) is not None:
                gone[vid] = f"в более новом файле {f.name} товар не продаётся"
        n_kept = sum(1 for p in latest.values() if p)
        when = datetime.fromtimestamp(f.stat().st_mtime).strftime("%d.%m %H:%M")
        depts = ", ".join(f"{GENDER_LABEL.get(g, g)} {n}" for g, n in per_gender.items())
        print(f"[yoox_import] {f.name} ({when}): страниц {n_pages}, товаров {n_hits}, подошло {n_kept}"
              + (f" ({depts})" if depts else ""))
        for name, (nh, nk) in per_search.items():
            if name:
                print(f"[yoox_import]    · {name}: товаров {nh}, подошло {nk}")

    # Пропал ли товар: самый новый файл (новее того, где товар видели), поиск которого его охватывал, — без него.
    def check(vid: str, brand: str, gender: str | None, price, after_t: float | None, after_idx: int) -> None:
        for k in range(len(file_cov) - 1, after_idx, -1):
            fc = file_cov[k]
            if after_t is not None and fc["t"] <= after_t:
                break
            name = _covering_search(fc, _bkey(brand), gender, price)
            if name:
                if vid not in fc["ids"]:
                    gone[vid] = f"нет в файле {fc['name']}, поиск «{name}»"
                return

    for vid, p in list(by_id.items()):
        check(vid, p.brand, p.gender, p.price_now, None, src_file.get(vid, -1))
        if vid in gone:
            del by_id[vid]
    # товары с нашего сайта (run.py передаёт их в source_opts["_watch"]), которых нет ни в одном свежем файле
    for vid, w in (so.get("_watch") or {}).items():
        if vid in by_id or vid in gone or not isinstance(w, dict):
            continue
        try:
            price = float(w.get("price_now"))
        except (TypeError, ValueError):
            price = None
        check(vid, str(w.get("brand") or ""), w.get("gender"), price, _epoch(w.get("fetched_at")), -1)
    LAST_RUN.update(absence_means_gone=False, gone_ids=sorted(gone), gone_reasons=dict(gone))
    if gone:
        print(f"[yoox_import] пропали из выдачи YOOX (проданы или подорожали) по более новым файлам: {len(gone)}")

    products = sorted(by_id.values(), key=lambda p: (-(p.discount_pct or 0), p.price_now))
    totals: dict[str | None, int] = {}
    for p in products:
        totals[p.gender] = totals.get(p.gender, 0) + 1
    no_disc = sum(1 for p in products if not p.discount_pct)
    parts = [f"{GENDER_LABEL.get(g, g)} {n}" for g, n in totals.items()] + ([f"без скидки {no_disc}"] if no_disc else [])
    print(f"[yoox_import] итого без повторов: {len(products)}" + (f" ({', '.join(parts)})" if parts else ""))
    if products and so.get("download_images", True):
        _download_images(products, int(so.get("max_images", 200)))
    return products
