#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh, into the current compose project's volumes.
# Run from the directory holding docker-compose.yml.
#
# Usage:
#   ./scripts/restore.sh path/to/db-STAMP.sql.gz path/to/media-STAMP.tar.gz
#
# This OVERWRITES the running stack's database and media/. Rehearse it first against a
# throwaway project (a second checkout, or COMPOSE_PROJECT_NAME=ekg-restore-test docker
# compose up -d db) before trusting it against the real one -- an untested restore is a
# hope, not a backup. See README, "Despliegue", "Copias de seguridad".
set -euo pipefail

DB_DUMP="${1:?Usage: restore.sh <db-dump.sql.gz> <media.tar.gz>}"
MEDIA_ARCHIVE="${2:?Usage: restore.sh <db-dump.sql.gz> <media.tar.gz>}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "==> Stopping api and worker (db stays up for the restore)"
docker compose stop api worker

echo "==> Restoring Postgres into ${POSTGRES_DB:-ekg}"
gunzip -c "$DB_DUMP" | docker compose exec -T db psql -U "${POSTGRES_USER:-ekg}" "${POSTGRES_DB:-ekg}"

echo "==> Restoring media/"
docker compose run --rm --no-deps api sh -c "rm -rf /data/media/* && tar xzf - -C /data/media" < "$MEDIA_ARCHIVE"

echo "==> Starting api and worker"
docker compose start api worker

echo "==> Restore finished. Confirm with: curl -f https://\$DOMAIN/healthz/"
