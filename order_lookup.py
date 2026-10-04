"""Разбор заказа, пришедшего обычным сообщением в Telegram (запасной путь, когда order_api не настроен).

    python order_lookup.py "Здравствуйте, хочу XZXUTRY размер 2XL и 2fujjww"
    python order_lookup.py < message.txt          # текст из файла / буфера обмена

Находит в тексте коды товаров (7 знаков: латиница и цифры, регистр не важен) и для каждого печатает:
бренд, название, нашу цену, магазин-источник, ссылку, цену в магазине, себестоимость/маржу и текущие
размеры. Данные — каталог сайта (site/data/ или site/products.json) и закрытые site/admin/ или
site/products-admin.js (как у order_api.py, через catalog_files.py).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from order_api import Catalog, money, shop_name, src_price

ROOT = Path(__file__).resolve().parent
CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{7})(?![A-Za-z0-9])")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def extract_codes(text: str, known: set[str] | None = None) -> tuple[list[str], list[str]]:
    """(коды из каталога, похожие на код, но не найденные) — по порядку появления, без повторов.
    «Похожие» — 7 знаков, где есть и буква, и цифра (слова вроде «привет» не считаются)."""
    found, unknown, seen = [], [], set()
    for m in CODE_RE.finditer(text or ""):
        code = m.group(1).upper()
        if code in seen:
            continue
        seen.add(code)
        if known is not None and code in known:
            found.append(code)
        elif known is None:
            found.append(code)
        elif re.search(r"\d", code) and re.search(r"[A-Z]", code):
            unknown.append(code)
    return found, unknown


def describe(code: str, prod: dict, adm: dict) -> str:
    lines = [f"[{code}] {prod.get('brand', '')} — {prod.get('title', '')}",
             f"  наша цена:   {money(prod.get('price_uzs'))} сум"]
    if adm:
        price = f"{src_price(adm.get('price_now'))} {adm.get('currency', '')}"
        if adm.get("price_old") and adm.get("price_old") != adm.get("price_now"):
            price += f" (было {src_price(adm.get('price_old'))})"
        lines += [f"  магазин:     {shop_name(adm)}",
                  f"  ссылка:      {adm.get('url', '')}",
                  f"  цена там:    {price}"]
        if adm.get("cost_uzs") is not None:
            lines.append(f"  себест./маржа: {money(adm.get('cost_uzs'))} / {money(adm.get('margin_uzs'))} сум")
    else:
        lines.append("  закрытых данных нет (products-admin.js без этого кода)")
    sizes = ", ".join(str(s) for s in prod.get("sizes") or []) or "—"
    stock = "в наличии" if prod.get("in_stock", True) else "НЕТ В НАЛИЧИИ"
    lines.append(f"  размеры:     {sizes}  ({stock})")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Коды товаров из сообщения покупателя → закупочные данные")
    ap.add_argument("text", nargs="*", help="текст сообщения (или подайте его на вход)")
    args = ap.parse_args()
    text = " ".join(args.text) if args.text else sys.stdin.read()
    cat = Catalog(ROOT / "site")
    cat.refresh()
    if not cat.products:
        print("Каталога нет (ни site/data/manifest.json, ни site/products.json) — сначала python run.py")
        return 1
    codes, unknown = extract_codes(text, set(cat.products))
    if not codes:
        print("Кодов товаров в тексте не нашёл." + (f" Похоже на код, но нет в каталоге: {', '.join(unknown)}" if unknown else ""))
        return 1
    total = 0
    for c in codes:
        prod, adm = cat.get(c)
        print(describe(c, prod, adm))
        print()
        total += int(prod.get("price_uzs") or 0)
    if len(codes) > 1:
        print(f"Всего кодов: {len(codes)}, сумма по нашим ценам (по 1 шт.): {money(total)} сум")
    if unknown:
        print(f"Похоже на код, но нет в каталоге (снят с сайта?): {', '.join(unknown)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
