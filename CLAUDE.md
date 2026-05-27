# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Phantom API — anonymous AI inference proxy. Pay any crypto via NowPayments, get an OpenAI-compatible API key. FastAPI on a VPS forwards inference to Phala's Redpill gateway (some models run in Intel TDX + NVIDIA CC TEEs). No accounts, no logs, SQLCipher-encrypted DB unlocked manually per boot.

## Commands

```bash
# Local dev
python3.12 -m venv venv && source venv/bin/activate
# macOS: brew install sqlcipher first
#   export C_INCLUDE_PATH="$(brew --prefix sqlcipher)/include"
#   export LIBRARY_PATH="$(brew --prefix sqlcipher)/lib"
pip install -r requirements.txt
cp .env.example .env  # edit REDPILL_API_KEY + NP_API_KEY + NP_IPN_SECRET
export PHANTOM_DB_PASSPHRASE=devonly-replace-me
python -c "import asyncio, db; asyncio.run(db.init_db('data/phantom.db'))"
PHANTOM_DEV=1 uvicorn main:app --reload

# Run tests
pip install -r requirements-dev.txt
python -m pytest tests/ -v   # 55 tests

# Phase 0 — verify Redpill endpoint + per-model attestation
./scripts/phase-0-smoke.sh

# VPS bootstrap (run as root on fresh Debian/Ubuntu)
./scripts/setup-vps.sh

# Deploy from dev machine to VPS
./scripts/deploy.sh

# Unlock DB after VPS reboot (operator SSHes in)
./scripts/unlock.sh   # prompts: SQLCipher passphrase + Redpill API key
```

## Dependency pinning

Production installs from `requirements.lock` (pip-compile output with `--generate-hashes`). `requirements.txt` is the human-edited input; `requirements.lock` is what runs.

Regenerate after editing `requirements.txt`:
```bash
pip-compile --generate-hashes --output-file requirements.lock requirements.txt
```

`setup-vps.sh` installs with `pip install --require-hashes -r requirements.lock` so a compromised PyPI version can't slip in.

## Architecture

Single process:

- **`main.py`** — FastAPI on `127.0.0.1:8000`, fronted by Caddy. Endpoints:
  - `/v1/models`, `/v1/bundles` — public catalog
  - `/v1/purchase` (bundle OR `amount_usd`, with `rail` ∈ {`xmr`, `multi`}), `/v1/purchase/{id}/status`
  - `/v1/nowpayments/ipn` — HMAC-SHA512 verified webhook from NowPayments
  - `/v1/key/balance`, `/v1/key/rotate`
  - `/v1/chat/completions` (proxy + stream), `/v1/embeddings`, `/v1/images/generations`
  - `/v1/inference-attest`, `/v1/signature/{id}` — Phala TEE attestation passthrough
  - `/health`

Static frontend served by Caddy from `frontend/` in prod; FastAPI mounts it only when `PHANTOM_DEV=1`.

Payment flow:
```
customer → POST /v1/purchase {bundle, rail} 
        → phantom mints NowPayments invoice via REST
        → returns { checkout_url, payment_id }
        → customer pays on NowPayments-hosted page
        → NowPayments POSTs /v1/nowpayments/ipn (HMAC signed)
        → phantom flips payments row: pending → confirming → ready
        → customer's next GET /v1/purchase/{id}/status sees status='ready'
          → atomic UPDATE WHERE status='ready' + INSERT api_keys
          → returns plaintext sk-... once (rowcount=0 → already claimed)
```

## Money math — micro-USD everywhere

All monetary values are integer **micro-USD** (`MICRO = 1_000_000`, $1 = 1e6). Never use floats for money. `cost_micro_usd(model, p_tok, c_tok)` in `config.py` applies tier-specific markup over the upstream wholesale per-million rates.

`BUNDLES` use `price_micro` + `credit_micro` (integer micro-USD), NOT float USD. `/v1/bundles` exposes them as `price_usd` / `credit_usd` for display only — compute at boundary.

Custom-amount purchases: `/v1/purchase` accepts `{"amount_usd": N}` as alternative to `{"bundle": "name"}`. Bounded by `CUSTOM_MIN_MICRO` / `CUSTOM_MAX_MICRO` in config (default $10–$1000). Custom = 1:1 credit, no volume bonus, `CUSTOM_VALIDITY_DAYS` lifetime.

Capacity check: `/v1/purchase` refuses if `db.outstanding_credit_micro() + new_credit > REDPILL_BUDGET_MICRO`. Operator must keep Redpill balance ≥ `REDPILL_BUDGET_USD` env value.

