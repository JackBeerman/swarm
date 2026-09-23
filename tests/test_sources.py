"""
Tests for sources.py and tools/measure_sources.py.

Fixtures in tests/fixtures/sources/ are REAL responses captured 2026-09-23
(trimmed to a few items; public feeds, nothing stripped):
  mlb_live_824061.json  statsapi live feed, White Sox at Royals, 7th inning,
                        fetched with MLB_LIVE_FIELDS
  mlb_schedule.json     statsapi schedule, hydrate=team,probablePitcher
  mlb_transactions.json, gnews_yankees.xml, nws_obs_knyc.json,
  nws_alerts_fl.json, bsky_rotowiremlb.json, status_claude_history.rss,
  github_releases_openai_python.json
No network: httpx is mocked with respx.
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

import sources as src

FIX = Path(__file__).parent / "fixtures" / "sources"


def load(name: str):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


def _measure_module():
    spec = importlib.util.spec_from_file_location(
        "measure_sources", Path(__file__).parent.parent / "tools" / "measure_sources.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# MLB live feed: data, not headlines
# --------------------------------------------------------------------------

def test_live_feed_yields_starter_removed_per_team_with_names():
    items = src.parse_live_feed(load("mlb_live_824061.json"))
    starters = [i for i in items if i["kind"] == "starter_removed"]
    assert [i["title"] for i in starters] == [
        "Kansas City Royals starting pitcher removed. "
        "Pitching Change: Tony Gonsolin replaces Daniel Lynch IV.",
        "Chicago White Sox starting pitcher removed. "
        "Pitching Change: Noah Schultz replaces Anthony Kay.",
    ]
    # The second change for the same team is an ordinary pitching change.
    later = [i for i in items if i["kind"] == "pitching_change"]
    assert len(later) == 1 and "Nolan Hoffman replaces Tony Gonsolin" in later[0]["title"]
    assert later[0]["title"].startswith("Kansas City Royals:")


def test_live_feed_times_are_the_field_clock_and_items_are_complete():
    items = src.parse_live_feed(load("mlb_live_824061.json"))
    for it in items:
        assert set(it) == set(src.ITEM_KEYS)
        if it["kind"] != "final":
            assert datetime.fromisoformat(it["published_at"]).tzinfo is not None
    starter = next(i for i in items if i["kind"] == "starter_removed")
    assert starter["published_at"] == "2026-09-23T00:49:16.474000+00:00"
    assert len({i["url"] for i in items}) == len(items)


def test_live_feed_advisories_name_the_game_and_scores_carry_the_score():
    items = src.parse_live_feed(load("mlb_live_824061.json"))
    adv = [i for i in items if i["kind"] == "advisory"]
    assert adv[0]["title"] == "Chicago White Sox at Kansas City Royals, top 1: Injury Delay."
    scores = [i for i in items if i["kind"] == "score"]
    assert len(scores) == 11
    assert "Chicago White Sox 2 - Kansas City Royals 0 (top 1)" in scores[0]["title"]
    # Status changes (pre-game, warmup) are noise, not items.
    assert not any("Warmup" in i["title"] for i in items)


def test_live_feed_is_idempotent_and_emits_final():
    feed = load("mlb_live_824061.json")
    assert src.parse_live_feed(feed) == src.parse_live_feed(copy.deepcopy(feed))
    assert not any(i["kind"] == "final" for i in src.parse_live_feed(feed))
    feed["gameData"]["status"]["abstractGameState"] = "Final"
    final = [i for i in src.parse_live_feed(feed) if i["kind"] == "final"]
    assert len(final) == 1 and final[0]["title"].startswith("Final: Chicago White Sox ")


def test_schedule_flags_postponements_and_probable_pitcher_changes():
    body = load("mlb_schedule.json")
    games = src.parse_schedule(body)
    assert games[0]["away"]["short"] == "Rays" and games[0]["home"]["probable"]
    prev: dict = {}
    assert src.schedule_items(games, prev) == []          # first look records, never alerts
    body2 = copy.deepcopy(body)
    g = body2["dates"][0]["games"][1]
    g["teams"]["home"]["probablePitcher"]["fullName"] = "Luis Gil"
    g["status"]["detailedState"] = "Postponed"
    items = src.schedule_items(src.parse_schedule(body2), prev)
    kinds = {i["kind"]: i for i in items}
    assert kinds["probable_change"]["title"] == (
        "New York Yankees change starting pitcher: Luis Gil replaces Max Fried")
    assert kinds["game_status"]["title"].endswith(": Postponed")
    assert all(i["published_at"] is None for i in items)


def test_transactions_are_items_without_a_time():
    items = src.parse_transactions(load("mlb_transactions.json"))
    assert items[0]["title"].startswith("Athletics transferred RHP J.T. Ginn")
    assert items[0]["url"] == "mlb://transaction/943685" and items[0]["published_at"] is None


def _live_schedule() -> dict:
    body = copy.deepcopy(load("mlb_schedule.json"))
    g = body["dates"][0]["games"][0]
    g["gamePk"] = 824061
    g["status"]["abstractGameState"] = "Live"
    return body


@respx.mock
async def test_mlb_live_source_follows_live_games_only_and_filters_by_title():
    sched = respx.get(f"{src.MLB_API}/v1/schedule").mock(
        return_value=httpx.Response(200, json=_live_schedule()))
    feed = respx.get(f"{src.MLB_API}/v1.1/game/824061/feed/live").mock(
        return_value=httpx.Response(200, json=load("mlb_live_824061.json")))
    async with httpx.AsyncClient() as c:
        s = src.MLBLiveSource(min_interval=0)
        items = await s.poll(c)
        assert any(i["kind"] == "starter_removed" for i in items)
        assert all(i["fetched_at"] for i in items)
        assert feed.calls.last.request.url.params["fields"] == src.MLB_LIVE_FIELDS
        assert "swarm-research" in feed.calls.last.request.headers["User-Agent"]
        # The schedule is not refetched on every poll.
        await s.poll(c)
        assert sched.call_count == 1 and feed.call_count == 2
        # Watching only an NFL event: the game is not followed at all.
        other = src.MLBLiveSource(min_interval=0, match_titles=["NY Giants vs LA Rams"])
        assert await other.poll(c) == [] and feed.call_count == 2
        mine = src.MLBLiveSource(min_interval=0, match_titles=["Rays vs Yankees"])
        assert await mine.poll(c) and feed.call_count == 3


# --------------------------------------------------------------------------
# text and social
# --------------------------------------------------------------------------

def test_google_news_url_and_event_query():
    assert src.event_query("NY Giants vs LA Rams") == "NY Giants LA Rams"
    url = src.google_news_url("Rays Yankees")
    assert url.startswith("https://news.google.com/rss/search?q=Rays+Yankees+when%3A1h&")


@respx.mock
async def test_google_news_source_parses_real_feed():
    respx.get(url__startswith="https://news.google.com/rss/search").mock(
        return_value=httpx.Response(200, text=load("gnews_yankees.xml")))
    async with httpx.AsyncClient() as c:
        s = src.GoogleNewsSource("Yankees", label="Rays at Yankees")
        items = await s.poll(c)
    assert len(items) == 5 and s.name == "gnews:Rays at Yankees"
    assert items[0]["title"].endswith(" - MLB.com")
    assert items[0]["published_at"] == "2026-09-23T05:31:21+00:00"
    assert {i["kind"] for i in items} == {"news"}


def test_bluesky_feed_skips_reposts_and_other_authors():
    body = load("bsky_rotowiremlb.json")
    items = src.parse_bsky_feed(body, "rotowiremlb.bsky.social")
    assert len(items) == 5
    assert items[0]["title"] == "Mike Trout: Gets aboard four times"
    assert items[0]["url"].startswith("https://bsky.app/profile/rotowiremlb.bsky.social/post/")
    body2 = copy.deepcopy(body)
    body2["feed"][0]["reason"] = {"$type": "app.bsky.feed.defs#reasonRepost"}
    body2["feed"][1]["post"]["author"]["handle"] = "someone.else"
    assert len(src.parse_bsky_feed(body2, "rotowiremlb.bsky.social")) == 3


@respx.mock
async def test_bluesky_one_bad_handle_does_not_hide_the_rest():
    def reply(request):
        if request.url.params["actor"] == "gone.bsky.social":
            return httpx.Response(400, json={"error": "InvalidRequest"})
        return httpx.Response(200, json=load("bsky_rotowiremlb.json"))
    respx.get(src.BSKY_API).mock(side_effect=reply)
    async with httpx.AsyncClient() as c:
        s = src.BlueskyAuthorSource(["gone.bsky.social", "rotowiremlb.bsky.social"])
        assert len(await s.poll(c)) == 5 and s.last_ok


# --------------------------------------------------------------------------
# weather and tech
# --------------------------------------------------------------------------

def test_nws_observation_in_fahrenheit_with_its_own_timestamp():
    body = load("nws_obs_knyc.json")
    [it] = src.parse_nws_observation(body, "KNYC")
    assert it["title"] == "KNYC observed 55F (12.8C), Clear"
    assert it["published_at"] == "2026-09-23T08:51:00+00:00"
    body["properties"]["temperature"]["value"] = None
    assert src.parse_nws_observation(body, "KNYC") == []


def test_nws_alerts_and_github_releases():
    [a, _] = src.parse_nws_alerts(load("nws_alerts_fl.json"))
    assert a["title"].startswith("Flood Advisory") and a["published_at"] == "2026-09-23T07:31:00+00:00"
    rel = src.parse_github_releases(load("github_releases_openai_python.json"), "openai/openai-python")
    assert len(rel) == 3 and rel[0]["title"].startswith("openai/openai-python released ")
    assert rel[0]["url"].startswith("https://github.com/openai/openai-python/releases/")
    assert datetime.fromisoformat(rel[0]["published_at"]).tzinfo is not None


@respx.mock
async def test_status_history_rss_through_rss_source():
    respx.get(src.STATUS_FEEDS["anthropic"]).mock(
        return_value=httpx.Response(200, text=load("status_claude_history.rss")))
    async with httpx.AsyncClient() as c:
        items = await src.RSSSource("status:anthropic", src.STATUS_FEEDS["anthropic"],
                                    kind="status").poll(c)
    assert items[0]["title"] == "Elevated errors for multiple models"
    assert items[0]["kind"] == "status" and items[0]["published_at"]


@respx.mock
async def test_nws_source_sends_the_identifying_user_agent():
    route = respx.get(f"{src.NWS_API}/stations/KNYC/observations/latest").mock(
        return_value=httpx.Response(200, json=load("nws_obs_knyc.json")))
    async with httpx.AsyncClient() as c:
        assert len(await src.NWSObservationSource(["KNYC"]).poll(c)) == 1
    assert route.calls.last.request.headers["User-Agent"] == src.NWS_USER_AGENT


# --------------------------------------------------------------------------
# politeness and failure
# --------------------------------------------------------------------------

@respx.mock
async def test_poll_is_throttled_to_min_interval():
    route = respx.get(src.BSKY_API).mock(
        return_value=httpx.Response(200, json=load("bsky_rotowiremlb.json")))
    async with httpx.AsyncClient() as c:
        s = src.BlueskyAuthorSource(["rotowiremlb.bsky.social"], min_interval=60)
        assert len(await s.poll(c)) == 5
        assert await s.poll(c) == []             # too soon: no request at all
    assert route.call_count == 1 and s.polls == 1


@respx.mock
async def test_a_failing_source_returns_nothing_and_counts_the_error():
    respx.get(url__startswith="https://news.google.com/").mock(
        return_value=httpx.Response(503, text="unavailable"))
    async with httpx.AsyncClient() as c:
        s = src.GoogleNewsSource("x", min_interval=0)
        assert await s.poll(c) == []
    assert s.errors == 1 and not s.last_ok and s.ok_polls == 0


def test_sources_for_by_tag():
    names = [s.name for s in src.sources_for(("mlb",), ["Rays vs Yankees"])]
    # Google News is opt-in: its robots.txt disallows /rss/.
    assert names == ["mlb_live", "bluesky"]
    with_g = src.sources_for(("mlb",), ["Rays vs Yankees", "Rays vs Yankees"], google_news=True)
    assert [s.name for s in with_g] == ["gnews:Rays vs Yankees", "mlb_live", "bluesky"]
    mlb = next(s for s in src.sources_for(("mlb",), ["Rays vs Yankees"]) if s.name == "mlb_live")
    assert mlb.titles == ["rays vs yankees"]
    assert [s.name for s in src.sources_for(("weather",))] == ["nws_obs"]
    assert src.sources_for(("crypto",)) == []
    assert [s.name for s in src.sources_for(("tech",))] == ["status:openai", "status:anthropic"]


def test_fastlane_default_is_unchanged():
    import fastlane
    sig = inspect.signature(fastlane.run)
    assert sig.parameters["fast_sources"].default is False


# --------------------------------------------------------------------------
# the lag harness
# --------------------------------------------------------------------------

def test_harness_excludes_baseline_from_lag_and_computes_percentiles():
    ms = _measure_module()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ms._SCHEMA)
    t0 = datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc)
    conn.execute("INSERT INTO runs (id, started_at, ended_at, minutes) VALUES (1,?,?,60)",
                 (t0.isoformat(), (t0 + timedelta(hours=1)).isoformat()))

    def item(n: int, age_s: float) -> dict:
        return src.make_item("f", f"u{n}", f"t{n}", published_at=t0 - timedelta(seconds=age_s))

    # Baseline: an item an hour old, present on the first poll.
    assert ms.record_items(conn, 1, "f", [item(0, 3600)], baseline=True, seen_at=t0) == 1
    # Four new items seen 30, 60, 90 and 600 s after publication.
    for n, lag in enumerate((30, 60, 90, 600), start=1):
        ms.record_items(conn, 1, "f", [item(n, 0)], baseline=False,
                        seen_at=t0 + timedelta(seconds=lag))
    # A repeat of a known item is not new.
    assert ms.record_items(conn, 1, "f", [item(1, 0)], baseline=False, seen_at=t0) == 0
    conn.execute("INSERT INTO polls (run_id, at, feed, ok) VALUES (1, ?, 'f', 1)", (t0.isoformat(),))
    [row] = ms.summarize(conn, 1)
    assert row["new"] == 4 and row["n_lag"] == 4
    assert row["median"] == pytest.approx(75) and row["fastest"] == pytest.approx(30)
    assert row["p90"] == pytest.approx(600)
    assert row["per_hour"] == pytest.approx(4.0)
    assert row["under_2m"] == pytest.approx(0.75)
    assert row["newest_at_start"] == pytest.approx(3600)
