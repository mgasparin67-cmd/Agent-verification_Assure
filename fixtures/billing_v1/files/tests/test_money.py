from billing.money import format_cents, parse_amount


def test_format():
    assert format_cents(123450) == "$1,234.50"
    assert format_cents(-250) == "-$2.50"


def test_parse_simple():
    assert parse_amount("$12.50") == 1250


def test_parse_thousands():
    assert parse_amount("$1,234.50") == 123450
