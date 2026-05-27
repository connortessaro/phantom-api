# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Phantom API — anonymous AI inference proxy. Monero in, OpenAI-compatible API out. FastAPI on a VPS forwards to Phala Confidential AI (Intel TDX + NVIDIA CC TEEs). No accounts, no logs, SQLCipher-encrypted DB unlocked manually per boot.

Read `HANDOFF.md` for full architecture rationale, threat model, and honest framing (the proxy itself is NOT a hardware-enforced enclave — only inference at Phala is TEE-attested).

## Commands

```bash
# Local dev skeleton (DB only; full run needs wallet RPC + Phala key)
python3.12 -m venv venv && source venv/bin/activate
# macOS: needs brew sqlcipher first
#   brew install sqlcipher
#   export C_INCLUDE_PATH="$(brew --prefix sqlcipher)/include"
#   export LIBRARY_PATH="$(brew --prefix sqlcipher)/lib"
pip install -r requirements.txt
cp .env.example .env  # edit REDPILL_API_KEY, WALLET_ONION, WALLET_RPC_PASSWORD
export PHANTOM_DB_PASSPHRASE=devonly-replace-me
python -c "import asyncio, db; asyncio.run(db.init_db('data/phantom.db'))"
uvicorn main:app --reload

# Phase 0 — verify Redpill endpoint, model attestation, pricing
export REDPILL_API_KEY=sk-...   # legacy PHALA_API_KEY still accepted
./scripts/phase-0-smoke.sh   # writes notes/phase-0-output/

# Background payment poller (separate process)
python scripts/poll-payments.py

# VPS bootstrap (run as root on fresh Debian/Ubuntu)
./scripts/setup-vps.sh

# Unlock DB after VPS boot (operator runs via SSH)
./scripts/unlock.sh           # prompts passphrase, writes tmpfs, starts phantom-api

# Wallet sweep (run on operator PC)
COLD_ADDRESS=... WALLET_RPC_PASSWORD=... python scripts/sweep.py
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v       # 44 tests: cost math, xmr math, image estimate, stream parser, concurrency
```

## Dependency pinning

Production installs from `requirements.lock` (pip-compile output with `--generate-hashes`). `requirements.txt` is the human-edited input; `requirements.lock` is what runs.

Regenerate after editing `requirements.txt`:
```bash
pip-compile --generate-hashes --output-file requirements.lock requirements.txt
```

`setup-vps.sh` installs with `pip install --require-hashes -r requirements.lock` so a compromised PyPI version can't slip in.

## Architecture

Three processes:
1. **`main.py`** — FastAPI on `127.0.0.1:8000`, fronted by Caddy. Endpoints: `/v1/models`, `/v1/bundles`, `/v1/purchase` (bundle OR `amount_usd`), `/v1/purchase/{id}/status`, `/v1/purchase/{id}/qr.svg`, `/v1/key/balance`, `/v1/key/rotate`, `/v1/chat/completions` (proxy + stream), `/v1/embeddings` (proxy), `/v1/inference-attest`, `/v1/signature/{id}`, `/health`. Static frontend served by Caddy from `frontend/` (no FastAPI mount on VPS).
2. **`scripts/poll-payments.py`** — systemd unit. Polls wallet via `incoming_transfers` filtered by `subaddr_indices` every 30s; transitions `pending → confirming → ready` (≥10 confirmations, ≥98% expected amount). Confirmations computed from `daemon_height - block_height + 1`. Key issuance happens atomically at the status endpoint, not here.
3. **Operator PC** — runs `monero-wallet-rpc` exposed via Tor hidden service. VPS reaches `.onion:18083` via local Tor SOCKS (`socks5://127.0.0.1:9050` — NOT `socks5h://`, `python-socks` library uses the former scheme + handles remote DNS automatically). VPS never holds funds and never learns home IP.

Data flow: user pays XMR → poller marks `ready` → `GET /purchase/{id}/status` atomically transitions `ready → completed` and returns plaintext API key exactly once (`UPDATE ... WHERE status='ready'`, `rowcount==0` means already claimed).

## Money math — micro-USD everywhere

All monetary values are integer **micro-USD** (`MICRO = 1_000_000`, $1 = 1e6). Never use floats for money. `cost_micro_usd(model, p_tok, c_tok)` in `config.py` applies `MARKUP_NUM/100` over the upstream wholesale per-million rates (Redpill pricing). Pricing returned by `/v1/models` already includes markup. XMR amounts stored as decimal strings, never floats; `payments.py` uses `Decimal` and `PICONERO = 1e12`.

`BUNDLES` use `price_micro` + `credit_micro` (integer micro-USD), NOT float USD. `/v1/bundles` exposes them as `price_usd` / `credit_usd` for display only — compute at boundary.

Custom-amount purchases: `/v1/purchase` accepts `{"amount_usd": N}` as alternative to `{"bundle": "name"}`. Bounded by `CUSTOM_MIN_MICRO` / `CUSTOM_MAX_MICRO` in config (default $1–$1000). Custom = 1:1 credit, no volume bonus, `CUSTOM_VALIDITY_DAYS` lifetime.

