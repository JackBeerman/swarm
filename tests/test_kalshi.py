"""
kalshi.py is read-only by construction. These tests pin the wire parsing
against responses captured live on 2026-09-23 (tests/fixtures/xvenue/)
and pin that the module can never grow an order path unnoticed.
"""

import json
import re
from pathlib import Path

import httpx
import pytest
import respx

import kalshi

FIX = Path(__file__).parent / "fixtures" / "xvenue"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_module_has_no_write_path():
    src = (Path(kalshi.__file__)).read_text(encoding="utf-8")
    for pat in (r"\.post\(", r"\.put\(", r"\.delete\(", r"\.patch\(", r"/portfolio",
                r"/orders", r"KALSHI-ACCESS", r"private_key", r"api_key"):
        assert not re.search(pat, src, re.I), f"kalshi.py must stay read-only: {pat}"


def test_fee_matches_the_published_table():
    # Kalshi Fee Schedule (2022-09-22), General Trading Fees Table:
    # 100 contracts at $0.50 -> $1.75; at $0.10 -> $0.63; 1 contract at $0.01 -> $0.01.
    assert kalshi.taker_fee(0.50, 100) == 1.75
    assert kalshi.taker_fee(0.10, 100) == 0.63
    assert kalshi.taker_fee(0.01, 1) == 0.01
    assert kalshi.taker_fee(0.60, 100) == 1.68
    assert kalshi.taker_fee(0.5, 100, multiplier=0.5) == 0.88   # 0.875 rounded up
    assert kalshi.taker_fee(0.0, 10) == 0.0


def test_listed_quote_parses_dollar_strings_and_empty_sides():
    ev = load("kalshi_events_KXMLBGAME.json")["events"][0]
    m = [x for x in ev["markets"] if x["ticker"].endswith("-CWS")][0]
    q = kalshi.market_quote(m)
    assert (q["yes_bid"], q["yes_ask"]) == (0.51, 0.52)
    assert q["yes_ask_size"] == pytest.approx(19356.12)
    assert kalshi.market_quote({"yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0100"}) == {
        "yes_bid": None, "yes_ask": 0.01, "yes_bid_size": None, "yes_ask_size": None}


def test_orderbook_bids_become_ask_ladders():
    lad = kalshi.ladders(load("kalshi_orderbook.json"))
    ob = load("kalshi_orderbook.json")["orderbook_fp"]
    best_no_bid = max(float(p) for p, _ in ob["no_dollars"])
    assert lad["yes_asks"][0][0] == pytest.approx(1 - best_no_bid)
    assert [p for p, _ in lad["yes_asks"]] == sorted(p for p, _ in lad["yes_asks"])
    best_yes_bid = max(float(p) for p, _ in ob["yes_dollars"])
    assert lad["no_asks"][0][0] == pytest.approx(1 - best_yes_bid)


def test_series_filter_blocks_politics_and_policy():
    assert kalshi.series_blocked(load("kalshi_series_KXHIGHNY.json")["series"]) is None
    assert kalshi.series_blocked(load("kalshi_series_KXMLBGAME.json")["series"]) is None
    assert kalshi.series_blocked({"category": "Politics", "title": "x"})
    assert kalshi.series_blocked({"category": "Economics", "title": "Fed decision"})
    assert kalshi.series_blocked({"category": "Sports", "tags": ["us-pol"], "title": "x"})
    assert kalshi.series_blocked({"category": "Entertainment", "title": "Next president"})


@respx.mock
def test_client_is_paced_get_only_and_honours_the_deadline():
    route = respx.get(f"{kalshi.BASE_URL}/series/KXHIGHNY").mock(
        return_value=httpx.Response(200, json=load("kalshi_series_KXHIGHNY.json")))
    with kalshi.Kalshi(min_interval=0.0) as kc:
        assert kc.series("KXHIGHNY")["ticker"] == "KXHIGHNY"
    assert route.call_count == 1
    with kalshi.Kalshi(min_interval=0.0, deadline=0.0) as kc:
        with pytest.raises(kalshi.KalshiError, match="budget"):
            kc.series("KXHIGHNY")


@respx.mock
def test_events_follow_the_cursor():
    respx.get(f"{kalshi.BASE_URL}/events").mock(side_effect=[
        httpx.Response(200, json={"events": [{"event_ticker": "A"}], "cursor": "c1"}),
        httpx.Response(200, json={"events": [{"event_ticker": "B"}], "cursor": ""}),
    ])
    with kalshi.Kalshi(min_interval=0.0) as kc:
        assert [e["event_ticker"] for e in kc.events("KXHIGHNY")] == ["A", "B"]
