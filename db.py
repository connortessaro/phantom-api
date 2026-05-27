"""SQLCipher access layer. Passphrase loaded from env, then wiped.
All monetary values stored as integer micro-USD ($1 = 1_000_000)."""
import os
import asyncio
import hashlib
from datetime import datetime, timezone
import sqlcipher3 as sqlite3

_lock = asyncio.Lock()
_conn = None


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def init_db(path: str):
    """Open SQLCipher DB using passphrase from PHANTOM_DB_PASSPHRASE env."""
    global _conn
    passphrase = os.environ.get("PHANTOM_DB_PASSPHRASE")
    if not passphrase:
        raise RuntimeError("PHANTOM_DB_PASSPHRASE not set — DB cannot be opened")

    _conn = sqlite3.connect(path, check_same_thread=False)
    # SQL single-quoted string literal: only need to double up single quotes.
    # Single-quote form is preferred over double-quote (which SQL treats as an
    # identifier-quote in some contexts).
    safe = passphrase.replace("'", "''")
    _conn.execute(f"PRAGMA key = '{safe}'")
    _conn.execute("PRAGMA cipher_page_size = 4096")
    _conn.execute("PRAGMA kdf_iter = 256000")
    _conn.execute("PRAGMA journal_mode = WAL")
    _conn.execute("PRAGMA synchronous = NORMAL")

    try:
        _conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as e:
        msg = str(e).lower()
        if "i/o" in msg or "io error" in msg:
            raise RuntimeError(
                f"SQLCipher I/O error opening {path} — check for stale .db-wal / .db-shm "
                f"files, dir permissions, or partial deletion. Original: {e}"
            )
        raise RuntimeError(f"SQLCipher passphrase rejected (or corrupt DB): {e}")

    with open("schema.sql") as f:
        _conn.executescript(f.read())
    _conn.commit()

    # Idempotent migration: add NowPayments columns to pre-existing payments tables.
    # SQLite has no "ADD COLUMN IF NOT EXISTS"; we detect + apply manually.
    existing_cols = {
        row[1] for row in _conn.execute("PRAGMA table_info(payments)").fetchall()
    }
    np_columns = {
        "np_invoice_id":      "TEXT",
        "np_payment_id":      "TEXT",
        "pay_currency":       "TEXT",
        "pay_amount":         "TEXT",
        "outcome_amount":     "TEXT",
        "parent_payment_id":  "TEXT",
    }
    for col, col_type in np_columns.items():
        if col not in existing_cols:
            _conn.execute(f"ALTER TABLE payments ADD COLUMN {col} {col_type}")
    _conn.commit()

    os.environ.pop("PHANTOM_DB_PASSPHRASE", None)


def conn():
    if _conn is None:
        raise RuntimeError("DB not initialized — call init_db first")
    return _conn


async def outstanding_credit_micro() -> int:
    """Sum of unspent credit across active keys + paid-but-unissued payments.
    Excludes 'pending' (subaddress created, no payment received) so a flood of
    abandoned $1000 orders can't lock the budget for an hour. Tradeoff: if many
    confirming/ready payments convert simultaneously near the cap, we may
    briefly oversell by the value of payments that arrived since last check.
    Operator must keep upstream balance > REDPILL_BUDGET_MICRO anyway."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with _lock:
        active = _conn.execute(
            "SELECT COALESCE(SUM(credit_balance), 0) FROM api_keys "
            "WHERE is_active = 1 AND expires_at > ?",
            (now_iso,),
        ).fetchone()[0]
        committed = _conn.execute(
            "SELECT COALESCE(SUM(credit_micro_usd), 0) FROM payments "
            "WHERE status IN ('confirming', 'ready')"
        ).fetchone()[0]
    return int(active or 0) + int(committed or 0)


async def check_credits(key_hash: str, min_required_micro: int) -> dict | None:
    """Return key row if active and balance >= min_required_micro, else None."""
    async with _lock:
        row = _conn.execute(
            "SELECT key_hash, credit_balance, credit_spent, expires_at, is_active "
            "FROM api_keys WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
    if not row:
        return None
    kh, balance, spent, expires_at, is_active = row
    if not is_active or balance < min_required_micro:
        return None
    if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
        return None
    return {
        "key_hash": kh,
        "credit_balance": balance,
        "credit_spent": spent,
        "expires_at": expires_at,
    }


async def create_np_payment(
    payment_id: str,
    label: str,
    price_micro: int,
    credit_micro: int,
    validity_days: int,
    *,
    np_invoice_id: str,
    expires_at_iso: str,
) -> None:
    """Persist a freshly-created NowPayments invoice. Reuses the `payments`
    table with the legacy-XMR columns nullable; we add the NowPayments
    invoice id + leave xmr_* unused. status starts as 'pending' and walks
    pending → confirming → ready → completed (claim) via IPN updates."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with _lock:
        _conn.execute(
            "INSERT INTO payments (payment_id, xmr_address, xmr_subaddr_index, "
            "  xmr_amount, credit_micro_usd, bundle_name, validity_days, "
            "  status, created_at, expires_at, np_invoice_id) "
            "VALUES (?, ?, 0, '0', ?, ?, ?, 'pending', ?, ?, ?)",
            (
                payment_id,
                f"np:{np_invoice_id}",   # placeholder string in xmr_address slot
                credit_micro,
                label,
                validity_days,
                now_iso,
                expires_at_iso,
                np_invoice_id,
            ),
        )
        _conn.commit()