Capacity check: `/v1/purchase` refuses if `db.outstanding_credit_micro() + new_credit > REDPILL_BUDGET_MICRO` (active key balances + pending bundle credit). Operator must keep Redpill balance ≥ `REDPILL_BUDGET_USD` env value. Legacy `PHALA_BUDGET_USD` still accepted as fallback.

## Streaming budget accounting

`_stream_phala()` buffers the last 8KB of the response stream and parses Phala's final SSE `"usage"` block in `finally`. Three billing branches:
1. Usage block found → use it (truthful, exact)
2. Stream aborted with bytes streamed → estimate completion tokens from `bytes_streamed // 30`, clamp to `max_tokens` (prevents free inference on intentional aborts)
3. Nothing streamed → no charge (Phala did no work)

Non-streaming path uses the JSON `usage` block. Pre-flight `check_credits` uses worst-case `max_tokens` cost via `_estimate_prompt_tokens` (tiktoken `cl100k_base` + 1530 tokens/image for vision parts).

`max_tokens` is clamped on every chat request to `min(client_requested, model_context // 2, 32_768)` to prevent single-request bundle drain.

Per-key concurrent stream cap: `_MAX_STREAMS_PER_KEY = 10`, in-memory counter, resets on uvicorn restart. Returns 429 when exceeded.

## Privacy invariants — do not violate

These break the product's stated value:
- **No IP logging** — Caddy `log { output discard }`, uvicorn `--no-access-log`, slowapi keyed off SHA-256 of API key OR SHA-256 of client IP (read from `X-Forwarded-For` behind Caddy). IP hashes live in slowapi's in-memory bucket only; never logged, never persisted.
- **No prompt/completion logging** — `safe_exception_handler` only logs exception type and path, never bodies. `usage_log` table stores token counts only, never content.
- **API keys hashed** — `db.hash_key` is SHA-256. Plaintext returned exactly once at `claim_and_issue` or `rotate_key`. Recovery intentionally impossible.
- **No PII columns** — no email/name/IP anywhere in `schema.sql`.
- **DB encrypted at rest** — SQLCipher passphrase loaded from `PHANTOM_DB_PASSPHRASE` env, then `os.environ.pop`'d. Fed via tmpfs file `/run/phantom/phantom-secrets.env` (mode 0400). Wallet RPC password + Phala API key also tmpfs-only (operator types all three at `unlock.sh` prompt). Never on persistent disk, never in `.env`.
- **Body whitelist to upstream** — `_REDPILL_CHAT_BODY_KEYS` + `_REDPILL_EMBED_BODY_KEYS` strictly limit what's forwarded. Drops `user`, `metadata`, etc. that could fingerprint customer or trigger billable side effects.

If marketing copy is touched: phrase as "anonymous payment, no accounts, no logs, encrypted at rest, Phala-attested inference." Do NOT claim "TEE-attested API" or "cryptographically private proxy" — only inference is.

## Model catalog

`config.MODELS` is the source of truth for allowed models and wholesale pricing. Each entry has a `kind` field (`chat` or `embedding`). `CHAT_MODELS` and `EMBEDDING_MODELS` are derived sets — chat models gate `/v1/chat/completions`, embedding models gate `/v1/embeddings`. Adding a model requires verifying it's TEE-attested via `phase-0-smoke.sh` first.

`phala/*` prefix = "runs in Phala TEE enclave" (capability tag, not brand). Embedding models use upstream IDs (`qwen/qwen3-embedding-8b`, `sentence-transformers/all-minilm-l6-v2`) — they're also Phala-attested but Redpill catalog uses the model author's prefix.

Venice Uncensored 24B is included — no AUP filtering on output. Customers accept terms in `/terms.html#aup`.

## Frontend

Static HTML/CSS/JS only. **No frameworks, no CDN, no fonts loaded from third parties.** Served by Caddy from `frontend/`. `purchase.js` polls `/v1/purchase/{id}/status` for completion. Anything that pulls a remote asset breaks the no-third-parties posture.

## Conventions

- `async with db._lock:` around every read AND write — single shared `sqlcipher3` connection, asyncio-serialized.
- `datetime.now(timezone.utc).isoformat()` for all timestamps. Never `utcnow()`.
- HTTP errors via `HTTPException(status, "lowercase reason")`. Generic upstream failures → 503 `"upstream unavailable"`.
- `httpx.AsyncClient` for outbound; payments use `AsyncProxyTransport.from_url(TOR_SOCKS_URL)` + `httpx.DigestAuth`.
- Python 3.12; macOS Bash is 3.x (no `declare -A` in any shell script).

## What NOT to do

- Don't add Sentry / third-party error tracking — exfils prompts on exception.
- Don't add Postgres, Redis, Docker — explicitly rejected in HANDOFF.md.
- Don't log request bodies, IPs, or completion content anywhere.
- Don't store XMR amounts as floats; use `Decimal` + decimal strings.
- Don't enable `phantom-api.service` on boot — it must wait for manual SSH unlock.
- Don't run wallet RPC on the VPS — wallet lives on operator PC, reachable only via Tor hidden service.
