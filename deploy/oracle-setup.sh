#!/usr/bin/env bash
# Puts the whole piloto on a fresh Oracle Cloud Always Free VM (Ubuntu 24.04, Ampere A1)
# with the analysis on Modal's GPUs. Run it once, on the VM, as the default `ubuntu` user:
#
#   curl -fsSL https://raw.githubusercontent.com/reeenatamc/api-EKG/<rama>/deploy/oracle-setup.sh -o setup.sh
#   DOMAIN=ekgreader.duckdns.org DUCKDNS_TOKEN=... MODAL_TOKEN_ID=ak-... MODAL_TOKEN_SECRET=as-... \
#     bash setup.sh
#
# Safe to run again: it skips what is already done and never overwrites an existing .env
# (so the database password and SECRET_KEY survive). Before running it, in the Oracle
# console: the subnet's security list must allow TCP 80 and 443 from 0.0.0.0/0.
#
# What it does: Docker, the VM's own firewall (Oracle's Ubuntu image rejects everything but
# SSH in iptables, on top of the console's security list), 4 GB of swap for the image build,
# DuckDNS kept pointing at this VM, the repository, a production .env, the Modal app
# deployed, and `docker compose up`.
set -euo pipefail

: "${DOMAIN:?DOMAIN, e.g. ekgreader.duckdns.org}"
: "${MODAL_TOKEN_ID:?MODAL_TOKEN_ID from modal.com, Settings, API Tokens}"
: "${MODAL_TOKEN_SECRET:?MODAL_TOKEN_SECRET from the same place}"
BRANCH="${BRANCH:-main}"
REPO="${REPO:-https://github.com/reeenatamc/api-EKG.git}"
DIR="${DIR:-$HOME/api-EKG}"

step() { printf '\n==> %s\n' "$*"; }

step "Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi
DOCKER="sudo docker"

step "Firewall of the VM: open 80 and 443"
for port in 80 443; do
  if ! sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 1 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  fi
done
if command -v netfilter-persistent >/dev/null; then
  sudo netfilter-persistent save
fi

step "Swap (the image build peaks above what a small VM has free)"
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

if [ -n "${DUCKDNS_TOKEN:-}" ] && [[ "$DOMAIN" == *.duckdns.org ]]; then
  step "DuckDNS: $DOMAIN -> this VM, refreshed every 5 minutes"
  sub="${DOMAIN%.duckdns.org}"
  url="https://www.duckdns.org/update?domains=${sub}&token=${DUCKDNS_TOKEN}&ip="
  curl -fsS "$url" && echo
  ( crontab -l 2>/dev/null | grep -v duckdns.org; echo "*/5 * * * * curl -fsS '$url' >/dev/null 2>&1" ) | crontab -
fi

step "Repository ($BRANCH)"
sudo apt-get install -y -q git python3-venv >/dev/null
if [ ! -d "$DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO" "$DIR"
else
  git -C "$DIR" fetch -q origin && git -C "$DIR" checkout -q "$BRANCH" && git -C "$DIR" pull -q --ff-only
fi
cd "$DIR"

step ".env"
if [ ! -f .env ]; then
  secret() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
  umask 077
  cat > .env <<EOF
DEBUG=0
SECRET_KEY=$(secret 50)
DOMAIN=$DOMAIN
ALLOWED_HOSTS=$DOMAIN
CSRF_TRUSTED_ORIGINS=https://$DOMAIN
DATABASE_ENGINE=postgresql
POSTGRES_DB=ekg
POSTGRES_USER=ekg
POSTGRES_PASSWORD=$(secret 24)
POSTGRES_HOST=db
ECGFOUNDER_WEIGHTS_DIR=/data/weights
ECG_PATHWAY=rhythm
ECG_TOP_K=10
ECG_BACKEND=modal
ECG_WORKER_THREADS=10
WORKER_REPLICAS=1
WORKER_MEMORY_LIMIT=2g
GUNICORN_WORKERS=2
GUNICORN_WORKER_CLASS=gthread
GUNICORN_THREADS=4
MODAL_TOKEN_ID=$MODAL_TOKEN_ID
MODAL_TOKEN_SECRET=$MODAL_TOKEN_SECRET
EOF
  chmod 600 .env
  echo "written (secrets generated here, never printed)"
else
  echo "already there, left as is"
fi

step "Modal: deploy the GPU app (the image builds on Modal's side, ~10 min the first time)"
python3 -m venv "$HOME/.modal-venv"
"$HOME/.modal-venv/bin/pip" install -q "modal>=1.0,<2.0"
MODAL_TOKEN_ID="$MODAL_TOKEN_ID" MODAL_TOKEN_SECRET="$MODAL_TOKEN_SECRET" \
  "$HOME/.modal-venv/bin/modal" deploy gpu/modal_app.py

step "Build and start (the first build takes a while on 2 ARM cores)"
$DOCKER compose build
$DOCKER compose up -d

step "Waiting for https://$DOMAIN/healthz/"
for _ in $(seq 1 60); do
  if curl -fsS "https://$DOMAIN/healthz/" >/dev/null 2>&1; then
    echo "OK: https://$DOMAIN/healthz/ answers"
    $DOCKER compose ps
    echo
    echo "Next, once: $DOCKER compose exec api python manage.py createsuperuser"
    exit 0
  fi
  sleep 10
done
echo "Not answering yet. Look at: $DOCKER compose logs --tail 50 caddy api worker" >&2
exit 1
