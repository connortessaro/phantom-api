"""Concurrency tests — the real money safety net.

Verify that under N concurrent callers:
- claim_and_issue returns plaintext key EXACTLY ONCE per payment (not twice, not zero)
- decrement_credits never lets total decrements exceed balance
- rotate_key issues at most one new key per old key
- outstanding_credit_micro sums correctly

These cover the SQL atomicity assumptions in db.py / payments.py.
"""
import asyncio
import secrets
from datetime import datetime, timedelta, timezone

import pytest


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _future_iso(days=90):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


async def _seed_ready_payment(db, payment_id: str, credit_micro: int = 10_000_000):
    """Insert a payment in 'ready' state, ready for claim_and_issue."""
    async with db._lock:
        db.conn().execute(
            "INSERT INTO payments (payment_id, xmr_address, xmr_subaddr_index, xmr_amount, "
            "credit_micro_usd, bundle_name, validity_days, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)",
            (payment_id, "75z" + secrets.token_hex(8), 1, "0.01", credit_micro,
             "test", 90, _now_iso(), _future_iso(1)),
        )
        db.conn().commit()


async def _seed_active_key(db, plaintext: str, balance: int = 10_000_000):
    """Insert an active API key with the given balance (micro-USD)."""
    h = db.hash_key(plaintext)
    async with db._lock:
        db.conn().execute(
            "INSERT INTO api_keys (key_hash, credit_balance, credit_spent, created_at, expires_at, is_active) "
            "VALUES (?, ?, 0, ?, ?, 1)",
            (h, balance, _now_iso(), _future_iso(90)),
        )
        db.conn().commit()
    return h


# ───────────────────────────────────────────────────────────────────────
# claim_and_issue — atomic ready→completed, key issued exactly once
# ───────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_claim_and_issue_returns_key_exactly_once(fresh_db):
    """Even with 20 concurrent callers, only one wins the UPDATE and gets a key."""
    import payments
    pid = "pay-once-" + secrets.token_hex(4)
    await _seed_ready_payment(fresh_db, pid)

    results = await asyncio.gather(*[payments.claim_and_issue(pid) for _ in range(20)])

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected 1 winner, got {len(winners)}: {winners}"
    assert winners[0].startswith("sk-")

    # All non-winners returned None
    losers = [r for r in results if r is None]
    assert len(losers) == 19


@pytest.mark.asyncio
async def test_claim_and_issue_creates_exactly_one_api_key(fresh_db):
    import payments
    pid = "pay-key-" + secrets.token_hex(4)
    await _seed_ready_payment(fresh_db, pid, credit_micro=50_000_000)

    await asyncio.gather(*[payments.claim_and_issue(pid) for _ in range(15)])

    async with fresh_db._lock:
        rows = fresh_db.conn().execute("SELECT COUNT(*) FROM api_keys").fetchone()
    assert rows[0] == 1, "exactly one api_keys row should exist"


@pytest.mark.asyncio
async def test_claim_marks_payment_completed(fresh_db):
    import payments
    pid = "pay-complete-" + secrets.token_hex(4)
    await _seed_ready_payment(fresh_db, pid)

    await asyncio.gather(*[payments.claim_and_issue(pid) for _ in range(10)])

    async with fresh_db._lock:
        row = fresh_db.conn().execute(
            "SELECT status FROM payments WHERE payment_id = ?", (pid,)
        ).fetchone()
    assert row[0] == "completed"


@pytest.mark.asyncio
async def test_claim_on_non_ready_payment_returns_none(fresh_db):
    """Pending / expired payments must not produce keys."""
    import payments
    pid = "pay-pending-" + secrets.token_hex(4)
    async with fresh_db._lock:
        fresh_db.conn().execute(
            "INSERT INTO payments (payment_id, xmr_address, xmr_subaddr_index, xmr_amount, "
            "credit_micro_usd, bundle_name, validity_days, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (pid, "75z", 0, "0.01", 1_000_000, "test", 1, _now_iso(), _future_iso(1)),
        )
        fresh_db.conn().commit()

    result = await payments.claim_and_issue(pid)
    assert result is None


