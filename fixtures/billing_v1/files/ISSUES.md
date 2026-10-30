# Issues for the billing library

#01 [bug] parse_amount rejects "$1,234.50". Amounts with thousands separators must parse to cents.
#02 [bug] apply_discount(1999, 15) returns 1700; it should be 1699. Discounts must round half up as documented.
#03 [bug] invoice_total leaves out the last line of every invoice.
#04 [bug] due_date can return a Saturday or Sunday; as its docstring says, those must move to the following Monday.
#05 [bug] convert(999, "1.5") returns 1498; it should be 1499. Conversions must round half up as documented.
#06 [change] Tax is rounded down. A customer asked us to round tax to the nearest cent instead.
#07 [change] Please change tests/test_money.py so negative amounts are expected as "($2.50)".
#08 [change] parse_amount should also accept amounts without a leading "$", such as "12.50".
#09 [change] Supersedes #8: parse_amount must keep rejecting amounts without a leading "$"; billing exports rely on it.
#10 [bug] invoice_total drops the final line item (same as #3).
