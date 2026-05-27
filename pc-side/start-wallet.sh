#!/usr/bin/env bash
# Operator PC. Starts monero-wallet-rpc bound to localhost; Tor handles inbound.
set -euo pipefail

WALLET_DIR="$HOME/.phantom-wallet"
mkdir -p "$WALLET_DIR"

if [[ ! -f "$WALLET_DIR/rpc-password" ]]; then
    openssl rand -hex 24 > "$WALLET_DIR/rpc-password"
    chmod 600 "$WALLET_DIR/rpc-password"
    echo "Generated RPC password at $WALLET_DIR/rpc-password"
fi

monero-wallet-rpc \
    --rpc-bind-port 18083 \
    --rpc-bind-ip 127.0.0.1 \
    --rpc-login phantom:"$(cat "$WALLET_DIR/rpc-password")" \
    --wallet-file "$WALLET_DIR/phantom" \
    --password-file "$WALLET_DIR/wallet-password" \
    --daemon-address node.sethforprivacy.com:18089 \
    --trusted-daemon \
    --log-level 0 \
    --confirm-external-bind
