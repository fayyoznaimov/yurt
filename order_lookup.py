"""Разбор заказа, пришедшего обычным сообщением в Telegram (запасной путь, когда order_api не настроен).

    python order_lookup.py "Здравствуйте, хочу XZXUTRY размер 2XL и 2fujjww"
    python order_lookup.py < message.txt          # текст из файла / буфера обмена
    python order_lookup.py YR-261005-ABCD         # заказ из базы (data/orders/orders.sqlite): статус, история

Находит в тексте коды товаров (7 знаков: латиница и цифры, регистр не важен) и для каждого печатает:
бренд, название, нашу цену, магазин-источник, ссылку, цену в магазине, себестоимость/маржу и текущие
размеры. Данные — каталог сайта (site/data/ или site/products.json) и закрытые site/admin/ или
site/products-admin.js (как у order_api.py, через catalog_files.py).
Номера заказов (YR-…) в тексте ищутся в базе заказов (orders_db.py): покупатель, статус, история.
Вывод — только в терминал продавца (телефоны в журналы не пишутся).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import orders_db
from order_api import Catalog, money, shop_name, src_price

ROOT = Path(__file__).resolve().parent
CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{7})(?![A-Za-z0-9])")
ORDER_NO_RE = re.compile(r"(?<![A-Za-z0-9])(YR-\d{6}-[A-HJ-NP-Z2-9]{4})(?![A-Za-z0-9])", re.I)

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


def extract_order_numbers(text: str) -> list[str]:
    """Номера заказов YR-ГГММДД-XXXX в тексте (регистр не важен), по порядку, без повторов."""
    out: list[str] = []
    for m in ORDER_NO_RE.finditer(text or ""):
        no = m.group(1).upper()
        if no not in out:
            out.append(no)
    return out


def describe_order(o: dict) -> str:
    """Заказ из базы (orders_db.get_order(..., with_events=True)) — для терминала продавца."""
    st = o.get("status") or ""
    lines = [f"[{o['order_no']}] {o.get('created_at', '')} · статус: {orders_db.STATUS_LABELS.get(st, st)}"
             f" · источник заказа: {o.get('source') or 'site'}",
             f"  покупатель:  {o.get('customer_name') or '—'} · {o.get('customer_phone') or 'без телефона'}"
             + (f" · @{o['customer_telegram']}" if o.get("customer_telegram") else "")
             + (f" · {o['customer_city']}" if o.get("customer_city") else ""),
             f"  заказов у покупателя: {o.get('customer_orders_count') or 0}"
             + (" · бот подключён" if o.get("customer_telegram_user_id") else " · бот не подключён")]
    for it in o.get("items") or []:
        if isinstance(it, dict):
            lines.append(f"  - {it.get('id', '')} {it.get('brand', '')} — {it.get('title', '')}, размер "
                         f"{it.get('size') or '—'} ×{it.get('qty', 1)} · {money(it.get('price_uzs'))} сум"
                         + (f" · {it.get('shop')}: {it.get('url')}" if it.get("url") else ""))
    lines.append(f"  итого: {money(o.get('total_uzs'))} сум · предоплата 50%: {money(o.get('prepay_uzs'))} сум")
    for ev in o.get("events") or []:
        what = orders_db.STATUS_LABELS.get(ev.get("status") or "", ev.get("status") or "")
        lines.append(f"  {ev.get('at', '')}  {what or ''}{' · ' if what and ev.get('note') else ''}{ev.get('note') or ''}"
                     + (f"  ({ev['by']})" if ev.get("by") else ""))
    return "\n".join(lines)


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
    order_nos = extract_order_numbers(text)
    if order_nos:
        db = orders_db.OrdersDB(orders_db.default_path())
        try:
            for no in order_nos:
                o = db.get_order(no, with_events=True)
                print(describe_order(o) if o else f"[{no}] в базе нет")
                print()
        finally:
            db.close()
        text = ORDER_NO_RE.sub(" ", text)
        if not CODE_RE.search(text):
            return 0
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
