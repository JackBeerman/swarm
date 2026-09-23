"""
Completeness is the whole risk in a complete-set trade: these tests pin
that only a provably exhaustive ladder is admitted.
"""

import pytest

import arb
from fees import fee_usd

NYC = ["tc-temp-nychigh-2026-09-23-lt65f", "tc-temp-nychigh-2026-09-23-gte65lt66f",
       "tc-temp-nychigh-2026-09-23-gte67lt68f", "tc-temp-nychigh-2026-09-23-gte69lt70f",
       "tc-temp-nychigh-2026-09-23-gte71lt72f", "tc-temp-nychigh-2026-09-23-gte73f"]


def test_band_parsing_matches_the_displayed_outcomes():
    assert arb.band_of("tc-temp-nychigh-2026-09-21-gte66lt67f") == (66.0, 67.0)
    assert arb.band_of("tc-temp-nychigh-2026-09-21-lt66f") == (None, 65.0)
    assert arb.band_of("tc-temp-nychigh-2026-09-21-gte74f") == (74.0, None)
    assert arb.band_of("aec-mlb-sd-lad-2026-09-22") is None


def test_a_chained_ladder_with_open_ends_is_complete():
    assert arb.complete_ladder(NYC)


@pytest.mark.parametrize("slugs", [
    NYC[1:],                         # bottom band missing: below 65 wins nothing
    NYC[:-1],                        # top band missing
    NYC[:2] + NYC[3:],               # a gap in the middle
    NYC + ["tc-temp-nychigh-2026-09-23-gte66lt67f"],   # overlap
    ["aec-mlb-sd-lad-2026-09-22", "atc-mlb-sd-lad-2026-09-22-i4-draw"],
    NYC[:1],
])
def test_anything_not_provably_exhaustive_is_refused(slugs):
    assert not arb.complete_ladder(slugs)


def _legs(prices):
    return [{"slug": f"l{i}", "bid": b, "ask": a, "bid_shares": 100, "ask_shares": s}
            for i, (b, a, s) in enumerate(prices)]


def test_yes_set_profit_is_one_minus_asks_minus_fees():
    res = arb.price_set(_legs([(0.08, 0.09, 50), (0.28, 0.29, 20), (0.58, 0.59, 80)]))
    assert res["sum_ask"] == pytest.approx(0.97)
    fees = sum(fee_usd(a, 1) for a in (0.09, 0.29, 0.59))
    assert res["yes_profit"] == pytest.approx(1 - 0.97 - fees)
    assert res["yes_sets"] == 20, "the thinnest leg bounds the size"


def test_no_set_profit_when_bids_sum_above_one():
    res = arb.price_set(_legs([(0.20, 0.22, 10), (0.45, 0.47, 10), (0.40, 0.42, 10)]))
    assert res["sum_bid"] == pytest.approx(1.05)
    fees = sum(fee_usd(1 - b, 1) for b in (0.20, 0.45, 0.40))
    assert res["no_profit"] == pytest.approx(1.05 - 1 - fees)


def test_a_leg_without_an_ask_makes_the_yes_set_unbuyable():
    res = arb.price_set(_legs([(0.10, None, 0), (0.50, 0.60, 10)]))
    assert res["yes_profit"] is None and res["no_profit"] is not None
