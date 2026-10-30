from pricing.discount import apply_discount


def test_discount_whole_cents():
    assert apply_discount(1000, 10) == 900


def test_discount_rounding():
    assert apply_discount(1999, 15) == 1699  # 299.85 off rounds to 300
