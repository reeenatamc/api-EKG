# One image, two roles: docker-compose.yml runs it as `api` (gunicorn) and as `worker`
# (manage.py run_worker), differing only in the command each service passes it. Splitting
# it into two images would mean keeping two copies of the same pinned dependency stack in
# sync; this service's whole premise (see README, "Environment") is that Django and the
# pipeline share one interpreter, so the image that runs either should be the one that has
# both.
#
# The ECGFounder checkpoints (~700 MB) are NOT baked in here -- see the WEIGHTS_ROOT volume
# in docker-compose.yml and docker/entrypoint-worker.sh, which downloads them on first
# start with ecg-pipeline's own scripts/download_weights.sh. Everything else this image
# needs to run a study end to end -- Django, ecg-pipeline, and Open-ECG-Digitizer with its
# own small U-Net weights (~108 MB, git-lfs) -- is baked in, pinned by commit or tag, never
# by branch: an upstream change must not be able to change what this image runs without a
# commit here saying so.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# git + git-lfs: Open-ECG-Digitizer's own U-Net weights are LFS objects, pulled during the
# clone below. ca-certificates and curl are for the HTTPS clones and, later, the weights
# download.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git git-lfs ca-certificates curl \
    && git lfs install --skip-repo \
    && rm -rf /var/lib/apt/lists/*

# Pinned sources. Bump ECG_PIPELINE_REF and DIGITIZER_COMMIT together, the same way the CI
# workflow and this repository's README are kept in step (see .github/workflows/ci.yml):
# a reading made under a different pipeline version is not one this build's tests vouched
# for.
ARG ECG_PIPELINE_REF=v0.1.4
ARG DIGITIZER_COMMIT=963387f
ARG DIGITIZER_HOME=/opt/open-ecg-digitizer
ARG PIPELINE_SRC=/opt/ecg-pipeline-src

WORKDIR /app

# --- Python dependencies, in the order least likely to change first -----------------
# torch/torchvision from the CPU wheel index, pinned to the exact pair validated in
# README's "Environment" (torch 2.2.2, numpy 1.26, Intel macOS) -- the index keeps the
# resolver off a CUDA build it would otherwise be free to pick.
COPY docker/requirements-inference.txt ./docker/requirements-inference.txt
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.2.2 torchvision==0.17.2 \
    && pip install -r docker/requirements-inference.txt

COPY requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# --- ecg-pipeline, at the tag CI installs (see .github/workflows/ci.yml) -------------
# --no-deps: its dependencies are already pinned and installed above; letting pip resolve
# them again is how the numpy/torch pair described in README would quietly move.
RUN git clone --branch "$ECG_PIPELINE_REF" --depth 1 \
        https://github.com/reeenatamc/ecg-pipeline.git "$PIPELINE_SRC" \
    && pip install --no-deps "$PIPELINE_SRC" \
    && cp "$PIPELINE_SRC/scripts/download_weights.sh" /usr/local/bin/download-ecgfounder-weights \
    && chmod +x /usr/local/bin/download-ecgfounder-weights

# --- Open-ECG-Digitizer: cloned, pinned, patched, never installed --------------------
# Not a Python package: ecg_pipeline.digitizer runs it as an external subprocess (see that
# repository's README on why -- it is CC BY-SA 4.0 and this is the licensing boundary).
# Patches come from the ecg-pipeline checkout above, applied in the filename order the
# upstream setup_digitizer.sh itself uses (each is a diff against what the previous one
# left behind), then that checkout is discarded -- only its scripts/download step needed
# to survive past this point, and it is already copied out above.
RUN git clone https://github.com/Ahus-AIM/Open-ECG-Digitizer.git "$DIGITIZER_HOME" \
    && git -C "$DIGITIZER_HOME" checkout "$DIGITIZER_COMMIT" \
    && git -C "$DIGITIZER_HOME" lfs pull \
    && for patch in "$PIPELINE_SRC"/patches/*.patch; do \
         echo "applying $patch"; \
         git -C "$DIGITIZER_HOME" apply "$patch"; \
       done \
    && rm -rf "$PIPELINE_SRC" "$DIGITIZER_HOME/.git"

ENV OPEN_ECG_DIGITIZER_HOME=$DIGITIZER_HOME

# --- application code ----------------------------------------------------------------
COPY . .
COPY docker/entrypoint-api.sh docker/entrypoint-worker.sh /app/docker/
RUN chmod +x /app/docker/entrypoint-api.sh /app/docker/entrypoint-worker.sh

# Runs as an unprivileged user, per the deployment report's access checklist. Owns the
# digitizer checkout too: interpret_ecg.py and digitize.py write nothing there today, but
# nothing about a read-only checkout is load-bearing enough to depend on staying that way.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/media /app/work /app/staticfiles \
    && chown -R appuser:appuser /app "$DIGITIZER_HOME"
USER appuser

EXPOSE 8000
