# Phantom API

Anonymous AI inference proxy. Pay any crypto, get an OpenAI-compatible API key. Hardware-attested TEE inference via Phala Redpill. No accounts, no email, no logs, SQLCipher-encrypted at rest.

## Architecture

```
                    ┌─────────────────────────┐
                    │   customer browser /     │
                    │   any OpenAI SDK         │
                    └────────────┬────────────┘
                                 │ HTTPS / Tor
                                 ▼
                          ┌──────────────┐
                          │    Caddy     │  TLS + static frontend + reverse proxy
                          └──────┬───────┘
                                 ▼
                          ┌──────────────┐
                          │  phantom-api │  FastAPI, 127.0.0.1:8000
                          │  (uvicorn)   │  SQLCipher DB (passphrase via tmpfs)
                          └──────┬───────┘
                                 │
                ┌────────────────┴────────────────┐
                ▼                                 ▼
        ┌──────────────┐                  ┌──────────────┐
        │  NowPayments │                  │ Redpill / Phala│
        │  (payments)  │                  │  (inference) │
        └──────────────┘                  └──────────────┘
```

Customer pays → NowPayments → phantom receives webhook → customer's next `/status` poll returns plaintext API key once. Customer uses key against `/v1/chat/completions` → phantom forwards to Redpill (some models run in Intel TDX + NVIDIA CC TEEs) → bills usage from customer's prepaid credit.

## Quick start (dev)

```bash
git clone https://github.com/connortessaro/phantom-api.git
cd phantom-api
./setup.sh                    # detects OS, installs sqlcipher, creates venv
cp .env.example .env          # fill in REDPILL_API_KEY + NP_API_KEY + NP_IPN_SECRET
export PHANTOM_DB_PASSPHRASE=devonly-replace-me
python -c "import asyncio, db; asyncio.run(db.init_db('data/phantom.db'))"
PHANTOM_DEV=1 uvicorn main:app --reload
```

Open http://127.0.0.1:8000 — static frontend served by FastAPI in dev mode (Caddy handles it in prod).

## Prod deploy (VPS)

1. Provision a Debian/Ubuntu VPS, point your domain at it.
2. Replace `phantom.codes` with your domain in:
   - `Caddyfile`
   - `frontend/sitemap.xml`, `frontend/robots.txt`
   - `frontend/index.html`, `frontend/docs.html`, `frontend/models.html`, `frontend/terms.html`
3. Replace the `<your-onion-here.onion>` placeholder if you set up a Tor hidden service.
4. Replace `frontend/pgp.txt` with your own public key + the `<YOUR-PGP-FINGERPRINT>` references in `frontend/terms.html` + `frontend/docs.html`.
5. SCP this repo to `/opt/phantom-api/` on the VPS, run `./scripts/setup-vps.sh` as root.
6. SSH in, run `sudo /opt/phantom-api/scripts/unlock.sh` after every boot.

See [RUNBOOK.md](RUNBOOK.md) for ops + NowPayments dashboard config.

## Layout

```
.
├── main.py             FastAPI app — all routes
├── nowpayments.py      NowPayments REST + IPN verifier
├── db.py               SQLCipher access layer
├── catalog.py          Live model catalog (live → disk → builtin fallback)
├── config.py           Bundles, env vars, model pricing
├── schema.sql          SQLite schema
├── Caddyfile           TLS + reverse proxy + static
├── frontend/           HTML/CSS/JS, no frameworks, no CDN
├── systemd/            phantom-api.service
├── scripts/            setup-vps.sh, unlock.sh, deploy.sh, backup-db.py
└── tests/              pytest, 55 tests
```

## Payment rail

Single PSP: NowPayments. Customer picks one of two checkouts via the `rail` field on `POST /v1/purchase`:

| Rail | Behavior | Surcharge |
|---|---|---|
| `xmr` | NowPayments invoice locked to XMR | 0% |
| `multi` | Multi-crypto checkout (BTC/ETH/USDT/USDC/LTC/SOL/DOGE) | +5% |

Both routes hit the same PSP. Customer never KYCs; operator (you) does, once. NowPayments converts non-XMR deposits to XMR and forwards to your cold wallet.

To self-sovereign-host without a PSP, fork this repo and add MoneroPay or direct `monero-wallet-rpc` integration — earlier git history has reference implementations.

## Privacy invariants

Phantom enforces these in code; PRs that violate them will be closed (see [CONTRIBUTING.md](CONTRIBUTING.md)):

- **No IP logging.** Caddy `log { output discard }`, uvicorn `--no-access-log`, rate-limiter keyed on hashed IP held only in memory.
- **No prompt/completion logging.** Body whitelists strip `user`/`metadata` before forwarding; `safe_exception_handler` logs only exception type.
- **API keys hashed.** SHA-256 only. Plaintext returned exactly once at claim/rotate. Recovery intentionally impossible.
- **No PII columns.** Schema has no email, name, IP, or address fields.
- **DB encrypted at rest.** SQLCipher AES-256. Passphrase via tmpfs at `/run/phantom/phantom-secrets.env`, never on disk.
- **Body whitelist to upstream.** Strict allow-list of fields forwarded to Redpill.

See [SECURITY.md](SECURITY.md) for the full threat model + known limitations (operator subpoena, physical seizure, etc).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v   # 55 tests: cost math, image cost, stream parser, concurrency
```

## License

AGPL-3.0. See [LICENSE](LICENSE). If you run phantom as a hosted service, modifications must be published under AGPL — this matches the "run your own privacy proxy" philosophy.

## Credits

- [Phala Network](https://phala.network) / [Redpill](https://redpill.ai) — TEE inference gateway
- [NowPayments](https://nowpayments.io) — multi-crypto PSP
- [Caddy](https://caddyserver.com), [FastAPI](https://fastapi.tiangolo.com), [SQLCipher](https://www.zetetic.net/sqlcipher/)
