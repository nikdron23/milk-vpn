#!/usr/bin/env bash
# Бэкап базы SQLite в ./backups (хранятся 14 дней). Запускается по cron ежедневно, можно вручную.
# Восстановление: остановите сервис, положите нужную копию в volume как vpnshop.db (см. docs/INSTALL.md).
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p backups
stamp="$(date +%Y-%m-%d_%H%M)"

if ! docker compose ps --status running -q vpnshop | grep -q .; then
  echo "Контейнер vpnshop не запущен — бэкап пропущен" >&2
  exit 1
fi

docker compose exec -T vpnshop python - <<'PY'
import os, sqlite3, sys
src, dst = "/app/data/vpnshop.db", "/app/data/backup.tmp.db"
if not os.path.exists(src):
    sys.exit("БД SQLite не найдена (если используете PostgreSQL — делайте pg_dump отдельно)")
if os.path.exists(dst):
    os.remove(dst)
a, b = sqlite3.connect(src), sqlite3.connect(dst)
a.backup(b)  # консистентная копия, даже если сервис пишет в БД
b.close(); a.close()
PY
docker compose cp vpnshop:/app/data/backup.tmp.db "backups/vpnshop-$stamp.db"
docker compose exec -T vpnshop rm -f /app/data/backup.tmp.db
find backups -name 'vpnshop-*.db' -mtime +14 -delete
echo "Бэкап сохранён: backups/vpnshop-$stamp.db"
