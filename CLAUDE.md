# phantom-api

**Stack:** Python 3.12, FastAPI, SQLCipher, httpx, uvicorn, Caddy | **Port:** 8000 (uvicorn, localhost only)

## What

Anonymous AI inference proxy. Customers pay XMR or other crypto, receive a single-use `sk-*` bearer token, call OpenAI-compatible endpoints. Inference forwards to Phala's Redpill gateway (api.redpill.ai); TEE-attested models run in Intel TDX + NVIDIA CC enclaves. The proxy itself is a hardened VPS, not a hardware enclave.

## Quick start

```bash
./setup.sh                                   # first-time dev setup
source venv/bin/activate
export PHANTOM_DB_PASSPHRASE=devonly-replace-me
PHANTOM_DEV=1 uvicorn main:app --reload      # http://localhost:8000
python -m pytest tests/ -v                   # run test suite
```

## Commands

```bash
# Dev
pip install -r requirements.txt              # install deps (inside venv)
pip install -r requirements-dev.txt          # + test deps
cp .env.example .env                         # edit: REDPILL_API_KEY + payment rail
python -c "import asyncio, db; asyncio.run(db.init_db('data/phantom.db'))"  # init DB

# Tests (44+ tests)
python -m pytest tests/ -v
python -m pytest tests/test_cost_math.py -v  # targeted

# Deploy (requires PHANTOM_SSH alias in ~/.ssh/config)
./scripts/deploy.sh                          # all: backend + scripts + frontend
./scripts/deploy.sh backend                  # python modules only + restart
./scripts/deploy.sh frontend                 # static files only, no restart
./scripts/deploy.sh caddy                    # validate + reload Caddyfile

# VPS ops
sudo ./scripts/setup-vps.sh                  # one-time bootstrap (run as root)
./scripts/unlock.sh                          # after every boot: feeds 4 secrets via tmpfs

# Phala / model verification
export REDPILL_API_KEY=sk-...
./scripts/phase-0-smoke.sh                   # verify endpoint, attestation, pricing

# Hot wallet sweep
python scripts/sweep-hot.py                  # sweep above threshold to cold address

# Regenerate dependency lockfile
pip-compile --generate-hashes --output-file requirements.lock requirements.txt
```

## Architecture

```
main.py          FastAPI app — all routes (chat, embed, image, purchase, key, attest)
payments.py      Legacy XMR rail: wallet RPC over Tor SOCKS (httpx + httpx-socks)
monero_pay.py    MoneroPay rail: self-hosted daemon, callback via per-payment URL token
nowpayments.py   NowPayments PSP rail: HMAC-SHA512 IPN signature verification
db.py            SQLCipher access layer — single conn, asyncio.Lock on every op
config.py        Bundles, env vars, cost_micro_usd(), MODELS catalog, image models
catalog.py       Dynamic model catalog: refreshes from Redpill live, disk-caches, falls back
pricing.py       XMR/USD via Kraken public ticker (5-min TTL cache)
schema.sql       SQLite schema (api_keys, payments, usage_log) — no PII columns
scripts/
  unlock.sh      SSH-side: prompts 4 secrets, writes to tmpfs, starts services
  deploy.sh      rsync + install + restart on VPS
  poll-payments.py  Background poller: pending → confirming → ready (legacy_xmr)
  setup-vps.sh   Fresh VPS bootstrap (Debian/Ubuntu, Caddy, Tor, Python, systemd)
frontend/        Static HTML/CSS/JS — no frameworks, no CDN, no external assets
```

Three processes in production:
1. **uvicorn** (`phantom-api.service`) — FastAPI on `127.0.0.1:8000`, fronted by Caddy
2. **poll-payments.py** (`phantom-poller.service`) — polls wallet RPC every 30s for legacy_xmr; MoneroPay and NowPayments use webhook callbacks
3. **Operator PC** — runs `monero-wallet-rpc` behind a Tor hidden service; VPS never holds funds

## Key files

```
main.py            All routes + rate limiting + streaming proxy
config.py          BUNDLES dict, MODELS dict, all env vars, cost_micro_usd()
db.py              init_db(), check_credits(), decrement_credits(), claim_and_issue()
schema.sql         Source of truth for table shapes; init_db() applies it on start
catalog.py         Live Redpill model list; phantom/* rebrand; disk fallback
.env.example       All supported env vars with comments
scripts/unlock.sh  The manual-boot secret-feeding ceremony
Caddyfile          TLS termination config; has domain placeholder comments
```

## Money math — critical invariants

- **All monetary values are integer micro-USD.** `MICRO = 1_000_000`. $1.00 = 1_000_000.
- **Never use floats for money.** XMR amounts stored as `Decimal` strings; `PICONERO = 1e12`.
- `cost_micro_usd(model, prompt_tokens, completion_tokens)` in `config.py` applies tier markup (`MARKUP_TEE_NUM/100` or `MARKUP_PROXY_NUM/100`) over upstream wholesale per-million rates.
- `BUNDLES` use `price_micro` + `credit_micro` (integers). `/v1/bundles` converts to `price_usd` at the boundary only.
- Custom-amount purchases: `{"amount_usd": N}`. Bounded by `CUSTOM_MIN_MICRO`/`CUSTOM_MAX_MICRO`. 1:1 credit, no bonus.

## Streaming billing

`_stream_phala()` in `main.py` buffers the last 8KB of the SSE stream and parses Phala's final `"usage"` block in `finally`:
1. Usage block found → charge exact tokens
2. Stream aborted with bytes sent → estimate `completion = bytes_streamed // 30`, clamped to `max_tokens`
3. Nothing sent → no charge

`max_tokens` is clamped to `min(client_requested, model_ctx // 2, 32_768)` before forwarding. Per-key stream cap: `_MAX_STREAMS_PER_KEY = 10`.

## DB / concurrency

Single `sqlcipher3` connection. Every read AND write uses `async with db._lock:`. No threading. No concurrent access from outside the process. The connection is held for the lifetime of the process; unlock.sh starts the service with the passphrase in a tmpfs env file shredded immediately after.

## Configuration

All config via `.env`. See `.env.example` for full list.

| Variable | Notes |
|----------|-------|
| `REDPILL_API_KEY` | Required. Legacy `PHALA_API_KEY` accepted. |
| `PAYMENT_PROVIDER` | `legacy_xmr` / `monero_pay` / `nowpayments` / `hybrid` |
| `PHANTOM_DB_PASSPHRASE` | Dev only. Production: fed via `unlock.sh` to tmpfs. |
| `REDPILL_BUDGET_USD` | Refuse new purchases if outstanding credit would exceed this. |
| `MARKUP_TEE_PERCENT` / `MARKUP_PROXY_PERCENT` | Markup tiers. Legacy `MARKUP_PERCENT` applies to both. |
| `PUBLIC_BASE_URL` | Must be reachable from internet (IPN callbacks). |

## What NOT to do

- No Sentry or third-party error tracking — would exfil prompts on exception
- No Postgres, Redis, Docker — explicitly rejected; SQLite is enough
- No IP logging anywhere — not in Caddy, not in uvicorn, not in any logger call
- No float for money — use `int` micro-USD or `Decimal` for XMR
- Do not enable `phantom-api.service` on boot — it must wait for `unlock.sh`
- Do not run wallet RPC on the VPS — wallet lives on operator PC via Tor only
- Do not add PII columns to schema.sql
- Do not claim "TEE-attested proxy" — only inference at Phala is TEE-attested

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Privacy invariants are enforced as hard review criteria.
