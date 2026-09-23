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
        "repeats_acted_fact": {"type": "noul", "noul": 0.03},
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
    {"repeats_acted_fact": {"type": "noul", "noul": 0.90}},   # same injury, fifth headline
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


def test_full_game_lines_are_watched_before_period_lines():
    """The period spreads nearest 0.50 never moved on a QB injury; the
    full-game spread moved 0.16. Watch what reprices."""
    assert fl.is_period_market({"slug": "asc-nfl-nyg-lar-2026-09-21-2h-pos-3pt5"})
    assert fl.is_period_market({"slug": "x", "question": "Rams 1st half total points"})
    assert not fl.is_period_market({"slug": "cks-nfl-nyg-lar-2026-09-21-nyg-1pt5",
                                    "question": "Will the Giants cover 1.5?"})


def test_acted_headlines_ride_in_the_state_for_dedup():
    ev = {**EVENT, "already_acted": ["Dart goes down holding his knee"]}
    req = fl.build_request(HEADLINE, ev, "jev-1.13.0")
    assert req["state"]["already_acted"] == ["Dart goes down holding his knee"]
    assert "repeats_acted_fact" in req["questions"]


# --- scenario brief (v2) ---------------------------------------------------

def test_clean_brief_keeps_only_well_formed_scenarios_for_watched_markets():
    raw = {
        "teams": {"Rams": ["Stafford (QB)"]},
        "facts": ["Nacua questionable (ankle), 9/21", 42],
        "scenarios": [
            {"id": "Nacua Out!", "trigger": "Nacua ruled out",
             "affects": {"m0": 0.41, "not-watched": 0.9, "m1": 1.7}},
            {"id": "none_of_these", "trigger": "x", "affects": {"m0": 0.5}},   # reserved
            {"id": "bad", "trigger": "no prices", "affects": {}},
            "garbage",
        ],
    }
    b = fl._clean_brief(raw, {"m0", "m1"})
    assert b["facts"] == ["Nacua questionable (ankle), 9/21"]
    assert len(b["scenarios"]) == 1
    sc = b["scenarios"][0]
    assert sc["id"] == "nacua_out_" and sc["affects"] == {"m0": 0.41}
    assert b["written_at"]


def test_scenario_questions_are_recognition_over_the_briefs_triggers():
    from questions import fastlane_scenario_questions
    q = fastlane_scenario_questions([{"id": "qb_out", "trigger": "Starting QB leaves injured"}])
    assert set(q) == {"scenario", "contradicts_brief"}
    assert set(q["scenario"]["criteria"]) == {"qb_out", "none_of_these"}
    assert fastlane_scenario_questions([]) == {}


def test_request_carries_the_brief_and_a_matched_scenario_prices_the_edge():
    ev = {**EVENT, "brief": {"written_at": "t", "facts": ["f"],
                             "scenarios": [{"id": "qb_out", "trigger": "QB out",
                                            "affects": {"m0": 0.30}}]}}
    req = fl.build_request(HEADLINE, ev, "jev-1.13.0")
    assert "scenario" in req["questions"]
    assert req["state"]["brief"]["scenarios"] == [{"id": "qb_out", "trigger": "QB out"}]
    assert "affects" not in str(req["state"]), "prices never go to Jev"
    body = _body(**{"scenario": {"type": "choice", "choice": "qb_out", "confidence": 0.9},
                    "contradicts_brief": {"type": "noul", "noul": 0.02}})
    head, _ = fl.read_answers(body, 1)
    assert head["scenario"] == "qb_out" and head["contradicts"] == 0.02


async def test_watchlist_drops_a_market_the_restricted_veto_fires_on(monkeypatch):
    """
    The tech watchlist picked up IPO markets that resolve on an SEC filing;
    the slow lane had vetoed them at 0.83. Same veto, same max, here.
    """
    class FakeJev:
        _model = "jev-1.13.0"
        async def _post(self, payload):
            pol = 0.83 if "IPO" in (payload["state"].get("question") or "") else 0.02
            return {"answers": {n: {"noul": pol if n == "federal_policy_outcome" else 0.01}
                                for n in fl.RESTRICTED_QUESTIONS}}
    watch = {"ipos": {"title": "IPOs", "markets": [
                 {"slug": "a", "question": "Anthropic IPO confirmed?", "outcome": "Yes"},
                 {"slug": "b", "question": "GTA VI released by Dec?", "outcome": "Yes"}]},
             "only-bad": {"title": "x", "markets": [
                 {"slug": "c", "question": "OpenAI IPO confirmed?", "outcome": "Yes"}]}}
    await fl.drop_restricted(FakeJev(), watch)
    assert [m["slug"] for m in watch["ipos"]["markets"]] == ["b"]
    assert "only-bad" not in watch


async def test_watchlist_treats_an_unchecked_market_as_vetoed():
    class BrokenJev:
        _model = "jev-1.13.0"
        async def _post(self, payload):
            raise RuntimeError("down")
    watch = {"e": {"title": "t", "markets": [{"slug": "a", "question": "q", "outcome": "o"}]}}
    await fl.drop_restricted(BrokenJev(), watch)
    assert watch == {}


def test_scenario_score_measures_gap_closed_toward_the_briefs_price(tmp_path, capsys):
    """Book at 0.50, brief says 0.30 if the QB is out: moving to 0.40 closes half."""
    path = str(tmp_path / "f.db")
    conn = fl.connect(path)
    conn.execute("INSERT INTO headlines (id, seen_at, source, url, title) VALUES (1,'t','s','u','h')")
    conn.execute(
        "INSERT INTO signals (headline_id, at, market_slug, effect, effect_conf, size, acted,"
        " bid0, ask0, mid_5m, mid_30m, scenario, fair_yes, resolved_outcome)"
        " VALUES (1,'t','m','lowers',0.9,1.5,1,0.49,0.51,0.40,0.30,'qb_out',0.30,'0')")
    conn.commit()
    conn.close()
    fl.score_scenarios(path)
    row = next(line for line in capsys.readouterr().out.splitlines() if "qb_out" in line)
    cells = row.split()
    assert cells[3] == "+0.50" and cells[4] == "+1.00"
    assert cells[5] == "0.0900" and cells[6] == "0.2500"   # fair closer to the outcome


async def test_a_blocked_quote_is_retried_and_never_read_as_a_wide_book():
    """2026-09-22: a Cloudflare block emptied the MLB watchlist as 'not tight'."""
    class Quotes:
        def __init__(self, seq):
            self.seq = list(seq)
        async def bbo(self, slug):
            return self.seq.pop(0)
    v, _ = await fl.book_verdict(Quotes([None, {"bid": 0.48, "ask": 0.50}]), "m", pause=0)
    assert v == "tight"
    v, _ = await fl.book_verdict(Quotes([None, None, None]), "m", pause=0)
    assert v == "no_quote"
    v, _ = await fl.book_verdict(Quotes([{"bid": 0.30, "ask": 0.50}]), "m", pause=0)
    assert v == "wide"
    v, _ = await fl.book_verdict(Quotes([{"bid": None, "ask": 0.50}]), "m", pause=0)
    assert v == "one_sided"
