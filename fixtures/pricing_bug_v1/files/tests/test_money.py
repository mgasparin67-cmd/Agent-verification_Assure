from pricing.money import format_cents


def test_format():
    assert format_cents(1234567) == "$12,345.67"
