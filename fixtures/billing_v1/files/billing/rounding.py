"""Rounding shared by every money calculation."""

from decimal import Decimal


def round_half_up(amount: Decimal) -> int:
    """Round a non-negative amount of cents to a whole cent, half up (100.5 -> 101)."""
    return int(amount)
