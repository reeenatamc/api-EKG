#!/usr/bin/env bash
# Entrypoint for the `api` service (docker-compose.yml). Prepares the database and static
# files, then hands off to gunicorn -- never runs migrations from the worker, so two
# containers starting together cannot race each other's schema changes.
set -euo pipefail

echo "==> Applying migrations"
python manage.py migrate --noinput

# Best-effort: only matters once CACHE_BACKEND is switched to DatabaseCache (see
# .env.example), and fails harmlessly with "table already exists" on every start after the
# first. A real failure to reach the database at all is not swallowed here -- migrate above
# already would have stopped the container on that.
echo "==> Ensuring the cache table exists (no-op if CACHE_BACKEND is not DatabaseCache)"
python manage.py createcachetable --noinput 2>/dev/null || true

echo "==> Collecting static files"
python manage.py collectstatic --noinput

echo "==> Starting gunicorn"
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-2}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile -
