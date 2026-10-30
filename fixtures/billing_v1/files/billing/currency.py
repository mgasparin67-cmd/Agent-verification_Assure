"""Currency conversion."""

from decimal import Decimal

from billing.rounding import round_half_up


def convert(cents: int, rate: str) -> int:
    """Convert an amount in cents at `rate`, a decimal string such as "1.0825", rounded half up."""
    return round_half_up(Decimal(cents) * Decimal(rate))
