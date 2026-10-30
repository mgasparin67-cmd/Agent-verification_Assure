from pricing.money import format_cents


def test_format():
    assert format_cents(1234567) == "$12,345.67"


def test_format_trims_trailing_zero():
    assert format_cents(123450) == "$1,234.5"
