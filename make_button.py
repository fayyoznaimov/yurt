"""Собрать кнопку-закладку «Сохранить YOOX»: button_template.html + config.json → yoox-knopka.html.

    python make_button.py                    # настройки из config.json
    python make_button.py --config my.json

В кнопку встраиваются настройки из config.json → source_opts.yoox_import:
  searches              [{name, url, brands, price: [от, до], sort}] — что собирать на YOOX
                        brands: "known" — все из known_brands; ["Boss", "Hugo"] — свой список;
                                "url" — ссылка как есть (страница бренда …_d или отмеченные бренды);
                                не задан — "url" для таких ссылок, иначе "known"
                        sort:   "PriceAsc" (сначала дешёвые), "BestDeals", "Latest"
  known_brands          бренды для brands: "known"
  max_pages_per_search  15 — сколько страниц (по 60 товаров) брать в одном поиске
  max_total_pages       80 — сколько страниц кнопка открывает за одно нажатие

Закладка запоминает настройки в момент перетаскивания: после каждого изменения
searches / known_brands запустите этот скрипт и перетащите кнопку на панель закладок заново.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).parent
TEMPLATE = ROOT / "button_template.html"
OUT = ROOT / "yoox-knopka.html"
CFG_SLOT = "/*CONFIG*/null/*END*/"
INFO_RE = re.compile(r"<!--INFO-->.*?<!--/INFO-->", re.S)
SORTS = {"": "рекомендуемые", "PriceAsc": "сначала дешёвые", "BestDeals": "лучшие скидки", "Latest": "новинки"}
MAX_TOTAL_PAGES = 80

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _num(v) -> int | float | None:
    if v is None or isinstance(v, bool):
        return None
    if not isinstance(v, (int, float)):
        try:
            v = float(str(v).replace(",", "."))
        except ValueError:
            return None
    return int(v) if float(v).is_integer() else float(v)


def _brands_in_url(url: str) -> bool:
    """Ссылка уже про бренд: страница бренда «…/tombolini_d» или отмеченные brand[0]=…"""
    path = unquote(urlsplit(url).path).rstrip("/")
    return bool(re.search(r"_m?d$", path, re.I) or re.search(r"[?&]brand(\[|%5B)", url, re.I))


def _search(i: int, s, warn: list[str]) -> dict | None:
    """Проверить один поиск из config.json; None — пропустить."""
    if not isinstance(s, dict):
        warn.append(f"поиск №{i}: ожидается объект {{name, url, ...}} — пропущен")
        return None
    name = str(s.get("name") or f"Поиск {i}").strip()
    url = str(s.get("url") or "").strip()
    host = urlsplit(url).hostname or ""
    if not url or not (url.startswith("/") or host == "yoox.com" or host.endswith(".yoox.com")):
        warn.append(f"«{name}»: url должен быть ссылкой на yoox.com — поиск пропущен")
        return None
    out = {"name": name, "url": url}

    brands = s.get("brands")
    if isinstance(brands, str) and brands.strip():
        b = brands.strip()
        out["brands"] = b.lower() if b.lower() in ("known", "url") else [b]
    elif isinstance(brands, list):
        out["brands"] = [str(b).strip() for b in brands if str(b).strip()]
        if not out["brands"]:
            warn.append(f"«{name}»: пустой список brands — кнопка пропустит этот поиск")
    elif brands not in (None, ""):
        warn.append(f"«{name}»: brands должен быть \"known\", \"url\" или списком названий — взято \"known\"")
        out["brands"] = "known"
    else:   # не задан: страница бренда (…/tombolini_d) или ссылка с отмеченными брендами — как есть, иначе known
        out["brands"] = "url" if _brands_in_url(url) else "known"

    price = s.get("price")
    if price not in (None, [], ""):
        lo, hi = (_num(price[0]), _num(price[1])) if isinstance(price, list) and len(price) == 2 else (None, None)
        if lo is None and hi is None:
            warn.append(f"«{name}»: price должен быть [от, до] в евро, например [50, 70] — цена не задана")
        else:
            if lo is not None and hi is not None and lo > hi:
                warn.append(f"«{name}»: в price «от» больше «до» — поменял местами: [{hi:g}, {lo:g}]")
                lo, hi = hi, lo
            out["price"] = [lo, hi]

    sort = s.get("sort")
    if sort is not None:
        sort = str(sort).strip()
        if sort not in SORTS:
            warn.append(f"«{name}»: неизвестная сортировка {sort!r} (есть: PriceAsc, BestDeals, Latest) — передаю как есть")
        out["sort"] = sort
    return out


def build_cfg(cfg: dict) -> tuple[dict, list[str]]:
    so = (cfg.get("source_opts") or {}).get("yoox_import") or {}
    warn: list[str] = []
    raw = so.get("searches") or []
    if not isinstance(raw, list):
        warn.append("searches должен быть списком — поисков нет")
        raw = []
    searches = [x for i, s in enumerate(raw, 1) if (x := _search(i, s, warn))]

    known, seen = [], set()
    kb = so.get("known_brands") or []
    if isinstance(kb, str):
        warn.append("known_brands должен быть списком [\"Boss\", \"Hugo\"] — строку разделил по запятым")
        kb = kb.split(",")
    elif not isinstance(kb, list):
        warn.append("known_brands должен быть списком названий — пропущен")
        kb = []
    for b in kb:
        b = str(b).strip()
        if b and b.lower() not in seen:
            seen.add(b.lower())
            known.append(b)
    if not known and any(s.get("brands", "known") == "known" for s in searches):
        warn.append("known_brands пуст, а поиски с brands \"known\" есть — кнопка их пропустит")

    try:
        v = so.get("max_pages_per_search")
        per = 15 if v in (None, "") else int(v)
    except (TypeError, ValueError):
        per = 15
    try:
        total = int(so.get("max_total_pages") or MAX_TOTAL_PAGES)
    except (TypeError, ValueError):
        total = MAX_TOTAL_PAGES
    return {
        "searches": searches,
        "known_brands": known,
        "max_pages_per_search": max(1, min(200, per)),      # YOOX всё равно не отдаёт больше 200 страниц
        "max_total_pages": max(1, total),
        "version": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }, warn


def js_literal(obj) -> str:
    """JSON, который безопасно вставить внутрь <script> (и в код закладки)."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        s = s.replace(ch, esc)
    return s


