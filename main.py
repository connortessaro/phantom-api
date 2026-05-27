"""Phantom API — FastAPI app."""
import logging
import re

import httpx
import tiktoken
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

import db
import nowpayments
import catalog
from config import (
    DB_PATH, REDPILL_API_BASE, REDPILL_API_KEY, BUNDLES,
    cost_micro_usd, MICRO, REDPILL_BUDGET_MICRO,
    CUSTOM_MIN_MICRO, CUSTOM_MAX_MICRO, CUSTOM_VALIDITY_DAYS,
    IMAGE_MODELS, IMAGE_ALLOWED_SIZES, IMAGE_MAX_N, image_cost_micro_usd,
    PAYMENT_EXPIRY_MINUTES,
)

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("uvicorn.access").disabled = True


def _trusted_client_ip(request: Request) -> str:
    """Trusted client IP string. Caddy overwrites X-Forwarded-For with the real
    remote.host, so the last non-empty XFF entry is authoritative. Returns ""
    if no IP can be resolved (no XFF, no client.host — shouldn't happen behind Caddy)."""
    xff = request.headers.get("X-Forwarded-For", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    return parts[-1] if parts else (request.client.host if request.client else "")


def key_for_limit(request: Request) -> str:
    """Primary slowapi bucket. Bearer-hash when authenticated, IP-hash otherwise.
    A single API key gets its own bucket so neighbors behind shared NAT can't
    drain the customer's per-minute budget. See `ip_only_key` for the secondary
    per-IP bucket stacked on auth-required endpoints — without it, an attacker
    could rotate random Bearer values to allocate fresh per-key buckets and
    bypass the rate limit entirely.

    IP hash lives only in slowapi's in-memory bucket; never logged, persisted,
    or sent off-box."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return db.hash_key(auth.removeprefix("Bearer ").strip())
    client = _trusted_client_ip(request)
    if client:
        return "ip:" + db.hash_key(client)
    return request.cookies.get("phantom_session", "global")


def ip_only_key(request: Request) -> str:
    """Secondary slowapi bucket — always IP. Stacked alongside `key_for_limit`
    on auth-required endpoints. Closes the bearer-rotation bypass: random
    Bearer values produce a new primary bucket each request, but they all
    converge on the same IP bucket here."""
    client = _trusted_client_ip(request)
    return "ip:" + db.hash_key(client) if client else "ip:none"


def _client_ip_hash(request: Request) -> str | None:
    """Return hash of trusted client IP. Used for per-IP pending-payment cap.
    None if no IP resolvable."""
    client = _trusted_client_ip(request)
    return db.hash_key(client) if client else None


limiter = Limiter(key_func=key_for_limit)


from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Startup: open SQLCipher DB. Passphrase is loaded from env at import time
    # then wiped by db.init_db so process can't leak it later via /proc/self/environ.
    await db.init_db(DB_PATH)
    # Fetch live model catalog from Redpill; falls back to disk cache then builtin.
    await catalog.refresh()
    refresh_task = catalog.start_background_refresh()
    try:
        yield
    finally:
        refresh_task.cancel()
    # Shutdown: SQLCipher connection released on process exit.

app = FastAPI(title="Phantom API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
app.state.limiter = limiter


# Hard cap on request body size for /v1/* endpoints. Without this, a multi-GB
# POST OOMs the worker before reaching json parsing. Caddy + uvicorn enforce
# no body limit by default. 8 MB is generous for chat (base64-image messages)
# while still bounded. Chunked-transfer requests with no Content-Length skip
# this guard — Caddyfile `request_body { max_size 8MB }` covers that at edge.
_MAX_REQUEST_BYTES = 8 * 1024 * 1024


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    if request.url.path.startswith("/v1/"):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > _MAX_REQUEST_BYTES:
                    return JSONResponse({"error": "request body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "invalid content-length"}, status_code=400)
    return await call_next(request)


@app.exception_handler(RateLimitExceeded)
async def _rl_handler(request, exc):
    return JSONResponse({"error": "rate_limited"}, status_code=429)


@app.exception_handler(Exception)
async def safe_exception_handler(request, exc):
    logging.error(f"Unhandled {type(exc).__name__} on {request.method} {request.url.path}")
    return JSONResponse({"error": "internal_error"}, status_code=500)


@app.get("/health")
async def health():
    """Public health endpoint. Returns service state booleans only — no internal
    addresses, no balances, no key counts. Safe to expose. Pings NowPayments
    /status to confirm the payment rail is reachable."""
    db_ok = True
    try:
        async with db._lock:
            db.conn().execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False

    components = {"db": db_ok, "models": len(catalog.all_models())}
    np_ok = False
    try:
        from config import NP_API_KEY, NP_BASE
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{NP_BASE}/status",
                            headers={"x-api-key": NP_API_KEY})
            np_ok = r.status_code == 200
    except Exception:
        pass
    components["payments"] = np_ok
    ok = db_ok and np_ok

    return {"status": "ok" if ok else "degraded", **components}


def _auth_key_hash(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing API key")
    return db.hash_key(auth.removeprefix("Bearer ").strip())


@app.get("/v1/stats")
@limiter.limit("60/minute")
async def public_stats(request: Request):
    """Public aggregate counters for the landing page proof strip. Aggregates
    only — total requests served + tokens processed. No per-key data, no
    timestamps, no model breakdown. Cannot deanonymize anyone (no buckets
    small enough to identify a single key)."""
    async with db._lock:
        row = db.conn().execute(
            "SELECT COUNT(*) AS reqs, "
            "       COALESCE(SUM(prompt_tokens), 0) AS p, "
            "       COALESCE(SUM(completion_tokens), 0) AS c "
            "FROM usage_log"
        ).fetchone()
    reqs, p_tok, c_tok = row
    return {
        "requests_served": int(reqs or 0),
        "tokens_processed": int((p_tok or 0) + (c_tok or 0)),
    }


@app.get("/v1/models")
async def list_models():
    """Live catalog. Each entry carries:
      - tier: "tee" (full TEE attestation, content invisible to Redpill+phantom)
              "proxy" (gateway in TDX, but vendor sees prompt content)
      - kind: "chat", "embedding", or "image"
      - For chat/embedding: input_per_m_usd_user / output_per_m_usd_user (per-token)
      - For image: image_pricing_usd_user (flat per-image at each quality)
    Customers MUST check `tier` before sending sensitive prompts to proxy models."""
    out = []
    for mid, meta in sorted(catalog.all_models().items()):
        entry = {
            "id": mid,
            "object": "model",
            "tier": meta["tier"],
            "kind": meta["kind"],
            "description": meta["description"],
            "context": meta["context"],
            "input_modalities": meta.get("input_modalities", ["text"]),
            "providers": meta.get("providers", []),
            "input_per_m_usd_user":  round(catalog.cost_micro_usd(meta, 1_000_000, 0) / MICRO, 4),
            "output_per_m_usd_user": round(catalog.cost_micro_usd(meta, 0, 1_000_000) / MICRO, 4),
        }
        if meta["kind"] == "image":
            entry["max_size"] = meta.get("max_size")
            entry["image_pricing_usd_user"] = {
                q: round(image_cost_micro_usd(mid, 1, q) / MICRO, 4)
                for q in ("standard", "hd")
            }
        out.append(entry)
    return {"object": "list", "data": out, "catalog_info": catalog.info()}


@app.get("/v1/bundles")
async def list_bundles():
    # Hide internal "test" bundle ($0.05) from the public listing. It's still
    # accepted by /v1/purchase (operator smoke-tests) but not advertised.
    from config import MULTI_CRYPTO_SURCHARGE_PERCENT
    surcharge_factor = (100 + MULTI_CRYPTO_SURCHARGE_PERCENT) / 100
    return {
        "object": "list",
        "multi_crypto_surcharge_pct": MULTI_CRYPTO_SURCHARGE_PERCENT,
        "data": [
            {
                "name": name,
                "price_usd": round(b["price_micro"] / MICRO, 6),
                "price_usd_multi_crypto": round(b["price_micro"] * surcharge_factor / MICRO, 2),
                "credit_usd": round(b["credit_micro"] / MICRO, 6),
                "validity_days": b["validity_days"],
            }
            for name, b in BUNDLES.items()
            if name != "test"
        ],
    }


# Per-IP open-purchase limiter. In-memory only (resets on uvicorn restart).
# Time-based: each acquire records a timestamp; entries older than the payment
# TTL drop off naturally. Caps the number of unpaid pending payments a single
# client can open at once. Prevents a single attacker filling the wallet's
# subaddress book + capacity budget with abandoned $1000 orders for an hour.
from collections import deque as _deque
import time as _time
from config import PAYMENT_EXPIRY_MINUTES as _PAY_TTL_MIN
_MAX_PENDING_PER_IP = 3
_PENDING_WINDOW_SEC = (_PAY_TTL_MIN + 5) * 60
_pending_by_ip: dict[str, "_deque[float]"] = {}
_pending_ip_lock = __import__("asyncio").Lock()


async def _pending_ip_acquire(ip_hash: str) -> bool:
    now = _time.time()
    async with _pending_ip_lock:
        q = _pending_by_ip.get(ip_hash)
        if q is None:
            q = _deque()
            _pending_by_ip[ip_hash] = q
        while q and (now - q[0]) > _PENDING_WINDOW_SEC:
            q.popleft()
        if len(q) >= _MAX_PENDING_PER_IP:
            if not q:
                _pending_by_ip.pop(ip_hash, None)
            return False
        q.append(now)
        return True


async def _pending_ip_release(ip_hash: str):
    async with _pending_ip_lock:
        q = _pending_by_ip.get(ip_hash)
        if q:
            try:
                q.pop()
            except IndexError:
                pass
        if q is not None and not q:
            _pending_by_ip.pop(ip_hash, None)


@app.post("/v1/purchase")
@limiter.limit("10/minute")
@limiter.limit("30/minute", key_func=ip_only_key)
async def purchase(request: Request):
    """Body: {"bundle":"small"} OR {"amount_usd": 7.5}.
    Bundle gets volume bonus. amount_usd is exact, no bonus, validity 90 days."""
    body = await request.json()
    bundle = body.get("bundle")
    amount_usd = body.get("amount_usd")

    if bundle and bundle in BUNDLES:
        b = BUNDLES[bundle]
        label = bundle
        price_micro = b["price_micro"]
        credit_micro = b["credit_micro"]
        validity_days = b["validity_days"]
    elif amount_usd is not None:
        try:
            price_micro = int(round(float(amount_usd) * MICRO))
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(400, "amount_usd must be a finite number")
        if not (CUSTOM_MIN_MICRO <= price_micro <= CUSTOM_MAX_MICRO):
            raise HTTPException(400, "amount outside allowed range")
        label = "custom"
        credit_micro = price_micro  # 1:1, no volume bonus on custom
        validity_days = CUSTOM_VALIDITY_DAYS
    else:
        raise HTTPException(400, "specify either 'bundle' or 'amount_usd'")

    outstanding = await db.outstanding_credit_micro()
    if outstanding + credit_micro > REDPILL_BUDGET_MICRO:
        raise HTTPException(503, "service at capacity")

    # Per-IP open-purchase cap (defense against capacity-DOS).
    ip_hash = _client_ip_hash(request)
    if ip_hash:
        if not await _pending_ip_acquire(ip_hash):
            raise HTTPException(429, "too many open purchases — finish or wait for prior orders to expire")

    # Rail selection. Client picks:
    #   {"rail":"xmr"}   → NowPayments invoice locked to XMR, sticker price.
    #   {"rail":"multi"} → NowPayments multi-crypto checkout, +surcharge.
    # Default = multi.
    rail = (body.get("rail") or "").strip().lower()
    is_xmr_rail = (rail == "xmr")

    try:
        return await _purchase_nowpayments(
            label, price_micro, credit_micro, validity_days,
            apply_surcharge=(not is_xmr_rail),
            pay_currency=("xmr" if is_xmr_rail else None),
        )
    except Exception:
        if ip_hash:
            await _pending_ip_release(ip_hash)
        raise


async def _purchase_nowpayments(
    label, price_micro, credit_micro, validity_days,
    *,
    apply_surcharge: bool = True,
    pay_currency: str | None = None,
):
    """Mint a NowPayments invoice + persist the pending row. Returns the
    JSON the frontend uses to redirect the customer to NowPayments' hosted
    checkout page.

    `apply_surcharge` (default True): when True, customer pays sticker ×
    (1 + MULTI_CRYPTO_SURCHARGE_PERCENT/100) to cover NowPayments fee +
    crypto-to-XMR conversion + rate volatility buffer. When False (XMR
    rail), customer pays sticker exactly; operator absorbs the smaller
    NowPayments fee out of margin (~0.5-1% on XMR-only invoices since
    there's no conversion).

    `pay_currency`: when set, NowPayments hosted checkout locks to that
    coin (no multi-crypto picker, no conversion to XMR). Use "xmr" for
    the privacy-first rail. None = multi-crypto checkout.

    Credit issued is always `credit_micro` (unchanged by surcharge).
    """
    import secrets as _s
    from datetime import datetime, timedelta, timezone
    from config import MULTI_CRYPTO_SURCHARGE_PERCENT
    payment_id = _s.token_urlsafe(16)
    if apply_surcharge:
        billed_micro = price_micro * (100 + MULTI_CRYPTO_SURCHARGE_PERCENT) // 100
        surcharge_pct = MULTI_CRYPTO_SURCHARGE_PERCENT
    else:
        billed_micro = price_micro
        surcharge_pct = 0
    usd_amount = billed_micro / MICRO
    invoice = await nowpayments.create_invoice(
        payment_id, usd_amount, pay_currency=pay_currency,
    )
    now = datetime.now(timezone.utc)
    # NowPayments invoices live 7 days normally; fixed-rate invoices have a
    # 10-minute rate-lock window. We track the longer side for state.
    expires_at = (now + timedelta(minutes=PAYMENT_EXPIRY_MINUTES)).isoformat()
    await db.create_np_payment(
        payment_id, label, billed_micro, credit_micro, validity_days,
        np_invoice_id=str(invoice["id"]),
        expires_at_iso=expires_at,
    )
    return {
        "payment_id":     payment_id,
        "checkout_url":   invoice["invoice_url"],
        "bundle":         label,
        "credit_usd":     round(credit_micro / MICRO, 6),
        "price_usd":      round(billed_micro / MICRO, 2),
        "surcharge_pct":  surcharge_pct,
        "pay_currency":   pay_currency,
        "expires_at":     expires_at,
    }


@app.post("/v1/nowpayments/ipn")
@limiter.limit("120/minute")
async def nowpayments_ipn(request: Request):
    """NowPayments webhook receiver. Validates HMAC-SHA512 signature, maps
    their payment_status to phantom's internal state, transitions the row,
    and (on 'finished') the next /v1/purchase/{id}/status call atomically
    issues the API key.

    Re-deposit guard: if `parent_payment_id` is set on the IPN body AND the
    parent payment_id is already in a terminal state (completed/expired),
    we log + ignore. Otherwise the re-deposit would re-issue a key for a
    customer who already claimed one.

    Never raises on bad signatures — returns 401. Crashing would let
    NowPayments retry indefinitely against a panicked endpoint."""
    raw = await request.body()
    sig = request.headers.get("x-nowpayments-sig", "")
    if not nowpayments.verify_ipn(raw, sig):
        raise HTTPException(401, "bad ipn signature")
    try:
        import json as _json
        data = _json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "bad ipn body")

    order_id = data.get("order_id")
    np_status = data.get("payment_status")
    if not order_id or not np_status:
        raise HTTPException(400, "missing order_id or payment_status")

    parent_payment_id = data.get("parent_payment_id")
    np_payment_id = str(data.get("payment_id", "")) or None
    pay_currency = data.get("pay_currency")
    pay_amount = data.get("pay_amount")
    actually_paid = data.get("actually_paid")
    outcome_amount = data.get("outcome_amount")
    if pay_amount is not None:
        pay_amount = str(pay_amount)
    if outcome_amount is not None:
        outcome_amount = str(outcome_amount)

    phantom_status, _issue = nowpayments.map_status(np_status)

    # Re-deposit guard
    if parent_payment_id:
        # parent_payment_id is NowPayments' id, not ours. Map via np_payment_id of original.
        async with db._lock:
            row = db.conn().execute(
                "SELECT status FROM payments WHERE np_payment_id = ?",
                (str(parent_payment_id),),
            ).fetchone()
        if row and row[0] in ("completed", "expired"):
            logging.warning(
                f"re-deposit ignored: parent np_payment_id={parent_payment_id} already terminal"
            )
            return {"ok": True, "ignored": "re-deposit"}

    await db.update_np_payment_status(
        order_id, phantom_status,
        np_payment_id=np_payment_id,
        pay_currency=pay_currency,
        pay_amount=pay_amount,
        outcome_amount=outcome_amount,
        parent_payment_id=str(parent_payment_id) if parent_payment_id else None,
    )
    return {"ok": True, "phantom_status": phantom_status}


@app.get("/v1/purchase/{payment_id}/status")
@limiter.limit("60/minute")
async def purchase_status(payment_id: str, request: Request):
    async with db._lock:
        row = db.conn().execute(
            "SELECT status, bundle_name, credit_micro_usd, expires_at "
            "FROM payments WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "unknown payment")
    status, bundle, credit_micro, expires = row

    if status == "ready":
        plaintext = await db.claim_and_issue(payment_id)
        if plaintext:
            return {"status": "completed", "api_key": plaintext, "shown_once": True}
        return {"status": "completed", "api_key": None}

    return {
        "status": status,
        "bundle": bundle,
        "expires_at": expires,
    }


@app.post("/v1/key/rotate")
@limiter.limit("5/hour")
@limiter.limit("30/hour", key_func=ip_only_key)
async def key_rotate(request: Request):
    """Rotate an API key. Credit + expiry carry over. Old key deactivates."""
    key_hash = _auth_key_hash(request)
    new_plaintext = await db.rotate_key(key_hash)
    if not new_plaintext:
        raise HTTPException(401, "invalid or expired key")
    return {"api_key": new_plaintext, "shown_once": True}


@app.get("/v1/key/balance")
@limiter.limit("60/minute")
@limiter.limit("300/minute", key_func=ip_only_key)
async def key_balance(request: Request):
    key_hash = _auth_key_hash(request)
    async with db._lock:
        row = db.conn().execute(
            "SELECT credit_balance, credit_spent, expires_at, is_active "
            "FROM api_keys WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "invalid key")
    balance, spent, expires, active = row
    return {
        "credit_balance_usd": round(balance / MICRO, 6),
        "credit_spent_usd": round(spent / MICRO, 6),
        "expires_at": expires,
        "is_active": bool(active),
    }


def _count_tokens(text: str) -> int:
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


# Conservative worst-case image token estimate for pre-flight credit check.
# OpenAI vision: "auto"/"high" detail = 85 + 170*tiles, up to ~1530 tokens per image.
# We don't know dimensions client-side → assume worst case to avoid bypass.
_IMAGE_TOK_ESTIMATE = 1530


def _estimate_prompt_tokens(body: dict) -> int:
    """Best-effort token count over messages content. Counts text + image parts."""
    msgs = body.get("messages", [])
    total = 0
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str):
            total += _count_tokens(c)
        elif isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    total += _count_tokens(part.get("text", ""))
                elif ptype in ("image_url", "input_image", "image"):
                    total += _IMAGE_TOK_ESTIMATE
    return total or 1


# Concurrent stream caps. In-memory only; reset on uvicorn restart.
#   _MAX_STREAMS_PER_KEY = single key can't hog the worker pool
#   _MAX_STREAMS_GLOBAL  = box-wide ceiling. Prevents DOS via many keys at once
# Sized to a 2-vCPU droplet running uvicorn at default workers. Raise when scaling up.
_MAX_STREAMS_PER_KEY = 10
_MAX_STREAMS_GLOBAL  = 200
_active_streams: dict[str, int] = {}
_global_streams = 0
_stream_lock = __import__("asyncio").Lock()


async def _stream_acquire(key_hash: str) -> bool:
    global _global_streams
    async with _stream_lock:
        if _global_streams >= _MAX_STREAMS_GLOBAL:
            return False
        n = _active_streams.get(key_hash, 0)
        if n >= _MAX_STREAMS_PER_KEY:
            return False
        _active_streams[key_hash] = n + 1
        _global_streams += 1
        return True


async def _stream_release(key_hash: str):
    global _global_streams
    async with _stream_lock:
        n = _active_streams.get(key_hash, 0) - 1
        if n <= 0:
            _active_streams.pop(key_hash, None)
        else:
            _active_streams[key_hash] = n
        if _global_streams > 0:
            _global_streams -= 1


# Whitelist of fields forwarded to upstream chat-completions. Drop anything that
# could fingerprint user (e.g. `user`, `metadata`) or trigger billable side effects.
_REDPILL_CHAT_BODY_KEYS = {
    "model", "messages", "max_tokens", "temperature", "top_p", "top_k",
    "stream", "tools", "tool_choice", "response_format", "stop",
    "presence_penalty", "frequency_penalty", "seed", "n", "logprobs",
    "top_logprobs", "logit_bias", "reasoning", "reasoning_effort",
}

# Whitelist for /v1/embeddings. Smaller surface than chat.
_REDPILL_EMBED_BODY_KEYS = {
    "model", "input", "encoding_format", "dimensions",
}

# Whitelist for /v1/images/generations. Drop anything that could be used to
# fingerprint the customer or trigger billable side effects upstream.
_REDPILL_IMAGE_BODY_KEYS = {
    "model", "prompt", "n", "size", "quality",
    "response_format", "style", "negative_prompt", "seed",
}


@app.post("/v1/chat/completions")
@limiter.limit("60/minute")
@limiter.limit("300/minute", key_func=ip_only_key)
async def chat_completions(request: Request):
    key_hash = _auth_key_hash(request)
    raw_body = await request.json()
    if not isinstance(raw_body, dict):
        raise HTTPException(400, "body must be json object")
    body = {k: v for k, v in raw_body.items() if k in _REDPILL_CHAT_BODY_KEYS}
    model = body.get("model")
    meta = catalog.get(model)
    if not meta or meta["kind"] != "chat":
        raise HTTPException(400, "model not available on /v1/chat/completions (try /v1/embeddings for embedding models)")
    # Translate phantom-branded id (or legacy alias) to the upstream Redpill id
    # before forwarding. Customer sees `phantom/kimi-k2.6`, Redpill sees the
    # original `phala/kimi-k2.6`.
    body["model"] = meta.get("upstream_id", model)

    # Clamp max_tokens to model context / 2 and hard ceiling. Prevents single-request bundle drain
    # and protects against absurdly large client requests upstream may accept.
    requested_max = int(body.get("max_tokens", 4096))
    model_ctx = meta["context"]
    HARD_CEIL = 32_768
    max_tokens = max(1, min(requested_max, model_ctx // 2, HARD_CEIL))
    body["max_tokens"] = max_tokens  # forward the clamped value to Phala

    est_prompt = _estimate_prompt_tokens(body)
    est_cost = cost_micro_usd(model, est_prompt, max_tokens)

    key_row = await db.check_credits(key_hash, est_cost)
    if not key_row:
        raise HTTPException(402, "insufficient credit or expired key")

    stream = bool(body.get("stream", False))
    if stream:
        if not await _stream_acquire(key_hash):
            raise HTTPException(429, "too many concurrent streams for this key")
        return StreamingResponse(
            _stream_phala(body, key_hash, model, est_prompt, max_tokens),
            media_type="text/event-stream",
        )

    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            f"{REDPILL_API_BASE}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {REDPILL_API_KEY}"},
        )
    if r.status_code >= 500:
        raise HTTPException(503, "upstream unavailable")
    data = r.json()
    # Only charge if Phala returned a usage block (= actual work done). 4xx errors / malformed
    # responses surface upstream's status to client without billing.
    usage = data.get("usage") or None
    if r.status_code == 200 and usage:
        p_tok = int(usage.get("prompt_tokens", 0))
        c_tok = int(usage.get("completion_tokens", 0))
        # Defense-in-depth: clamp usage to the request's bounds even if upstream
        # reports a higher figure. A malicious model output could in theory
        # embed a fake usage-shaped substring; pre-flight already gated by these
        # ceilings, so honoring upstream beyond them creates no extra revenue.
        c_tok = max(0, min(c_tok, max_tokens))
        p_tok = max(0, min(p_tok, model_ctx))
        if p_tok > 0 or c_tok > 0:
            actual_cost = cost_micro_usd(model, p_tok, c_tok)
            await db.decrement_credits(key_hash, actual_cost, model, p_tok, c_tok)
    # Forward Retry-After so clients respect upstream backoff on 429.
    out_headers = {}
    if "retry-after" in r.headers:
        out_headers["Retry-After"] = r.headers["retry-after"]
    return JSONResponse(data, status_code=r.status_code, headers=out_headers)


@app.post("/v1/embeddings")
@limiter.limit("60/minute")
@limiter.limit("300/minute", key_func=ip_only_key)
async def embeddings(request: Request):
    """OpenAI-compatible embeddings endpoint. Forwards to upstream Redpill.
    Bills per prompt token only (output_per_m = 0 for embedding models).
    Pre-flight worst-case estimate: input tokens × model.input_per_m."""
    key_hash = _auth_key_hash(request)
    raw_body = await request.json()
    if not isinstance(raw_body, dict):
        raise HTTPException(400, "body must be json object")
    body = {k: v for k, v in raw_body.items() if k in _REDPILL_EMBED_BODY_KEYS}
    model = body.get("model")
    emb_meta = catalog.get(model)
    if not emb_meta or emb_meta["kind"] != "embedding":
        raise HTTPException(400, "model not available on /v1/embeddings (chat models go to /v1/chat/completions)")
    body["model"] = emb_meta.get("upstream_id", model)

    # Worst-case estimate: tiktoken on stringified input. Embeddings accept str
    # or list[str] or list[int]; we just count chars/4 as conservative upper bound
    # for the pre-flight check. Actual billing uses upstream's usage block.
    inp = body.get("input")
    if isinstance(inp, str):
        approx_tokens = max(1, len(inp) // 4)
    elif isinstance(inp, list):
        approx_tokens = max(1, sum(len(s) // 4 if isinstance(s, str) else 1 for s in inp))
    else:
        raise HTTPException(400, "input must be a string or list of strings")
    # Clamp to model context to avoid pre-charging absurd amounts on garbage input.
    approx_tokens = min(approx_tokens, emb_meta["context"])

    est_cost = cost_micro_usd(model, approx_tokens, 0)
    key_row = await db.check_credits(key_hash, est_cost)
    if not key_row:
        raise HTTPException(402, "insufficient credit or expired key")

    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{REDPILL_API_BASE}/embeddings",
            json=body,
            headers={"Authorization": f"Bearer {REDPILL_API_KEY}"},
        )
    if r.status_code >= 500:
        raise HTTPException(503, "upstream unavailable")
    data = r.json()
    usage = data.get("usage") or None
    if r.status_code == 200 and usage:
        p_tok = int(usage.get("prompt_tokens", 0) or usage.get("total_tokens", 0))
        # Clamp to embedding model context — pre-flight gated by this anyway.
        p_tok = max(0, min(p_tok, emb_meta["context"]))
        if p_tok > 0:
            actual_cost = cost_micro_usd(model, p_tok, 0)
            await db.decrement_credits(key_hash, actual_cost, model, p_tok, 0)
    out_headers = {}
    if "retry-after" in r.headers:
        out_headers["Retry-After"] = r.headers["retry-after"]
    return JSONResponse(data, status_code=r.status_code, headers=out_headers)


_USAGE_RE = re.compile(r'"prompt_tokens"\s*:\s*(\d+).*?"completion_tokens"\s*:\s*(\d+)', re.DOTALL)
_TAIL_BYTES = 8192  # SSE usage block sits in final ~1KB; 8KB tail covers all real-world fragmentations


def _extract_usage(buf: str) -> tuple[int, int]:
    """Find largest (prompt, completion) pair in buf. Returns (0,0) if none found."""
    p_tok = c_tok = 0
    for m in _USAGE_RE.finditer(buf):
        p_tok = max(p_tok, int(m.group(1)))
        c_tok = max(c_tok, int(m.group(2)))
    return p_tok, c_tok


async def _stream_phala(body: dict, key_hash: str, model: str, est_prompt: int, max_tokens: int):
    """Stream proxy + bill-on-finally. Three billing branches:
    1. Phala emitted usage in tail buffer → use it (truthful).
    2. Client aborted mid-stream → estimate completion from bytes streamed.
    3. Nothing streamed → don't bill (Phala did no work).
    Tail buffer survives chunk-boundary splits of the usage block.

    Defense-in-depth: clamp completion to max_tokens and prompt to the model's
    context window in branch 1 too. The tail-regex parser sees the last 8KB of
    SSE bytes, which could contain a model-generated string that looks like a
    usage block. Pre-flight already gated against these ceilings, so honoring
    upstream beyond them grants free inference / drains balance unexpectedly.
    """
    tail = ""
    bytes_streamed = 0
    # Resolve model context for clamp (may be missing on dynamic catalog races)
    _meta = catalog.get(model) or {}
    model_ctx = int(_meta.get("context") or 32_768)
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            async with c.stream(
                "POST", f"{REDPILL_API_BASE}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {REDPILL_API_KEY}"},
            ) as resp:
                async for chunk in resp.aiter_text():
                    bytes_streamed += len(chunk)
                    tail = (tail + chunk)[-_TAIL_BYTES:]
                    yield chunk
    finally:
        await _stream_release(key_hash)
        p_tok, c_tok = _extract_usage(tail)
        if p_tok == 0 and c_tok == 0:
            if bytes_streamed > 0:
                # Aborted before Phala emitted final usage. Best-effort estimate:
                # SSE wrapping per token is ~30 bytes; rough completion estimate.
                # Clamp to max_tokens so we never over-bill beyond the request cap.
                p_tok = est_prompt
                c_tok = min(max_tokens, max(1, bytes_streamed // 30))
            else:
                p_tok = c_tok = 0  # nothing streamed, no charge
        else:
            c_tok = max(0, min(c_tok, max_tokens))
            p_tok = max(0, min(p_tok, model_ctx))
        cost = cost_micro_usd(model, p_tok, c_tok)
        if cost > 0:
            await db.decrement_credits(key_hash, cost, model, p_tok, c_tok)


@app.post("/v1/images/generations")
@limiter.limit("30/minute")
@limiter.limit("60/minute", key_func=ip_only_key)
async def images_generations(request: Request):
    """OpenAI-compatible image generation. Flat-rate billing per image,
    no streaming. Pre-flight credit check covers worst-case n × per-image cost;
    actual decrement uses the same calculation (no upstream usage block exists
    for images, so what we estimate is what we charge). Forwards to upstream
    Redpill /v1/images/generations."""
    key_hash = _auth_key_hash(request)
    raw_body = await request.json()
    if not isinstance(raw_body, dict):
        raise HTTPException(400, "body must be json object")
    body = {k: v for k, v in raw_body.items() if k in _REDPILL_IMAGE_BODY_KEYS}

    model = body.get("model")
    if not isinstance(model, str) or model not in IMAGE_MODELS:
        raise HTTPException(400, "model not available on /v1/images/generations")

    # Prompt: required, length-bounded to limit upstream abuse + customer typos.
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(400, "prompt required")
    if len(prompt) > 4000:
        raise HTTPException(400, "prompt too long (max 4000 chars)")

    # n: 1..IMAGE_MAX_N
    try:
        n = int(body.get("n", 1))
    except (TypeError, ValueError):
        raise HTTPException(400, "n must be an integer")
    if not (1 <= n <= IMAGE_MAX_N):
        raise HTTPException(400, f"n must be between 1 and {IMAGE_MAX_N}")
    body["n"] = n

    # size: must be in our allowed set + within model's max_size
    size = body.get("size", "1024x1024")
    if size not in IMAGE_ALLOWED_SIZES:
        raise HTTPException(400, f"size must be one of {sorted(IMAGE_ALLOWED_SIZES)}")
    max_size = IMAGE_MODELS[model].get("max_size")
    if max_size:
        try:
            req_w, req_h = (int(x) for x in size.split("x"))
            max_w, max_h = (int(x) for x in max_size.split("x"))
            if req_w > max_w or req_h > max_h:
                raise HTTPException(400, f"size {size} exceeds model max {max_size}")
        except ValueError:
            raise HTTPException(400, "malformed size")
    body["size"] = size

    # quality: "standard" or "hd"
    quality = body.get("quality", "standard")
    if quality not in ("standard", "hd"):
        raise HTTPException(400, "quality must be 'standard' or 'hd'")
    body["quality"] = quality

    # response_format: url | b64_json (default url, mirrors OpenAI)
    rfmt = body.get("response_format", "url")
    if rfmt not in ("url", "b64_json"):
        raise HTTPException(400, "response_format must be 'url' or 'b64_json'")
    body["response_format"] = rfmt

    # Pre-flight cost check. Image billing is exact: estimate == actual.
    cost = image_cost_micro_usd(model, n, quality)
    if cost <= 0:
        raise HTTPException(500, "pricing error")
    if not await db.check_credits(key_hash, cost):
        raise HTTPException(402, "insufficient credit or expired key")

    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(
            f"{REDPILL_API_BASE}/images/generations",
            json=body,
            headers={"Authorization": f"Bearer {REDPILL_API_KEY}"},
        )
    if r.status_code >= 500:
        raise HTTPException(503, "upstream unavailable")
    data = r.json()
    # Bill only on 2xx with actual image data — protects against 4xx tantrums
    # billed by accident.
    if r.status_code == 200 and isinstance(data, dict) and isinstance(data.get("data"), list) and data["data"]:
        # Record n in prompt_tokens slot so usage_log queries still work
        # (cost reflects images, not tokens). completion_tokens left zero.
        await db.decrement_credits(key_hash, cost, model, n, 0)
    out_headers = {}
    if "retry-after" in r.headers:
        out_headers["Retry-After"] = r.headers["retry-after"]
    return JSONResponse(data, status_code=r.status_code, headers=out_headers)


@app.get("/v1/inference-attest")
@limiter.limit("20/minute")
@limiter.limit("60/minute", key_func=ip_only_key)
async def inference_attest(request: Request, model: str, nonce: str | None = None,
                            signing_address: str | None = None):
    """Proxy to Phala /v1/attestation/report. Returns the TDX quote chain
    + NVIDIA GPU attestation for the requested model. Used as step 3 of the
    verification flow (after chat → signature → THIS)."""
    # Require Bearer auth to prevent unauthenticated quote-spam (Phala may rate-limit
    # our account if abused) and to discourage anonymous probing.
    _auth_key_hash(request)
    meta = catalog.get(model)
    if not meta:
        raise HTTPException(400, "model not available")
    upstream_model = meta.get("upstream_id", model)
    import secrets as _s
    nonce = nonce or _s.token_hex(32)
    # Nonce validation: must be hex, sensible length. Prevents passing arbitrary
    # garbage / control chars to upstream and possible header smuggling.
    if not re.fullmatch(r"[0-9a-fA-F]{8,128}", nonce):
        raise HTTPException(400, "nonce must be 8-128 hex chars")
    params = {"model": upstream_model, "nonce": nonce}
    if signing_address:
        # Accept 0x-prefixed eth-style (40 hex) or sui/sol-style (32/64 hex).
        if not re.fullmatch(r"0x[0-9a-fA-F]{40,64}", signing_address):
            raise HTTPException(400, "signing_address must be 0x-prefixed hex")
        params["signing_address"] = signing_address
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{REDPILL_API_BASE}/attestation/report",
            params=params,
            headers={"Authorization": f"Bearer {REDPILL_API_KEY}"},
        )
    if r.status_code != 200:
        raise HTTPException(502, "attestation unavailable")
    return r.json()


@app.get("/v1/signature/{request_id}")
@limiter.limit("60/minute")
@limiter.limit("300/minute", key_func=ip_only_key)
async def inference_signature(request: Request, request_id: str, model: str,
                               signing_algo: str | None = None):
    """Proxy to Phala /v1/signature/{request_id}. Returns the cryptographic
    signature bound to a specific chat-completion response id. Step 2 of the
    verification flow (after the chat call returns its id, before fetching the
    attestation report)."""
    _auth_key_hash(request)
    meta = catalog.get(model)
    if not meta:
        raise HTTPException(400, "model not available")
    upstream_model = meta.get("upstream_id", model)
    # request_id is taken from the path; restrict to safe alphabet so we never
    # forward an attacker-controlled path-traversal-ish value to upstream.
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", request_id):
        raise HTTPException(400, "invalid request_id")
    params = {"model": upstream_model}
    if signing_algo:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", signing_algo):
            raise HTTPException(400, "invalid signing_algo")
        params["signing_algo"] = signing_algo
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{REDPILL_API_BASE}/signature/{request_id}",
            params=params,
            headers={"Authorization": f"Bearer {REDPILL_API_KEY}"},
        )
    if r.status_code != 200:
        raise HTTPException(502, "signature unavailable")
    return r.json()


# Dev-mode static file serving. In production Caddy serves frontend/ directly and
# uvicorn only listens on 127.0.0.1:8000, so this mount is never reached.
# Guarded behind PHANTOM_DEV=1 so a Caddy misconfig can't accidentally expose
# FastAPI's StaticFiles (different MIME-sniff behavior than Caddy's file_server).
# Mounted LAST so /v1/* and /health resolve to API routes first.
import pathlib as _pl
import os as _os
from fastapi.staticfiles import StaticFiles
if _os.environ.get("PHANTOM_DEV") and _pl.Path("frontend").is_dir():
    app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
