#!/usr/bin/env bash
# =============================================================================
#  Подготовка VPN-сервера (ноды): установка панели 3x-ui на отдельный VPS.
#  Запуск от root на сервере в нужной стране:   sudo bash install-node.sh
#
#  Скрипт запускает ОФИЦИАЛЬНЫЙ установщик 3x-ui (github.com/MHSanaei/3x-ui) и по желанию
#  включает BBR. Inbound (VLESS + Reality) создаётся вручную в панели — шаги в docs/NODES.md.
# =============================================================================
set -Eeuo pipefail

die() { printf '\n✘ %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Запустите от root: sudo bash install-node.sh"
command -v apt-get >/dev/null 2>&1 || die "Скрипт рассчитан на Ubuntu/Debian"

INSTALLER_URL="https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh"
cat <<EOF

Будет скачан и запущен официальный установщик 3x-ui:
  $INSTALLER_URL
Он интерактивный: спросит, менять ли порт и учётные данные панели. ЗАПИШИТЕ выданные
адрес панели (с секретным путём), логин и пароль — они понадобятся в админке Milk VPN.

EOF
if [[ -t 0 ]]; then
  read -r -p "Продолжить? [y/N]: " ans
  [[ "${ans:-n}" =~ ^[YyДд] ]] || die "Отменено"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl ca-certificates

curl -fsSL "$INSTALLER_URL" -o /tmp/3x-ui-install.sh
bash /tmp/3x-ui-install.sh
rm -f /tmp/3x-ui-install.sh

if [[ -t 0 ]]; then
  read -r -p "Включить BBR (ускоряет TCP на большинстве VPS)? [Y/n]: " bbr
  if [[ "${bbr:-y}" =~ ^[YyДд] ]]; then
    cat > /etc/sysctl.d/99-milkvpn-bbr.conf <<'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
    sysctl --system >/dev/null
    echo "BBR: $(sysctl -n net.ipv4.tcp_congestion_control)"
  fi
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  echo
  echo "ВНИМАНИЕ: ufw активен. Откройте порты: порт панели 3x-ui и порт inbound (обычно 443/tcp):"
  echo "    ufw allow 443/tcp"
  echo "    ufw allow <порт_панели>/tcp"
fi

cat <<'EOF'

Дальше (подробно — docs/NODES.md):
  1. Откройте панель 3x-ui в браузере и войдите.
  2. «Inbounds» → «Add Inbound»: VLESS, порт 443, Transmission TCP, Security Reality.
  3. В админке Milk VPN: «Серверы» → «Добавить сервер» — укажите адрес панели, логин, пароль и ID inbound.
EOF
