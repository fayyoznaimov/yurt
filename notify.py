"""Уведомления владельцу в Telegram (необязательно: без настроек ничего не делает).

    python notify.py "текст"           # отправить сообщение
    python notify.py --check           # проверить источники (data/raw_*.meta.json) и сообщить о проблемах
    python notify.py --test            # пробное сообщение: настроено ли

Настройки — переменные окружения (на сервере — файл /etc/yurt/yurt.env, в GitHub Actions — секреты):
    TELEGRAM_BOT_TOKEN   токен бота от @BotFather
    TELEGRAM_CHAT_ID     куда писать: ваш id (узнать — написать боту и открыть
                         https://api.telegram.org/bot<токен>/getUpdates) или id канала/группы
    YURT_NAME            подпись в сообщениях (по умолчанию «yurt»)
    YURT_STALE_HOURS     через сколько часов без обновления турецкий источник считается «застрявшим» (30)

--check пишет только об ИЗМЕНЕНИЯХ: источник сломался / застрял / снова работает. Что уже сообщено —
в data/notify_state.json, поэтому запуск каждые 15 минут не засыпает чат одинаковыми сообщениями.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE = DATA / "notify_state.json"
AUTO_SOURCES = ("pcardin_tr", "cacharel_tr", "trendyol")
BAD = ("stale", "failed", "partial")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str) -> bool:
    """Отправить сообщение. Не настроено или сеть упала — False, без исключений (уведомление не должно ронять запуск)."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    name = os.environ.get("YURT_NAME") or "yurt"
    if not (token and chat):
        print(f"[notify] Telegram не настроен — сообщение не отправлено: {text[:200]}")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": f"[{name}] {text}"[:4000],
                                "disable_web_page_preview": "true"}, timeout=20)
        if r.status_code != 200:
            print(f"[notify] Telegram ответил {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[notify] Telegram недоступен: {e.__class__.__name__}")
        return False


def _age_hours(ts: str | None) -> float | None:
    try:
        return (time.time() - datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()) / 3600
    except (TypeError, ValueError):
        return None


def source_problems(stale_hours: float) -> dict[str, str]:
    """{источник: что не так} по data/raw_*.meta.json."""
    out = {}
    for meta in sorted(DATA.glob("raw_*.meta.json")):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        src = m.get("source") or meta.name[4:-10]
        status = m.get("status")
        age = _age_hours(m.get("collected_at"))
        if status in BAD:
            out[src] = f"{status}: {str(m.get('note') or '')[:160]}"
        elif src in AUTO_SOURCES and age is not None and age > stale_hours:
            out[src] = f"не обновлялся {age:.0f} ч"
    return out


def check(stale_hours: float) -> int:
    probs = source_problems(stale_hours)
    try:
        before = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except ValueError:
        before = {}
    lines = []
    for src, why in probs.items():
        if before.get(src) != why.split(":")[0]:
            lines.append(f"Источник {src}: {why}. На сайте остаются прошлые данные.")
    for src in before:
        if src not in probs:
            lines.append(f"Источник {src} снова обновляется.")
    if lines:
        send("\n".join(lines))
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({s: w.split(":")[0] for s, w in probs.items()}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print("[notify] проблем нет" if not probs else "[notify] проблемы: " + "; ".join(f"{s} — {w}" for s, w in probs.items()))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Уведомления в Telegram")
    ap.add_argument("text", nargs="*", help="текст сообщения")
    ap.add_argument("--check", action="store_true", help="сообщить о сломавшихся/застрявших источниках")
    ap.add_argument("--test", action="store_true", help="пробное сообщение")
    args = ap.parse_args()
    stale = float(os.environ.get("YURT_STALE_HOURS") or 30)
    if args.test:
        ok = send(f"проверка уведомлений с {socket.gethostname()}")
        print("Отправлено." if ok else "Не отправлено (см. выше).")
        return 0 if ok else 1
    if args.check:
        return check(stale)
    if args.text:
        send(" ".join(args.text))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
