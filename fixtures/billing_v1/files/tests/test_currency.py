from billing.currency import convert


def test_convert():
    assert convert(999, "1.5") == 1499  # 1498.5 rounds up
