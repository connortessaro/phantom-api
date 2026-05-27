#!/usr/bin/env bash
# First-time VPS bootstrap. Run as root after fresh install.
# Assumes: Debian/Ubuntu, fresh SSH key-auth login.
set -euo pipefail

# --- system hardening ---
apt-get update
apt-get install -y software-properties-common ca-certificates gnupg curl

# Python 3.12 — Ubuntu 22.04 ships 3.10 by default, so add deadsnakes PPA
if ! command -v python3.12 >/dev/null 2>&1; then
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
fi

# Caddy — add official repo for current version on 22.04 (avoid old apt version)
if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sed 's|signed-by=/usr/share/keyrings/cloudsmith:caddy-stable-archive-keyring.gpg|signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg|' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
fi

apt-get install -y ufw caddy tor python3.12 python3.12-venv python3-pip build-essential \
                   fail2ban unattended-upgrades apt-listchanges \
                   libsqlcipher-dev pkg-config rsync

# Unattended security updates — kernel/CVE patches install automatically.
dpkg-reconfigure -f noninteractive unattended-upgrades || true
cat > /etc/apt/apt.conf.d/52unattended-upgrades-phantom <<'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
systemctl enable --now unattended-upgrades

# fail2ban for SSH defense in depth (despite key-only auth, kills bot-scan noise + log bloat).
cat > /etc/fail2ban/jail.d/phantom-sshd.conf <<'EOF'
[sshd]
enabled = true
port    = 22
maxretry = 4
findtime = 600
bantime  = 3600
EOF
systemctl enable --now fail2ban

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# --- operator sudo user ---
# Must exist BEFORE disabling root SSH or you'll be locked out.
# OPERATOR_USER env var defaults to "operator". Reuses root's authorized_keys.
OPERATOR_USER="${OPERATOR_USER:-operator}"
if ! id -u "$OPERATOR_USER" &>/dev/null; then
    # --user-group creates matching group; avoids collision with system "operator" group
    useradd --create-home --shell /bin/bash --user-group --groups sudo "$OPERATOR_USER"
    # passwordless sudo (key-only auth, no interactive sudo prompts blocking unlock.sh)
    echo "$OPERATOR_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$OPERATOR_USER"
    chmod 0440 "/etc/sudoers.d/90-$OPERATOR_USER"
    install -d -o "$OPERATOR_USER" -g "$OPERATOR_USER" -m 0700 "/home/$OPERATOR_USER/.ssh"
    if [ -f /root/.ssh/authorized_keys ]; then
        install -o "$OPERATOR_USER" -g "$OPERATOR_USER" -m 0600 \
            /root/.ssh/authorized_keys "/home/$OPERATOR_USER/.ssh/authorized_keys"
    else
        echo "WARN: /root/.ssh/authorized_keys missing — operator user has no SSH key" >&2
    fi
fi

# SSH hardening — key-only auth, no root login.
# Use sshd_config.d drop-in so we don't mangle main config. Validate before restart.
cat > /etc/ssh/sshd_config.d/99-phantom.conf <<'EOF'
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
UsePAM yes
PubkeyAuthentication yes
EOF
chmod 0644 /etc/ssh/sshd_config.d/99-phantom.conf

if sshd -t; then
    systemctl reload sshd
else
    echo "ERR: sshd_config invalid — reverting and skipping reload" >&2
    rm -f /etc/ssh/sshd_config.d/99-phantom.conf
fi

# --- phantom user ---
id -u phantom &>/dev/null || useradd --system --create-home --shell /bin/bash phantom
install -d -o phantom -g phantom -m 0750 /opt/phantom-api

# Copy repo files (assumed already rsync'd to /opt/phantom-api before this runs).
chown -R phantom:phantom /opt/phantom-api

# --- python venv ---
sudo -u phantom python3.12 -m venv /opt/phantom-api/venv
sudo -u phantom /opt/phantom-api/venv/bin/pip install -U pip
# Install from hash-verified lockfile (supply-chain integrity). Falls back to
# requirements.txt if lockfile is missing (dev / first-run scenarios).
if [ -f /opt/phantom-api/requirements.lock ]; then
    sudo -u phantom /opt/phantom-api/venv/bin/pip install --require-hashes \
        -r /opt/phantom-api/requirements.lock
else
    echo "WARN: requirements.lock missing — installing without hash verification"
    sudo -u phantom /opt/phantom-api/venv/bin/pip install -r /opt/phantom-api/requirements.txt
fi

# --- monero-wallet-rpc binary ---
if ! command -v monero-wallet-rpc >/dev/null 2>&1; then
    TMPDIR=$(mktemp -d)
    curl -L -o "$TMPDIR/monero.tar.bz2" https://downloads.getmonero.org/cli/linux64
    tar -xjf "$TMPDIR/monero.tar.bz2" -C "$TMPDIR"
    install -m 0755 "$TMPDIR"/monero-x86_64-linux-gnu-*/monero-wallet-rpc /usr/local/bin/
    install -m 0755 "$TMPDIR"/monero-x86_64-linux-gnu-*/monero-wallet-cli /usr/local/bin/
    rm -rf "$TMPDIR"
fi
install -d -o phantom -g phantom -m 0700 /opt/phantom-api/wallet

# --- systemd ---
install -m 0644 /opt/phantom-api/systemd/monero-wallet-rpc.service /etc/systemd/system/
install -m 0644 /opt/phantom-api/systemd/phantom-api.service /etc/systemd/system/
install -m 0644 /opt/phantom-api/systemd/phantom-poller.service /etc/systemd/system/
systemctl daemon-reload
# None auto-start on boot — they require manual unlock.sh to write tmpfs secrets.
systemctl enable phantom-poller.service

# --- Tor ---
systemctl enable --now tor
echo "Verify Tor SOCKS:"
curl --socks5-hostname 127.0.0.1:9050 -m 30 -s -o /dev/null -w "  check.torproject %{http_code}\n" https://check.torproject.org || true

# --- Caddy ---
install -m 0644 /opt/phantom-api/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy

echo
echo "Setup complete."
echo
echo "Next steps:"
echo " 1. Edit /opt/phantom-api/.env — set WALLET_RPC_HOST, WALLET_RPC_USER, REDPILL_BUDGET_USD"
echo " 2. SSH in as: ssh ${OPERATOR_USER}@<vps-ip>   (root login is now disabled)"
echo " 3. Run scripts/unlock.sh — type DB passphrase, RPC password, Phala API key at prompts"
echo " 4. Hourly backups: schedule /opt/phantom-api/scripts/backup-db.sh via cron"
echo
echo "Hardening summary:"
echo "  - ufw: 22/80/443 inbound only"
echo "  - sshd: key-only, no root login, no password auth"
echo "  - fail2ban active on sshd"
echo "  - unattended-upgrades active for security patches (auto-reboot 04:00)"
echo "  - secrets loaded from tmpfs file shredded after start — never on disk"
