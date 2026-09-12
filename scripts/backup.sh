#!/usr/bin/env bash
# Daily backup: a Postgres dump plus a tar of media/ (the original ECG images). work/ is
# intermediates only -- disposable, see analysis/runner.py -- and is left out on purpose.
#
# Usage, from the directory holding docker-compose.yml:
#   ./scripts/backup.sh [destination-directory]   # default: ./backups
#
# The result is plaintext. Encrypt both files (age or restic; either needs no server of its
# own) before they leave this machine -- see README, "Despliegue", "Copias de seguridad".
# A cron entry running this nightly is the other half of that section.
set -euo pipefail

DEST="${1:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$DEST"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "==> Dumping Postgres"
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-ekg}" "${POSTGRES_DB:-ekg}" | gzip > "$DEST/db-$STAMP.sql.gz"

echo "==> Archiving the original images (media/)"
docker compose exec -T api tar czf - -C /data/media . > "$DEST/media-$STAMP.tar.gz"

echo "==> Done:"
echo "    $DEST/db-$STAMP.sql.gz"
echo "    $DEST/media-$STAMP.tar.gz"
echo "Encrypt both before copying them off this machine. See scripts/restore.sh to verify one."
