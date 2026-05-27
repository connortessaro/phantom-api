"""Monero wallet RPC client. Defaults to localhost (hot wallet on VPS).
Routes through Tor SOCKS when WALLET_RPC_HOST is an .onion (operator-PC mode,
the original security posture: VPS holds no keys, only reaches wallet via hidden service)."""
import secrets
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import httpx
from httpx_socks import AsyncProxyTransport

import db
from config import (
    WALLET_RPC_URL, WALLET_RPC_USER, WALLET_RPC_PASSWORD, TOR_SOCKS_URL,
    WALLET_USE_TOR, PAYMENT_EXPIRY_MINUTES, BUNDLES, MICRO,
)

PICONERO = Decimal("1e12")
# Only proxy wallet calls through Tor when reaching an onion. Localhost = direct.
_transport = AsyncProxyTransport.from_url(TOR_SOCKS_URL) if WALLET_USE_TOR else None


def xmr_to_piconero(xmr: Decimal) -> int:
    return int(xmr * PICONERO)


def piconero_to_xmr_str(pico: int) -> str:
    return f"{Decimal(pico) / PICONERO:.12f}".rstrip("0").rstrip(".")


async def rpc(method: str, params: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
    client_kwargs = {
        "timeout": 30.0,
        "auth": httpx.DigestAuth(WALLET_RPC_USER, WALLET_RPC_PASSWORD),
    }
    if _transport is not None:
        client_kwargs["transport"] = _transport
    async with httpx.AsyncClient(**client_kwargs) as c:
        r = await c.post(WALLET_RPC_URL, json=payload)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"wallet rpc error: {data['error']}")
        return data["result"]


async def make_subaddress(label: str) -> tuple[int, str]:
    res = await rpc("create_address", {"account_index": 0, "label": label})
    return res["address_index"], res["address"]


async def create_payment(
    label: str,
    price_micro: int,
    credit_micro: int,
    validity_days: int,
    xmr_per_usd: Decimal,
) -> dict:
    """Generate payment + subaddress + xmr amount. Persist as 'pending'.
    `label` is bundle name (e.g. 'small') or 'custom'. validity_days carries to the issued key."""
    # price_micro / MICRO = USD as Decimal, then / xmr_per_usd = XMR amount
    xmr_amount = (Decimal(price_micro) / Decimal(MICRO) / xmr_per_usd).quantize(Decimal("0.000000000001"))
    payment_id = secrets.token_urlsafe(16)
    subaddr_index, address = await make_subaddress(payment_id)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=PAYMENT_EXPIRY_MINUTES)

    async with db._lock:
        db.conn().execute(
            "INSERT INTO payments (payment_id, xmr_address, xmr_subaddr_index, xmr_amount, "
            "credit_micro_usd, bundle_name, validity_days, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (payment_id, address, subaddr_index, str(xmr_amount), credit_micro, label,
             validity_days, now.isoformat(), expires.isoformat()),
        )
        db.conn().commit()

    return {
        "payment_id": payment_id,
        "xmr_address": address,
        "xmr_amount": piconero_to_xmr_str(xmr_to_piconero(xmr_amount)),
        "bundle": label,
        "credit_usd": round(credit_micro / MICRO, 6),
        "expires_at": expires.isoformat(),
    }


async def claim_and_issue(payment_id: str) -> str | None:
    """Atomic ready->completed transition + api_keys insert. Returns plaintext key exactly once.
    Wrapped in BEGIN IMMEDIATE so any failure rolls back both UPDATE and INSERT atomically."""
    plaintext = "sk-" + secrets.token_urlsafe(48)
    key_hash = db.hash_key(plaintext)
    now = datetime.now(timezone.utc)

    async with db._lock:
        conn = db.conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE payments SET status = 'completed', key_hash = ?, confirmed_at = ? "
                "WHERE payment_id = ? AND status = 'ready'",
                (key_hash, now.isoformat(), payment_id),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return None

            row = conn.execute(
                "SELECT credit_micro_usd, validity_days FROM payments WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            credit_micro, validity = row
            expires = (now + timedelta(days=int(validity))).isoformat()

            conn.execute(
                "INSERT INTO api_keys (key_hash, credit_balance, credit_spent, created_at, expires_at, is_active) "
                "VALUES (?, ?, 0, ?, ?, 1)",
                (key_hash, credit_micro, now.isoformat(), expires),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return plaintext
