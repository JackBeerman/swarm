"""
fees.py -- what an order actually costs on Polymarket US.

Measured from the account's activity ledger (2026-09-20/21): every fill
was charged more than shares x price.

    order                          shares x price   charged   fee
    Eagles-Titans over 24.5        2 x 0.89 = 1.78     1.79    0.01
    Rams win 2H by 3.5+            3 x 0.43 = 1.29     1.34    0.05
    Rams win Q3 by 1.5+            3 x 0.41 = 1.23     1.28    0.05
    Rams win Q4 by 1.5+            3 x 0.49 = 1.47     1.52    0.05

That fits a fee proportional to p(1-p) -- largest near 0.50, small at the
extremes -- at roughly 5-7% of p(1-p) per share, rounded up to the cent.
FEE_RATE = 0.07 with round-up never underestimates any observed fill
(tests/test_fees.py pins that). Four orders is a small sample; when the
exchange publishes its schedule or more fills accrue, refit here. Every
consumer (sizing, the arbitrage scanner, paper portfolios) reads this
module, so there is one number to change.
"""

from __future__ import annotations

import math

FEE_RATE = 0.07


def fee_usd(price: float, shares: float, rate: float = FEE_RATE) -> float:
    """Estimated fee for one fill, rounded up to the cent."""
    if shares <= 0 or not (0.0 < price < 1.0):
        return 0.0
    raw = rate * shares * price * (1.0 - price)
    return math.ceil(raw * 100 - 1e-9) / 100.0


def fee_per_share(price: float, rate: float = FEE_RATE) -> float:
    """Marginal fee per share before rounding: what an edge must clear."""
    if not (0.0 < price < 1.0):
        return 0.0
    return rate * price * (1.0 - price)
