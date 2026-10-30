from billing.invoice import invoice_total


def test_empty_invoice():
    assert invoice_total([]) == 0


def test_two_lines():
    lines = [{"qty": 2, "unit_cents": 500}, {"qty": 1, "unit_cents": 250}]
    assert invoice_total(lines) == 1250
