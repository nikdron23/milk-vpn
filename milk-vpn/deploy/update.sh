#!/usr/bin/env bash
# Обновление Milk VPN: замените файлы проекта в /opt/milkvpn (не трогая .env и data), затем:
#     cd /opt/milkvpn && ./deploy/update.sh
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

[[ -f .env ]] || { echo "Нет .env — это не папка установки" >&2; exit 1; }
echo "==> Бэкап базы перед обновлением"
./deploy/backup.sh || echo "  (бэкап не удался — продолжаю)"
echo "==> Пересборка и перезапуск"
docker compose --profile https up -d --build
docker image prune -f >/dev/null 2>&1 || true
echo "Готово. Логи: docker compose logs --tail 50"
