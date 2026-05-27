"""MoneroPay client + callback validator.

MoneroPay is a self-hosted Go daemon that sits on top of monero-wallet-rpc.
We use it as a direct XMR rail (zero PSP, zero operator KYC, max privacy)
alongside NowPayments in hybrid mode.

MoneroPay does NOT sign callbacks (no HMAC). Per the Baskket reference
pattern in MoneroPay's own docs, authentication is via per-payment secret
tokens embedded in the callback URL path. Phantom passes its 16-byte
`payment_id` as the URL token; on callback we look up the payment_id and
verify the body's `description` field matches. Triple-bind:

    URL path token (random 16-byte secret)
    == body.description (echoed from /receive request)
    == DB row payment_id (lookup key)

If any of those don't line up we reject the callback. Daemon shouldn't
know the payment_id (it just stores+echoes the description field), so an
attacker who hits the callback endpoint without first calling /receive
has no way to guess a valid 16-byte token_urlsafe.

References:
- https://moneropay.eu/api/receive.html
- https://moneropay.eu/api/callback.html
- https://kernal.eu/posts/payments-via-moneropay/ (Baskket reference impl)
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from config import (
    MONEROPAY_URL, MONEROPAY_USE_TOR,
    TOR_SOCKS_URL, PUBLIC_BASE_URL,
)

logger = logging.getLogger("phantom.monero_pay")

# MoneroPay denominates everything in piconero (1 XMR = 1e12 piconero).
PICONERO = Decimal("1000000000000")


def _client() -> httpx.AsyncClient:
    """HTTPX client configured for the deployment topology. Routes via Tor
    SOCKS when MONEROPAY_URL is an .onion (operator PC), direct on localhost."""
    if MONEROPAY_USE_TOR:
        # Same SOCKS scheme rule as payments.py: use AsyncProxyTransport not
        # `proxies=` because python-socks needs the socks5:// (not socks5h://)
        # form and handles remote DNS itself.
        from httpx_socks import AsyncProxyTransport  # type: ignore
        transport = AsyncProxyTransport.from_url(TOR_SOCKS_URL)
        return httpx.AsyncClient(transport=transport, timeout=30)
    return httpx.AsyncClient(timeout=30)


async def create_receive(
    payment_id: str,
    xmr_amount_decimal: Decimal,
    *,
    description: str | None = None,
) -> dict[str, Any]:
    """Mint a payment subaddress on MoneroPay for a phantom purchase.

    `xmr_amount_decimal` is the expected payment amount in XMR (decimal).
    Converted to piconero ints — MoneroPay's API works in piconero.

    `payment_id` doubles as:
      1. our DB row PK
      2. MoneroPay's `description` field (echoed back on every callback)
      3. URL path token in the callback URL we register with MoneroPay
    A 16-byte token_urlsafe is unguessable, so the callback URL itself is
    the authentication: only someone who already knows payment_id (i.e.
    MoneroPay, which we just told) can call back.

    Returns a dict with at minimum:
        address     - the XMR subaddress to display to the customer
        amount      - piconero (int)
        created_at  - ISO8601 timestamp from the daemon

    Raises RuntimeError on any non-2xx upstream response.
    """
    piconero = int((xmr_amount_decimal * PICONERO).to_integral_value())
    body = {
        "amount": piconero,
        # `description` is what MoneroPay echoes back on every callback.
        # We set it to the payment_id so the callback handler can sanity-
        # check that the URL-path token matches the body description.
        "description": description or payment_id,
        # Per-payment callback URL with payment_id as the secret path token.
        "callback_url": f"{PUBLIC_BASE_URL}/v1/monero-pay/callback/{payment_id}",
    }
    async with _client() as c:
        r = await c.post(f"{MONEROPAY_URL}/receive", json=body)
    if r.status_code >= 500:
        raise RuntimeError(f"monero_pay upstream {r.status_code}")
    if r.status_code >= 400:
        raise RuntimeError(f"monero_pay {r.status_code}: {r.text[:200]}")
    data = r.json()
    if not data.get("address") or "amount" not in data:
        raise RuntimeError(f"monero_pay receive missing address/amount: {data}")
    return data


async def get_health() -> dict[str, Any]:
    """Best-effort daemon liveness check. Used by /health endpoint when
    hybrid mode is enabled, so a stuck MoneroPay daemon surfaces on the
    public health JSON without crashing the API."""
    async with _client() as c:
        r = await c.get(f"{MONEROPAY_URL}/health")
    r.raise_for_status()
    return r.json()


# ─── Callback shape parsing ───────────────────────────────────────────────────
# MoneroPay callback body (per https://moneropay.eu/api/callback.html):
#
#   {
#     "amount": {
#       "expected": <piconero>,
#       "covered": {"total": <piconero>, "unlocked": <piconero>}
#     },
#     "complete": <bool>,            # true once unlocked >= expected
#     "description": "<our payment_id>",
#     "created_at": "...",
#     "transaction": {               # singular on per-tx callbacks
#       "amount": <piconero>,
#       "confirmations": <int>,
#       "double_spend_seen": <bool>,
#       "fee": <piconero>,
#       "height": <int>,
#       "timestamp": "...",
#       "tx_hash": "...",
#       "unlock_time": <int>,
#       "locked": <bool>
#     }
#   }
#
# 0-conf mode fires three callbacks per payment (0-conf, 1-conf, 10-conf).
# Default mode fires two (lock + unlock). `complete: true` is the
# authoritative "fully paid + unlocked" signal regardless of mode.


def parse_callback(data: dict[str, Any]) -> dict[str, Any]:
    """Extract the fields phantom cares about from a MoneroPay callback body.

    Returns a normalized dict:
        description       - our payment_id (echoed from /receive)
        expected_pico     - amount we asked for in piconero
        received_pico     - total received so far (locked or unlocked) in piconero
        unlocked_pico     - portion of received that's unlocked + spendable
        complete          - bool, true iff MoneroPay considers payment done
        confirmations     - min conf count across received tx(s)
        double_spend_seen - bool, true if any incoming tx flagged double-spend

    Tolerates both `transaction` (singular, per-tx callback) and `transactions`
    (plural, from GET /receive/<address>) shapes for forward-compat.
    """
    amount = data.get("amount") or {}
    covered = amount.get("covered") or {}
    expected_pico = int(amount.get("expected") or 0)
    received_pico = int(covered.get("total") or 0)
    unlocked_pico = int(covered.get("unlocked") or 0)
    complete = bool(data.get("complete"))

    txs = []
    if "transactions" in data and isinstance(data["transactions"], list):
        txs = data["transactions"]
    elif "transaction" in data and isinstance(data["transaction"], dict):
        txs = [data["transaction"]]

    confirmations = min(
        (int(t.get("confirmations") or 0) for t in txs),
        default=0,
    )
    double_spend = any(bool(t.get("double_spend_seen")) for t in txs)

    return {
        "description":        (data.get("description") or "").strip(),
        "expected_pico":      expected_pico,
        "received_pico":      received_pico,
        "unlocked_pico":      unlocked_pico,
        "complete":           complete,
        "confirmations":      confirmations,
        "double_spend_seen":  double_spend,
    }


def map_status(parsed: dict[str, Any]) -> tuple[str, bool]:
    """Translate a parsed callback to (phantom_status, should_issue_key).

    Phantom states:  pending → confirming → ready → completed
                                        ↘ expired

    Rules:
      - double_spend_seen on any incoming tx → expired (anti-fraud)
      - complete=true → ready (MoneroPay says fully paid + unlocked)
      - received > 0 + < expected (2% underpayment slack) → expired
        (matches NowPayments rail behavior on partial pay)
      - received > 0 + above slack but not complete → confirming
      - received = 0 → pending (shouldn't normally hit on callback path)
    """
    if parsed["double_spend_seen"]:
        return ("expired", False)
    if parsed["complete"]:
        return ("ready", True)
    received = parsed["received_pico"]
    expected = parsed["expected_pico"]
    if received <= 0:
        return ("pending", False)
    # 2% underpayment slack covers fee dust + rounding. Matches NowPayments
    # behavior: under that threshold treat as expired (no partial-pay flow).
    threshold = int(expected * 98 // 100)
    if received < threshold:
        return ("expired", False)
    return ("confirming", False)
