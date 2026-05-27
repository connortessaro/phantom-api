# Phantom API — Handoff Document v3 (Path C: VPS + LUKS + SQLCipher + Home Wallet via Tor)

> Read this entire document before writing any code. v3 supersedes v1 and v2.
>
> **Architecture summary:** FastAPI proxy on a regular VPS with LUKS-encrypted disk; SQLCipher DB unlocked manually via SSH on every boot; Monero wallet runs on the operator's personal PC and is exposed to the VPS exclusively via a Tor hidden service; inference forwarded to Phala Confidential AI.
>
> **Honest framing (read this first):**
> - This is a privacy-focused service, not a hardware-enforced enclave service. While the VPS is running, the FastAPI process holds prompts in plaintext in RAM. The hosting provider could in principle dump memory. We mitigate by: no logs, no third parties in the inference path, encrypted at rest, anonymous payment.
> - Inference itself runs in Phala TEEs and is attested. We forward Phala's per-request attestation to users. That part is cryptographic.
> - Operator (you) is identifiable via VPS contract. Users are anonymous via Monero + no-account flow. This asymmetry is irreducible at this architecture tier.
> - Marketing copy MUST reflect this. Do not claim "TEE-attested API" or "cryptographically private proxy." Sell on what's true: anonymous payment, no accounts, no logs, encrypted at rest, Phala-attested inference, no KYC.

---

## What changed from v1 and v2

| Concern | v1 (drafted) | v2 (path B, Phala CVM) | **v3 (this doc)** |
|---|---|---|---|
| Proxy host | VPS, plain | Phala Cloud CVM (TDX TEE) | VPS with LUKS root |
| Plaintext exposure | "trust me" | enclave-sealed | **"trust me" + LUKS at rest** |
| DB at rest | SQLite on encrypted disk | SQLCipher, dstack-sealed key | **SQLCipher, manual SSH passphrase on boot** |
| Web server | Caddy | Phala ingress | Caddy + Let's Encrypt |
| Process manager | systemd | Docker via Phala | systemd |
| Wallet location | Same VPS as API | External $5 wallet VPS | **Operator's personal PC** |
| Wallet ↔ proxy transport | localhost | WireGuard | **Tor hidden service** |
| Attestation | Phala only | Phala + own CVM TDX quote | Phala only |
| Reproducible build | n/a | required | n/a |
| Estimated cost | $5-20/mo VPS | $30-100/mo CVM + $5 wallet VPS | **$5-20/mo VPS only** |
| Operator anonymity | partial (VPS contract) | partial | partial (VPS contract + home IP if Tor breaks) |

