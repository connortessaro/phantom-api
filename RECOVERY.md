# Disaster Recovery — Phantom

Checklists for when things go wrong. Read top-to-bottom under pressure. Each scenario assumes you have:
- DigitalOcean account access (cold credentials, paper or vault)
- SSH keys (cold copy in vault, plus laptop)
- SQLCipher passphrase (memorized + paper backup in vault)
- Wallet seed phrase (paper backup)
- Phala API key (cold copy in vault)
- Tor hidden service `hs_ed25519_secret_key` file (cold copy in vault — losing this loses the onion address)

If any of these are missing, fix that first. Pre-launch checklist at the bottom.

---

## 1. VPS dead (hardware fail, accidental destroy, DigitalOcean account locked)

**Symptoms:** `api.phantom.codes` and `phantom.codes` 5xx/timeout. `ssh phantom` connection refused.

**Steps:**

1. Check DigitalOcean status page first — could be DC-wide outage, just wait.
2. If droplet truly gone, restore from most recent snapshot:
   ```bash
   doctl compute snapshot list --format ID,Name,CreatedAt | head -5
   doctl compute droplet create phantom \
     --image <snapshot-id> --region nyc3 --size s-1vcpu-2gb \
     --ssh-keys <your-ssh-key-id> --wait
   ```
3. Update DNS A records (`phantom.codes`, `api.phantom.codes`) at registrar to new public IP. TTL was 300s, propagation ~5 min.
4. SSH into new droplet, `./scripts/unlock.sh` to load SQLCipher passphrase + Phala API key + wallet RPC password into tmpfs.
5. Start services: `sudo systemctl start phantom-api phantom-poller`.
6. Restore Tor hidden service key so onion stays same:
   ```bash
   # On new VPS, as root:
   sudo systemctl stop tor
   # Copy your cold-vaulted hs_ed25519_secret_key + hs_ed25519_public_key + hostname
   # to /var/lib/tor/phantom_hidden_service/
   sudo chown -R debian-tor:debian-tor /var/lib/tor/phantom_hidden_service
   sudo chmod 700 /var/lib/tor/phantom_hidden_service
   sudo chmod 600 /var/lib/tor/phantom_hidden_service/*
   sudo systemctl start tor
   ```
7. Verify: `curl https://api.phantom.codes/health` returns `ok`, plus the same onion address resolves.

**Estimated downtime:** 15-30 min if snapshot is < 24h old.

---

## 2. DB corrupt or data dir wiped (recoverable from backup)

**Symptoms:** Service starts but `/v1/key/balance` returns 404 for every known key, OR `phantom-api.service` fails to start with sqlcipher I/O error.

**Steps:**

