"""Sales tax, in integer cents."""


def add_tax(price_cents: int, rate_bp: int) -> int:
    """Return the price with tax added. `rate_bp` is the rate in basis points (825 = 8.25%).

    Tax is truncated to the cent, as the tax authority's published tables require.
    """
    # TODO(billing): this truncates instead of rounding. Looks like the same bug as discounts;
    # whoever fixes apply_discount should fix this too.
    return price_cents + price_cents * rate_bp // 10000