v3 fixes carried over from earlier review:
- Streaming budget decremented in `try/finally` (won't leak on disconnect)
- Atomic `ready → completed` payment transition (no race on key issuance)
- slowapi keyed off API-key hash, not IP (consistent with no-IP-logging)
- Exception handler scrubs request body before logging
- `datetime.now(timezone.utc)` instead of deprecated `utcnow()`

---

## What we're building (unchanged)

Anonymous AI inference API. Pay in Monero, get an API key, call an OpenAI-compatible endpoint. Phala handles inference inside their TEEs.

1. User pays XMR → gets API key (shown once)
2. User calls `/v1/chat/completions`
3. We forward to Phala Confidential AI, decrement budget on response
4. User can fetch Phala's per-request inference attestation via `/v1/inference-attest`

---

## Tech Stack (path C, finalized)

| Component | Choice | Why |
|---|---|---|
| VPS | **Hetzner, OVH, or similar (paid in crypto if possible)** | Cheap, predictable, decent privacy posture relative to US providers |
| Disk | **LUKS2 full-disk encryption on root** | Protects data when powered off / disk seized |
| Web server | **Caddy** | Auto-TLS, minimal config, no Certbot |
| App framework | **FastAPI + uvicorn** | Async, OpenAI-compatible patterns, streaming |
| Database | **SQLCipher** (sqlcipher3-binary) | AES-256 at rest; passphrase entered manually on boot |
| Inference | **Phala Confidential AI** | TEE-attested OpenAI-compatible inference (the one true cryptographic guarantee in the stack) |
| Wallet | **monero-wallet-rpc on operator's personal PC** | Operator controls keys; never on the VPS |
| Wallet transport | **Tor hidden service** on PC; VPS reaches it via local Tor SOCKS | VPS never learns operator's home IP |
| Pricing source | **Kraken public ticker** (XMR/USD), 5-min cache | No API key, no third-party tracker |
| Rate limiting | **slowapi** with custom key_func (API-key hash, not IP) | Consistent with no-IP stance |
| HTTP client | **httpx** (with SOCKS support via httpx-socks) | Async; reaches .onion via Tor |
| Process mgr | **systemd** | Native, auto-restart |
| QR | **qrcode-svg** inline (~5 KB) | No CDN |

**Explicitly rejected (unchanged from v1):**
- ❌ PostgreSQL — overkill, more attack surface
- ❌ Supabase / managed DB — third party in data path
- ❌ Sentry / third-party error tracking — exfils prompts on exception
- ❌ Docker — not needed; systemd handles three units fine
- ❌ Redis — SQLite enough
- ❌ default slowapi (IP-keyed) — contradicts privacy stance

---

## Architecture

```
                   ┌─ User (anonymous, possibly via Tor)
                   │
                   ▼ HTTPS
              ┌─────────────────────────────────────────┐
              │  Caddy (TLS via Let's Encrypt)          │
              │  on VPS (LUKS-encrypted root)           │
              └─────────────────────────────────────────┘
                   │
                   ▼ HTTP (localhost)
              ┌─────────────────────────────────────────┐
              │  FastAPI (uvicorn 127.0.0.1:8000)       │
              │  /v1/chat/completions                   │
              │  /v1/purchase                           │
              │  /v1/purchase/{id}/status               │
              │  /v1/key/balance                        │
              │  /v1/inference-attest                   │
              │  /health                                │
              └─────────────────────────────────────────┘
                  │  │
                  │  ▼
                  │  ┌───────────────────────────┐
                  │  │  SQLCipher (./data/phantom.db) │
                  │  │  passphrase loaded on boot │
                  │  │  via SSH-fed env file      │
                  │  └───────────────────────────┘
                  │
                  ▼
              ┌───────────────────────────┐         ┌──────────────────────────────┐
              │  Phala Confidential AI    │         │  Tor client (SOCKS5 :9050)   │
              │  (HTTPS, external TEE)    │         │  on VPS                       │
              └───────────────────────────┘         └──────────────────────────────┘
                                                              │
                                                              │ SOCKS5 to .onion
                                                              ▼
                                                   ╔══════════════════════════╗
                                                   ║ Operator's personal PC    ║
                                                   ║  - Tor hidden service     ║
                                                   ║    (wallet RPC on :18083) ║
                                                   ║  - monero-wallet-rpc      ║
                                                   ║  - monerod (local or      ║
                                                   ║    trusted remote)        ║
                                                   ╚══════════════════════════╝
```

**Privacy posture this architecture enforces:**

1. **No IP logging.** Caddy access logging set to `discard`. FastAPI never logs IPs. uvicorn run with `--no-access-log`.
2. **No content logging.** Prompts and completions never written to disk; exception handler scrubs bodies; uvicorn access log disabled; systemd `StandardError=null` for the wallet poller.
3. **No PII storage.** No emails, names, IPs in any table.
4. **Keys hashed.** API keys stored as SHA-256 hex; recovery impossible.
5. **DB encrypted at rest.** SQLCipher key never on disk. Operator unlocks via SSH after each boot.
6. **Inference attested.** Phala's per-request TEE attestation forwarded to user via `/v1/inference-attest`.
7. **Wallet isolated.** VPS holds zero funds. All XMR lives on operator's PC; VPS only sees payment metadata via Tor.
8. **VPS contains no link to operator's home IP.** Tor hidden service breaks that link.

**These are not optional.** Violating any breaks the (honest) value proposition.

---

## File Structure

```
phantom-api/
├── Caddyfile                    # TLS termination + reverse proxy
├── main.py                      # FastAPI app — all routes
├── payments.py                  # Wallet RPC client via Tor SOCKS
├── db.py                        # SQLCipher access layer
├── config.py                    # Bundles, env vars, constants
├── schema.sql                   # SQLite schema, applied on first run
├── pricing.py                   # XMR/USD with caching
├── data/                        # Gitignored; lives on LUKS-encrypted disk
│   └── phantom.db               # SQLCipher database
├── frontend/
│   ├── index.html               # Landing + purchase flow
│   ├── docs.html                # API documentation
│   ├── style.css
│   └── purchase.js              # Payment polling
├── systemd/
│   ├── phantom-api.service      # FastAPI service (waits for DB unlock)
│   ├── phantom-unlock.service   # Reads passphrase from runtime file (template)
│   └── tor.service              # System Tor; usually distro-managed
├── scripts/
│   ├── setup-vps.sh             # First-time VPS bootstrap
│   ├── poll-payments.py         # Background worker for payment confirmation
│   ├── unlock.sh                # SSH-side: prompts you for passphrase, writes runtime file
│   └── sweep.py                 # Run on PC: sweeps wallet balance to cold address
├── pc-side/                     # Files for operator's personal PC
│   ├── torrc.example            # Tor config exposing wallet RPC as hidden service
│   ├── start-wallet.sh
│   └── README-PC.md             # PC setup checklist
├── .env.example                 # ENV variables (no passphrase)
├── .gitignore                   # MUST include: .env, data/, *.db, *.passphrase
├── requirements.txt
└── README.md
```

---

## Database Schema (SQLCipher)

Same schema as v1/v2; only the storage layer changes.

```sql
-- schema.sql

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash        TEXT PRIMARY KEY,
    token_budget    INTEGER NOT NULL,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id      TEXT PRIMARY KEY,
    xmr_address     TEXT NOT NULL,
    xmr_amount      TEXT NOT NULL,        -- decimal string; never float
    token_bundle    INTEGER NOT NULL,
    bundle_name     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- status values: pending, confirming, ready, completed, expired
    key_hash        TEXT,
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT,
    expires_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash        TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_address ON payments(xmr_address);
CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_log(key_hash);
```

**Status flow:** `pending` → `confirming` → `ready` → `completed` (or `expired`). Key issuance happens on the `ready → completed` transition, performed atomically when the user polls — see "Plaintext Key Race Fix" below.

**NEVER add columns for:** IP addresses, user agents, prompt content, completion content, email/contact info, sub-minute timestamps (timing side-channel).

### SQLCipher initialization

```python
# db.py
import os, asyncio
import sqlcipher3 as sqlite3   # sqlcipher3-binary package

_lock = asyncio.Lock()
_conn = None

async def init_db(path: str):
    """Open SQLCipher DB using passphrase from PHANTOM_DB_PASSPHRASE env."""
    global _conn
    passphrase = os.environ.get("PHANTOM_DB_PASSPHRASE")
    if not passphrase:
        raise RuntimeError("PHANTOM_DB_PASSPHRASE not set — DB cannot be opened")

    _conn = sqlite3.connect(path, check_same_thread=False)
    # Quote properly to avoid injection in the PRAGMA. The passphrase is operator-controlled but still escape quotes.
    safe = passphrase.replace('"', '""')
    _conn.execute(f'PRAGMA key = "{safe}"')
    _conn.execute("PRAGMA cipher_page_size = 4096")
    _conn.execute("PRAGMA kdf_iter = 256000")
    _conn.execute("PRAGMA journal_mode = WAL")
    _conn.execute("PRAGMA synchronous = NORMAL")

    # Verify the key actually decrypts something; SQLCipher won't error on bad key
    # until a read is attempted.
    try:
        _conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError:
        raise RuntimeError("SQLCipher passphrase rejected — check input")

    with open("schema.sql") as f:
        _conn.executescript(f.read())
    _conn.commit()

    # Immediately wipe passphrase from environment to shrink window of exposure.
    os.environ.pop("PHANTOM_DB_PASSPHRASE", None)
```

WAL files (`*-wal`, `*-shm`) are also encrypted by SQLCipher.

---

## SQLCipher Manual Unlock Flow

Goal: passphrase never persists on disk.

### Boot sequence

1. VPS boots; LUKS root is unlocked (either via Hetzner rescue/recovery console at install time using `dropbear-initramfs`, or as an unencrypted root with only `data/` on a LUKS-encrypted partition — operator's choice; see Phase 3 below for tradeoffs).
2. Caddy, Tor, and a one-shot `phantom-unlock` service are up.
3. `phantom-api.service` is **disabled at boot** or waits on `phantom-unlock.service` to complete.
4. Operator SSHes in and runs `./scripts/unlock.sh`. That script:
   - Prompts for the passphrase (no echo).
   - Writes it to `/run/phantom/phantom-db.env` (mode `0400`, owned by the `phantom` user). `/run` is a tmpfs — never on disk.
   - `systemctl start phantom-api.service`.
5. `phantom-api.service` loads `/run/phantom/phantom-db.env`, starts uvicorn. App calls `init_db()` which:
   - Reads `PHANTOM_DB_PASSPHRASE`.
   - Opens the DB.
   - Wipes the env var.
6. Optionally: after successful start, a `systemd` `ExecStartPost=` step `shred -u /run/phantom/phantom-db.env` so the file disappears once consumed.

### `systemd/phantom-api.service`

```ini
[Unit]
Description=Phantom API
After=network-online.target tor.service
Wants=network-online.target
ConditionPathExists=/run/phantom/phantom-db.env

[Service]
Type=simple
User=phantom
Group=phantom
WorkingDirectory=/opt/phantom-api
EnvironmentFile=/opt/phantom-api/.env
EnvironmentFile=/run/phantom/phantom-db.env
ExecStartPre=/bin/sh -c 'test -s /run/phantom/phantom-db.env'
ExecStart=/opt/phantom-api/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
ExecStartPost=/bin/sh -c 'shred -u /run/phantom/phantom-db.env || rm -f /run/phantom/phantom-db.env'
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=null

[Install]
WantedBy=multi-user.target
```

Notes:
- `ConditionPathExists` makes the unit refuse to start if the runtime passphrase file is missing.
- `StandardError=null` prevents tracebacks from landing in journal (uncaught traceback could contain request fragments).
- `StandardOutput=journal` is fine *if* the app's logger is configured to never include request bodies — see "Log Hygiene" below. If you want to be paranoid, set `StandardOutput=null` and rely solely on structured app-level metrics.

### `scripts/unlock.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR=/run/phantom
ENV_FILE="$RUNTIME_DIR/phantom-db.env"

sudo mkdir -p "$RUNTIME_DIR"
sudo chmod 700 "$RUNTIME_DIR"
sudo chown phantom:phantom "$RUNTIME_DIR"

# Prompt with no echo
read -s -p "SQLCipher passphrase: " PASS
echo

# Write the env file as the phantom user with strict perms
sudo install -o phantom -g phantom -m 0400 /dev/null "$ENV_FILE"
echo "PHANTOM_DB_PASSPHRASE=$PASS" | sudo tee "$ENV_FILE" > /dev/null
unset PASS

sudo systemctl start phantom-api.service
sudo systemctl status phantom-api.service --no-pager
```

After a reboot the API is down until you run `unlock.sh`. That is the price of the no-on-disk-passphrase property. Acceptable for low-frequency reboots; do not enable any "auto-unlock" feature.

---

## Configuration

```python
# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# Phala Confidential AI (verify exact endpoint via Phala Cloud console)
PHALA_API_BASE = os.environ.get("PHALA_API_BASE", "https://api.phala.network/openai/v1")
PHALA_API_KEY  = os.environ["PHALA_API_KEY"]

# Monero wallet RPC reached via local Tor SOCKS to operator's hidden service
WALLET_ONION = os.environ["WALLET_ONION"]            # e.g. abc123xyz.onion
WALLET_RPC_URL = f"http://{WALLET_ONION}:18083/json_rpc"
TOR_SOCKS_URL = os.environ.get("TOR_SOCKS_URL", "socks5h://127.0.0.1:9050")
MIN_CONFIRMATIONS = 10
PAYMENT_EXPIRY_MINUTES = 60

# DB
DB_PATH = os.environ.get("DB_PATH", "/opt/phantom-api/data/phantom.db")

# Token bundles: name -> (tokens, usd_price, validity_days)
BUNDLES = {
    "starter":  (1_000_000,    6,   90),
    "standard": (5_000_000,    30,  90),
    "pro":      (20_000_000,   120, 180),
    "bulk":     (100_000_000,  480, 365),
}

MODELS = {
    "deepseek-v3":     {"description": "General reasoning, coding, analysis", "context": 128000},
    "qwen3-coder":     {"description": "Code generation, debugging",          "context": 128000},
    "llama-3.1-405b":  {"description": "General purpose",                     "context": 128000},
    "qwen3-235b":      {"description": "Balanced reasoning and creative",     "context": 128000},
}

PRICE_PER_MILLION_TOKENS_USD = 6   # tune against actual Phala per-model pricing
```

---

## API Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/chat/completions` | Bearer | Validate key, check budget, proxy to Phala, decrement on response |
| GET  | `/v1/models` | none | Hardcoded list from `config.MODELS` |
| POST | `/v1/purchase` | none | Generate unique subaddress; return payment instructions |
| GET  | `/v1/purchase/{payment_id}/status` | none | Poll; returns key once on `ready → completed` |
| GET  | `/v1/key/balance` | Bearer | Return tokens remaining, expiry |
| GET  | `/v1/inference-attest?model=...` | none | Returns Phala's per-request attestation for the model (forwarded) |
| GET  | `/health` | none | `{status: "ok"}` |

---

## Critical Implementation Details

### Authentication

```python
import hashlib

def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()

auth = request.headers.get("Authorization", "")
if not auth.startswith("Bearer "):
    raise HTTPException(401, "Missing API key")
key = auth.removeprefix("Bearer ").strip()
key_hash = hash_key(key)
```

### Budget Enforcement (atomic)

```python
from datetime import datetime, timezone

async def decrement_budget(key_hash: str, tokens_used: int, model: str) -> bool:
    """Atomic decrement. Returns False if insufficient budget or inactive key."""
    async with _lock:
        cur = _conn.execute(
            "UPDATE api_keys "
            "SET token_budget = token_budget - ?, tokens_used = tokens_used + ? "
            "WHERE key_hash = ? AND token_budget >= ? AND is_active = 1",
            (tokens_used, tokens_used, key_hash, tokens_used)
        )
        if cur.rowcount == 0:
            return False
        _conn.execute(
            "INSERT INTO usage_log (key_hash, model, prompt_tokens, completion_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key_hash, model, 0, tokens_used,
             datetime.now(timezone.utc).isoformat())
        )
        _conn.commit()
    return True
```

### Streaming Responses (with disconnect safety)

```python
async def stream_phala(body: dict, key_hash: str):
    total_tokens = 0
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{PHALA_API_BASE}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {PHALA_API_KEY}"}
            ) as resp:
                async for chunk in resp.aiter_text():
                    total_tokens += count_tokens_in_sse_chunk(chunk, body["model"])
                    yield chunk
    finally:
        if total_tokens > 0:
            await decrement_budget(key_hash, total_tokens, body["model"])
```

`count_tokens_in_sse_chunk` uses Phala's `usage` block when present, falls back to a local `tiktoken` count on disconnect.

### Pre-flight Budget Check

Before forwarding to Phala, require `token_budget >= max_tokens_requested` (parse from request body; default to 4096 if not specified). Prevents a single oversized request from massively overdrawing.

### Plaintext Key Race Fix (atomic ready→completed)

```python
import secrets
from datetime import datetime, timedelta, timezone

async def claim_and_issue(payment_id: str) -> str | None:
    """Atomically transition payment ready->completed and return plaintext key once."""
    plaintext = "sk-" + secrets.token_urlsafe(48)
    key_hash = hash_key(plaintext)
    now = datetime.now(timezone.utc)

    async with _lock:
        cur = _conn.execute("""
            UPDATE payments
            SET status = 'completed', key_hash = ?, confirmed_at = ?
            WHERE payment_id = ? AND status = 'ready'
        """, (key_hash, now.isoformat(), payment_id))
        if cur.rowcount == 0:
            return None

        row = _conn.execute(
            "SELECT token_bundle, bundle_name FROM payments WHERE payment_id = ?",
            (payment_id,)
        ).fetchone()
        tokens, bundle = row
        validity = BUNDLES[bundle][2]
        expires = (now + timedelta(days=validity)).isoformat()

        _conn.execute("""
            INSERT INTO api_keys (key_hash, token_budget, tokens_used, created_at, expires_at, is_active)
            VALUES (?, ?, 0, ?, ?, 1)
        """, (key_hash, tokens, now.isoformat(), expires))
        _conn.commit()

    return plaintext
```

The background poller only moves `confirming → ready`. Key generation happens here, atomically, once. Plaintext returned exactly once; never persisted.

### Rate Limiting Without IPs

```python
from slowapi import Limiter
from fastapi import Request

def key_for_limit(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return hash_key(auth.removeprefix("Bearer ").strip())
    return request.cookies.get("phantom_session", "global")

limiter = Limiter(key_func=key_for_limit)

@app.post("/v1/chat/completions")
@limiter.limit("60/minute")
async def chat_completions(...): ...

@app.post("/v1/purchase")
@limiter.limit("10/minute")
async def purchase(...): ...
```

Trade-off: `/v1/purchase` abuse is harder to pin to a specific attacker. Accept it; that's the price of the privacy stance.

### Log Hygiene

```python
import logging

logging.basicConfig(level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("uvicorn.access").disabled = True

@app.exception_handler(Exception)
async def safe_exception_handler(request, exc):
    # NEVER include request body in the log message.
    logging.error(f"Unhandled {type(exc).__name__} on {request.method} {request.url.path}")
    return JSONResponse({"error": "internal_error"}, status_code=500)
```

Audit your codebase: zero occurrences of `print(...)`, zero `logger.*` calls that interpolate request bodies, headers, or response content. Caddy log set to `output discard`.

### Monero Amount Precision

```python
from decimal import Decimal

PICONERO = Decimal("1e12")

def xmr_to_piconero(xmr: Decimal) -> int:
    return int(xmr * PICONERO)

def piconero_to_xmr_str(pico: int) -> str:
    return f"{Decimal(pico) / PICONERO:.12f}".rstrip("0").rstrip(".")
```

Never use `float` for amounts. Store as decimal string in DB.

### Payment Tolerance

Policy: accept ≥98% of expected amount; reject below. Surplus credited as bonus tokens proportional to overpayment, capped at +50%. Document this clearly on the purchase page.

---

## Wallet RPC over Tor

### PC side (operator's personal computer)

**Goal:** expose `monero-wallet-rpc` as a Tor hidden service so the VPS can reach it without ever learning your home IP.

#### `pc-side/torrc.example`

Add this to your existing `torrc` (Linux/macOS: `/etc/tor/torrc`; Windows: `C:\Users\<you>\AppData\Roaming\tor\torrc`):

```
HiddenServiceDir /var/lib/tor/phantom-wallet/
HiddenServiceVersion 3
HiddenServicePort 18083 127.0.0.1:18083
```

After `sudo systemctl restart tor` (or equivalent), read the generated onion address:

```
sudo cat /var/lib/tor/phantom-wallet/hostname
```

Set this as `WALLET_ONION` in the VPS `.env`. **Treat the hostname like a secret-ish identifier** — anyone who knows it can attempt to connect (though they still need to bypass any wallet auth).

#### `pc-side/start-wallet.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

WALLET_DIR="$HOME/.phantom-wallet"
mkdir -p "$WALLET_DIR"

# Bind to localhost only; Tor handles inbound routing.
monero-wallet-rpc \
    --rpc-bind-port 18083 \
    --rpc-bind-ip 127.0.0.1 \
    --rpc-login phantom:"$(cat "$WALLET_DIR/rpc-password")" \
    --wallet-file "$WALLET_DIR/phantom" \
    --password-file "$WALLET_DIR/wallet-password" \
    --daemon-address node.sethforprivacy.com:18089 \
    --trusted-daemon \
    --log-level 0 \
    --confirm-external-bind
```

Use `--rpc-login` so even if the .onion leaks the wallet still requires HTTP Basic. Generate the password once: `openssl rand -hex 24 > ~/.phantom-wallet/rpc-password`. Share `phantom:<that-password>` with the VPS via env.

#### `pc-side/README-PC.md` (operator checklist)

- [ ] Install Tor (`brew install tor` / `apt install tor`).
- [ ] Install Monero CLI (download from getmonero.org, verify GPG signature).
- [ ] Create wallet via `monero-wallet-cli` (keep view + spend keys offline backup).
- [ ] Generate RPC password (`openssl rand -hex 24`).
- [ ] Configure `torrc` per above; restart Tor; capture `.onion` hostname.
- [ ] Run `start-wallet.sh` (consider `launchd`/systemd-user for restart-on-crash).
- [ ] Ensure PC is on whenever payments need to be confirmed (failed polls = users wait).
- [ ] Set up `sweep.py` cron — sweep float above $500 worth to a cold address daily.

### VPS side

#### Tor client install

```bash
apt-get install -y tor
systemctl enable --now tor
# Verify SOCKS at 127.0.0.1:9050:
curl --socks5-hostname 127.0.0.1:9050 https://check.torproject.org
```

#### `payments.py` (excerpt)

```python
import httpx
from httpx_socks import AsyncProxyTransport
from config import WALLET_RPC_URL, TOR_SOCKS_URL

_transport = AsyncProxyTransport.from_url(TOR_SOCKS_URL)

async def rpc(method: str, params: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
    async with httpx.AsyncClient(transport=_transport, timeout=30.0,
                                 auth=("phantom", WALLET_RPC_PASSWORD)) as c:
        r = await c.post(WALLET_RPC_URL, json=payload)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"wallet rpc error: {data['error']}")
        return data["result"]
```

`WALLET_RPC_PASSWORD` lives in `/opt/phantom-api/.env`. Reachable only while LUKS is unlocked. If you want a stricter layer: also feed it via the same SSH-mounted runtime file as the SQLCipher passphrase (defense in depth).

---

## Phala Inference Integration

`/v1/inference-attest` exposes whatever attestation token Phala returns per request. Forward it to the user verbatim.

**Phase 0 task — verify in Phala Cloud console (before any code):**
1. Exact base URL for Confidential AI
2. Auth header format
3. Available model IDs (update `config.MODELS`)
4. Per-model pricing (set `PRICE_PER_MILLION_TOKENS_USD` to ~1.7x highest, or per-model markup)
5. How attestation is returned (response headers vs body)
6. Refund/credit policy on failed requests

Save responses in a `notes/phase-0-phala.md` file before writing code.

---

## Caddyfile

```
# Apex: frontend + API (frontend uses relative paths, no CORS).
phantom.codes {
    handle /docs* {
        root * /opt/phantom-api/frontend
        file_server
    }

    handle / {
        root * /opt/phantom-api/frontend
        file_server
    }

    handle /v1/* {
        reverse_proxy localhost:8000
    }

    handle /health {
        reverse_proxy localhost:8000
    }

    log {
        output discard
    }

    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
        Permissions-Policy "interest-cohort=()"
        -Server
    }
}

# API host for external SDK consumers: base_url = "https://api.phantom.codes/v1"
api.phantom.codes {
    handle /v1/*   { reverse_proxy localhost:8000 }
    handle /health { reverse_proxy localhost:8000 }
    handle         { respond 404 }
    log { output discard }
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
        -Server
    }
}
```

Verify Caddy strips its own `Server` header (the `-Server` directive). Some FastAPI middleware also leaks a `Server: uvicorn` header; disable with `uvicorn --server-header=False` if available, or strip in Caddy.

---

## Frontend Requirements (unchanged from v1; honesty edits)

### Design constraints
- No JS frameworks, React, Vue, build steps
- No CDN dependencies (everything self-hosted)
- No Google Fonts (system fonts or self-host)
- No analytics, no tracking pixels, no cookies
- Works without JS (purchase flow degrades gracefully)
- Dark theme default
- Loads in under 2 s on slow links
- Tor-friendly (no JS hitting external domains)

### Pages

**index.html:**
- Hero: privacy-focused AI API, anonymous payment, no accounts. Avoid words "TEE-attested API," "cryptographically private proxy," "phantom-grade encryption."
- Models: cards for each available model
- Pricing: bundles with transparent breakdown ("Phala charges us $X, we charge $Y")
- Purchase flow: select bundle → unique XMR address + QR → poll → display key
- How it works: 3 steps
- **Trust boundaries section (NEW, required):** explicit honest list of what we do/don't guarantee — see "Honest Disclosure" below

**docs.html:**
- Quick start: curl, Python, JS examples
- Endpoint reference (including `/v1/inference-attest`)
- Model comparison
- Phala inference attestation: what it proves and how to verify
- FAQ

---

## Honest Disclosure (bake into index.html and docs.html)

```
What we cryptographically guarantee:
  - Your inference runs inside a Phala TEE (you can verify per-request via /v1/inference-attest)
  - Your Monero payment cannot be linked to your API key — different subaddresses, never correlated
  - Your API key is stored only as a SHA-256 hash; we cannot recover it

What we do — but ask you to trust us on:
  - We do not log prompts, completions, IP addresses, or user agents
  - We do not retain any PII; we never collect emails, names, or identifiers
  - We rate-limit by API-key hash, not by IP

What we cannot guarantee (and won't pretend to):
  - The proxy server (this service) is a regular VPS. While running, it holds your prompt in
    memory in plaintext between receiving it from you and forwarding it to Phala. If the
    hosting provider were compelled to dump RAM, prompts in flight could be exposed.
  - The hosting provider could in theory MITM us. We use TLS and HSTS; verify the cert
    via your client and Certificate Transparency logs.

What we are not:
  - A custodial wallet — keys are bearer; lose them and they're gone
  - An identity provider — no recovery, no support tickets that ask "what's your email"
  - A logging service — we don't have the data to help you debug your prompts
```

This is the difference between honest privacy product and Venice-AI-style overpromise. Sophisticated users will look for this; not having it is a red flag.

---

## Build Order

### Phase 0: Verify Phala and PC-side wallet (Day 0, ~3 hrs)

0a. Log into Phala Cloud console; confirm endpoint URL, auth, model IDs, pricing, attestation flow.
0b. On PC, install Tor + Monero CLI, set up wallet (stagenet first), configure hidden service, verify reachable via `torsocks curl http://<onion>:18083/json_rpc -d '...'`.
0c. Confirm wallet RPC can call `make_uri`, `get_payments`, etc. via Tor.
0d. Stop and resolve any issues before writing code.

### Phase 1: Backend end-to-end locally (Day 1)

1. `pip install fastapi uvicorn httpx httpx-socks sqlcipher3-binary slowapi python-dotenv tiktoken qrcode-svg-py`.
2. Build `db.py`, `config.py`, `schema.sql`. Test SQLCipher init with a throwaway passphrase.
3. Build `payments.py` — wallet RPC over Tor SOCKS to your PC's onion (stagenet).
4. Build `pricing.py` — Kraken XMR/USD with caching.
5. Build `main.py` — all routes; `/v1/inference-attest`.
6. Build `scripts/poll-payments.py` — background worker.
7. **Stagenet end-to-end test:**
   - POST `/v1/purchase` → get stagenet address.
   - Send stagenet XMR from another wallet.
   - Poll status → eventually `ready` → poll again → get plaintext key once.
   - POST `/v1/chat/completions` → real Phala response.
   - GET `/v1/key/balance` → decremented budget.
   - GET `/v1/inference-attest?model=...` → Phala attestation token.

### Phase 2: Frontend (Day 2)

8. Build `index.html` with purchase flow and trust-boundaries section.
9. Build `docs.html`.
10. Write `purchase.js` for polling.
11. Style with `style.css`.
12. Test full UX locally.

### Phase 3: Deployment (Day 2–3)

13. Provision VPS (Hetzner/OVH; pay in crypto if possible).
14. Install with **encrypted root** (LUKS2) via provider's installer. Alternatively, install with a normal root + separate LUKS-encrypted partition mounted at `/opt/phantom-api/data`. **Recommended path:**
    - **Strict:** encrypted root with `dropbear-initramfs` so you SSH in at boot to enter the LUKS passphrase. Highest security; most ops friction.
    - **Pragmatic (start here):** unencrypted root, LUKS-encrypted `data/` partition unlocked via SSH after boot; SQLCipher key entered separately. Tradeoff: app binaries and configs are in the clear; only DB + secrets are encrypted. Combined with the strict no-log policy this is acceptable for v1.
15. Run `scripts/setup-vps.sh`:
    - Create `phantom` user
    - Install Caddy, Tor, Python 3.12, sqlite/sqlcipher build deps (handled by binary wheel)
    - Set up ufw: only 22, 80, 443 inbound; SSH key-only auth
    - Disable systemd-resolved local DNS leak via `/etc/resolv.conf` if your VPS hits DNS often
    - Install systemd units (api + tor + unlock template)
16. Create `.env` directly on VPS (never commit). Include `PHALA_API_KEY`, `WALLET_ONION`, `WALLET_RPC_PASSWORD`.
17. Test Tor-to-onion reachability from VPS: `curl --socks5-hostname 127.0.0.1:9050 http://<wallet-onion>:18083/json_rpc -d '...'`.
18. Configure Caddyfile, point DNS, watch auto-TLS.
19. (Optional v1+) Set up `.onion` hidden service for the API itself.
20. End-to-end test with real mainnet XMR (≤ $5).

### Phase 4: Polish (Day 3)

21. Run security checklist (below).
22. Write README + launch post.
23. Trusted-friend test before going public.
24. Launch on r/Monero, r/LocalLLaMA, Hacker News.

---

## Security Checklist (verify before public launch)

- [ ] LUKS encrypts at minimum the data partition (`/opt/phantom-api/data`)
- [ ] SQLCipher passphrase never on disk; loaded via `/run/phantom/phantom-db.env` (tmpfs) and shredded after start
- [ ] `phantom-api.service` won't start without runtime passphrase file
- [ ] Caddy access logging set to `output discard`
- [ ] uvicorn started with `--no-access-log`
- [ ] No `print()` calls logging request data anywhere
- [ ] No `logger.info()` calls with prompt content
- [ ] Exception handler logs only method + path, never body
- [ ] `.env` in `.gitignore` and never committed
- [ ] `WALLET_RPC_PASSWORD` in `.env` (not in code, not in systemd unit visible to non-root)
- [ ] `Authorization: Bearer ...` is the only auth path; no API tokens in URLs
- [ ] SQLite/SQLCipher DB on encrypted disk (LUKS) AND encrypted via SQLCipher (defense in depth)
- [ ] Tor service active, reachable; VPS can talk to `<wallet-onion>:18083`
- [ ] Wallet hidden service uses HTTP Basic (`--rpc-login`) on top of Tor
- [ ] Server headers stripped (`-Server` in Caddy)
- [ ] SSL Labs grade A or A+
- [ ] HSTS header present, `max-age` ≥ 31536000
- [ ] Rate limiting active: `/v1/purchase` 10/min, `/v1/chat/completions` 60/min, keyed by API-key hash not IP
- [ ] No PII columns in any DB table
- [ ] Plaintext API keys returned exactly once via `claim_and_issue`
- [ ] Streaming `try/finally` decrements on disconnect
- [ ] Firewall (ufw) on VPS: only 22, 80, 443 inbound
- [ ] SSH on VPS: key-only auth, password auth disabled, root login disabled
- [ ] (Optional) VPS API also reachable via `.onion` for Tor-using clients
- [ ] PC firewall blocks inbound 18083 from anywhere; only Tor can reach it locally
- [ ] PC `sweep.py` cron live; hot float capped (e.g., $500 worth max on PC at any time)
- [ ] Honest "Trust boundaries" section visible on index.html (not buried in docs)

---

## Things That Will Come Up

### VPS reboots

API down until you run `unlock.sh`. Set up an out-of-band alert (email-free option: simple uptime monitor pinging `/health` from your phone) and a docs.html status note explaining maintenance behavior.

### PC offline / asleep

Wallet RPC unreachable → payment confirmations stall → users wait → `/v1/purchase/{id}/status` keeps returning `pending` or `confirming`. Acceptable for short outages. Decisions:
- Auto-extend `expires_at` if PC was offline during the payment window? Document either way.
- Add a public status note on the site.
- For zero downtime: run a second instance of `monero-wallet-rpc` on the same wallet from a different home machine. (`monero-wallet-rpc` does NOT support concurrent access to the same wallet file without trouble — use a small Linux box rather than your main PC if uptime matters.)

### Hot wallet exposure

Wallet on PC = anyone with code execution on your PC can drain it. Mitigations:
- Run wallet under a dedicated unprivileged user (Linux) or a separate macOS account
- Keep only operational float on this wallet; sweep above $500 to a cold address daily
- The cold wallet seed lives on paper, in a safe, NOT on this PC

### Phala downtime

`/v1/chat/completions` returns 503. **Do not decrement budget on failed requests.** Don't insert a `usage_log` row either — that becomes a behavioral signal about user activity. Just return the error and move on.

### Phala raises per-token cost mid-bundle

Bundles already have 90–365 day expiry. Re-price new bundles. Don't retroactively reduce existing budgets.

### Edge cases to handle (unchanged from v1)
- User sends slightly less XMR (fees) — allow ≥98% tolerance
- User sends slightly more — accept as bonus, cap at +50%
- User sends to wrong address — not our problem, document clearly
- Phala errors mid-stream — `try/finally` ensures we only decrement what was actually streamed
- Two concurrent requests draining last tokens — atomic UPDATE handles
- User restarts payment with same browser — generate new `payment_id`, never reuse

### What NOT to do (expanded)
- Don't add user accounts "for convenience"
- Don't add email recovery
- Don't log prompts "for debugging" — even temporarily
- Don't add analytics — even aggregate counts are operationally suspect
- Don't use third-party error tracking (Sentry exfils prompts on exception)
- Don't expose admin endpoints publicly — SSH only
- Don't make the SQLCipher passphrase persistent on disk "just for now"
- Don't make the wallet reachable on the public internet "just temporarily"
- Don't market this as TEE-attested at the proxy layer. It isn't. Only the inference is.

---

## Reference Documentation

- Phala Cloud: https://docs.phala.network
- Phala Confidential AI: https://docs.phala.network/phala-cloud/confidential-ai
- Monero RPC: https://docs.getmonero.org/rpc-library/wallet-rpc/
- Caddy: https://caddyserver.com/docs/caddyfile
- FastAPI: https://fastapi.tiangolo.com
- slowapi: https://slowapi.readthedocs.io
- SQLCipher: https://www.zetetic.net/sqlcipher/sqlcipher-api/
- sqlcipher3-binary: https://pypi.org/project/sqlcipher3-binary/
- httpx-socks: https://pypi.org/project/httpx-socks/
- Tor hidden services: https://community.torproject.org/onion-services/setup/
- LUKS / cryptsetup: https://gitlab.com/cryptsetup/cryptsetup
- dropbear-initramfs (remote LUKS unlock): https://manpages.debian.org/dropbear-initramfs

---

## Success Criteria

MVP ships when all of these hold:

1. A user can visit the site (clearnet, .onion optional in v1)
2. Select a token bundle
3. Receive a unique Monero subaddress + exact amount
4. Send XMR from any wallet
5. See confirmation progress on poll
6. Receive an API key (returned exactly once)
7. Use the key with any OpenAI-compatible client → real Phala-attested inference response
8. Check remaining balance any time via `/v1/key/balance`
9. Fetch Phala inference attestation via `/v1/inference-attest`
10. Trust-boundaries section visible on the landing page and accurate
11. Zero account, email, name, IP, or user-agent collected end-to-end
12. SQLCipher DB unreadable without the operator-entered passphrase (test by killing the unit, reading the file with `sqlite3`, confirming garbage)
13. Wallet on PC reachable from VPS over Tor; not reachable from public internet

If all 13 hold → ship. Iterate from there.
