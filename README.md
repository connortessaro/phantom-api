# Phantom API

Anonymous AI inference. Monero in, OpenAI-compatible API out. Inference runs in Phala TEEs.

See [HANDOFF.md](HANDOFF.md) for full architecture rationale, security model, and build order.

## Layout

```
.
├── main.py              FastAPI app — all routes
├── payments.py          Wallet RPC over Tor SOCKS
├── db.py                SQLCipher access layer
├── config.py            Bundles, env vars, allowed models
├── pricing.py           Kraken XMR/USD ticker (5-min cache)
├── schema.sql           SQLite schema, applied on init
├── Caddyfile            TLS + reverse proxy
├── requirements.txt
├── frontend/            Static HTML/CSS/JS (no frameworks, no CDN)
├── systemd/             phantom-api + phantom-poller units
├── scripts/             setup-vps.sh, unlock.sh, poll-payments.py, sweep.py
├── pc-side/             Operator's PC: torrc, start-wallet.sh
└── data/                SQLCipher DB lives here (LUKS-encrypted partition)
```

## Local dev (skeleton smoke)

```bash
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit
export PHANTOM_DB_PASSPHRASE=devonly-replace-me
python -c "import asyncio, db; asyncio.run(db.init_db('data/phantom.db'))"
uvicorn main:app --reload
```

Note: full local run requires a reachable Monero wallet RPC and a Phala key.
For phase 0 sanity checks, use `scripts/phase-0-smoke.sh` first.

## Build phases

See HANDOFF.md "Build Order". TL;DR:

1. **Phase 0** — verify Phala/Redpill endpoint and per-model attestation. Lock `ALLOWED_MODELS`.
2. **Phase 1** — backend end-to-end on stagenet.
3. **Phase 2** — frontend.
4. **Phase 3** — VPS + LUKS + Tor + Caddy.
5. **Phase 4** — security checklist, friend test, launch.
