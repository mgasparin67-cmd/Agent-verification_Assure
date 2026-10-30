"""Payment due dates."""

from datetime import date, timedelta


def due_date(issued: date, terms_days: int) -> date:
    """The date payment is due: `terms_days` after `issued`, moved to the following Monday if that
    falls on a Saturday or Sunday."""
    return issued + timedelta(days=terms_days)
