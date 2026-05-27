# Phantom Runbook

Operational checklist. Pre-launch, deploy, day-2 ops.

---

## Pre-launch checklist

### 1. Operator PC wallet (mainnet)

- [ ] Generate fresh **mainnet** wallet on always-on machine (Raspberry Pi 4/5 ideal). Seed phrase on paper, in two physical locations.
- [ ] Funded with $0 initially. Receives only from customers.
- [ ] `monerod` synced (~80 GB mainnet, can prune to ~35 GB).
- [ ] Tor installed, hidden service configured for `monero-wallet-rpc` on port 18083. Save `.onion` hostname.
- [ ] `monero-wallet-rpc` starts at boot. `--rpc-bind-ip 127.0.0.1`, `--rpc-login phantom:<rpc-pass>`, `--password-file` (NOT `--password`).
- [ ] Verify reachable via Tor: `curl --socks5-hostname 127.0.0.1:9050 --digest -u phantom:<pass> http://<onion>:18083/json_rpc -d '{"jsonrpc":"2.0","id":"0","method":"get_version"}'`
- [ ] `scripts/sweep.py` cron set (e.g. nightly): move balance above $500 worth to cold address.

### 2. VPS

- [ ] Provisioned (Hetzner CCX12+ / OVH / BuyVM — crypto payment if possible).
- [ ] LUKS encryption on data partition OR full root.
- [ ] Regular sudo user with SSH key in place; root login will be disabled.
- [ ] DNS pointed at VPS IP (Njalla / 1984 / Porkbun privacy-friendly registrars).
- [ ] Repo deployed via `scripts/verify-and-deploy.sh <tag>` — verifies operator GPG signature before sync. Refuses unsigned releases. Operator imports their own GPG public key first: `gpg --import operator-public-key.asc && gpg --lsign-key <key-id>`.
- [ ] Run `./scripts/setup-vps.sh` as root one time. Installs Caddy, Tor, ufw, fail2ban, unattended-upgrades, sets up phantom user, systemd units.
- [ ] Edit `/opt/phantom-api/.env`:
  - `WALLET_ONION=<your.onion>`
  - `WALLET_RPC_USER=phantom`
  - `TOR_SOCKS_URL=socks5://127.0.0.1:9050`
  - `DB_PATH=/opt/phantom-api/data/phantom.db`
  - `PHALA_BUDGET_USD=<your Phala balance × 0.5>`
  - `CUSTOM_MIN_USD=1`
  - `CUSTOM_MAX_USD=1000`
- [ ] Caddyfile uses `phantom.codes` (apex frontend + API) and `api.phantom.codes` (API-only). Edit both blocks if domain changes.

### 3. Phala account

- [ ] Account funded with at least $500 (covers expected outstanding credit + buffer).
- [ ] API key generated (`phak_…`). Do NOT commit, do NOT put in .env.

### 4. First boot (unlock)

- [ ] SSH in as sudo user.
- [ ] Run `cd /opt/phantom-api && ./scripts/unlock.sh`. Type three secrets at prompts:
  - SQLCipher DB passphrase (generate via `openssl rand -hex 32`)
  - Wallet RPC password (matches operator PC `--rpc-login`)
  - Phala API key
- [ ] `systemctl status phantom-api phantom-poller` — both `active (running)`.
- [ ] `shred -u /run/phantom/phantom-secrets.env` after confirming both services started.
- [ ] Visit `https://phantom.codes/health` → expect `{"status":"ok"}`.
- [ ] Visit `https://phantom.codes/v1/models` → expect 24 models.

### 5. End-to-end mainnet test ($5)

- [ ] From a clean browser (Tor or otherwise), buy `$1` custom amount.
- [ ] Send mainnet XMR from a personal wallet to the displayed subaddress.
- [ ] Watch status: `pending → confirming → completed` (~20 min).
- [ ] Receive `sk-…` key once. Test chat completion against `phala/qwen-2.5-7b-instruct`.
- [ ] Confirm credit decremented.

### 6. Security checklist (HANDOFF.md:858)

Walk through each item before public launch.

---

## Day-2 ops

### Reboots / wake from sleep

VPS reboot → `phantom-api.service` does NOT auto-start (waits for unlock).

Operator must:
1. SSH in.
2. Run `scripts/unlock.sh` → type three secrets.
3. Verify with `systemctl status phantom-api`.

Customers see 503 between boot and unlock. Set up an uptime monitor (e.g. UptimeRobot Free, NodePing) pinging `/health`. Alert by phone push, no email.

### Operator PC offline / sleep

