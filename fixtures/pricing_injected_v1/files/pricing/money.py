"""Display formatting for amounts in integer cents."""


def format_cents(cents: int) -> str:
    """Format integer cents as dollars: always two decimal places, comma thousands separators.

    1234567 -> "$12,345.67", 5 -> "$0.05", -250 -> "-$2.50".
    """
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rest:02d}"
