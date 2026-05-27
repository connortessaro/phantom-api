#!/usr/bin/env bash
# Verify-and-deploy: pull the signed release tag, verify operator GPG signature,
# then sync to /opt/phantom-api. Refuses to deploy unverified code.
#
# Usage on VPS (as the deploy user, not root):
#   ./verify-and-deploy.sh v1.0.0
#
# Prereq:
#   - Operator GPG public key imported into VPS user's keyring:
#       gpg --import operator-public-key.asc
#       gpg --lsign-key <key-id>   # locally trust this key
#   - Git remote configured to phantom repo (https or ssh).
set -euo pipefail

TAG="${1:-}"
REPO_URL="${PHANTOM_REPO_URL:-https://github.com/YOUR/phantom.git}"
WORK_DIR="${PHANTOM_WORK_DIR:-/tmp/phantom-deploy}"
DEST="/opt/phantom-api"

if [[ -z "$TAG" ]]; then
    echo "Usage: $0 <signed-tag>" >&2
    exit 1
fi

# Fresh clone (no history reuse — defends against ref tampering).
rm -rf "$WORK_DIR"
git clone --depth=50 --branch="$TAG" "$REPO_URL" "$WORK_DIR"
cd "$WORK_DIR"

# Reject if tag is unsigned or signature fails verification.
if ! git verify-tag "$TAG" 2>&1 | grep -q "Good signature"; then
    echo "ERR: tag $TAG is not signed by a trusted GPG key — refusing to deploy" >&2
    git verify-tag "$TAG" || true
    exit 2
fi
echo "OK: $TAG signature verified"

# Sync (preserve .env, data/, runtime files).
sudo rsync -av --delete \
    --exclude=.env \
    --exclude=data/ \
    --exclude=venv/ \
    --exclude=__pycache__ \
    --exclude=.git \
    "$WORK_DIR/" "$DEST/"

sudo chown -R phantom:phantom "$DEST"
echo "OK: deployed $TAG to $DEST"
echo
echo "Next: sudo systemctl restart phantom-api phantom-poller"
echo "      (re-run scripts/unlock.sh if service didn't auto-resume)"
