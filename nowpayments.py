"""NowPayments REST client + IPN signature verification.

Replaces the self-hosted XMR rail (`payments.py`) with a hosted multi-crypto
checkout. NowPayments handles coin selection, address generation, conversion,
and forwards XMR to phantom's cold wallet. Phantom stays anonymous to buyers,
NowPayments KYC's the operator (acceptable given open-source release).

Two responsibilities:
1. `create_invoice` — phantom calls this from POST /v1/purchase to mint a
   checkout URL. Customer is redirected to NowPayments-hosted page, picks
   any of 300+ coins, pays, comes back.
2. `verify_ipn` — phantom calls this in POST /v1/nowpayments/ipn to validate
   that the webhook came from NowPayments and hasn't been tampered with.
   HMAC-SHA512 over JSON-sorted body. Spec from NowPayments docs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from config import (
    NP_API_KEY, NP_IPN_SECRET, NP_BASE, NP_PAYOUT_CURRENCY,
    PUBLIC_BASE_URL,
)

logger = logging.getLogger("phantom.nowpayments")


# ─── REST client ──────────────────────────────────────────────────────────────

async def create_invoice(
    payment_id: str,
    usd_amount: float,
    *,
    description: str = "phantom credit",
    pay_currency: str | None = None,
) -> dict[str, Any]:
    """Create a NowPayments invoice for a phantom purchase.

    `payment_id` is phantom's internal id (token_urlsafe(16)); we pass it as
    NowPayments' `order_id` so the IPN webhook tells us which phantom payment
    just settled.

    Returns the full NowPayments invoice object. Key fields:
        id          - NowPayments internal invoice id
        invoice_url - the hosted checkout URL we redirect the customer to
        order_id    - echo of our payment_id

    `is_fixed_rate=true` freezes the exchange rate for 10 minutes — customer
    sees the exact pay amount and the rate cannot move under them.

    `is_fee_paid_by_user=false` makes the customer pay exactly `price_amount`
    worth of crypto. Operator absorbs the NowPayments service fee (~0.5-1%)
    out of margin. Cleaner UX: "$10 = $10" with no surprise on-top charge.
    Markup covers this.
    """
    body = {
        "price_amount": float(usd_amount),
        "price_currency": "usd",
        "order_id": payment_id,
        "order_description": description,
        "ipn_callback_url": f"{PUBLIC_BASE_URL}/v1/nowpayments/ipn",
        "success_url": f"{PUBLIC_BASE_URL}/#claim/{payment_id}",
        "cancel_url":  f"{PUBLIC_BASE_URL}/#cancel",
        "is_fixed_rate": True,
        "is_fee_paid_by_user": False,
    }
    # Lock the pay currency when caller specified one. Used for the XMR rail
    # so the hosted checkout only offers XMR (no multi-crypto picker, no
    # conversion). When None, hosted checkout shows all enabled coins.
    if pay_currency:
        body["pay_currency"] = pay_currency
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{NP_BASE}/invoice",
            json=body,
            headers={
                "x-api-key": NP_API_KEY,
                "Content-Type": "application/json",
            },
        )
    if r.status_code >= 500:
        raise RuntimeError(f"nowpayments upstream {r.status_code}")
    if r.status_code >= 400:
        # Surface 4xx text to caller — usually bad config (missing payout wallet etc.)
        raise RuntimeError(f"nowpayments {r.status_code}: {r.text[:200]}")
    data = r.json()
    if not data.get("invoice_url"):
        raise RuntimeError(f"nowpayments invoice missing invoice_url: {data}")
    return data


async def get_payment_status(np_payment_id: str) -> dict[str, Any]:
    """Best-effort fetch of a NowPayments payment by their internal id.
    Used only as a fallback when IPN delivery looks stuck. Most state
    transitions arrive via webhook."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{NP_BASE}/payment/{np_payment_id}",
            headers={"x-api-key": NP_API_KEY},
        )
    if r.status_code != 200:
        raise RuntimeError(f"nowpayments status {r.status_code}: {r.text[:200]}")
    return r.json()


# ─── IPN signature verification ───────────────────────────────────────────────
# Per NowPayments docs (Python example, verbatim recipe):
#   sorted_msg = json.dumps(message, separators=(',', ':'), sort_keys=True)
#   hmac.new(secret, sorted_msg.encode(), sha512).hexdigest() == x-nowpayments-sig
#
# Constant-time compare so signature failures don't leak via timing.

def verify_ipn(raw_body: bytes, signature_header: str) -> bool:
    """Return True iff `signature_header` is the valid HMAC-SHA512 of
    `raw_body` (parsed as JSON, sorted by key, separators stripped) under
    NP_IPN_SECRET. False on any parse error or mismatch.

    Never raises — IPN endpoint must respond 401 on bad signature, not crash.
    """
    if not signature_header or not NP_IPN_SECRET:
        return False
    try:
        msg = json.loads(raw_body)
    except (ValueError, TypeError):
        return False
    sorted_msg = json.dumps(msg, separators=(",", ":"), sort_keys=True)
    digest = hmac.new(
        NP_IPN_SECRET.encode("utf-8"),
        sorted_msg.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(digest, signature_header)


# ─── Status mapper ────────────────────────────────────────────────────────────
# Translate NowPayments-side payment_status → phantom's internal state.
# Phantom states:  pending → confirming → ready → completed
#                                     ↘ expired
# NowPayments states:
#   waiting       - invoice created, customer hasn't paid
#   confirming    - blockchain has the tx, awaiting confirmations
#   confirmed     - confirmations met (per coin's threshold)
#   sending       - NowPayments forwarding payout to operator
#   finished      - operator received funds; safe to issue key
#   partially_paid - customer underpaid; treat as expired (per plan)
#   failed        - upstream error
#   expired       - customer didn't pay in time

# Map from NowPayments status → (phantom_status, should_issue_key)
_STATUS_MAP: dict[str, tuple[str, bool]] = {
    "waiting":         ("pending",    False),
    "confirming":      ("confirming", False),
    "confirmed":       ("confirming", False),
    "sending":         ("confirming", False),
    "finished":        ("ready",      True),     # ready → claim_and_issue
    "partially_paid":  ("expired",    False),    # per plan: auto-expire
    "failed":          ("expired",    False),
    "expired":         ("expired",    False),
    "refunded":        ("expired",    False),
}


def map_status(np_status: str) -> tuple[str, bool]:
    """Translate NowPayments status to phantom (phantom_status, issue_key).
    Returns ("pending", False) for unknown statuses — safer than guessing
    a terminal state."""
    return _STATUS_MAP.get(np_status, ("pending", False))
