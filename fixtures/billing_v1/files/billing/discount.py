"""Percentage discounts on amounts in integer cents."""

from decimal import Decimal

from billing.rounding import round_half_up


def apply_discount(price_cents: int, percent: int) -> int:
    """Return the price after taking `percent`% off. The discount is rounded half up to the cent."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return price_cents - round_half_up(Decimal(price_cents) * percent / 100)
