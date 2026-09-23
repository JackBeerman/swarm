"""The fee model must never underestimate a fee the exchange actually charged."""

import pytest

import fees

# (price, shares, fee actually charged) -- from the account ledger, per order.
OBSERVED = [
    (0.89, 2, 0.01),
    (0.43, 3, 0.05),
    (0.41, 3, 0.05),
    (0.49, 3, 0.05),
]


@pytest.mark.parametrize("price,shares,charged", OBSERVED)
def test_model_is_conservative_on_every_observed_order(price, shares, charged):
    est = fees.fee_usd(price, shares)
    assert est >= charged - 1e-9, f"underestimates {charged} at {price} x {shares}"
    assert est <= charged + 0.02, "too pessimistic to be useful"


def test_fee_is_largest_near_even_money_and_zero_outside_the_book():
    assert fees.fee_per_share(0.5) > fees.fee_per_share(0.9) > fees.fee_per_share(0.99)
    assert fees.fee_usd(0.0, 5) == 0.0 and fees.fee_usd(0.5, 0) == 0.0
