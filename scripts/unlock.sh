#!/usr/bin/env bash
# Operator runs this via SSH after VPS boot. Prompts for the two secrets
# phantom needs at runtime and writes them to a tmpfs env file that the
# phantom-api service reads via systemd EnvironmentFile.
#   - SQLCipher DB passphrase (encrypts api_keys + payments at rest)
#   - Redpill API key (forwards inference)
# Neither touches persistent disk.
set -euo pipefail

RUNTIME_DIR=/run/phantom
ENV_FILE="$RUNTIME_DIR/phantom-secrets.env"

sudo mkdir -p "$RUNTIME_DIR"
sudo chmod 700 "$RUNTIME_DIR"
sudo chown phantom:phantom "$RUNTIME_DIR"

read -s -p "SQLCipher passphrase:  " DB_PASS;  echo
read -s -p "Redpill API key:       " REDPILL;  echo

sudo install -o phantom -g phantom -m 0400 /dev/null "$ENV_FILE"
# Heredoc keeps secrets out of process argv (sudo tee reads from stdin).
sudo tee "$ENV_FILE" > /dev/null <<EOF
PHANTOM_DB_PASSPHRASE=$DB_PASS
REDPILL_API_KEY=$REDPILL
EOF
unset DB_PASS REDPILL

sudo systemctl start phantom-api.service
sudo systemctl status phantom-api.service --no-pager
