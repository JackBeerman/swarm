"""
Tests for the fast-lane recorder. No network: feeds, Jev and quotes are
all plain data here.
"""

from __future__ import annotations

import pytest

import fastlane as fl
from questions import FastLaneThresholds

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>x</title>
<item><title><![CDATA[Rams WR Puka Nacua ruled out &amp; more]]></title>
<link>https://example.com/a</link>
<description><![CDATA[<p>Nacua (ankle) is <b>out</b>.</p>]]></description>
<pubDate>Mon, 21 Sep 2026 22:40:00 GMT</pubDate></item>
<item><title>No link, skipped</title></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom item</title><link href="https://example.com/b"/>
<updated>2026-09-21T22:41:00Z</updated><summary>s</summary></entry></feed>"""


def test_parse_rss_strips_markup_and_reads_the_publish_time():
    items = fl.parse_feed(RSS, "src")
    assert len(items) == 1
    h = items[0]
    assert h["title"] == "Rams WR Puka Nacua ruled out & more"
    assert h["summary"] == "Nacua (ankle) is  out ."
    assert h["published_at"].startswith("2026-09-21T22:40:00")
    assert h["url"] == "https://example.com/a" and h["source"] == "src"


def test_parse_atom_and_garbage():
    assert fl.parse_feed(ATOM, "a")[0]["url"] == "https://example.com/b"
    assert fl.parse_feed("<html>blocked</html", "a") == []


EVENT = {"title": "NY Giants vs LA Rams",
         "teams": {"Los Angeles Rams": ["Puka Nacua (WR)"]},
         "markets": [{"slug": "m0", "question": "q0", "outcome": "o0"},
                     {"slug": "m1", "question": "q1", "outcome": "o1"}]}
HEADLINE = {"title": "Nacua ruled out", "summary": "", "source": "src"}


def test_one_request_carries_the_headline_checks_and_every_market():
    """The whole decision is a single Jev call: that is the speed claim."""
    req = fl.build_request(HEADLINE, EVENT, "jev-1.13.0")
    q = set(req["questions"])
    assert {"reports_new_fact", "concerns_event", "headline_political"} <= q
    assert {"effect_0", "size_0", "effect_1", "size_1"} <= q
    assert "`markets[1]`" in req["questions"]["effect_1"]["instructions"]["question"]
    # The brief is in the state, and the effect question points at it.
    assert req["state"]["teams"] == EVENT["teams"]
    assert "`teams`" in req["questions"]["effect_0"]["instructions"]["inspect"]
    # No prices: Jev judges the text, code compares it to the book.
    assert "slug" not in req["state"]["markets"][0]


def _body(**over):
    a = {
        "reports_new_fact": {"type": "noul", "noul": 0.96},
        "concerns_event": {"type": "noul", "noul": 0.98},
        "headline_political": {"type": "noul", "noul": 0.01},
        "effect_0": {"type": "choice", "choice": "lowers", "confidence": 0.9},
        "size_0": {"type": "score", "score": 1.4, "confidence": 0.7},
    }
    a.update(over)
    return {"answers": a, "model": "jev-1.13.0"}


def test_read_answers_and_would_act():
    head, per = fl.read_answers(_body(), 1)
    assert fl.would_act({**head, **per[0]}, FastLaneThresholds()) is True


@pytest.mark.parametrize("over", [
    {"headline_political": {"type": "noul", "noul": 0.40}},
    {"reports_new_fact": {"type": "noul", "noul": 0.05}},      # a preview, not news
    {"concerns_event": {"type": "noul", "noul": 0.02}},        # another team
    {"effect_0": {"type": "choice", "choice": "no_clear_effect", "confidence": 0.9}},
    {"effect_0": {"type": "choice", "choice": "lowers", "confidence": 0.2}},
    {"size_0": {"type": "score", "score": 0.3, "confidence": 0.8}},
])
def test_any_one_failed_check_keeps_the_fast_lane_quiet(over):
    head, per = fl.read_answers(_body(**over), 1)
    assert fl.would_act({**head, **per[0]}, FastLaneThresholds()) is False


@pytest.mark.parametrize("over", [
    {"headline_political": {"type": "noul", "noul": None}},
    {"headline_political": {"type": "noul"}},
    {"effect_0": {"type": "choice", "choice": "sideways", "confidence": 0.9}},
])
def test_malformed_answers_raise_rather_than_reading_as_zero(over):
    """0.0 on the political check would mean 'allowed'. Fail closed."""
    with pytest.raises((ValueError, KeyError, TypeError)):
        fl.read_answers(_body(**over), 1)


def test_score_averages_within_a_headline_first(tmp_path, capsys):
    """Six markets on one headline move together; the unit is the headline."""
    path = str(tmp_path / "f.db")
    conn = fl.connect(path)
    conn.execute("INSERT INTO headlines (id, seen_at, lag_seconds, source, url, title)"
                 " VALUES (1,'t',90,'s','u','h')")
    for slug, effect, later in (("a", "lowers", 0.46), ("b", "lowers", 0.48)):
        conn.execute(
            "INSERT INTO signals (headline_id, at, market_slug, effect, effect_conf, size,"
            " acted, jev_ms, bid0, ask0, mid_5m, reports_new_fact, concerns_event, political)"
            " VALUES (1,'t',?,?,0.9,1.5,1,180,0.49,0.51,?,0.9,0.9,0.01)", (slug, effect, later))
    conn.commit()
    conn.close()
    fl.score(path)
    out = capsys.readouterr().out
    assert "median 1.5 min" in out            # feed lag is reported, not assumed
    assert "median 180 ms" in out
    # lowers, and the mid fell 0.04 and 0.02 -> +0.03 signed, ONE headline.
    row = next(line for line in out.splitlines() if "would act" in line)
    assert "+0.0300" in row and row.split()[3] == "1"


def test_fastlane_cannot_place_orders():
    """Shadow only, by construction: nothing here can reach the order path."""
    import inspect
    src = inspect.getsource(fl)
    assert "orders.create" not in src and "import daemon" not in src


def test_a_retitled_live_updates_item_counts_as_a_new_headline(tmp_path):
    """Yahoo's live page keeps one URL and re-titles it as news breaks."""
    conn = fl.connect(str(tmp_path / "f.db"))
    rec = fl.Recorder(conn, jev=None, quotes=None, watch={}, th=FastLaneThresholds())
    base = {"summary": "", "source": "yahoo", "published_at": None, "url": "https://y/live"}
    assert rec._store_headline({**base, "title": "Live updates: pregame"}) is not None
    assert rec._store_headline({**base, "title": "Live updates: pregame"}) is None
    assert rec._store_headline({**base, "title": "Live updates: Nacua ruled out"}) is not None
    conn.close()
