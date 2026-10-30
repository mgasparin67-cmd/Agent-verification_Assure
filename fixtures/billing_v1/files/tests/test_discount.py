from billing.discount import apply_discount


def test_discount():
    assert apply_discount(1999, 15) == 1699  # 299.85 off rounds to 300
