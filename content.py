"""Тексты сайта: site/content.json (правит продавец) -> site/content.js.

    python content.py

content.js нужен, чтобы тексты были видны и при открытии site/index.html двойным щелчком (без сервера).
На хостинге страница дополнительно перечитывает content.json, поэтому правка видна и без этого шага.
Скрипт вызывают run.py (в конце каждого запуска) и deploy.py (перед публикацией).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "site" / "content.json"
OUT = ROOT / "site" / "content.js"
TEXT_FIELDS = ("name", "tagline", "announcement", "hero_title", "hero_text", "about", "authenticity",
               "delivery", "payment", "returns")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def load(path: Path = SRC) -> dict:
    """Читает content.json; понятная ошибка с номером строки, если JSON сломан."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(f"Нет файла {path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"Ошибка в {path.name}, строка {e.lineno}, позиция {e.colno}: {e.msg}. "
                         "Частые причины: забыта запятая между полями, лишняя запятая перед }, "
                         "кавычка \" внутри текста (пишите «ёлочки»).")
    if not isinstance(data, dict):
        raise SystemExit(f"{path.name}: ожидался объект {{...}}")
    return data


def warnings(data: dict) -> list[str]:
    out = []
    for k in TEXT_FIELDS:
        if k in data and not isinstance(data[k], str):
            out.append(f"поле {k} должно быть текстом в кавычках")
    if not isinstance(data.get("contacts", {}), dict):
        out.append("contacts должен быть объектом {...}")
    for i, f in enumerate(data.get("faq") or []):
        if not (isinstance(f, dict) and f.get("q") and f.get("a")):
            out.append(f"faq[{i + 1}]: нужны q (вопрос) и a (ответ)")
    for i, s in enumerate(data.get("order_steps") or []):
        if not (isinstance(s, dict) and s.get("title")):
            out.append(f"order_steps[{i + 1}]: нужен title")
    c = data.get("contacts") if isinstance(data.get("contacts"), dict) else {}
    if not any(str(c.get(k) or "").strip() for k in ("telegram", "instagram", "phone")):
        out.append("контакты пустые: кнопка «Заказать» ведёт на site.contact_url из config.json")
    ep = str(data.get("order_endpoint") or "").strip()
    if ep and not ep.startswith("https://"):
        out.append("order_endpoint должен начинаться с https:// (сайт на GitHub Pages открыт по HTTPS)")
    if "orders_telegram_username" in data and not isinstance(data["orders_telegram_username"], str):
        out.append("orders_telegram_username должен быть текстом в кавычках")
    if data.get("_draft"):
        out.append("\"_draft\": true — тексты помечены как черновик (напоминание видно в ?admin=1)")
    return out


def build(quiet: bool = False) -> Path:
    data = load()
    OUT.write_text("window.SITE_CONTENT = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n",
                   encoding="utf-8")
    if not quiet:
        print(f"Тексты сайта: {OUT.relative_to(ROOT)} обновлён из {SRC.name}")
        for w in warnings(data):
            print("  ! " + w)
    return OUT


if __name__ == "__main__":
    build()
