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
# gthread rather than the sync default: a sync worker blocks on the whole request,
# including the time it takes the client to finish sending the body, so one slow upload
# (a phone on classroom wifi) ties up an entire process for as long as the transfer takes.
# A thread only holds its own socket read; while it waits on I/O it releases the GIL, so
# GUNICORN_THREADS lets each of the few worker processes this machine can afford (they
# share it with two 6 GB analysis workers, see docker-compose.yml) serve several slow
# uploads at once instead of one. See .env.example and README, "Despliegue".
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --worker-class "${GUNICORN_WORKER_CLASS:-gthread}" \
    --workers "${GUNICORN_WORKERS:-2}" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile -
