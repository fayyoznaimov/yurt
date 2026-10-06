#!/usr/bin/env bash
# Одно задание каталога на своём сервере. Запускают systemd-таймеры (см. server/systemd/) или cron.
#
#   server/yurt-run.sh turkey-akinon     # каждый час: Pierre Cardin + Cacharel (разделы; карта сайта — раз в сутки),
#                                        #   свежий код из GitHub, сборка и публикация, если каталог изменился
#   server/yurt-run.sh trendyol-pdp      # каждый час: Trendyol — проверка размеров по ярусам (горячие / на сайте /
#                                        #   остальные, ~45 мин), сборка и публикация, если изменилось
#   server/yurt-run.sh trendyol-listing  # каждую ночь (01:10): Trendyol — обход всей выдачи (~2 ч); товары по ней
#                                        #   выйдут на сайт со следующей проверкой размеров
#   server/yurt-run.sh yoox              # каждые 15 мин: если во «входящих» новый yoox_*.json — импорт, сборка, публикация
#   server/yurt-run.sh fx                # раз в день 09:00 (Ташкент): пересчёт цен по свежему курсу ЦБ, публикация
#   server/yurt-run.sh cleanup           # раз в неделю: старые журналы, файлы «входящих», фото ушедших товаров
#   server/yurt-run.sh update            # вручную: всё сразу одним run.py (как раньше; на сервере таймера у него нет)
#
# YOOX сервер НЕ собирает и на yoox.com не ходит: товары YOOX — только из файлов, которые владелец сохраняет кнопкой
# в своём браузере и присылает (push_yoox.py) во «входящие».
#
# Замки (flock, data/locks/):
#   job-<задание>.lock  у каждого задания свой: то же задание второй раз не запускается (новый ход просто пропускается),
#                       а разные задания идут параллельно (Pierre Cardin не ждёт ночной обход Trendyol);
#   site.lock           сборка сайта и публикация — по одной (ждёт до часа);
#   внутри run.py — ещё data/run.lock (сборка) и data/collect_<источник>.lock (сбор одного источника).
# Журнал: journald (journalctl -u 'yurt-*') и data/logs/server_<задание>_<дата>.log.
# Настройки — переменные окружения из /etc/yurt/yurt.env (systemd EnvironmentFile), см. server/yurt.env.example.
set -uo pipefail

JOB="${1:-}"
case "$JOB" in
  turkey-akinon|trendyol-pdp|trendyol-listing|yoox|fx|cleanup|update) ;;
  *) echo "Использование: $0 turkey-akinon|trendyol-pdp|trendyol-listing|yoox|fx|cleanup|update" >&2; exit 2 ;;
esac

APP_DIR="${YURT_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$APP_DIR" || exit 1
PY="${YURT_PYTHON:-}"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

mkdir -p data/logs data/locks
LOG="data/logs/server_${JOB}_$(date +%Y%m%d).log"
exec > >(tee -a "$LOG") 2>&1

notify() { "$PY" notify.py "$@" || true; }
fail() {
  echo "ОШИБКА: $1"
  notify "Сбой задания «$JOB» на $(hostname): $1. Журнал: $APP_DIR/$LOG"
  exit 1
}

# свой замок задания: тот же ход ещё идёт — этот пропускаем (таймер запустит следующий)
exec 9>"data/locks/job-${JOB}.lock"
if ! flock -n 9; then
  [ "$JOB" = yoox ] || echo "$(date '+%F %T') задание $JOB ещё идёт с прошлого раза — пропускаю ход"
  exit 0
fi

[ "$JOB" = yoox ] || echo "=== $(date '+%F %T %Z') задание $JOB ==="

site_lock() {
  # сборка сайта и публикация — по одной за раз (разные задания заканчиваются в разное время)
  exec 8>"data/locks/site.lock"
  flock -w 3600 8 || fail "замок сборки data/locks/site.lock занят больше часа (зависла сборка или публикация?)"
}
site_unlock() { flock -u 8 2>/dev/null || true; exec 8>&-; }

maybe_pull() {
  # свежий код, config.json и site/index.html из GitHub (что владелец поменял на GitHub — подхватится само);
  # под замком сборки, чтобы код не менялся посреди чужой сборки
  [ "${YURT_AUTO_PULL:-1}" = 1 ] || return 0
  site_lock
  if ! git pull --ff-only -q 2>&1; then
    notify "git pull на $(hostname) не прошёл — работаю на прежнем коде. Проверьте: cd $APP_DIR && git status"
  fi
  site_unlock
}

deploy_if_changed() {
  # публикуем, только если сайт правда изменился с прошлой публикации (вызывать под site_lock)
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

build_and_deploy() {
  # сборка из data/raw_*.json — только если данные источников (без времени сбора) или config.json изменились
  site_lock
  "$PY" run.py --offline --if-changed || fail "сборка сайта (run.py --offline) не удалась"
  deploy_if_changed
  site_unlock
}

case "$JOB" in
  turkey-akinon)
    maybe_pull
    "$PY" run.py --collect-only --only pcardin_tr,cacharel_tr || fail "сбор Pierre Cardin / Cacharel завершился с ошибкой"
    build_and_deploy
    notify --check
    ;;
  trendyol-pdp)
    "$PY" run.py --collect-only --only trendyol --trendyol-mode pdp || fail "проверка размеров Trendyol завершилась с ошибкой"
    build_and_deploy
    notify --check
    ;;
  trendyol-listing)
    "$PY" run.py --collect-only --only trendyol --trendyol-mode listing || fail "обход выдачи Trendyol завершился с ошибкой"
    ;;
  update)
    # вручную, когда таймеры Турции выключены (первый запуск): run.py собирает всё и держит data/run.lock часами
    maybe_pull
    "$PY" update.py --no-deploy || fail "update.py завершился с ошибкой"
    site_lock
    deploy_if_changed
    site_unlock
    notify --check
    ;;
  yoox)
    out="$("$PY" update.py --yoox-only 2>&1)"; rc=$?
    case "$out" in "Новых файлов YOOX нет"*) exit 0 ;; esac   # обычный «пустой» ход — без шума в журнале
    echo "=== $(date '+%F %T %Z') задание yoox ==="
    echo "$out"
    [ $rc -eq 0 ] || fail "импорт YOOX завершился с ошибкой"
    site_lock
    deploy_if_changed
    site_unlock
    notify --check
    ;;
  fx)
    site_lock
    "$PY" run.py --offline || fail "пересчёт цен (run.py --offline) не удался"
    deploy_if_changed
    site_unlock
    ;;
  cleanup)
    "$PY" server/cleanup.py || fail "уборка не удалась"
    ;;
esac
echo "=== $(date '+%F %T %Z') задание $JOB готово ==="
