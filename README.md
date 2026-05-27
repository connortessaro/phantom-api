# phantom-api

Anonymous AI inference proxy. Pay crypto, get an OpenAI-compatible API key. No accounts, no email, no logs.

Customers pay XMR directly or via a multi-crypto PSP, receive a `sk-*` bearer token, and call any OpenAI-compatible endpoint. Inference forwards to [Phala's Redpill gateway](https://redpill.ai) where supported models run inside Intel TDX + NVIDIA CC TEEs. Per-request attestation is verifiable via `/v1/inference-attest`. The proxy itself is a hardened VPS — see [Trust model](#trust-model) for the honest framing.

Open-source under AGPL-3.0. Fork it, run your own instance.

---

## Features

- OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`, `/v1/images/generations`)
- Four payment rails: XMR direct (via MoneroPay daemon), operator wallet via Tor, NowPayments multi-crypto PSP, or hybrid
- No accounts, no email, no IP logging
- SQLCipher-encrypted database; passphrase fed via tmpfs at boot, never on disk
- API keys stored as SHA-256 hashes only; plaintext returned exactly once
- Streaming token billing with safe disconnect handling
- Per-request Phala TEE attestation forwarded to callers
- Static HTML/CSS/JS frontend; no CDN, no third-party assets
- Tor hidden service support for both API and frontend

---

## Quick start (local dev)

```bash
git clone https://github.com/connortessaro/phantom-api.git
cd phantom-api
./setup.sh                          # installs deps, creates venv, inits dev DB
source venv/bin/activate
export PHANTOM_DB_PASSPHRASE=devonly-replace-me
PHANTOM_DEV=1 uvicorn main:app --reload
```

Open `http://localhost:8000`. A Phala API key (`REDPILL_API_KEY`) and a running Monero wallet RPC are required for full end-to-end operation; the skeleton runs without them.

---

## Architecture

```
Customer (anonymous, optionally via Tor)
        │
        │ HTTPS
        ▼
  ┌─────────────────────────────────────┐
  │  Caddy  (TLS, access log: discard)  │
  │  static frontend served here        │
  └─────────────────────────────────────┘
        │ HTTP (127.0.0.1:8000)
        ▼
  ┌─────────────────────────────────────┐
  │  FastAPI / uvicorn (--no-access-log)│
  │  /v1/chat/completions               │
  │  /v1/purchase  (+ /status, /qr.svg) │
  │  /v1/key/balance  /v1/key/rotate    │
  │  /v1/embeddings  /v1/images/...     │
  │  /v1/inference-attest               │
  │  /health  /v1/stats                 │
  └─────────────────────────────────────┘
     │              │                │
     ▼              ▼                ▼
  SQLCipher     Phala Redpill     Payment rails
  (AES-256,     (inference TEE)
   tmpfs key)
                              ┌─── legacy_xmr ──────────────────────┐
                              │  operator-PC monero-wallet-rpc       │
                              │  reachable only via Tor hidden svc   │
                              └──────────────────────────────────────┘
                              ┌─── monero_pay ──────────────────────┐
                              │  self-hosted MoneroPay daemon        │
                              │  (XMR direct, zero PSP)             │
                              └──────────────────────────────────────┘
                              ┌─── nowpayments ─────────────────────┐
                              │  NowPayments PSP (multi-crypto)      │
                              │  operator KYC'd, buyer anonymous    │
                              └──────────────────────────────────────┘
                              ┌─── hybrid (recommended) ────────────┐
                              │  MoneroPay for XMR                  │
                              │  NowPayments for all other coins     │
                              └──────────────────────────────────────┘
```

---

## Payment rails

| Rail | `PAYMENT_PROVIDER` | How it works | Who needs accounts |
|------|--------------------|--------------|-------------------|
| **legacy_xmr** | `legacy_xmr` | Operator runs `monero-wallet-rpc` on their PC behind a Tor hidden service. VPS polls wallet over Tor SOCKS. | Operator only (no PSP) |
| **monero_pay** | `monero_pay` | Self-hosted [MoneroPay](https://moneropay.eu) daemon on operator PC. Daemon handles subaddresses and callbacks. | Nobody |
| **nowpayments** | `nowpayments` | [NowPayments](https://nowpayments.io) hosted PSP. Accepts 200+ coins; settles to XMR. 5% surcharge on non-XMR coins. | Operator KYC required |
| **hybrid** | `hybrid` | MoneroPay for XMR (zero PSP), NowPayments for other coins. Recommended for production. | Operator KYC for NowPayments |

Set `PAYMENT_PROVIDER` in `.env`. See `.env.example` for all rail-specific variables.

---

## Privacy invariants

These are non-negotiable. Violating any breaks the service's stated value.

- **No IP logging.** Caddy: `log { output discard }`. uvicorn: `--no-access-log`. slowapi rate-limit keys use SHA-256 of API key or IP hash — never plaintext, never persisted.
- **No prompt or completion logging.** Exception handler logs only method and path, never request bodies. `usage_log` table stores token counts only.
- **No PII columns.** `schema.sql` has no email, name, IP, or user-agent columns. Do not add any.
- **API keys hashed.** `db.hash_key` is SHA-256. Plaintext returned exactly once at `claim_and_issue` or `rotate_key`. Recovery is intentionally impossible.
- **DB encrypted at rest.** SQLCipher passphrase loaded from `PHANTOM_DB_PASSPHRASE` env, then `os.environ.pop`'d. On production, fed via tmpfs file `/run/phantom/phantom-secrets.env` (mode 0400), shredded after service start. Never on persistent disk.
- **Four secrets via tmpfs only.** `scripts/unlock.sh` prompts for: (1) SQLCipher passphrase, (2) wallet RPC password, (3) hot wallet password, (4) Redpill API key. All written to `/run/phantom/phantom-secrets.env` (tmpfs) and shredded on service start.
- **Body whitelist.** `_REDPILL_CHAT_BODY_KEYS` and `_REDPILL_EMBED_BODY_KEYS` in `main.py` strip `user`, `metadata`, and any other fields that could fingerprint the customer before forwarding to upstream.
- **Wallet isolated.** VPS holds zero funds. All XMR lives on the operator's PC; VPS sees only payment metadata via Tor.

---

## Trust model

This is an honest privacy product, not a hardware-enforced enclave service at the proxy layer.

**Cryptographically guaranteed:**
- Inference runs inside a Phala TEE (verifiable per-request via `/v1/inference-attest`)
- XMR payment cannot be linked to the resulting API key
- API key stored only as SHA-256; operator cannot recover it

**Policy-based (trust the operator):**
- Prompts and completions are not logged anywhere
- No PII retained; no email, name, or identifier collected
- Rate limiting by API-key hash, not IP

**Cannot be guaranteed:**
- The proxy VPS is a regular server. While running, it holds prompts in RAM between receipt and forwarding to Phala. A compelled memory dump could expose in-flight prompts.
- The hosting provider could in principle MITM the TLS termination. Verify the certificate via your client and Certificate Transparency logs.
- The operator is identifiable via VPS contract. Customers are anonymous via Monero; this asymmetry is irreducible at this architecture tier.

---

## Configuration

```bash
cp .env.example .env
# edit .env — minimum required: REDPILL_API_KEY, payment rail vars
```

Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `REDPILL_API_KEY` | Yes | Phala Redpill gateway API key |
| `PAYMENT_PROVIDER` | Yes | `legacy_xmr`, `monero_pay`, `nowpayments`, or `hybrid` |
| `PUBLIC_BASE_URL` | Yes | Publicly reachable base URL (used in IPN callbacks) |
| `NP_API_KEY` + `NP_IPN_SECRET` | nowpayments/hybrid | NowPayments credentials |
| `MONEROPAY_URL` | monero_pay/hybrid | MoneroPay daemon URL |
| `WALLET_RPC_PASSWORD` | legacy_xmr | Monero wallet RPC HTTP Basic password |
| `REDPILL_BUDGET_USD` | Yes | Max total outstanding customer credit; keep at or below Redpill balance |
| `PHANTOM_DB_PASSPHRASE` | Dev only | On production, fed via `scripts/unlock.sh` into tmpfs |

See `.env.example` for the full list with comments.

---

## Deployment checklist

Before going live, replace all operator-specific placeholder values:

1. **Domain.** Replace `phantom.codes` / `api.phantom.codes` with your domain throughout:
   - `Caddyfile`
   - `frontend/*.html`
   - `frontend/sitemap.xml`
   - `frontend/robots.txt`
   - `RUNBOOK.md`
   - `scripts/deploy.sh` (the `/health` verify step at the bottom)

2. **Tor .onion address.** Replace `<your-onion-here.onion>` in `Caddyfile` and `frontend/index.html`, `frontend/docs.html`, `frontend/terms.html` with your Tor hidden service hostname. See `pc-side/torrc.example` to generate one.

3. **PGP key.** Replace `frontend/pgp.txt` with your own public key. Update `<YOUR-PGP-FINGERPRINT>` in `frontend/terms.html` and `frontend/docs.html`.

4. **Environment.** Copy `.env.example` to `.env` on the VPS (never committed). Fill in all values.

5. **Bootstrap VPS.** Run `sudo ./scripts/setup-vps.sh` on a fresh Debian/Ubuntu server. Installs Caddy, Tor, Python 3.12, fail2ban, ufw, and systemd units.

6. **Unlock after every boot.** `scripts/unlock.sh` prompts for the four secrets and writes them to tmpfs. The API service will not start without it.

---

## Tests and dev loop

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v       # 44+ tests: cost math, XMR math, stream billing, concurrency
```

Smoke-test payment rails against a live instance:

```bash
./scripts/smoke-test-rails.sh
```

Verify Phala/Redpill endpoint and model attestation before configuring `ALLOWED_MODELS`:

```bash
export REDPILL_API_KEY=sk-...
./scripts/phase-0-smoke.sh       # writes notes/phase-0-output/
```

---

## Using with Claude Code

This project includes a `CLAUDE.md` that gives Claude Code full context on architecture, commands, and invariants.

```bash
claude    # start Claude Code — reads CLAUDE.md automatically
```

---

## Credits

- **[Phala Network / Redpill](https://redpill.ai)** — TEE-attested inference (Intel TDX + NVIDIA CC)
- **[NowPayments](https://nowpayments.io)** — multi-crypto payment processing
- **[MoneroPay](https://moneropay.eu)** — self-hosted XMR payment daemon
- **[Caddy](https://caddyserver.com)** — automatic TLS reverse proxy
- **[FastAPI](https://fastapi.tiangolo.com)** — async Python web framework
- **[SQLCipher](https://www.zetetic.net/sqlcipher/)** — AES-256 encrypted SQLite

---

## License

AGPL-3.0. See [LICENSE](LICENSE).

If you run a modified version of phantom-api as a public service, AGPL requires you to make the modified source available to your users.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
