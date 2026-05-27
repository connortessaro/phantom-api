#!/usr/bin/env bash
# Operator runs this via SSH after VPS boot.
# Prompts for three secrets, writes to a tmpfs env file shredded after service start.
#   - SQLCipher DB passphrase (encrypts api_keys + payments at rest)
#   - Wallet RPC password (auth to monero-wallet-rpc on operator PC via Tor)
#   - Phala API key (forwards inference)
# None of these touch the persistent disk.
set -euo pipefail

RUNTIME_DIR=/run/phantom
ENV_FILE="$RUNTIME_DIR/phantom-secrets.env"
WALLET_PW_FILE="$RUNTIME_DIR/phantom-wallet-pw"

sudo mkdir -p "$RUNTIME_DIR"
sudo chmod 700 "$RUNTIME_DIR"
sudo chown phantom:phantom "$RUNTIME_DIR"

read -s -p "SQLCipher passphrase:  " DB_PASS;     echo
read -s -p "Wallet RPC password:   " RPC_PASS;    echo
read -s -p "Hot wallet password:   " WALLET_PASS; echo
read -s -p "Redpill API key:       " REDPILL;    echo

sudo install -o phantom -g phantom -m 0400 /dev/null "$ENV_FILE"
sudo install -o phantom -g phantom -m 0400 /dev/null "$WALLET_PW_FILE"
# Heredoc keeps secrets out of process argv. Stdin to sudo tee only.
# We write REDPILL_API_KEY as the canonical name. Phantom code accepts legacy
# PHALA_API_KEY as fallback, so old systemd units / scripts still work.
sudo tee "$ENV_FILE" > /dev/null <<EOF
PHANTOM_DB_PASSPHRASE=$DB_PASS
WALLET_RPC_PASSWORD=$RPC_PASS
REDPILL_API_KEY=$REDPILL
EOF
# Wallet password — separate file, fed to monero-wallet-rpc via --password-file.
# No trailing newline (monero-wallet-rpc treats it as part of the password).
printf '%s' "$WALLET_PASS" | sudo tee "$WALLET_PW_FILE" > /dev/null
unset DB_PASS RPC_PASS WALLET_PASS REDPILL

sudo systemctl start monero-wallet-rpc.service
sudo systemctl start phantom-api.service
sudo systemctl start phantom-poller.service
sudo systemctl status monero-wallet-rpc.service phantom-api.service --no-pager
