"""Логотипы брендов: site/brands.json -> site/brands.js (+ проверка).

    python brands.py            # пересобрать brands.js и показать бренды без логотипа

site/brands.json: {"Бренд как в products.json": {"logo": "img/brands/<slug>.svg|png" или null,
"source": откуда взят логотип, "license": короткая пометка, "invert_ok": можно ли инвертировать
(filter: invert) на тёмном фоне — только для одноцветных тёмных знаков}}.
logo = null — сайт рисует название бренда буквами. Файлы логотипов — site/img/brands/.
brands.js нужен для открытия index.html двойным щелчком (file://); его же пересобирает deploy.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SITE = ROOT / "site"
SRC = SITE / "brands.json"
OUT = SITE / "brands.js"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def load() -> dict:
    try:
        data = json.loads(SRC.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise SystemExit(f"Ошибка в {SRC.name}, строка {e.lineno}, позиция {e.colno}: {e.msg}")
    if not isinstance(data, dict):
        raise SystemExit(f"{SRC.name}: ожидался объект {{...}}")
    return data


def problems(data: dict) -> list[str]:
    out = []
    for brand, v in data.items():
        logo = (v or {}).get("logo") if isinstance(v, dict) else None
        if logo and not (SITE / logo).is_file():
            out.append(f"{brand}: нет файла site/{logo}")
    try:
        products = json.loads((SITE / "products.json").read_text(encoding="utf-8")).get("products", [])
        missing = sorted({p.get("brand") for p in products if p.get("brand")} - set(data), key=str.lower)
        if missing:
            out.append("бренды из products.json, которых нет в brands.json (будут буквами): " + ", ".join(missing))
    except (OSError, ValueError):
        pass
    return out


def build(quiet: bool = False) -> Path | None:
    data = load()
    if not data:
        return None
    OUT.write_text("window.BRANDS = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n", encoding="utf-8")
    if not quiet:
        with_logo = sum(1 for v in data.values() if isinstance(v, dict) and v.get("logo"))
        print(f"Логотипы брендов: {OUT.relative_to(ROOT)} обновлён ({with_logo} из {len(data)} с логотипом)")
        for p in problems(data):
            print("  ! " + p)
    return OUT


if __name__ == "__main__":
    build()
