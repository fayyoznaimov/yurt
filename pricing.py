"""Курсы ЦБ Узбекистана и расчёт цены продажи в сумах."""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import requests

CBU_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/{ccy}/"
FX_CACHE = Path(__file__).parent / "data" / "fx.json"


def load_rates(currencies: set[str], manual: dict | None = None) -> dict[str, float]:
    """Курс UZS за 1 единицу валюты. Кэшируется на день в data/fx.json."""
    manual = {k.upper(): float(v) for k, v in (manual or {}).items()}
    cache = {}
    if FX_CACHE.exists():
        cache = json.loads(FX_CACHE.read_text(encoding="utf-8"))
    today = date.today().isoformat()
    rates = {}
    for ccy in sorted(c.upper() for c in currencies):
        if ccy in manual:
            rates[ccy] = manual[ccy]
            continue
        hit = cache.get(ccy)
        if hit and hit.get("fetched") == today:
            rates[ccy] = hit["rate"]
            continue
        try:
            r = requests.get(CBU_URL.format(ccy=ccy), timeout=20)
            r.raise_for_status()
            row = r.json()[0]
            rate = float(row["Rate"]) / float(row.get("Nominal") or 1)
            cache[ccy] = {"rate": rate, "cbu_date": row.get("Date"), "fetched": today}
            rates[ccy] = rate
        except Exception as e:  # сеть упала — берём вчерашний курс, если есть
            if hit:
                rates[ccy] = hit["rate"]
                print(f"[fx] {ccy}: cbu.uz недоступен ({e}), беру курс от {hit.get('cbu_date')}")
            else:
                raise RuntimeError(f"Нет курса {ccy}: cbu.uz недоступен ({e}). Укажите pricing.fx_manual в config.json")
    FX_CACHE.parent.mkdir(exist_ok=True)
    FX_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return rates


def cargo_uzs(type_: str | None, country: str, rates: dict, cfg: dict) -> float:
    """Карго до Ташкента. cargo_usd_per_kg × вес вещи по типу (weight_kg); без них — старый
    фиксированный cargo_uzs_per_item по стране."""
    per_kg = cfg.get("cargo_usd_per_kg")
    if per_kg:
        weights = cfg.get("weight_kg") or {}
        kg = float(weights.get(type_ or "", weights.get("_default", 1.0)))
        return float(per_kg) * kg * rates["USD"]
    return float(cfg.get("cargo_uzs_per_item", {}).get(country, 0))


def margin_pct(cost: float, cfg: dict) -> float:
    """Маржа по ступеням себестоимости: margin_tiers = [{"max_cost_uzs": N, "pct": P}, ..., {"pct": P}]."""
    for tier in cfg.get("margin_tiers") or []:
        if "max_cost_uzs" not in tier or cost <= tier["max_cost_uzs"]:
            return float(tier["pct"])
    return float(cfg.get("margin_pct", 0))


def sell_price_uzs(price: float, currency: str, country: str, rates: dict, cfg: dict,
                   type_: str | None = None) -> dict:
    """Цена продажи.

    себестоимость = цена × курс × (1 + наценка_на_курс) + карго (по весу типа вещи)
    цена = max(себестоимость × (1 + маржа ступени), себестоимость + мин_маржа), округлённая вверх
    """
    fx = rates[currency.upper()] * (1 + cfg.get("fx_markup_pct", 0) / 100)
    goods = price * fx
    cost = goods + cargo_uzs(type_, country, rates, cfg)
    with_margin = max(cost * (1 + margin_pct(cost, cfg) / 100), cost + cfg.get("min_margin_uzs", 0))
    step = cfg.get("round_to_uzs", 1000) or 1
    final = math.ceil(with_margin / step) * step
    return {"price_uzs": int(final), "cost_uzs": int(round(cost)), "margin_uzs": int(round(final - cost))}
