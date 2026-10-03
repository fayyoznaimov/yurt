"""Собрать товары со всех источников, посчитать цену в сумах и обновить сайт.

    python run.py                 # все источники из config.json
    python run.py --only trendyol # один источник
    python run.py --offline       # не ходить на сайты, пересчитать из data/raw_*.json
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from pricing import load_rates, sell_price_uzs
from sources import ADAPTERS
from sources.base import Product, Query

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "site"
CODE_PREFIX = {"pcardin_tr": "PC", "cacharel_tr": "CC", "trendyol": "TY", "yoox": "YX"}
# адаптер -> код источника, который стоит в товарах (product.source) и в source_filters
PRODUCT_SOURCE = {"yoox_import": "yoox"}
TOP_BRANDS = 25          # сколько брендов печатать в сводке, остальные — одной строкой

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def product_source(adapter: str) -> str:
    return PRODUCT_SOURCE.get(adapter, adapter)


def _clean(d: dict | None) -> dict:
    """Ключи на «_» в config.json — пояснения для человека, не настройки."""
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def filters_for(source: str, cfg: dict) -> dict:
    """Фильтры для товаров источника: filters, поверх них source_filters[source]."""
    f = _clean(cfg.get("filters"))
    f.update(_clean((cfg.get("source_filters") or {}).get(source)))
    return f


def collect(source: str, cfg: dict) -> list[dict]:
    """Запускает один адаптер. Ошибка одного источника не роняет остальные."""
    module_name, opts, _country = ADAPTERS[source]
    f = filters_for(product_source(source), cfg)
    so = cfg.get("source_opts", {}).get(source, {})
    query = Query(
        brands=f.get("brands", []), types=f.get("types", []), genders=f.get("genders", []),
        discount_min=f.get("discount_min", 0),
        max_pages=so.get("max_pages") or cfg.get("crawl", {}).get("max_pages", 3),
        source_opts=so,
    )
    t0 = time.time()
    try:
        module = importlib.import_module(module_name)
        items: list[Product] = module.fetch(query, **opts)
        rows = [p.to_dict() for p in items]
        (DATA / f"raw_{source}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        with_disc = sum(1 for r in rows if r.get("discount_pct"))
        print(f"[{source}] собрано {len(rows)} товаров (со скидкой {with_disc}) за {time.time() - t0:.0f} с")
        return rows
    except Exception as e:
        print(f"[{source}] ОШИБКА, источник пропущен: {e}")
        traceback.print_exc(limit=3)
        return []


def load_raw(source: str) -> list[dict]:
    p = DATA / f"raw_{source}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


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


def stats(items: list[dict]) -> dict | None:
    """Сводка: число товаров, цены в сумах, скидки (средняя — только по товарам со скидкой)."""
    if not items:
        return None
    prices = [i["price_uzs"] for i in items]
    disc = [i["discount_pct"] for i in items if i.get("discount_pct")]
    return {"count": len(items), "price_min": min(prices), "price_max": max(prices),
            "with_discount": len(disc), "discount_max": max(disc, default=0),
            "discount_avg": round(sum(disc) / len(disc), 1) if disc else 0}


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """plural(3, ("бренд", "бренда", "брендов")) -> "бренда"."""
    a, b = n % 100, n % 10
    return forms[2] if 10 < a < 20 else forms[0] if b == 1 else forms[1] if 1 < b < 5 else forms[2]


def stats_line(st: dict) -> str:
    s = f"{st['count']:>4} шт · {st['price_min']:,} – {st['price_max']:,} сум"
    if st["with_discount"]:
        s += f" · скидка до {st['discount_max']:.0f}% (в среднем {st['discount_avg']:.0f}%)"
    no_disc = st["count"] - st["with_discount"]
    if no_disc:
        s += f" · без скидки {no_disc}"
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="код источника из sources/__init__.py")
    ap.add_argument("--offline", action="store_true", help="не ходить на сайты, взять data/raw_*.json")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    DATA.mkdir(exist_ok=True)
    sources = [args.only] if args.only else cfg["sources"]

    rows: list[dict] = []
    for s in cfg["sources"]:
        if s in sources and not args.offline:
            rows += collect(s, cfg)
        else:
            rows += load_raw(s)

    # дубли внутри источника
    rows = list({(r["source"], r["source_item_id"]): r for r in rows}.values())

    rates = load_rates({r["currency"] for r in rows} or {"EUR"}, cfg["pricing"].get("fx_manual"))
    for r in rows:
        country = ADAPTERS[r["source"]][2]
        r.update(sell_price_uzs(r["price_now"], r["currency"], country, rates, cfg["pricing"]))
        r["country"] = country

    # у каждого источника свои фильтры: filters + source_filters[источник]
    eff = {s: filters_for(s, cfg) for s in {r["source"] for r in rows}}
    kept = sorted((r for r in rows if passes(r, eff[r["source"]])),
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

    # Публичные файлы — без закупочных данных: ссылки на источник, себестоимость и маржа
    # уходят в products-admin.js, который НЕ выкладывается на хостинг (нужен только для ?admin=1).
    private = ("url", "cost_uzs", "margin_uzs")
    public, admin = [], {}
    for r in kept:
        r["code"] = f"{CODE_PREFIX.get(r['source'], r['source'][:2].upper())}-{r['source_item_id']}"
        admin[f"{r['source']}:{r['source_item_id']}"] = {k: r[k] for k in private}
        public.append({k: v for k, v in r.items() if k not in private})
    public_summary = {k: v for k, v in summary.items() if k != "filters"}

    payload = {"summary": public_summary, "site": cfg.get("site", {}), "products": public}
    SITE.mkdir(exist_ok=True)
    (SITE / "products.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # products.js — чтобы сайт открывался и двойным щелчком по index.html (без сервера)
    (SITE / "products.js").write_text("window.DEALS = " + json.dumps(payload, ensure_ascii=False) + ";", encoding="utf-8")
    (SITE / "products-admin.js").write_text("window.DEALS_ADMIN = " + json.dumps(admin, ensure_ascii=False) + ";", encoding="utf-8")

    print(f"\nВсего товаров: {len(rows)}, подошло под фильтры: {len(kept)}")
    print("Курсы ЦБ:", ", ".join(f"1 {k} = {v:,.2f} сум" for k, v in summary["rates"].items()))
    print("По источникам (подошло из собранного):")
    for s in sorted(seen_by_source):
        st = summary["by_source"].get(s)
        print(f"  {s:<14} {len(by_source.get(s, [])):>5} из {seen_by_source[s]:<5}" + (f" · {stats_line(st)}" if st else ""))
    if by_brand:
        print(f"По брендам ({len(by_brand)}):")
        top = sorted(by_brand, key=lambda b: (-len(by_brand[b]), b.lower()))
        for b in top[:TOP_BRANDS]:
            print(f"  {b[:24]:<24} {stats_line(summary['by_brand'][b])}")
        rest = top[TOP_BRANDS:]
        if rest:
            print(f"  …ещё {len(rest)} {plural(len(rest), ('бренд', 'бренда', 'брендов'))} "
                  f"({sum(len(by_brand[b]) for b in rest)} шт)")
    print(f"\nСайт обновлён: {SITE / 'index.html'}")


if __name__ == "__main__":
    main()
