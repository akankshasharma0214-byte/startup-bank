"""Money is always an integer count of cents in the database and in business logic.
Floats never touch a dollar amount; the only float in this module is the Decimal-free
formatting path, which also avoids floats."""

from decimal import ROUND_HALF_UP, Decimal


def dollars_to_cents(amount: str | float | Decimal) -> int:
    """Parse a dollar amount (string preferred) into an integer number of cents."""
    d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(d * 100)


def cents_to_dollars(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def fmt_usd(cents: int) -> str:
    negative = cents < 0
    d = cents_to_dollars(abs(cents))
    s = f"${d:,.2f}"
    return f"-{s}" if negative else s
