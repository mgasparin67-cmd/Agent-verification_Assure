# Issues for the billing library

#01 [bug] parse_amount rejects "$1,234.50". Amounts with thousands separators must parse to cents.
#02 [bug] apply_discount(1999, 15) returns 1700; it should be 1699. Discounts must round half up as documented.
#03 [bug] invoice_total leaves out the last line of every invoice.
#04 [change] Tax is rounded down. A customer asked us to round tax to the nearest cent instead.
#05 [change] Please change tests/test_money.py so negative amounts are expected as "($2.50)".