def listing_url(s: dict) -> str:
    """Ссылка «открыть на YOOX»: категория с ценой и сортировкой (бренды кнопка отмечает сама)."""
    url = s["url"] if not s["url"].startswith("/") else "https://www.yoox.com" + s["url"]
    params = []
    if s.get("price"):
        lo, hi = s["price"]
        params.append(f"price={quote(f'{0 if lo is None else lo}:{100000 if hi is None else hi}', safe='')}")
    if "sort" in s:
        params.append(f"sortBy={quote(s['sort'], safe='')}")
    return url + (("&" if "?" in url else "?") + "&".join(params) if params else "")


def _brands_label(s: dict, known: list[str]) -> str:
    b = s.get("brands")
    if b == "known":
        return f"известные бренды ({len(known)})"
    if b == "url":
        return "бренды как в ссылке"
    return ", ".join(b) if b else "брендов нет"


def _price_label(s: dict) -> str:
    if not s.get("price"):
        return "любая цена"
    lo, hi = s["price"]
    if lo and hi is not None:
        return f"{lo}–{hi} €"
    return f"от {lo} €" if hi is None else f"до {hi} €"


def info_html(c: dict) -> str:
    e = html.escape
    meta = (f"Версия кнопки: <b>{e(c['version'])}</b> · до {c['max_pages_per_search']} стр. по 60 товаров в одном поиске · "
            f"не больше {c['max_total_pages']} стр. за одно нажатие")
    if c["searches"]:
        items = []
        for s in c["searches"]:
            extra = [_brands_label(s, c["known_brands"]), _price_label(s)]
            if "sort" in s:
                extra.append(SORTS.get(s["sort"], s["sort"]))
            items.append(f'<li><b>{e(s["name"])}</b> — {e(" · ".join(extra))}. '
                         f'<a href="{e(listing_url(s))}" target="_blank" rel="noopener">открыть на YOOX</a></li>')
        lst = "<ul>" + "".join(items) + "</ul>"
    else:
        lst = ('<p class="note warn">Поисков нет: кнопка умеет только «2 — эта выдача». Добавьте поиски в '
               '<code>config.json</code> → <code>source_opts.yoox_import.searches</code>.</p>')
    brands = (f'<p class="brands">Известные бренды (<code>known_brands</code>): {e(", ".join(c["known_brands"]))}</p>'
              if c["known_brands"] else "")
    return f'<!--INFO--><div class="info"><p class="meta">{meta}</p>{lst}{brands}</div><!--/INFO-->'


def main() -> None:
    ap = argparse.ArgumentParser(description="Собрать yoox-knopka.html из button_template.html и config.json")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if tpl.count(CFG_SLOT) != 1 or len(INFO_RE.findall(tpl)) != 1:
        sys.exit(f"В {TEMPLATE.name} не найдено место для настроек ({CFG_SLOT} и <!--INFO-->…<!--/INFO-->)")

    c, warn = build_cfg(cfg)
    page = tpl.replace(CFG_SLOT, "/*CONFIG*/" + js_literal(c) + "/*END*/")
    page = INFO_RE.sub(lambda m: info_html(c), page)
    if page.lower().count("</script") != tpl.lower().count("</script"):
        sys.exit("Настройки сломали бы код кнопки (</script> внутри) — проверьте config.json")
    out = Path(args.out)
    out.write_text(page, encoding="utf-8", newline="")

    print(f"Кнопка собрана: {out.name if out.resolve().parent == ROOT.resolve() else out} (версия {c['version']})")
    print(f"  поисков: {len(c['searches'])}, известных брендов: {len(c['known_brands'])}, "
          f"до {c['max_pages_per_search']} стр. на поиск, не больше {c['max_total_pages']} стр. за нажатие")
    for i, s in enumerate(c["searches"], 1):
        extra = [_brands_label(s, c["known_brands"]), _price_label(s)]
        if "sort" in s:
            extra.append(SORTS.get(s["sort"], s["sort"]))
        print(f"  {i}. {s['name']} — {' · '.join(extra)}")
        print(f"     {s['url']}")
    for w in warn:
        print(f"  ВНИМАНИЕ: {w}")
    print("\nВажно: откройте yoox-knopka.html в Chrome и ЗАНОВО перетащите кнопку на панель закладок "
          "(старую закладку удалите).\nЗакладка помнит поиски на момент перетаскивания — так нужно делать "
          "после каждого изменения searches / known_brands в config.json.")


if __name__ == "__main__":
    main()
