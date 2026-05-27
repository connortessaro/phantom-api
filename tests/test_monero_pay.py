"""Unit tests for monero_pay.parse_callback + status mapper.

MoneroPay does NOT sign callbacks; phantom authenticates via per-payment
URL path tokens (the 16-byte token_urlsafe payment_id). The HTTP endpoint
verifies the URL token matches body description + a known DB row; those
checks live in main.py's handler, not in this module.

Tests here cover the pure functions:
- parse_callback: extracts normalized fields from a MoneroPay callback body
- map_status: translates parsed callback to (phantom_status, should_issue_key)
"""
import monero_pay


# ─── parse_callback ───────────────────────────────────────────────────────────

def test_parse_callback_singular_transaction():
    """Callback shape per moneropay.eu/api/callback.html — single 'transaction'."""
    body = {
        "amount": {
            "expected": 200000000,
            "covered": {"total": 200000000, "unlocked": 200000000},
        },
        "complete": True,
        "description": "abc123",
        "transaction": {
            "amount": 200000000,
            "confirmations": 10,
            "double_spend_seen": False,
            "tx_hash": "deadbeef",
        },
    }
    p = monero_pay.parse_callback(body)
    assert p["description"] == "abc123"
    assert p["expected_pico"] == 200000000
    assert p["received_pico"] == 200000000
    assert p["unlocked_pico"] == 200000000
    assert p["complete"] is True
    assert p["confirmations"] == 10
    assert p["double_spend_seen"] is False


def test_parse_callback_plural_transactions():
    """GET /receive/<addr> uses plural 'transactions' — handle both."""
    body = {
        "amount": {"expected": 1000, "covered": {"total": 1000, "unlocked": 1000}},
        "complete": True,
        "description": "xyz",
        "transactions": [
            {"amount": 600, "confirmations": 5},
            {"amount": 400, "confirmations": 8},
        ],
    }
    p = monero_pay.parse_callback(body)
    assert p["confirmations"] == 5  # min across all txs


def test_parse_callback_double_spend_flagged():
    body = {
        "amount": {"expected": 100, "covered": {"total": 100, "unlocked": 0}},
        "complete": False,
        "description": "ds-test",
        "transactions": [
            {"amount": 100, "confirmations": 1, "double_spend_seen": True},
        ],
    }
    p = monero_pay.parse_callback(body)
    assert p["double_spend_seen"] is True


def test_parse_callback_empty_description():
    p = monero_pay.parse_callback({"amount": {"expected": 0, "covered": {}}, "complete": False})
    assert p["description"] == ""
    assert p["expected_pico"] == 0
    assert p["received_pico"] == 0


def test_parse_callback_missing_fields_safe():
    """Tolerates missing/null fields — must not crash on malformed input."""
    p = monero_pay.parse_callback({})
    assert p["description"] == ""
    assert p["expected_pico"] == 0
    assert p["confirmations"] == 0
    assert p["complete"] is False


# ─── map_status ────────────────────────────────────────────────────────────────


def _parsed(expected=10**12, received=0, unlocked=0, complete=False, confs=0, ds=False):
    return {
        "description":        "test",
        "expected_pico":      expected,
        "received_pico":      received,
        "unlocked_pico":      unlocked,
        "complete":           complete,
        "confirmations":      confs,
        "double_spend_seen":  ds,
    }


def test_map_status_double_spend_expires():
    """Any double-spend flag → expired. Anti-fraud safety."""
    assert monero_pay.map_status(_parsed(received=10**12, ds=True)) == ("expired", False)


def test_map_status_complete_issues_key():
    """`complete: true` → ready. MoneroPay's authoritative paid signal."""
    assert monero_pay.map_status(_parsed(received=10**12, unlocked=10**12, complete=True)) == ("ready", True)


def test_map_status_no_payment_received():
    assert monero_pay.map_status(_parsed()) == ("pending", False)


def test_map_status_partial_payment_expires():
    """< 98% received → expired (matches NowPayments behavior, no partial flow)."""
    assert monero_pay.map_status(_parsed(received=5 * 10**11)) == ("expired", False)


def test_map_status_within_2pct_slack_confirming():
    """≥98% received but not complete → confirming (waiting for unlock)."""
    expected = 10**12
    received = int(expected * 0.985)
    assert monero_pay.map_status(_parsed(received=received)) == ("confirming", False)


def test_map_status_complete_overrides_slack():
    """complete=true wins even if confirmations field is zero. MoneroPay
    only flips complete after unlock + threshold passed."""
    assert monero_pay.map_status(_parsed(received=10**12, complete=True, confs=0)) == ("ready", True)
