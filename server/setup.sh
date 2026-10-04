#!/usr/bin/env bash
# Установка каталога на свой сервер Ubuntu/Debian. Можно запускать повторно (обновит и ничего не сломает).
#
#   sudo bash setup.sh                                   # всё по умолчанию
#   sudo GH_TOKEN_FILE=/root/gh_token bash setup.sh      # публикация через fine-grained токен GitHub (HTTPS)
#   sudo DEPLOY_KEY=generate bash setup.sh               # или: создать SSH deploy key (покажет, что вставить в GitHub)
#   sudo OWNER_PUBKEY_FILE=/root/pc.pub bash setup.sh    # пустить ваш компьютер класть файлы YOOX во «входящие»
#
# Переменные (все необязательны):
#   REPO_URL           https://github.com/fayyoznaimov/yurt.git
#   YURT_USER          yurt — системный пользователь, от которого всё работает (или ваш: YURT_USER=$SUDO_USER)
#   YURT_HOME          /opt/yurt — здесь app/ (код + data/), venv/, inbox/
#   GH_TOKEN_FILE      файл с fine-grained токеном GitHub (Contents: Read and write на этот репозиторий)
#   DEPLOY_KEY         generate — создать ключ ~yurt/.ssh/yurt_deploy; или путь к готовому приватному ключу
#   OWNER_PUBKEY_FILE  публичный ключ вашего компьютера (push_yoox.py --setup покажет его) — для scp во «входящие»
#   ENABLE_TIMERS      1 — включить таймеры systemd (по умолчанию 1)
#   ENABLE_ORDERS      1 — включить службу приёма заказов yurt-orders (order_api.py, по умолчанию 1)
# Токены и ключи в этот файл не пишутся и в git не попадают: только в файлы с правами 600 у пользователя yurt.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/fayyoznaimov/yurt.git}"
YURT_USER="${YURT_USER:-yurt}"
YURT_HOME="${YURT_HOME:-/opt/yurt}"
APP="$YURT_HOME/app"
VENV="$YURT_HOME/venv"
INBOX="$YURT_HOME/inbox"
ENV_DIR=/etc/yurt
ENV_FILE="$ENV_DIR/yurt.env"
ENABLE_TIMERS="${ENABLE_TIMERS:-1}"
ENABLE_ORDERS="${ENABLE_ORDERS:-1}"

