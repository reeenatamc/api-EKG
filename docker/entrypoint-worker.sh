#!/usr/bin/env bash
# Entrypoint for the `worker` service (docker-compose.yml). Ensures the ECGFounder
# checkpoints are present in the shared volume before taking any study off the queue, then
# runs until Docker stops it.
set -euo pipefail

: "${ECGFOUNDER_WEIGHTS_DIR:?ECGFOUNDER_WEIGHTS_DIR must be set (see .env.example)}"

echo "==> Checking ECGFounder weights in $ECGFOUNDER_WEIGHTS_DIR"
# download-ecgfounder-weights is ecg-pipeline's own scripts/download_weights.sh (copied in
# by the Dockerfile): it already skips any file that is present, so this is a no-op on every
# start after the first. Runs on every worker replica racing the same volume; a curl that
# loses the race writes to the same final filename anyway, so the worst case is a wasted
# download, not a corrupt one.
download-ecgfounder-weights

# --reclaim-stale requeues studies a dead worker left claimed as 'processing'. Safe here
# because docker-compose.yml starts every worker replica the same way -- if this ever runs
# alongside a worker fleet started some other way, that assumption is what to revisit (see
# analysis/management/commands/run_worker.py).
# ECG_DEVICE=cuda (the GPU image's default) is checked by run_worker before it takes a study:
# a container that cannot see its GPU exits with the cause instead of failing studies.
echo "==> Starting worker (ECG_DEVICE=${ECG_DEVICE:-cpu}, OMP_NUM_THREADS=${OMP_NUM_THREADS:-unset, torch default})"
exec python manage.py run_worker --reclaim-stale
