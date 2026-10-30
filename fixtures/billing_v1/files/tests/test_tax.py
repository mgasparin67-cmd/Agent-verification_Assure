from billing.tax import add_tax


def test_tax():
    assert add_tax(10000, 825) == 10825
