#!/usr/bin/env bash
# Push local code to VPS. One command. Handles owner/perms/restart in one shot.
# Usage:
#   ./scripts/deploy.sh             # deploy all (backend + scripts + frontend)
#   ./scripts/deploy.sh backend     # only py modules at repo root + restart services
#   ./scripts/deploy.sh scripts     # only scripts/ (no service restart)
#   ./scripts/deploy.sh frontend    # only frontend/ (no service restart)
#   ./scripts/deploy.sh caddy       # push + validate + reload Caddyfile
#   ./scripts/deploy.sh snapshot    # DO snapshot only, no code push
set -euo pipefail

HOST="${PHANTOM_SSH:-phantom}"
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_ROOT="/opt/phantom-api"
TMP_DIR="/tmp/phantom-deploy-$$"

target="${1:-all}"
deploy_backend=0
deploy_scripts=0
deploy_frontend=0
deploy_caddy=0
deploy_snapshot=0

case "$target" in
  all)       deploy_backend=1; deploy_scripts=1; deploy_frontend=1 ;;
  backend)   deploy_backend=1 ;;
  scripts)   deploy_scripts=1 ;;
  frontend)  deploy_frontend=1 ;;
  caddy)     deploy_caddy=1 ;;
  snapshot)  deploy_snapshot=1 ;;
  *)         echo "unknown target: $target (use: all|backend|scripts|frontend|caddy|snapshot)" >&2; exit 2 ;;
esac

bold() { printf "\n\033[1m▸ %s\033[0m\n" "$*"; }

if [[ $deploy_snapshot -eq 1 ]]; then
  bold "DigitalOcean snapshot"
  doctl compute droplet-action snapshot ${PHANTOM_DROPLET_ID} \
    --snapshot-name "phantom-$(date -u +%Y%m%d-%H%M)" --wait
  exit 0
fi

bold "Connect: $HOST"
ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOST" 'echo ok' >/dev/null || {
  echo "ERR: cannot reach $HOST. Run: ssh-add ~/.ssh/id_ed25519" >&2
  exit 1
}

# Stage everything in /tmp/phantom-deploy-$$ on VPS, then install atomically.
ssh "$HOST" "mkdir -p $TMP_DIR/scripts $TMP_DIR/frontend"
trap "ssh $HOST 'rm -rf $TMP_DIR' >/dev/null 2>&1 || true" EXIT

if [[ $deploy_backend -eq 1 ]]; then
  bold "Sync backend modules"
  rsync -avz \
    "$LOCAL_ROOT"/config.py \
    "$LOCAL_ROOT"/main.py \
    "$LOCAL_ROOT"/payments.py \
    "$LOCAL_ROOT"/nowpayments.py \
    "$LOCAL_ROOT"/monero_pay.py \
    "$LOCAL_ROOT"/db.py \
    "$LOCAL_ROOT"/pricing.py \
    "$LOCAL_ROOT"/catalog.py \
    "$LOCAL_ROOT"/schema.sql \
    "$HOST:$TMP_DIR/"
fi

if [[ $deploy_scripts -eq 1 ]]; then
  bold "Sync scripts"
  rsync -avz \
    "$LOCAL_ROOT"/scripts/*.py \
    "$LOCAL_ROOT"/scripts/smoke-test-rails.sh \
    "$HOST:$TMP_DIR/scripts/"
  # setup-monero-pay.sh runs on operator PC, not VPS — intentionally skipped.
fi

if [[ $deploy_frontend -eq 1 ]]; then
  bold "Sync frontend"
  rsync -avz \
    "$LOCAL_ROOT"/frontend/*.html \
    "$LOCAL_ROOT"/frontend/*.js \
    "$LOCAL_ROOT"/frontend/*.css \
    "$LOCAL_ROOT"/frontend/*.svg \
    "$LOCAL_ROOT"/frontend/*.png \
    "$LOCAL_ROOT"/frontend/*.xml \
    "$LOCAL_ROOT"/frontend/pgp.txt \
    "$LOCAL_ROOT"/frontend/robots.txt \
    "$HOST:$TMP_DIR/frontend/" 2>/dev/null || true
fi

bold "Install + restart on $HOST"
ssh "$HOST" "set -e
  if [[ $deploy_backend -eq 1 ]]; then
    sudo install -o phantom -g phantom -m 644 $TMP_DIR/*.py $REMOTE_ROOT/
    if [[ -f $TMP_DIR/schema.sql ]]; then
      sudo install -o phantom -g phantom -m 644 $TMP_DIR/schema.sql $REMOTE_ROOT/
    fi
  fi
  if [[ $deploy_scripts -eq 1 ]]; then
    sudo install -o phantom -g phantom -m 755 $TMP_DIR/scripts/*.py $REMOTE_ROOT/scripts/
    if ls $TMP_DIR/scripts/*.sh >/dev/null 2>&1; then
      sudo install -o phantom -g phantom -m 755 $TMP_DIR/scripts/*.sh $REMOTE_ROOT/scripts/
    fi
  fi
  if [[ $deploy_frontend -eq 1 ]]; then
    sudo install -o phantom -g phantom -m 644 $TMP_DIR/frontend/* $REMOTE_ROOT/frontend/
  fi
  if [[ $deploy_backend -eq 1 ]]; then
    sudo systemctl restart phantom-api phantom-poller
    sleep 2
    systemctl is-active phantom-api phantom-poller
  fi
  rm -rf $TMP_DIR
"

if [[ $deploy_caddy -eq 1 ]]; then
  bold "Push + validate Caddyfile"
  scp "$LOCAL_ROOT"/Caddyfile "$HOST:/tmp/Caddyfile.new"
  ssh "$HOST" '
    sudo install -o root -g root -m 644 /tmp/Caddyfile.new /etc/caddy/Caddyfile
    sudo caddy validate --config /etc/caddy/Caddyfile
    sudo systemctl reload caddy
    rm /tmp/Caddyfile.new
    systemctl is-active caddy
  '
fi

bold "Verify"
out=$(curl -s --max-time 10 https://api.phantom.codes/health || echo "{}")
echo "  /health: $out"
if [[ "$out" != *'"status":"ok"'* ]]; then
  echo "WARN: /health not ok. Investigate." >&2
  exit 1
fi
echo "  done."