1. Stop service: `sudo systemctl stop phantom-api phantom-poller`.
2. Move bad DB aside (don't delete — forensic value):
   ```bash
   sudo mv /opt/phantom-api/data/phantom.db{,-corrupt-$(date +%s)}
   ```
3. List backups:
   ```bash
   sudo ls -lt /opt/phantom-api/data/backups/ | head -5
   ```
4. Pick most recent. Copy in place:
   ```bash
   sudo cp /opt/phantom-api/data/backups/phantom-<latest>.db \
           /opt/phantom-api/data/phantom.db
   sudo chown phantom:phantom /opt/phantom-api/data/phantom.db
   sudo chmod 600 /opt/phantom-api/data/phantom.db
   ```
5. Restart: `sudo systemctl start phantom-api phantom-poller`.
6. Verify with a test purchase + key balance lookup.

**Data loss window:** up to 1 hour (backups run hourly at HH:05). Any keys issued between last backup and corruption are lost. Customers see "invalid key" — refund out of band if you can.

---

## 3. Wallet RPC unreachable

**Symptoms:** `/v1/health` shows `"wallet": false`. `/v1/purchase` returns 503 "upstream unavailable" for new buys. Existing keys still work.

**Steps:**

1. Confirm operator PC is on and Tor is running:
   ```bash
   ssh your-user@operator-pc 'brew services list | grep tor'
   ```
2. If wallet-rpc daemon died, restart:
   ```bash
   monero-wallet-rpc \
     --wallet-file $HOME/.phantom-wallet/phantom \
     --rpc-bind-port 18083 \
     --rpc-login phantom:<password> \
     --daemon-address node.community.rino.io:18081 \
     --tx-notify '...' --confirm-external-bind --non-interactive &
   ```
3. Verify reachable via Tor:
   ```bash
   curl --socks5-hostname 127.0.0.1:9050 --digest -u 'phantom:<password>' \
     http://<onion>:18083/json_rpc \
     -d '{"jsonrpc":"2.0","id":"0","method":"get_version"}'
   ```
4. VPS will recover on next poller tick (~30s) once RPC responds.

**If operator PC is dead:** restore wallet from seed on a new machine, re-publish onion to match same `.onion` address (Tor hidden service key needed). Update `WALLET_RPC_HOST` in VPS `/run/phantom/phantom-secrets.env` if address changed.

---

## 4. Phala / Redpill API down

**Symptoms:** `/v1/chat/completions` returns 503 "upstream unavailable" for all keys.

**Steps:**

1. Confirm with: `curl -H "Authorization: Bearer $PHALA_API_KEY" https://api.redpill.ai/v1/models | head -5`. If their API returns 5xx, it's their outage.
2. Post a notice in the docs FAQ or trust section. Customers see 503 — they understand.
3. If extended (> 4 hours), consider issuing replacement keys with extended validity to affected customers as goodwill.
4. Do not switch to a non-TEE provider as fallback. That breaks the security claim and the AUP.

**No action you can take on Phala's side.** Wait it out.

---

## 5. Lost SQLCipher passphrase

**Symptoms:** Service won't start after VPS reboot. Cannot decrypt DB. No backups will open either.

**Steps:**

1. Check paper backup of passphrase in vault.
2. If truly lost: all customer data is unrecoverable. SQLCipher AES-256, no backdoor.
3. Notify customers via posted notice on landing page that the service is being rebuilt; existing keys are invalid.
4. Re-init empty DB w/ new passphrase. Refund out of band if possible (but you don't have customer return addresses by design).

**This is the worst case.** Paper-back the passphrase. Keep two copies in geographically separate locations.

---

## 6. Lost SSH access (key + paper backup gone)

**Steps:**

1. DigitalOcean web console → droplet → "Console" → boot into recovery mode.
2. Mount root partition, add new SSH key to `/root/.ssh/authorized_keys` and `/home/your-user/.ssh/authorized_keys`.
3. Reboot. SSH back in with the new key.
4. Re-disable root login per `scripts/setup-vps.sh` SSH hardening if you used root for the recovery shim.

---

## 7. Tor hidden service key compromised (onion address must change)

**Symptoms:** You suspect the `hs_ed25519_secret_key` was exfiltrated (VPS root-compromised). You can no longer trust the existing onion address — anyone with that key can impersonate the service.

**Steps:**

1. Stop Tor: `sudo systemctl stop tor`.
2. Delete `/var/lib/tor/phantom_hidden_service/`.
3. Start Tor: `sudo systemctl start tor`. New onion address generated automatically.
4. Update onion address in all 3 places:
   - `/etc/caddy/Caddyfile` (the onion vhost block)
   - All `Onion-Location` headers in Caddyfile
   - `frontend/index.html`, `frontend/docs.html`, `frontend/terms.html` footer links
5. Reload Caddy: `sudo systemctl reload caddy`.
6. Announce new onion address via PGP-signed message on phantom.codes landing.

**Cold-vault the new key once stable.**

---

## 8. Operator PC dead / wallet seed lost

**If you have the seed (paper):** restore wallet on any Monero install w/ same seed. All historical funds + subaddresses recover deterministically. Update operator-side scripts to point at new wallet RPC. Funds in cold wallet are unaffected.

**If you don't have the seed:** funds in hot wallet are unrecoverable. Cold wallet may still be safe if its seed is separately backed up. Pre-launch action: write down hot + cold seeds on paper, store separately from each other and from passphrase.

---

## Pre-launch checklist

Confirm each of these BEFORE accepting first customer payment:

- [ ] SQLCipher passphrase paper-backed in two locations
- [ ] Hot wallet seed paper-backed
- [ ] Cold wallet seed paper-backed, separate from hot
- [ ] Tor hidden service `hs_ed25519_secret_key` copied to cold storage
- [ ] DigitalOcean snapshot exists within last 24h (use `doctl compute snapshot list`)
- [ ] `scripts/backup-db.py` runs hourly via cron (`crontab -l` as phantom user)
- [ ] At least one restore test completed (see this file, scenario #2)
- [ ] DNS records: who is the registrar, do you have login credentials cold-vaulted
- [ ] Phala API key paper-backed
- [ ] Wallet RPC credentials paper-backed
- [ ] ntfy.sh topic name recorded somewhere outside the VPS
- [ ] PGP private key passphrase paper-backed (for /pgp.txt key)
- [ ] You've practiced scenario #6 once (lost SSH key recovery via DO console)
