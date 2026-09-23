import pytest

from app.money import cents_to_dollars, dollars_to_cents, fmt_usd


@pytest.mark.parametrize(
    "s,cents",
    [("10", 1000), ("10.00", 1000), ("10.5", 1050), ("10.555", 1056), ("0.01", 1), ("0", 0), ("1234567.89", 123456789)],
)
def test_dollars_to_cents(s, cents):
    assert dollars_to_cents(s) == cents


def test_cents_to_dollars_roundtrip():
    assert cents_to_dollars(123456) == pytest_approx_decimal("1234.56")


def pytest_approx_decimal(s):
    from decimal import Decimal

    return Decimal(s)


@pytest.mark.parametrize("cents,expected", [(0, "$0.00"), (100, "$1.00"), (150000, "$1,500.00"), (-500, "-$5.00"), (5, "$0.05")])
def test_fmt_usd(cents, expected):
    assert fmt_usd(cents) == expected


def test_no_float_leakage():
    # classic float trap: 0.1 + 0.2 != 0.3 in binary float. Confirm cents arithmetic avoids it entirely.
    a, b = dollars_to_cents("0.10"), dollars_to_cents("0.20")
    assert a + b == dollars_to_cents("0.30") == 30