## Streaming budget accounting

`_stream_phala()` in `main.py` buffers the last 8KB of the response stream and parses the final SSE `"usage"` block in `finally`. Three billing branches:

1. Usage block found → use it (truthful, exact)
2. Stream aborted with bytes streamed → estimate completion tokens from `bytes_streamed // 30`, clamp to `max_tokens` (prevents free inference on intentional aborts)
3. Nothing streamed → no charge (Phala did no work)

Non-streaming path uses the JSON `usage` block. Pre-flight `check_credits` uses worst-case `max_tokens` cost via `_estimate_prompt_tokens` (tiktoken `cl100k_base` + 1530 tokens/image for vision parts).

`max_tokens` is clamped on every chat request to `min(client_requested, model_context // 2, 32_768)` to prevent single-request bundle drain.

Per-key concurrent stream cap: `_MAX_STREAMS_PER_KEY = 10`, in-memory counter, resets on uvicorn restart. Returns 429 when exceeded. Global cap also enforced.

## Privacy invariants — do not violate

These break the product's stated value:

- **No IP logging** — Caddy `log { output discard }`, uvicorn `--no-access-log`. Rate-limiter (slowapi) keys on SHA-256 of API key OR SHA-256 of client IP (read from `X-Forwarded-For` overwritten by Caddy). Hashes live in slowapi's in-memory bucket only; never logged, never persisted.
- **No prompt/completion logging** — `safe_exception_handler` logs only exception type and path, never bodies. `usage_log` table stores token counts only.
- **API keys hashed** — `db.hash_key` is SHA-256. Plaintext returned exactly once at `db.claim_and_issue` or `db.rotate_key`. Recovery intentionally impossible.
- **No PII columns** — no email/name/IP anywhere in `schema.sql`.
- **DB encrypted at rest** — SQLCipher passphrase fed via tmpfs at `/run/phantom/phantom-secrets.env` (mode 0400). Never on persistent disk, never in `.env`.
- **Body whitelist to upstream** — `_REDPILL_CHAT_BODY_KEYS` + `_REDPILL_EMBED_BODY_KEYS` strictly limit forwarded fields. Drops `user`, `metadata`, etc. that could fingerprint customer.

If marketing copy is touched: phrase as "anonymous payment, no accounts, no logs, encrypted at rest, Phala-attested inference." Do NOT claim "TEE-attested API" or "cryptographically private proxy" — only inference is.

## Model catalog

`catalog.py` fetches Redpill's `/v1/models` at startup and refreshes hourly. Falls back to disk cache, then a minimal builtin. Each entry classified as tier `tee` (Intel TDX + NVIDIA CC verifiable) or `proxy` (vendor reads prompt). TEE entries get phantom-branded IDs (`phantom/<base>`); proxy entries keep vendor prefix (`anthropic/...`, `openai/...`). Backwards-compat aliases let legacy IDs still resolve.

Image models curated statically in `config.IMAGE_MODELS` (Redpill `/v1/models` doesn't list them). Flat per-image billing.

`CATALOG_BLOCKLIST` env var suppresses upstream duplicates so customers see one canonical entry per model.

## Frontend

Static HTML/CSS/JS only. **No frameworks, no CDN, no fonts loaded from third parties.** Served by Caddy from `frontend/`. `purchase.js` polls `/v1/purchase/{id}/status` for completion. `models.js` renders the use-case-first catalog picker. Anything that pulls a remote asset breaks the no-third-parties posture.

## Conventions

- `async with db._lock:` around every read AND write — single shared `sqlcipher3` connection, asyncio-serialized.
- `datetime.now(timezone.utc).isoformat()` for all timestamps. Never `utcnow()`.
- HTTP errors via `HTTPException(status, "lowercase reason")`. Generic upstream failures → 503 `"upstream unavailable"`.
- `httpx.AsyncClient` for outbound calls.
- Python 3.12; macOS Bash is 3.x (no `declare -A` in shell scripts).

## What NOT to do

- Don't add Sentry / third-party error tracking — exfils prompts on exception.
- Don't add Postgres, Redis, Docker — explicitly rejected design choices.
- Don't log request bodies, IPs, or completion content anywhere.
- Don't enable `phantom-api.service` on boot — it must wait for manual SSH unlock so the SQLCipher passphrase never lives on disk.
- Don't add a BYOK (bring-your-own-key) path — defeats the anonymity story (customer's vendor key = customer's billing identity).
