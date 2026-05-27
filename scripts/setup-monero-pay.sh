#!/usr/bin/env bash
# MoneroPay setup helper for the OPERATOR machine.
#
# Builds MoneroPay from source via `go install` (works on Linux + macOS).
# Generates the systemd unit (Linux) or printable launchctl plist (macOS).
# Prints torrc hidden service snippet + VPS .env block at the end.
#
# Auth model: MoneroPay does NOT sign callbacks. Phantom authenticates each
# callback via per-payment URL path token (the 16-byte phantom payment_id).
# No shared secret needed — daemon doesn't know the token ahead of time.
#
# Prereqs (script will check):
#   - Go toolchain
#   - monero-wallet-rpc on PATH (or full path provided)
#   - Tor (brew on macOS / apt on Linux)
#
# Usage:
#   ./scripts/setup-monero-pay.sh
#
# Env (optional overrides):
#   WALLET_RPC_PORT     default: 18083
#   MONEROPAY_PORT      default: 5000
#   GOBIN               default: $HOME/go/bin

set -euo pipefail

WALLET_RPC_PORT="${WALLET_RPC_PORT:-18083}"
MONEROPAY_PORT="${MONEROPAY_PORT:-5000}"
GOBIN_DIR="${GOBIN:-$HOME/go/bin}"

if [[ -t 1 ]]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; BOLD=$'\033[1m'; RST=$'\033[0m'
else
    GREEN=""; RED=""; YEL=""; BOLD=""; RST=""
fi
log()  { echo "${BOLD}::${RST} $*"; }
ok()   { echo "  ${GREEN}OK${RST}   $*"; }
warn() { echo "  ${YEL}WARN${RST} $*"; }
fail() { echo "  ${RED}FAIL${RST} $*" >&2; exit 1; }

# ─── preflight ────────────────────────────────────────────────────────────────
log "preflight"

case "$(uname -s)" in
    Linux*)  PLATFORM=linux ;;
    Darwin*) PLATFORM=darwin ;;
    *) fail "unsupported OS: $(uname -s)" ;;
esac
ok "platform: $PLATFORM/$(uname -m)"

if ! command -v go >/dev/null 2>&1; then
    if [[ "$PLATFORM" == "darwin" ]]; then
        fail "Go missing — install via: brew install go"
    else
        fail "Go missing — install via: apt install golang-go (or download from go.dev/dl)"
    fi
fi
ok "go $(go version | awk '{print $3}')"

if ! command -v monero-wallet-rpc >/dev/null 2>&1; then
    warn "monero-wallet-rpc not on PATH — make sure it's running before starting MoneroPay"
else
    ok "monero-wallet-rpc on PATH"
fi

if ! command -v tor >/dev/null 2>&1; then
    if [[ "$PLATFORM" == "darwin" ]]; then
        warn "tor missing — install via: brew install tor"
    else
        warn "tor missing — install via: apt install tor"
    fi
else
    ok "tor present"
fi

# ─── build moneropay ──────────────────────────────────────────────────────────
log "build moneropay"

if [[ -x "$GOBIN_DIR/moneropay" ]]; then
    EXISTING_VER=$("$GOBIN_DIR/moneropay" --help 2>&1 | head -1 || echo "unknown")
    ok "moneropay already built at $GOBIN_DIR/moneropay"
else
    # Module path is GitLab even though source mirrors to GitHub.
    if go install gitlab.com/moneropay/moneropay/v2/cmd/moneropay@latest 2>&1 | tail -5; then
        ok "built $GOBIN_DIR/moneropay"
    else
        fail "go install failed"
    fi
fi

# ─── platform-specific service unit ───────────────────────────────────────────
log "service unit"

if [[ "$PLATFORM" == "linux" ]]; then
    # Need root for systemd unit install. Re-exec via sudo if not root.
    if [[ $EUID -ne 0 ]]; then
        warn "Linux systemd install needs root — skipping unit install."
        warn "Re-run with: sudo $0"
    else
        cat > /etc/systemd/system/moneropay.service <<EOF
[Unit]
Description=MoneroPay payment processor for phantom
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SUDO_USER
ExecStart=$GOBIN_DIR/moneropay \\
  -rpc-address=http://127.0.0.1:$WALLET_RPC_PORT/json_rpc \\
  -rpc-username=phantom \\
  -rpc-password=\${WALLET_RPC_PASSWORD} \\
  -bind=127.0.0.1:$MONEROPAY_PORT \\
  -sqlite=$HOME/.moneropay/moneropay.db
EnvironmentFile=/etc/phantom-secrets.env
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable moneropay.service >/dev/null
        ok "systemd unit installed + enabled"
        ok "start it: systemctl start moneropay"
    fi
elif [[ "$PLATFORM" == "darwin" ]]; then
    cat <<EOF
On macOS, run MoneroPay in a terminal (or wrap with launchctl later):

  mkdir -p ~/.moneropay
  $GOBIN_DIR/moneropay \\
    -rpc-address=http://127.0.0.1:$WALLET_RPC_PORT/json_rpc \\
    -rpc-username=phantom \\
    -rpc-password=YOUR_WALLET_RPC_PASSWORD \\
    -bind=127.0.0.1:$MONEROPAY_PORT \\
    -sqlite=\$HOME/.moneropay/moneropay.db

Replace YOUR_WALLET_RPC_PASSWORD with the value you used for --rpc-login on
monero-wallet-rpc.

EOF
fi

# ─── Tor hidden service hint ──────────────────────────────────────────────────
log "tor hidden service"

if [[ "$PLATFORM" == "darwin" ]]; then
    TORRC="/opt/homebrew/etc/tor/torrc"
else
    TORRC="/etc/tor/torrc"
fi

cat <<EOF

Append to $TORRC:

  HiddenServiceDir /var/lib/tor/moneropay/
  HiddenServicePort $MONEROPAY_PORT 127.0.0.1:$MONEROPAY_PORT

Then reload tor:

  # macOS (brew):  brew services restart tor
  # Linux:         sudo systemctl reload tor

After ~30 seconds, read the .onion hostname:

  sudo cat /var/lib/tor/moneropay/hostname

EOF

# ─── output env block for VPS .env ────────────────────────────────────────────
log "VPS .env block — paste into /opt/phantom-api/.env on the VPS"

cat <<EOF

# ─── Hybrid rails ──────────────────────────────────────────────────────────
PAYMENT_PROVIDER=hybrid
MONEROPAY_URL=http://REPLACE-WITH-ONION-FROM-ABOVE.onion:$MONEROPAY_PORT

EOF

ok "done. next:"
echo "  1. start monero-wallet-rpc (you said it's already configured for phantom-hot)"
echo "  2. start moneropay (see launch command above)"
echo "  3. add hidden service to torrc, reload tor"
echo "  4. paste .env block on VPS"
echo "  5. sudo systemctl restart phantom-api"
echo "  6. API_BASE=https://api.phantom.codes ./scripts/smoke-test-rails.sh"
