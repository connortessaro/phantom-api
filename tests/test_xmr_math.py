"""Unit tests for XMR amount handling. All XMR values are Decimal or piconero int.
NEVER float.
"""
from decimal import Decimal
from payments import xmr_to_piconero, piconero_to_xmr_str, PICONERO


def test_one_xmr_round_trip():
    p = xmr_to_piconero(Decimal("1"))
    assert p == 10**12
    s = piconero_to_xmr_str(p)
    assert s == "1"


def test_tiny_amount_round_trip():
    # 0.000128955717 XMR — matches our stagenet test amount
    amount = Decimal("0.000128955717")
    p = xmr_to_piconero(amount)
    assert p == 128_955_717
    s = piconero_to_xmr_str(p)
    assert Decimal(s) == amount


def test_zero():
    assert xmr_to_piconero(Decimal("0")) == 0
    assert piconero_to_xmr_str(0) == "0"


def test_smallest_unit():
    # 1 piconero = smallest possible amount
    assert xmr_to_piconero(Decimal("0.000000000001")) == 1
    assert piconero_to_xmr_str(1) == "0.000000000001"


def test_xmr_to_piconero_returns_int():
    p = xmr_to_piconero(Decimal("0.5"))
    assert isinstance(p, int)


def test_no_float_in_round_trip():
    # Specifically reject float inputs (would cause precision drift).
    # We expect a Decimal-based code path. Use a value that loses precision as float.
    amt = Decimal("0.123456789012")
    p = xmr_to_piconero(amt)
    assert p == 123_456_789_012
    assert piconero_to_xmr_str(p) == "0.123456789012"


def test_piconero_strips_trailing_zeros():
    # "1.500000000000" should render as "1.5"
    p = xmr_to_piconero(Decimal("1.5"))
    s = piconero_to_xmr_str(p)
    assert s == "1.5"


def test_piconero_constant_is_exact():
    assert PICONERO == Decimal("1e12")
    assert int(PICONERO) == 10**12
