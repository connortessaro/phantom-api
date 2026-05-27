# Runbook

Operational tasks for a deployed phantom instance.

## After VPS reboot

```bash
ssh phantom
sudo /opt/phantom-api/scripts/unlock.sh
# prompts for two secrets:
#   - SQLCipher passphrase (decrypts api_keys + payments at rest)
#   - Redpill API key (forwards inference upstream)
# writes them to /run/phantom/phantom-secrets.env (tmpfs, 0400 phantom:phantom)
# starts phantom-api.service
```

Without unlock, `phantom-api` fails to open the SQLCipher DB and won't serve traffic.

## Deploy code changes

From your dev machine:

```bash
./scripts/deploy.sh           # full sync + restart + health check
./scripts/deploy.sh frontend  # static files only, no restart
./scripts/deploy.sh backend   # py modules + restart
./scripts/deploy.sh caddy     # Caddyfile validate + reload
```

## Health check

```bash
curl -s https://your-domain.com/health
# {"status":"ok","db":true,"models":N,"payments":true}
```

- `db: false` → SQLCipher passphrase not loaded; rerun `unlock.sh`
- `payments: false` → NowPayments API unreachable or NP_API_KEY rejected
- `models: 0` → catalog fetch failed; check Redpill API key

## NowPayments dashboard checklist

Configure these once in your NowPayments merchant account:

| Setting | Value |
|---|---|
| Payment markup | **0%** (NOT default) |
| Service fee paid by | **Sender** |
| Withdrawal fee paid by | **Sender** |
| Wrong-asset deposits | **Do not process** |
| Default payment status | **Partially Paid** |
| Payout coin | **XMR** to your cold wallet |
| Webhook URL | `https://your-domain.com/v1/nowpayments/ipn` |

Copy your API key + IPN secret into `.env` as `NP_API_KEY` and `NP_IPN_SECRET`.

## Budget management

Phantom refuses new purchases when outstanding customer credit + pending bundle credit exceeds `REDPILL_BUDGET_USD`. Keep your Redpill prepaid balance ≥ this value at all times.

Reload alert (optional): `scripts/reload-alert.py` pushes a ntfy.sh notification when estimated balance drops below `REDPILL_RELOAD_ALERT_USD`. Wire it via cron.

## DB backup

```bash
sudo -u phantom python /opt/phantom-api/scripts/backup-db.py
# writes encrypted snapshot to $PHANTOM_BACKUP_DIR
# keeps $PHANTOM_BACKUP_KEEP most recent; older ones pruned
```

Snapshots are SQLCipher-encrypted with the same passphrase as the live DB. Lose the passphrase → lose the backups too.

## Common incidents

**Customer paid but didn't receive key.** Look up by `payment_id`:

```bash
ssh phantom 'sudo journalctl -u phantom-api --since "1 hour ago" | grep <payment_id>'
```

Check NowPayments dashboard for the order_id. If status is `finished` but phantom still shows `pending`, IPN delivery failed — manually trigger one via NowPayments dashboard or call `/v1/purchase/<id>/status` to force a poll.

**Key claimed but never delivered to customer.** Atomic claim already fired; key cannot be recovered. Issue a fresh purchase + manually refund out-of-band.

**Redpill upstream down.** `/v1/chat/completions` returns 503 "upstream unavailable". Customers see the error; no charge applied (cost-on-success).

## Snapshot before risky changes

```bash
./scripts/deploy.sh snapshot
# triggers DigitalOcean droplet snapshot (requires doctl + PHANTOM_DROPLET_ID set)
```