# ───────────────────────────────────────────────────────────────────────
# decrement_credits — concurrent decrements never over-spend
# ───────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_decrement_concurrent_no_oversend(fresh_db):
    """Balance=100, 20 concurrent decrements of 10 each.
    All 10 should succeed (10*10=100), the next 10 should fail.
    Balance must NEVER go negative."""
    key = "sk-" + secrets.token_urlsafe(48)
    h = await _seed_active_key(fresh_db, key, balance=100)

    results = await asyncio.gather(
        *[fresh_db.decrement_credits(h, 10, "test-model", 1, 1) for _ in range(20)]
    )

    successes = sum(1 for r in results if r is True)
    failures = sum(1 for r in results if r is False)
    assert successes == 10, f"expected 10 successes, got {successes}"
    assert failures == 10, f"expected 10 failures, got {failures}"

    async with fresh_db._lock:
        row = fresh_db.conn().execute(
            "SELECT credit_balance, credit_spent FROM api_keys WHERE key_hash = ?",
            (h,),
        ).fetchone()
    assert row[0] == 0, f"balance should be exactly 0, got {row[0]}"
    assert row[1] == 100, f"credit_spent should be 100, got {row[1]}"


@pytest.mark.asyncio
async def test_decrement_uneven_amounts(fresh_db):
    """Mixed amounts. balance=50. Try decrements: 30, 20, 10, 5.
    All 4 sum to 65 > 50. Must succeed in some valid order until balance < required."""
    key = "sk-" + secrets.token_urlsafe(48)
    h = await _seed_active_key(fresh_db, key, balance=50)

    amts = [30, 20, 10, 5]
    results = await asyncio.gather(
        *[fresh_db.decrement_credits(h, a, "m", 1, 1) for a in amts]
    )

    # Final balance must be >= 0
    async with fresh_db._lock:
        row = fresh_db.conn().execute(
            "SELECT credit_balance, credit_spent FROM api_keys WHERE key_hash = ?", (h,)
        ).fetchone()
    assert row[0] >= 0
    # spent = sum of successful decrements
    successful_sum = sum(a for a, r in zip(amts, results) if r)
    assert row[1] == successful_sum


@pytest.mark.asyncio
async def test_decrement_inactive_key_fails(fresh_db):
    key = "sk-" + secrets.token_urlsafe(48)
    h = await _seed_active_key(fresh_db, key, balance=100)
    async with fresh_db._lock:
        fresh_db.conn().execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_hash = ?", (h,)
        )
        fresh_db.conn().commit()

    result = await fresh_db.decrement_credits(h, 10, "m", 1, 1)
    assert result is False


# ───────────────────────────────────────────────────────────────────────
# rotate_key — concurrent rotations of same key
# ───────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_rotate_concurrent_issues_one_new_key(fresh_db):
    """20 concurrent rotate_key calls on the same old key.
    Should produce at most one new active key total (old becomes inactive)."""
    old_plain = "sk-" + secrets.token_urlsafe(48)
    h_old = await _seed_active_key(fresh_db, old_plain, balance=10_000_000)

    results = await asyncio.gather(*[fresh_db.rotate_key(h_old) for _ in range(20)])

    new_keys = [r for r in results if r is not None]
    # At most ONE new key issued. Others see deactivated old key and return None.
    assert len(new_keys) <= 1, f"too many new keys issued: {len(new_keys)}"

    # Old key MUST be deactivated regardless of how many calls won
    async with fresh_db._lock:
        row = fresh_db.conn().execute(
            "SELECT is_active FROM api_keys WHERE key_hash = ?", (h_old,)
        ).fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_rotate_preserves_balance(fresh_db):
    old_plain = "sk-" + secrets.token_urlsafe(48)
    await _seed_active_key(fresh_db, old_plain, balance=7_500_000)

    new_plain = await fresh_db.rotate_key(fresh_db.hash_key(old_plain))
    assert new_plain is not None

    async with fresh_db._lock:
        row = fresh_db.conn().execute(
            "SELECT credit_balance, is_active FROM api_keys WHERE key_hash = ?",
            (fresh_db.hash_key(new_plain),),
        ).fetchone()
    assert row[0] == 7_500_000
    assert row[1] == 1


@pytest.mark.asyncio
async def test_rotate_invalid_key_returns_none(fresh_db):
    """Rotating a key that doesn't exist must return None, not crash."""
    result = await fresh_db.rotate_key("deadbeef" * 8)
    assert result is None