async def update_np_payment_status(
    payment_id: str,
    new_status: str,
    *,
    np_payment_id: str | None = None,
    pay_currency: str | None = None,
    pay_amount: str | None = None,
    outcome_amount: str | None = None,
    parent_payment_id: str | None = None,
) -> bool:
    """Move a NowPayments-tracked payment to a new state. Only transitions
    forward (pending→confirming→ready) or to terminal (expired). Returns
    True if a row was updated. False on no-op (e.g. already in target
    state, or terminal state we can't move out of).

    Re-deposits arrive with parent_payment_id != null. We persist it but
    refuse to flip an already-completed parent payment back to ready —
    that would double-credit the customer."""
    if new_status not in ("pending", "confirming", "ready", "expired"):
        return False
    sets = ["status = ?"]
    params: list = [new_status]
    if np_payment_id is not None:
        sets.append("np_payment_id = ?")
        params.append(np_payment_id)
    if pay_currency is not None:
        sets.append("pay_currency = ?")
        params.append(pay_currency)
    if pay_amount is not None:
        sets.append("pay_amount = ?")
        params.append(pay_amount)
    if outcome_amount is not None:
        sets.append("outcome_amount = ?")
        params.append(outcome_amount)
    if parent_payment_id is not None:
        sets.append("parent_payment_id = ?")
        params.append(parent_payment_id)
    params.append(payment_id)
    # Guard rails:
    #  - cannot leave a terminal state ('completed', 'expired')
    #  - re-deposit (parent_payment_id set) cannot push parent back to ready
    where = "payment_id = ? AND status NOT IN ('completed', 'expired')"
    sql = f"UPDATE payments SET {', '.join(sets)} WHERE {where}"
    async with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
    return cur.rowcount > 0


async def claim_and_issue(payment_id: str) -> str | None:
    """Atomic ready→completed transition + api_keys insert. Returns plaintext
    key exactly once, or None if the row isn't in 'ready' (either still pending
    or already claimed). BEGIN IMMEDIATE so a failure rolls back both UPDATE
    and INSERT atomically."""
    from datetime import timedelta
    import secrets as _s
    plaintext = "sk-" + _s.token_urlsafe(48)
    key_h = hash_key(plaintext)
    now = datetime.now(timezone.utc)
    async with _lock:
        try:
            _conn.execute("BEGIN IMMEDIATE")
            cur = _conn.execute(
                "UPDATE payments SET status = 'completed', key_hash = ?, confirmed_at = ? "
                "WHERE payment_id = ? AND status = 'ready'",
                (key_h, now.isoformat(), payment_id),
            )
            if cur.rowcount == 0:
                _conn.execute("ROLLBACK")
                return None
            row = _conn.execute(
                "SELECT credit_micro_usd, validity_days FROM payments WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            credit_micro, validity = row
            expires = (now + timedelta(days=int(validity))).isoformat()
            _conn.execute(
                "INSERT INTO api_keys (key_hash, credit_balance, credit_spent, created_at, expires_at, is_active) "
                "VALUES (?, ?, 0, ?, ?, 1)",
                (key_h, credit_micro, now.isoformat(), expires),
            )
            _conn.execute("COMMIT")
        except Exception:
            _conn.execute("ROLLBACK")
            raise
    return plaintext


async def rotate_key(old_hash: str) -> str | None:
    """Atomically issue a new plaintext key inheriting balance + expiry from the old one,
    then deactivate the old key. Returns new plaintext once, or None if old key invalid."""
    new_plaintext = "sk-" + __import__("secrets").token_urlsafe(48)
    new_hash = hash_key(new_plaintext)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with _lock:
        try:
            _conn.execute("BEGIN IMMEDIATE")
            row = _conn.execute(
                "SELECT credit_balance, credit_spent, expires_at, is_active "
                "FROM api_keys WHERE key_hash = ?",
                (old_hash,),
            ).fetchone()
            if not row or not row[3]:
                _conn.execute("ROLLBACK")
                return None
            if datetime.fromisoformat(row[2]) < datetime.now(timezone.utc):
                _conn.execute("ROLLBACK")
                return None
            _conn.execute(
                "INSERT INTO api_keys (key_hash, credit_balance, credit_spent, created_at, expires_at, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (new_hash, row[0], row[1], now_iso, row[2]),
            )
            _conn.execute(
                "UPDATE api_keys SET is_active = 0 WHERE key_hash = ?",
                (old_hash,),
            )
            _conn.execute("COMMIT")
        except Exception:
            _conn.execute("ROLLBACK")
            raise
    return new_plaintext


async def decrement_credits(
    key_hash: str,
    cost_micro_usd: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> bool:
    """Atomic decrement. Returns False if insufficient balance or inactive key."""
    if cost_micro_usd <= 0:
        return True
    async with _lock:
        cur = _conn.execute(
            "UPDATE api_keys "
            "SET credit_balance = credit_balance - ?, credit_spent = credit_spent + ? "
            "WHERE key_hash = ? AND credit_balance >= ? AND is_active = 1",
            (cost_micro_usd, cost_micro_usd, key_hash, cost_micro_usd),
        )
        if cur.rowcount == 0:
            return False
        _conn.execute(
            "INSERT INTO usage_log (key_hash, model, prompt_tokens, completion_tokens, cost_micro_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key_hash, model, prompt_tokens, completion_tokens, cost_micro_usd,
             datetime.now(timezone.utc).isoformat()),
        )
        _conn.commit()
    return True
