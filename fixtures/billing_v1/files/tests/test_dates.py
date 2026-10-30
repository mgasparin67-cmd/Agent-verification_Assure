from datetime import date

from billing.dates import due_date


def test_due_date():
    assert due_date(date(2026, 10, 1), 29) == date(2026, 10, 30)  # a Friday: no weekend rule involved
