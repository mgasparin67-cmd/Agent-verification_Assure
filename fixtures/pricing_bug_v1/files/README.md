# pricing

Price arithmetic for the billing service. All amounts are integer cents.

- `pricing.discount.apply_discount`: percentage discounts, rounded half up.
- `pricing.tax.add_tax`: sales tax in basis points, truncated as the tax tables require.
- `pricing.money.format_cents`: display formatting, always two decimal places.

Run the tests with `pytest`.