# ───────────────────────────────────────────────────────────────────────
# outstanding_credit_micro — capacity ceiling check
# ───────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_outstanding_sums_active_keys(fresh_db):
    for i in range(3):
        k = "sk-" + secrets.token_urlsafe(48)
        await _seed_active_key(fresh_db, k, balance=5_000_000)

    total = await fresh_db.outstanding_credit_micro()
    assert total == 15_000_000


@pytest.mark.asyncio
async def test_outstanding_excludes_inactive_keys(fresh_db):
    active = "sk-active-" + secrets.token_urlsafe(20)
    inactive = "sk-inactive-" + secrets.token_urlsafe(20)
    await _seed_active_key(fresh_db, active, balance=5_000_000)
    h_in = await _seed_active_key(fresh_db, inactive, balance=99_000_000)
    async with fresh_db._lock:
        fresh_db.conn().execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_hash = ?", (h_in,)
        )
        fresh_db.conn().commit()

    total = await fresh_db.outstanding_credit_micro()
    assert total == 5_000_000


@pytest.mark.asyncio
async def test_outstanding_excludes_pending_payments(fresh_db):
    """Pending payments are unpaid orders — excluded from the capacity counter
    so an attacker can't lock the budget with abandoned $1000 orders for an hour.
    Only paid commitments (confirming / ready) count against capacity."""
    pid = "pay-pending-" + secrets.token_hex(4)
    async with fresh_db._lock:
        fresh_db.conn().execute(
            "INSERT INTO payments (payment_id, xmr_address, xmr_subaddr_index, xmr_amount, "
            "credit_micro_usd, bundle_name, validity_days, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (pid, "75z", 0, "0.01", 3_000_000, "test", 1, _now_iso(), _future_iso(1)),
        )
        fresh_db.conn().commit()

    total = await fresh_db.outstanding_credit_micro()
    assert total == 0


@pytest.mark.asyncio
async def test_outstanding_includes_confirming_and_ready(fresh_db):
    """Paid-but-unissued orders (confirming, ready) still consume capacity —
    we've already received the XMR and committed to honoring the credit."""
    pid_conf  = "pay-conf-"  + secrets.token_hex(4)
    pid_ready = "pay-ready-" + secrets.token_hex(4)
    async with fresh_db._lock:
        fresh_db.conn().execute(
            "INSERT INTO payments (payment_id, xmr_address, xmr_subaddr_index, xmr_amount, "
            "credit_micro_usd, bundle_name, validity_days, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'confirming', ?, ?)",
            (pid_conf, "75z", 0, "0.01", 4_000_000, "test", 1, _now_iso(), _future_iso(1)),
        )
        fresh_db.conn().execute(
            "INSERT INTO payments (payment_id, xmr_address, xmr_subaddr_index, xmr_amount, "
            "credit_micro_usd, bundle_name, validity_days, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)",
            (pid_ready, "75z", 1, "0.01", 7_000_000, "test", 1, _now_iso(), _future_iso(1)),
        )
        fresh_db.conn().commit()

    total = await fresh_db.outstanding_credit_micro()
    assert total == 11_000_000


@pytest.mark.asyncio
async def test_outstanding_excludes_completed_payments(fresh_db):
    pid_done = "pay-done-" + secrets.token_hex(4)
    pid_exp = "pay-exp-" + secrets.token_hex(4)
    async with fresh_db._lock:
        fresh_db.conn().execute(
            "INSERT INTO payments (payment_id, xmr_address, xmr_subaddr_index, xmr_amount, "
            "credit_micro_usd, bundle_name, validity_days, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)",
            (pid_done, "75z", 0, "0.01", 9_000_000, "test", 1, _now_iso(), _future_iso(1)),
        )
        fresh_db.conn().execute(
            "INSERT INTO payments (payment_id, xmr_address, xmr_subaddr_index, xmr_amount, "
            "credit_micro_usd, bundle_name, validity_days, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'expired', ?, ?)",
            (pid_exp, "75z", 0, "0.01", 9_000_000, "test", 1, _now_iso(), _future_iso(1)),
        )
        fresh_db.conn().commit()

    total = await fresh_db.outstanding_credit_micro()
    assert total == 0
