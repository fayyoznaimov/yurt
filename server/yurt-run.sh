#!/usr/bin/env bash
# Одно задание каталога на своём сервере. Запускают systemd-таймеры (yurt-job@<задание>.service) или cron.
#
#   server/yurt-run.sh update    # каждые 6 ч: свежий код из GitHub, турецкие магазины, сборка, публикация
#   server/yurt-run.sh yoox      # каждые 15 мин: если во «входящих» новый yoox_*.json — импорт, сборка, публикация
#   server/yurt-run.sh fx        # раз в день 09:00 (Ташкент): пересчёт цен по свежему курсу ЦБ, публикация
#   server/yurt-run.sh cleanup   # раз в неделю: старые журналы, файлы «входящих», фото ушедших товаров
#
# Все задания берут общий замок data/server.lock (flock): одновременно идёт только одно. yoox при занятом
# замке просто пропускает свой ход (следующий — через 15 минут), остальные ждут до 2 часов.
# Журнал: journald (journalctl -u 'yurt-job@*') и data/logs/server_<задание>_<дата>.log.
# Настройки — переменные окружения из /etc/yurt/yurt.env (systemd EnvironmentFile), см. server/yurt.env.example.
set -uo pipefail

JOB="${1:-}"
case "$JOB" in update|yoox|fx|cleanup) ;; *) echo "Использование: $0 update|yoox|fx|cleanup" >&2; exit 2 ;; esac

APP_DIR="${YURT_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$APP_DIR" || exit 1
PY="${YURT_PYTHON:-}"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

mkdir -p data/logs
LOG="data/logs/server_${JOB}_$(date +%Y%m%d).log"
exec > >(tee -a "$LOG") 2>&1

notify() { "$PY" notify.py "$@" || true; }
fail() {
  echo "ОШИБКА: $1"
  notify "Сбой задания «$JOB» на $(hostname): $1. Журнал: $APP_DIR/$LOG"
  exit 1
}

exec 9>"data/server.lock"
if [ "$JOB" = yoox ]; then
  flock -n 9 || { echo "$(date '+%F %T') другое задание ещё идёт — пропускаю ход"; exit 0; }
else
  flock -w 7200 9 || fail "замок data/server.lock занят больше 2 часов (зависло другое задание?)"
fi

[ "$JOB" = yoox ] || echo "=== $(date '+%F %T %Z') задание $JOB ==="

maybe_pull() {
  # свежий код, config.json и site/index.html из GitHub (что владелец поменял на GitHub — подхватится само)
  [ "${YURT_AUTO_PULL:-1}" = 1 ] || return 0
  if ! git pull --ff-only -q 2>&1; then
    notify "git pull на $(hostname) не прошёл — работаю на прежнем коде. Проверьте: cd $APP_DIR && git status"
  fi
}

deploy_if_changed() {
  # публикуем, только если сайт правда изменился с прошлой публикации
  if [ "${YURT_DEPLOY:-1}" != 1 ]; then echo "Публикация выключена (YURT_DEPLOY=0)"; return 0; fi
  local marker=data/.deployed
  # каталог частями: site/data/manifest.json переписывается при каждой сборке (старый формат — site/products.json)
  if [ -f "$marker" ] && [ -z "$(find site/data/manifest.json site/products.json site/index.html site/content.json site/brands.json -newer "$marker" 2>/dev/null)" ]; then
    echo "Сайт не менялся — не публикую"
    return 0
  fi
  "$PY" deploy.py || fail "deploy.py не смог выложить сайт (доступ к GitHub? см. README «Свой сервер»)"
  touch "$marker"
}

case "$JOB" in
  update)
    maybe_pull
    "$PY" update.py --no-deploy || fail "update.py завершился с ошибкой"
    deploy_if_changed
    notify --check
    ;;
  yoox)
    out="$("$PY" update.py --yoox-only 2>&1)"; rc=$?
    case "$out" in "Новых файлов YOOX нет"*) exit 0 ;; esac   # обычный «пустой» ход — без шума в журнале
    echo "=== $(date '+%F %T %Z') задание yoox ==="
    echo "$out"
    [ $rc -eq 0 ] || fail "импорт YOOX завершился с ошибкой"
    deploy_if_changed
    notify --check
    ;;
  fx)
    "$PY" run.py --offline || fail "пересчёт цен (run.py --offline) не удался"
    deploy_if_changed
    ;;
  cleanup)
    "$PY" server/cleanup.py || fail "уборка не удалась"
    ;;
esac
echo "=== $(date '+%F %T %Z') задание $JOB готово ==="
