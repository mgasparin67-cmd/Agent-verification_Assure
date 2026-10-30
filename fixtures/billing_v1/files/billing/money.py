"""Parsing and display of dollar amounts in integer cents."""


def format_cents(cents: int) -> str:
    """Format cents as dollars with two decimal places and comma separators: -250 -> "-$2.50"."""
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rest:02d}"


def parse_amount(text: str) -> int:
    """Parse a dollar amount such as "$12.50" into cents. Raises ValueError if it is malformed."""
    if not text.startswith("$"):
        raise ValueError(f"not a dollar amount: {text!r}")
    dollars, _, rest = text[1:].partition(".")
    if len(rest) != 2 or not dollars.isdigit() or not rest.isdigit():
        raise ValueError(f"not a dollar amount: {text!r}")
    return int(dollars) * 100 + int(rest)
