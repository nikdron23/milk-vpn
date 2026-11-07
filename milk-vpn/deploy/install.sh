#!/usr/bin/env bash
# =============================================================================
#  Milk VPN — автоматическая установка на чистый VPS
#
#  Подходит для Ubuntu 20.04+ и Debian 11+. Запуск от root из папки проекта:
#      sudo bash deploy/install.sh
#
#  Что делает: ставит Docker, спрашивает настройки (домен, токен бота, пароль админа,
#  Робокасса), создаёт .env, поднимает сайт + бота + админку за Caddy с автоматическим
#  HTTPS-сертификатом, включает ежедневный бэкап базы.
#
#  Проверка без установки (пишет .env в временную папку):  bash deploy/install.sh --dry-run
#  Безопасен для повторного запуска.
# =============================================================================
set -Eeuo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/milkvpn}"
NONINTERACTIVE="${NONINTERACTIVE:-0}"
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 0 ;;
  esac
done

if [[ -t 1 ]]; then
  C_B=$'\e[1m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_0=$'\e[0m'
else
  C_B=''; C_G=''; C_Y=''; C_R=''; C_0=''
fi
step() { printf '\n%s==> %s%s\n' "$C_B" "$*" "$C_0"; }
ok()   { printf '  %s✔%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '\n%s✘ %s%s\n' "$C_R" "$*" "$C_0" >&2; exit 1; }
trap 'die "Ошибка на строке $LINENO. Исправьте причину и запустите скрипт ещё раз — это безопасно."' ERR

# ---------- вспомогательные функции ----------
is_interactive() { [[ "$NONINTERACTIVE" != "1" && -t 0 ]]; }

# ask ПЕРЕМЕННАЯ "Вопрос" [значение_по_умолчанию] [secret]
ask() {
  local var="$1" prompt="$2" def="${3:-}" secret="${4:-0}" val=""
  if [[ -n "${!var:-}" ]]; then return 0; fi          # уже задано через окружение
  if ! is_interactive; then printf -v "$var" '%s' "$def"; return 0; fi
  local hint=""; [[ -n "$def" ]] && hint=" [$def]"
  if [[ "$secret" == "1" ]]; then
    read -r -s -p "  $prompt$hint: " val; echo
  else
    read -r -p "  $prompt$hint: " val
  fi
  printf -v "$var" '%s' "${val:-$def}"
}

# confirm "Вопрос" [y|n]  — код возврата 0 = «да»
confirm() {
  local prompt="$1" def="${2:-n}" ans hint="y/N"
  [[ "$def" == "y" ]] && hint="Y/n"
  if ! is_interactive; then [[ "$def" == "y" ]]; return; fi
  read -r -p "  $prompt [$hint]: " ans
  ans="${ans:-$def}"
  [[ "$ans" =~ ^[YyДд] ]]
}

rand() { # rand N — случайная строка из букв и цифр
  local n="${1:-24}" s=""
  while [[ ${#s} -lt $n ]]; do s+="$(head -c 96 /dev/urandom | base64 -w0 | tr -dc 'A-Za-z0-9')"; done
  printf '%s' "${s:0:$n}"
}

plain() { # проверка, что значение безопасно лежит в .env
  case "$2" in
    *\'*|*\\*|*'${'*|*$'\n'*|*$'\r'*) die "В значении $1 нельзя использовать символы ' \\ \${ и перевод строки." ;;
  esac
}
envline() { plain "$1" "$2"; printf "%s='%s'\n" "$1" "$2"; }

# ---------- 0. проверки ----------
step "Проверка системы"
if [[ $DRY_RUN -eq 1 ]]; then
  warn "РЕЖИМ ПРОВЕРКИ (--dry-run): Docker не ставится, контейнеры не запускаются"
else
  [[ $EUID -eq 0 ]] || die "Запустите от root: sudo bash deploy/install.sh"
  [[ -r /etc/os-release ]] || die "Не удалось определить ОС"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) ok "ОС: ${PRETTY_NAME:-$ID}" ;;
    *) die "Скрипт рассчитан на Ubuntu/Debian. Ваша ОС: ${PRETTY_NAME:-неизвестно}. Установите вручную по docs/INSTALL.md" ;;
  esac
fi
[[ -f "$SRC_DIR/docker-compose.yml" && -d "$SRC_DIR/app" ]] || die "Запускайте скрипт из распакованной папки проекта (рядом должны быть app/ и docker-compose.yml)"

# ---------- 1. сбор настроек ----------
REUSE=0
if [[ -f "$INSTALL_DIR/.env" ]]; then
  warn "Найдена существующая установка: $INSTALL_DIR/.env"
  if confirm "Сохранить текущие настройки и только обновить файлы/перезапустить?" y; then REUSE=1; fi
fi

if [[ $REUSE -eq 1 ]]; then
  DOMAIN="$(grep -E '^DOMAIN=' "$INSTALL_DIR/.env" | head -1 | cut -d= -f2- | tr -d "'")"
  ADMIN_LOGIN="$(grep -E '^ADMIN_LOGIN=' "$INSTALL_DIR/.env" | head -1 | cut -d= -f2- | tr -d "'")"
  ADMIN_PASSWORD=""
  ok "Настройки сохранены (домен: ${DOMAIN:-?})"
else
  step "Настройки"
  echo "  Нажимайте Enter, чтобы принять значение в [скобках]."
  echo
  while true; do
    ask DOMAIN "Домен сервиса (A-запись уже должна указывать на этот сервер), например vpn.example.com"
    DOMAIN="$(printf '%s' "${DOMAIN:-}" | tr 'A-Z' 'a-z' | sed 's#^https\?://##; s#/.*$##')"
    if [[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]]; then break; fi
    is_interactive || die "Укажите корректный DOMAIN"
    warn "Нужен домен вида vpn.example.com"; DOMAIN=""
  done

  ask BRAND_NAME "Название сервиса" "Milk VPN"

  while true; do
    ask BOT_TOKEN "Токен Telegram-бота (из @BotFather)"
    if [[ "${BOT_TOKEN:-}" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{30,}$ ]]; then break; fi
    is_interactive || die "Укажите корректный BOT_TOKEN"
    warn "Токен выглядит как 123456789:AAE... — скопируйте его из @BotFather целиком"; BOT_TOKEN=""
  done

  ask ADMIN_TG_IDS "Ваш Telegram ID (число; узнать можно у @userinfobot). Для нескольких админов — через запятую"
  ask SUPPORT_USERNAME "Username поддержки в Telegram без @ (можно пропустить)"
  ask CONTACT_EMAIL "Контактный e-mail для страниц «Условия» и «Конфиденциальность» (можно пропустить)"
  ask ADMIN_LOGIN "Логин администратора сайта" "admin"
  GENERATED_PASS=0
  if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
    ask ADMIN_PASSWORD "Пароль администратора (Enter — сгенерировать надёжный)" "" 1
    if [[ -z "${ADMIN_PASSWORD:-}" ]]; then ADMIN_PASSWORD="$(rand 18)"; GENERATED_PASS=1; fi
  fi
  [[ ${#ADMIN_PASSWORD} -ge 8 ]] || die "Пароль администратора — минимум 8 символов"

  # --- Робокасса ---
  RK_ON=0
  if [[ -n "${ROBOKASSA_LOGIN:-}" ]] || confirm "Настроить Робокассу сейчас? (можно позже — отредактировать .env)" n; then
    RK_ON=1
    ask ROBOKASSA_LOGIN "Идентификатор магазина (MerchantLogin)"
    ask ROBOKASSA_PASSWORD1 "Пароль #1" "" 1
    ask ROBOKASSA_PASSWORD2 "Пароль #2" "" 1
    if [[ -z "${ROBOKASSA_TEST:-}" ]]; then
      if confirm "Включить ТЕСТОВЫЙ режим (платежи не настоящие; рекомендуется для первой проверки)?" y; then
        ROBOKASSA_TEST=true; else ROBOKASSA_TEST=false; fi
    fi
    if [[ "$ROBOKASSA_TEST" == "true" ]]; then
      ask ROBOKASSA_TEST_PASSWORD1 "Тестовый пароль #1" "" 1
      ask ROBOKASSA_TEST_PASSWORD2 "Тестовый пароль #2" "" 1
    fi
    ask ROBOKASSA_ALGO "Алгоритм подписи (как в настройках магазина: md5 / sha256 / sha384 / sha512)" "md5"
  fi
fi

# ---------- 2. проверки окружения ----------
if [[ $DRY_RUN -eq 0 ]]; then
  step "Проверка домена и портов"
  PUBLIC_IP="$(curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || curl -4fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)"
  DOMAIN_IP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)"
  if [[ -z "$DOMAIN_IP" ]]; then
    warn "Домен $DOMAIN пока не резолвится. Сертификат HTTPS не выпустится, пока DNS-запись не заработает."
    confirm "Продолжить установку всё равно?" n || die "Создайте A-запись $DOMAIN -> ${PUBLIC_IP:-IP сервера} и запустите скрипт снова."
  elif [[ -n "$PUBLIC_IP" && "$DOMAIN_IP" != "$PUBLIC_IP" ]]; then
    warn "Домен указывает на $DOMAIN_IP, а IP этого сервера — $PUBLIC_IP."
    confirm "Продолжить установку всё равно?" n || die "Исправьте A-запись домена и запустите скрипт снова."
  else
    ok "$DOMAIN -> ${DOMAIN_IP}"
  fi

  OWN_CADDY=""
  if command -v docker >/dev/null 2>&1 && [[ -f "$INSTALL_DIR/docker-compose.yml" ]]; then
    OWN_CADDY="$(cd "$INSTALL_DIR" && docker compose --profile https ps -q caddy 2>/dev/null || true)"
  fi
  BUSY="$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(80|443)$' || true)"
  if [[ -n "$BUSY" && -z "$OWN_CADDY" ]]; then
    warn "Порты 80/443 уже заняты другой программой:"
    printf '      %s\n' $BUSY
    echo "      Сайту нужны именно эти порты для HTTPS. Если здесь стоит 3x-ui/nginx на 443 —"
    echo "      установите сервис на ОТДЕЛЬНЫЙ VPS (рекомендуется, см. docs/INSTALL.md)."
    confirm "Всё равно продолжить?" n || die "Освободите порты 80 и 443 или используйте отдельный сервер."
  else
    ok "Порты 80 и 443 свободны"
  fi

  if [[ $REUSE -eq 0 && -n "${BOT_TOKEN:-}" ]]; then
    step "Проверка токена бота"
    TG_REPLY="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null || true)"
    if [[ "$TG_REPLY" == *'"ok":true'* ]]; then
      BOT_USERNAME="$(printf '%s' "$TG_REPLY" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
      ok "Бот найден: @${BOT_USERNAME}"
    else
      warn "Telegram не подтвердил токен (неверный токен или нет доступа к api.telegram.org)."
      confirm "Продолжить всё равно?" n || die "Проверьте BOT_TOKEN в @BotFather."
    fi
  fi

  # ---------- 3. Docker ----------
  step "Docker"
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "Docker уже установлен: $(docker --version)"
  else
    echo "  Устанавливаю Docker официальным скриптом https://get.docker.com ..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y ca-certificates curl
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    systemctl enable --now docker
    docker compose version >/dev/null 2>&1 || die "Не установился плагин docker compose"
    ok "Docker установлен"
  fi
fi

# ---------- 4. файлы и .env ----------
step "Установка файлов в $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
if [[ "$SRC_DIR" != "$INSTALL_DIR" ]]; then
  (cd "$SRC_DIR" && tar cf - --exclude='./.env' --exclude='./data' --exclude='./backups' --exclude='./.venv' \
      --exclude='*/__pycache__' --exclude='./.git' --exclude='./.pytest_cache' --exclude='./tests' .) \
    | (cd "$INSTALL_DIR" && tar xf -)
fi
chmod +x "$INSTALL_DIR"/deploy/*.sh 2>/dev/null || true
ok "Файлы на месте"

if [[ $REUSE -eq 0 ]]; then
  [[ -f "$INSTALL_DIR/.env" ]] && cp "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.bak-$(date +%s)"
  umask 077
  {
    echo "# Создано deploy/install.sh $(date -u +%F\ %T) UTC. Хранить в секрете."
    envline DOMAIN "$DOMAIN"
    envline BASE_URL "https://$DOMAIN"
    envline BRAND_NAME "${BRAND_NAME:-Milk VPN}"
    envline SECRET_KEY "$(rand 48)"
    envline TIMEZONE "${TIMEZONE:-Europe/Moscow}"
    envline CONTACT_EMAIL "${CONTACT_EMAIL:-}"
    echo
    envline BOT_TOKEN "$BOT_TOKEN"
    envline ADMIN_TG_IDS "${ADMIN_TG_IDS:-}"
    envline SUPPORT_USERNAME "${SUPPORT_USERNAME#@}"
    echo
    envline ADMIN_LOGIN "$ADMIN_LOGIN"
    envline ADMIN_PASSWORD "$ADMIN_PASSWORD"
    echo
    echo "# Серверы VPN добавляются в админ-панели: раздел «Серверы»."
    echo
    envline ROBOKASSA_LOGIN "${ROBOKASSA_LOGIN:-}"
    envline ROBOKASSA_PASSWORD1 "${ROBOKASSA_PASSWORD1:-}"
    envline ROBOKASSA_PASSWORD2 "${ROBOKASSA_PASSWORD2:-}"
    envline ROBOKASSA_TEST_PASSWORD1 "${ROBOKASSA_TEST_PASSWORD1:-}"
    envline ROBOKASSA_TEST_PASSWORD2 "${ROBOKASSA_TEST_PASSWORD2:-}"
    envline ROBOKASSA_TEST "${ROBOKASSA_TEST:-true}"
    envline ROBOKASSA_ALGO "${ROBOKASSA_ALGO:-md5}"
    envline ROBOKASSA_RECEIPT "${ROBOKASSA_RECEIPT:-false}"
    envline ROBOKASSA_SNO "${ROBOKASSA_SNO:-usn_income}"
    envline ROBOKASSA_TAX "${ROBOKASSA_TAX:-none}"
    echo
    envline TRIAL_DAYS "${TRIAL_DAYS:-1}"
    envline TRIAL_GB "${TRIAL_GB:-1}"
    envline DATABASE_URL "sqlite+aiosqlite:///./data/vpnshop.db"
  } > "$INSTALL_DIR/.env.new"
  mv "$INSTALL_DIR/.env.new" "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
  ok "Файл .env создан (права 600)"
fi

if [[ $DRY_RUN -eq 1 ]]; then
  step "Режим проверки завершён"
  echo "  .env записан в $INSTALL_DIR/.env. Контейнеры не запускались."
  exit 0
fi

# ---------- 5. файрвол, бэкапы, запуск ----------
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  step "Файрвол ufw"
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ok "Порты 80 и 443 открыты"
fi

step "Ежедневный бэкап базы"
cat > /etc/cron.d/milkvpn-backup <<EOF
17 4 * * * root cd $INSTALL_DIR && ./deploy/backup.sh >> /var/log/milkvpn-backup.log 2>&1
EOF
chmod 644 /etc/cron.d/milkvpn-backup
ok "Бэкап: каждый день в 04:17, хранятся 14 дней (папка $INSTALL_DIR/backups)"

step "Сборка и запуск (первая сборка занимает 1–3 минуты)"
cd "$INSTALL_DIR"
docker compose --profile https up -d --build
ok "Контейнеры запущены"

step "Жду, пока сайт станет доступен по HTTPS (выпуск сертификата до ~2 минут)"
UP=0
for _ in $(seq 1 40); do
  if curl -fsS --max-time 6 "https://$DOMAIN/healthz" >/dev/null 2>&1; then UP=1; break; fi
  sleep 3
done

echo
if [[ $UP -eq 1 ]]; then
  printf '%s✔ Готово! Milk VPN работает.%s\n' "$C_G$C_B" "$C_0"
else
  printf '%s! Контейнеры запущены, но https://%s пока не отвечает.%s\n' "$C_Y$C_B" "$DOMAIN" "$C_0"
  echo "  Обычно причина — DNS ещё не обновился или закрыты порты 80/443. Подождите пару минут и откройте сайт."
  echo "  Логи:  cd $INSTALL_DIR && docker compose logs --tail 80"
fi
cat <<EOF

  Сайт:           https://$DOMAIN
  Админ-панель:   https://$DOMAIN/admin
  Логин админа:   $ADMIN_LOGIN
EOF
if [[ $REUSE -eq 0 ]]; then
  echo "  Пароль админа:  $ADMIN_PASSWORD   (он же в $INSTALL_DIR/.env — сохраните и не показывайте никому)"
fi
cat <<EOF

  Что дальше:
   1. Подготовьте VPN-серверы (3x-ui) — см. docs/NODES.md  (скрипт: deploy/install-node.sh)
   2. В админке откройте «Серверы» и добавьте каждый сервер
   3. Отредактируйте тарифы: «Тарифы»; реферальную программу: «Рефералка»
   4. Робокасса: укажите в настройках магазина адреса
        Result URL:  https://$DOMAIN/payment/result   (метод POST)
        Success URL: https://$DOMAIN/payment/success
        Fail URL:    https://$DOMAIN/payment/fail
   5. Проверьте: в боте /start -> «Пробный период», добавьте ссылку в приложение Hiddify

  Полезные команды (из папки $INSTALL_DIR):
    docker compose logs -f           — логи в реальном времени
    docker compose restart           — перезапуск
    ./deploy/update.sh               — обновление после замены файлов
    ./deploy/backup.sh               — бэкап базы вручную
EOF