If wallet host (Pi at home) sleeps or loses internet:
- Existing customers with credit: **chat works fine** (doesn't touch wallet).
- New purchases: `/v1/purchase` fails with 503 (can't reach `.onion`).
- Pending payments: stay pending until wallet returns. Eventually expire after 60 min.

Mitigations:
- UPS on the Pi.
- Pi over wired ethernet, not Wi-Fi.
- Status banner on site reading wallet liveness (future work).

### Phala balance monitoring

Manual: check Phala dashboard weekly. Top up when approaching `PHALA_BUDGET_USD`.

Automated: scrape Phala usage (when their `/v1/usage` works), alert when balance < 2× outstanding customer credit.

```bash
# rough query — adjust to match db schema
sqlite3 /opt/phantom-api/data/phantom.db \
  "SELECT SUM(credit_balance) FROM api_keys WHERE is_active=1"
```

Compare to Phala balance. Refuse new sales by raising `PHALA_BUDGET_USD` ceiling DOWN if necessary.

### Sweep schedule

Run on operator PC weekly minimum:
```bash
COLD_ADDRESS=<your cold mainnet address> \
WALLET_RPC_PASSWORD=<your rpc pass> \
SWEEP_THRESHOLD_XMR=2 \
python /opt/phantom-api/scripts/sweep.py
```

Keeps hot wallet float capped. Loss exposure limited.

### Backups

`scripts/backup-db.sh` should run hourly via cron:
```cron
0 * * * * phantom /opt/phantom-api/scripts/backup-db.sh >> /opt/phantom-api/data/backup.log 2>&1
```

Verify: `ls -la /opt/phantom-api/data/backups/` shows hourly snapshots.

Test restore quarterly: copy a backup, open with SQLCipher CLI + same passphrase, verify schema.

### Unattended-upgrades reboots

`setup-vps.sh` configures automatic reboot at 04:00 UTC if a kernel security update needs it. This will:
1. Reboot the VPS.
2. `phantom-poller` auto-starts (no DB unlock needed for poller? — wait, IT NEEDS DB ACCESS. Same unlock flow applies.).

**Fix:** poller can't run before DB unlock. Either disable `Automatic-Reboot` and patch manually, OR change the poller unit to also wait for `phantom-secrets.env`. Decide before launch.

### Key rotation incidents

Customer reports leak:
- Have them hit `POST /v1/key/rotate` with their current key → new key returned, old deactivated.
- If they no longer have the old key → no recovery. Document clearly on docs page.

### Phala outage

Customers see `503 upstream unavailable`. Credit NOT decremented (verified in `M9` fix).

Page yourself if `> X%` of recent requests return 503. Add monitoring.

### XMR price volatility

Bundle prices are USD-anchored, converted to XMR at purchase time using Kraken ticker. XMR/USD pricing cached 5 min in `pricing.py`.

If XMR price moves 20%+ between sale and your conversion to USD/USDT:
- You lose margin on the float
- Convert ~50% of inbound XMR to USDT weekly to bound exposure

### Refund requests

Default response: "Crypto refunds require a return address we don't store. No refunds. Sorry."

If you choose to refund anyway:
- Ask for refund address out of band (Signal, Session, XMR.to)
- Send manually via `monero-wallet-cli`
- Mark the payment row as `refunded` (add column if needed)

---

## Incident playbooks

### "Service is down — health check failing"

1. SSH in.
2. `systemctl status phantom-api phantom-poller` — what's failed?
3. `journalctl -u phantom-api --since '5 min ago'` — error message?
4. Most common: VPS rebooted, needs unlock. Run `scripts/unlock.sh`.
5. Next most common: DB locked. Restart with `systemctl restart phantom-api`.

### "Customer says key doesn't work"

1. Can't help directly — we don't know which key is theirs.
2. Reply: "Check `GET /v1/key/balance` with your key. If 401, key is invalid. If `is_active: false`, the key was rotated. Otherwise, check `credit_balance_usd` — if 0, you've used all credit."

### "Phala account suspended"

Catastrophic. All chat completions fail.
1. Email Phala support.
2. Service down until resolved.
3. Outstanding customer credit is now irredeemable. Decide whether to comp users from personal funds when service returns.

### "VPS host kicked us off"

Privacy-friendly hosts (Hetzner, OVH) rarely do this without warning. If it happens:
1. Already have backup VPS provisioned (don't wait for incident).
2. Restore DB from latest backup.
3. Update DNS to new VPS.
4. Re-unlock.

### "Got a subpoena / law enforcement contact"

Out of scope for this runbook. Get a lawyer. The architecture limits what's available — no logs, no user content, hashed keys, encrypted DB — but operator is identifiable via VPS contract.

---

## Don't

- Don't enable `phantom-api.service` on boot — it MUST wait for manual unlock.
- Don't add Sentry / third-party error tracking — exfils prompts on exception.
- Don't store XMR amounts as floats anywhere.
- Don't log request bodies, IPs, or completion content.
- Don't run wallet RPC on the VPS. Ever.
- Don't add Caddy access logging (`log { output discard }` is mandatory).
- Don't commit `.env`, wallet keys, DB passphrase, or screenshots containing secrets.
