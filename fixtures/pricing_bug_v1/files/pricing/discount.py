"""Discounts for prices held in integer cents."""


def apply_discount(price_cents: int, percent: int) -> int:
    """Return the price after taking `percent`% off, in cents.

    The discount amount is rounded half up to the nearest cent (100.5 -> 101), then subtracted.
    `percent` must be between 0 and 100.
    """
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    discount = price_cents * percent / 100
    return price_cents - int(discount)