say() { printf '\n== %s\n' "$*"; }
as_user() { sudo -u "$YURT_USER" -H "$@"; }
[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo: sudo bash $0"; exit 1; }

# owner/repo из адреса: https://github.com/owner/repo(.git) или git@github.com:owner/repo(.git)
SLUG="$(printf '%s' "$REPO_URL" | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##')"
HTTPS_URL="https://github.com/$SLUG.git"
SSH_URL="git@github.com:$SLUG.git"

say "Пакеты: python3, venv, git, openssh-client, flock"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git openssh-client ca-certificates util-linux tzdata sudo >/dev/null

say "Пользователь $YURT_USER, папка $YURT_HOME"
if ! id "$YURT_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$YURT_HOME" --shell /bin/bash "$YURT_USER"
fi
USER_HOME="$(getent passwd "$YURT_USER" | cut -d: -f6)"
install -d -o "$YURT_USER" -g "$YURT_USER" -m 755 "$YURT_HOME"
install -d -o "$YURT_USER" -g "$YURT_USER" -m 750 "$INBOX"
install -d -o "$YURT_USER" -g "$YURT_USER" -m 700 "$USER_HOME/.ssh"

say "Код: $REPO_URL → $APP"
if [ -d "$APP/.git" ]; then
  as_user git -C "$APP" pull --ff-only -q || echo "ВНИМАНИЕ: git pull не прошёл (локальные правки в $APP?) — оставляю как есть"
else
  as_user git clone -q "$HTTPS_URL" "$APP"
fi
install -d -o "$YURT_USER" -g "$YURT_USER" "$APP/data" "$APP/data/logs" "$APP/site"
install -d -o "$YURT_USER" -g "$YURT_USER" -m 700 "$APP/data/orders"     # заказы с контактами покупателей

say "Python: $VENV (+ requests)"
[ -x "$VENV/bin/python" ] || as_user python3 -m venv "$VENV"
as_user "$VENV/bin/pip" install -q --upgrade pip requests

say "Git: имя для коммитов публикации"
as_user git config --global user.name "yurt-server"
as_user git config --global user.email "yurt-server@users.noreply.github.com"
as_user git config --global pull.ff only

say "Доступ к GitHub для публикации (ветка gh-pages)"
if [ -n "${GH_TOKEN_FILE:-}" ]; then
  [ -s "$GH_TOKEN_FILE" ] || { echo "Нет файла токена $GH_TOKEN_FILE"; exit 1; }
  TOKEN="$(tr -d ' \r\n' < "$GH_TOKEN_FILE")"
  umask 077
  printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > "$USER_HOME/.git-credentials"
  chown "$YURT_USER:$YURT_USER" "$USER_HOME/.git-credentials"; chmod 600 "$USER_HOME/.git-credentials"
  unset TOKEN
  as_user git config --global credential.helper store
  as_user git -C "$APP" remote set-url origin "$HTTPS_URL"
  echo "Токен сохранён в $USER_HOME/.git-credentials (600). Файл $GH_TOKEN_FILE можно удалить: shred -u $GH_TOKEN_FILE"
elif [ -n "${DEPLOY_KEY:-}" ]; then
  KEY="$USER_HOME/.ssh/yurt_deploy"
  if [ "$DEPLOY_KEY" = generate ]; then
    [ -f "$KEY" ] || as_user ssh-keygen -q -t ed25519 -N "" -C "yurt-server deploy" -f "$KEY"
  else
    install -o "$YURT_USER" -g "$YURT_USER" -m 600 "$DEPLOY_KEY" "$KEY"
  fi
  CFG="$USER_HOME/.ssh/config"
  if ! grep -q "yurt_deploy" "$CFG" 2>/dev/null; then
    printf 'Host github.com\n  HostName github.com\n  User git\n  IdentityFile %s\n  IdentitiesOnly yes\n' "$KEY" >> "$CFG"
    chown "$YURT_USER:$YURT_USER" "$CFG"; chmod 600 "$CFG"
  fi
  # ключи github.com: сверьте отпечатки с https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
  as_user bash -c "ssh-keyscan -t ed25519 github.com 2>/dev/null >> ~/.ssh/known_hosts; sort -u -o ~/.ssh/known_hosts ~/.ssh/known_hosts"
  as_user git -C "$APP" remote set-url origin "$SSH_URL"
  echo "Публичный deploy key — вставьте в GitHub: репозиторий → Settings → Deploy keys → Add deploy key,"
  echo "галочка «Allow write access»:"
  echo; cat "$KEY.pub"; echo
else
  echo "Доступ для публикации не настроен: сборка будет идти, а deploy.py не сможет выложить сайт."
  echo "Повторите с GH_TOKEN_FILE=... или DEPLOY_KEY=generate (см. README, «Свой сервер»)."
fi

if [ -n "${OWNER_PUBKEY_FILE:-}" ]; then
  say "Ваш компьютер → «входящие» YOOX по SSH (только ключ, без пароля)"
  AK="$USER_HOME/.ssh/authorized_keys"
  PUB="$(head -n1 "$OWNER_PUBKEY_FILE")"
  touch "$AK"
  if ! grep -qF "$PUB" "$AK"; then
    printf 'restrict %s\n' "$PUB" >> "$AK"      # restrict: без проброса портов, агента и терминала
  fi
  chown "$YURT_USER:$YURT_USER" "$AK"; chmod 600 "$AK"
  echo "Ключ добавлен в $AK"
fi

say "Настройки: $ENV_FILE"
install -d -m 750 -o root -g "$YURT_USER" "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  sed -e "s#/opt/yurt/venv#$VENV#; s#/opt/yurt/inbox#$INBOX#" "$APP/server/yurt.env.example" > "$ENV_FILE"
  echo "Создан $ENV_FILE — впишите туда TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID, если нужны уведомления."
else
  echo "$ENV_FILE уже есть — не трогаю."
  for v in TELEGRAM_ORDERS_CHAT_ID ORDER_ALLOWED_ORIGINS ORDER_API_PORT ORDER_API_BIND ORDER_RATE_LIMIT ORDER_RATE_WINDOW; do
    if ! grep -q "^$v=" "$ENV_FILE"; then
      grep "^$v=" "$APP/server/yurt.env.example" >> "$ENV_FILE"     # новые настройки заказов — со значениями по умолчанию
      echo "  добавлено в $ENV_FILE: $(grep "^$v=" "$APP/server/yurt.env.example")"
    fi
  done
fi
chown root:"$YURT_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"
chmod +x "$APP/server/yurt-run.sh" || true

say "systemd: задания и таймеры"
for f in "$APP"/server/systemd/*; do
  sed -e "s#@USER@#$YURT_USER#g; s#@APP@#$APP#g" "$f" > "/etc/systemd/system/$(basename "$f")"
done
systemctl daemon-reload
if [ "$ENABLE_TIMERS" = 1 ]; then
  systemctl enable --now yurt-update.timer yurt-yoox.timer yurt-fx.timer yurt-cleanup.timer >/dev/null
fi
systemctl list-timers 'yurt-*' --no-pager || true
if [ "$ENABLE_ORDERS" = 1 ]; then
  systemctl enable yurt-orders.service >/dev/null
  systemctl restart yurt-orders.service || echo "ВНИМАНИЕ: yurt-orders не запустился — journalctl -u yurt-orders"   # перезапуск — новый код
  sleep 2
  systemctl --no-pager --lines=5 status yurt-orders.service || true
fi

cat <<EOF

Готово. Дальше:
  1. Первый сбор вручную (10–25 минут):   sudo systemctl start yurt-job@update.service
     смотреть журнал:                      journalctl -u yurt-job@update -f
  2. Данные YOOX: с компьютера —           python push_yoox.py   (кладёт файлы в $INBOX)
     или перенесите текущие данные с компьютера (см. README, «Свой сервер», шаг «Перенос данных»).
  3. Уведомления: впишите токен бота в $ENV_FILE и проверьте:
     sudo -u $YURT_USER bash -c 'set -a; . $ENV_FILE; cd $APP && $VENV/bin/python notify.py --test'
  4. Заказы с сайта: служба yurt-orders слушает 127.0.0.1:8787 (ORDER_API_PORT). Впишите TELEGRAM_ORDERS_CHAT_ID в $ENV_FILE,
     затем: sudo systemctl restart yurt-orders && curl -s http://127.0.0.1:8787/api/health
     Наружу по HTTPS — Caddy (server/Caddyfile.example) или Cloudflare Tunnel (server/cloudflared.example.yml), см. README.
EOF
