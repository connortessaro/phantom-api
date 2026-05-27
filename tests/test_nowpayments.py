"""Unit tests for nowpayments.verify_ipn + status mapper.

HMAC verification is security-critical — bad sig must return False, never
raise. Status mapper translates NowPayments-side strings to phantom's state
machine. Both pure functions, easy to test without network.

Tests force-override `nowpayments.NP_IPN_SECRET` to a known fixture secret
so we don't depend on whatever value happens to be in the dev .env."""
import hashlib
import hmac
import json

import pytest

import nowpayments

_TEST_SECRET = "test-np-secret-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _override_secret(monkeypatch):
    monkeypatch.setattr(nowpayments, "NP_IPN_SECRET", _TEST_SECRET)
    yield


def _sign(body: bytes, secret: str = _TEST_SECRET) -> str:
    """Reference signature for tests — same algorithm as NowPayments + verify_ipn."""
    msg = json.loads(body)
    sorted_msg = json.dumps(msg, separators=(",", ":"), sort_keys=True)
    return hmac.new(secret.encode(), sorted_msg.encode(), hashlib.sha512).hexdigest()


def test_verify_ipn_valid_signature():
    body = b'{"order_id":"abc","payment_status":"finished"}'
    sig = _sign(body)
    assert nowpayments.verify_ipn(body, sig) is True


def test_verify_ipn_wrong_signature():
    body = b'{"order_id":"abc","payment_status":"finished"}'
    assert nowpayments.verify_ipn(body, "0" * 128) is False


def test_verify_ipn_tampered_body():
    body = b'{"order_id":"abc","payment_status":"finished"}'
    sig = _sign(body)
    tampered = b'{"order_id":"abc","payment_status":"waiting"}'
    assert nowpayments.verify_ipn(tampered, sig) is False


def test_verify_ipn_missing_signature_header():
    body = b'{"order_id":"abc"}'
    assert nowpayments.verify_ipn(body, "") is False
    assert nowpayments.verify_ipn(body, None) is False


def test_verify_ipn_malformed_body():
    # Garbage body — must return False, NOT raise.
    assert nowpayments.verify_ipn(b"not-json{{{", "deadbeef") is False
    assert nowpayments.verify_ipn(b"", "deadbeef") is False


def test_verify_ipn_key_order_canonical():
    """NowPayments docs require sort_keys=True. Two bodies with same fields
    in different order should produce same signature."""
    body_a = json.dumps({"a": 1, "b": 2}, separators=(",", ":")).encode()
    body_b = json.dumps({"b": 2, "a": 1}, separators=(",", ":")).encode()
    # Both should validate against the same canonical signature.
    canonical = json.dumps({"a": 1, "b": 2}, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(b"test-np-secret-do-not-use-in-prod", canonical, hashlib.sha512).hexdigest()
    assert nowpayments.verify_ipn(body_a, sig) is True
    assert nowpayments.verify_ipn(body_b, sig) is True


def test_verify_ipn_realistic_payload():
    """Webhook body shape from NowPayments docs."""
    body = json.dumps({
        "payment_id": 123456789,
        "parent_payment_id": None,
        "invoice_id": 4522625843,
        "payment_status": "finished",
        "pay_address": "address-here",
        "price_amount": 50,
        "price_currency": "usd",
        "pay_amount": 0.0012,
        "actually_paid": 0.0012,
        "pay_currency": "btc",
        "order_id": "phantom-pay-id-xyz",
        "outcome_amount": 0.31,
        "outcome_currency": "xmr",
    }, separators=(",", ":")).encode()
    sig = _sign(body)
    assert nowpayments.verify_ipn(body, sig) is True


# ─── Status mapper ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("np_status,expected", [
    ("waiting",        ("pending",    False)),
    ("confirming",     ("confirming", False)),
    ("confirmed",      ("confirming", False)),
    ("sending",        ("confirming", False)),
    ("finished",       ("ready",      True)),
    ("partially_paid", ("expired",    False)),
    ("failed",         ("expired",    False)),
    ("expired",        ("expired",    False)),
    ("refunded",       ("expired",    False)),
])
def test_map_status_known(np_status, expected):
    assert nowpayments.map_status(np_status) == expected


def test_map_status_unknown_safe_fallback():
    # Unknown status must NOT issue a key — safer to leave as pending and review.
    assert nowpayments.map_status("magical-new-status") == ("pending", False)
    assert nowpayments.map_status("") == ("pending", False)


def test_map_status_finished_is_only_issue_path():
    """No other NowPayments status should grant the key. Defensive — protects
    against future status names slipping into a state we'd treat as success."""
    for np_status in ["waiting", "confirming", "confirmed", "sending",
                      "partially_paid", "failed", "expired", "refunded",
                      "unknown-future"]:
        _, issue = nowpayments.map_status(np_status)
        assert issue is False, f"{np_status} unexpectedly issues a key"
    # Only finished issues.
    _, issue = nowpayments.map_status("finished")
    assert issue is True
