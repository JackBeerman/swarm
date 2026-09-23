"""
Tests for skills.py: the files parse, selection is deterministic code,
restricted markets select nothing, and a market no skill matches gets the
exact prompts it got before skills existed. No network, no keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import fastlane as fl
import skills
import swarm as sw
from questions import political_tag
from schemas import GatherRole

MLB = {"slug": "aec-mlb-sd-lad-2026-09-22", "tags": ["sports", "mlb"],
       "question": "Who will win in the upcoming baseball event San Diego Padres vs Los Angeles Dodgers?",
       "outcome": "San Diego Padres wins"}
UNMATCHED = {"slug": "fed-cuts-rates-october", "tags": ["economics"],
             "question": "Will the Fed cut rates in October?", "outcome": "Yes"}
GENERAL_UNMATCHED = {"slug": "prdc-gtavi-12-31-2026", "tags": ["culture", "entertainment"],
                     "question": "GTA VI Released By", "outcome": "December 31, 2026"}


def ids(market):
    return [s.id for s in skills.select(market)]


# --- the files --------------------------------------------------------------

def test_every_skill_file_parses_with_unique_ids():
    loaded = skills.load()
    assert {s.id for s in loaded} == {
        "mlb_game_lines", "nfl_game_lines", "thin_props_periods", "ai_releases_rankings",
        "charts", "price_ladders", "temperature_bands"}
    assert len(list(skills.SKILLS_DIR.glob("*/*.md"))) == len(loaded)


def test_every_claim_carries_its_evidence_marker():
    for s in skills.load():
        for name, items in s.sections:
            if name in skills.UNMARKED_SECTIONS:
                continue
            for b in items:
                assert skills.MARKER.search(b), (s.id, name, b)


def test_parser_rejects_an_unmarked_claim_and_a_wildcard_skill(tmp_path):
    good = (skills.SKILLS_DIR / "sports" / "mlb_game_lines.md").read_text(encoding="utf-8")
    bad = good.replace("## Pitfalls\n", "## Pitfalls\n- Totals always go over.\n")
    p = tmp_path / "mlb_game_lines.md"
    with pytest.raises(ValueError, match="unmarked claim"):
        skills.parse_skill(bad, p)
    wild = good.replace("tags: mlb, baseball", "tags:").replace("kinds: aec, asc, tsc, atc, cks", "kinds:")
    with pytest.raises(ValueError, match="match everything"):
        skills.parse_skill(wild, p)
    with pytest.raises(ValueError, match="file name"):
        skills.parse_skill(good, tmp_path / "other.md")


def test_no_skill_is_political():
    for s in skills.load():
        assert political_tag(sorted(s.tags)) is None, s.id
        assert s.family not in ("politics", "government", "elections")


# --- selection ----------------------------------------------------------------

@pytest.mark.parametrize("market, expected", [
    (MLB, ["mlb_game_lines"]),
    ({"slug": "aec-mlb-sd-lad-2026-09-22"}, ["mlb_game_lines"]),          # untagged: slug token
    ({"slug": "asc-mlb-sd-lad-2026-09-22-f5-pos-1pt5", "tags": ["mlb"]}, ["mlb_game_lines"]),
    ({"slug": "asc-nfl-nyg-lar-2026-09-21-pos-3pt5"}, ["nfl_game_lines"]),
    ({"slug": "asc-nfl-nyg-lar-2026-09-21-2h-pos-3pt5"}, ["nfl_game_lines", "thin_props_periods"]),
    ({"slug": "astatc-mlb-sd-lad-2026-09-22-er-x-gte1", "tags": ["mlb"]}, ["thin_props_periods"]),
    ({"slug": "tc-temp-nychigh-2026-09-23-gte67lt68f", "tags": ["weather"]}, ["temperature_bands"]),
    ({"slug": "cpc-btc-pricerange-yr-12-31-2026-82500", "tags": ["btc", "crypto"]}, ["price_ladders"]),
    ({"slug": "aimc-gemini-3pt5pro-2026-09-30", "tags": ["tech", "model-releases"]},
     ["ai_releases_rankings"]),
    ({"slug": "ccpc-bilbrd-1album-any2026-kenlam"}, ["charts"]),
    ({"slug": "aec-lol-mkf-blgj-2026-09-22", "tags": ["esports", "lol"]}, []),
    ({"slug": "tec-mlb-nlchamp-2026-09-27-lad", "tags": ["mlb"]}, []),        # futures
    ({"slug": "ipcc-2026ipos-anthropic", "tags": ["tech", "business"]}, []),
    (UNMATCHED, []),
    ({}, []),
])
def test_selection_by_tags_and_slug(market, expected):
    assert ids(market) == expected


def test_selection_is_deterministic_and_ignores_tag_order():
    a = {"slug": "asc-nfl-x-y-2026-09-21-2h-pos-3pt5", "tags": ["sports", "nfl"]}
    b = {**a, "tags": ["nfl", "sports"]}
    first = ids(a)
    assert all(ids(a) == first for _ in range(5))
    assert ids(b) == first
    assert len(skills.select({"slug": "asc-nfl-x-2h", "tags": ["nfl", "mlb"]})) <= skills.MAX_SKILLS


@pytest.mark.parametrize("market", [
    {"slug": "paccc-usho-midterms-2026-11-03-dem", "question": "U.S House Midterm Winner"},
    {"slug": "lawec-cryptoleg-2026-12-31", "tags": ["politics", "crypto", "us-pol"],
     "question": "Crypto Market Structure Legislation Becomes Law?"},
    {"slug": "cpc-btc-x", "tags": ["crypto"],
     "question": "Crypto Market Structure Legislation Becomes Law?"},
    {"slug": "aec-mlb-x", "tags": ["mlb", "us-pol"]},
    {"slug": "tc-temp-nychigh-x", "tags": ["weather"], "question": "Will the governor declare an emergency?"},
])
def test_restricted_markets_select_nothing(market):
    assert skills.is_restricted(market)
    assert skills.select(market) == []
    assert skills.gatherer_block(market) == ""
    assert skills.brief_block([market]) == ""


def test_team_names_are_not_restricted():
    m = {"slug": "aec-nfl-was-ne-2026-09-20", "tags": ["nfl"],
         "question": "Washington Commanders vs New England Patriots", "outcome": "Patriots wins"}
    assert not skills.is_restricted(m)
    assert ids(m) == ["nfl_game_lines"]


def test_one_restricted_market_empties_the_event():
    bad = {"slug": "aec-mlb-x", "tags": ["mlb", "politics"]}
    assert skills.select_for_event([MLB, bad]) == []
    assert [s.id for s in skills.select_for_event([MLB])] == ["mlb_game_lines"]


def test_weather_is_code_not_research():
    wx = {"slug": "tc-temp-nychigh-2026-09-23-gte67lt68f", "tags": ["weather"]}
    assert skills.research_allowed(wx) is False
    assert skills.research_allowed(MLB) is True
    assert "Do not search" in skills.gatherer_block(wx)


def test_period_rule_matches_fastlane():
    assert skills.PERIOD_TOKENS == fl._PERIOD_TOKENS
    for m in ({"slug": "asc-nfl-a-b-2h-pos-3pt5"}, {"slug": "asc-mlb-a-b-f5-pos-1pt5"},
              {"slug": "tsc-nfl-a-b", "question": "Total in the 1st half"}, MLB):
        assert skills._is_period(m) == fl.is_period_market(m)


# --- rendering ------------------------------------------------------------------

def test_rendered_blocks_are_delimited_and_carry_no_markers_or_role_names():
    for block in (skills.gatherer_block(MLB), skills.brief_block([MLB])):
        body = block.strip()
        assert body.startswith('<playbook skills="mlb_game_lines">') and body.endswith("</playbook>")
        assert "[general]" not in block and "[measured" not in block and "[rule]" not in block
        # tests/test_swarm.patch_llms finds a gatherer's role by its name in
        # the system prompt; a playbook must not name another role.
        for role in GatherRole:
            assert role.value not in block


def test_gatherer_prompt_is_byte_identical_when_no_skill_matches(monkeypatch):
    seen = []

    async def capture(model=None, messages=None, **kw):
        seen.append(messages[0]["content"])
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(sw, "acompletion", capture)

    async def search(q):
        return []

    import asyncio
    for role in GatherRole:
        seen.clear()
        state = sw.JevTriage.build_state(GENERAL_UNMATCHED, {})
        assert asyncio.run(sw._run_gatherer(role, state, search, [])) is None
        assert seen == [sw._GATHER_SYSTEM.format(role=role.value, brief=sw.ROLE_BRIEFS[role])]

        seen.clear()
        state = sw.JevTriage.build_state(MLB, {})
        asyncio.run(sw._run_gatherer(role, state, search, []))
        base = sw._GATHER_SYSTEM.format(role=role.value, brief=sw.ROLE_BRIEFS[role])
        assert seen[0].startswith(base) and "<playbook" in seen[0][len(base):]


def _brief_request(monkeypatch, markets, sport, **kw):
    captured = {}

    def handler(request):
        captured["content"] = request.content
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import asyncio

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await fl.build_brief(c, "Some event", markets, {}, sport=sport, **kw)
    asyncio.run(go())
    return captured["content"]


@pytest.mark.parametrize("market, sport", [(UNMATCHED, False), (GENERAL_UNMATCHED, False),
                                           ({"slug": "aec-lol-a-b", "tags": ["lol"]}, True)])
def test_brief_request_is_byte_identical_when_no_skill_matches(monkeypatch, market, sport):
    got = _brief_request(monkeypatch, [market], sport)
    prompt = (fl._BRIEF_PROMPT if sport else fl._BRIEF_PROMPT_GENERAL).format(
        event="Some event", markets=fl._markets_block([market], {}))
    # The request as build_brief wrote it before skills existed.
    before = {"model": fl.SEARCH_MODEL, "max_tokens": 3000,
              "messages": [{"role": "user", "content": prompt}],
              "tools": [{"type": fl.WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 3}]}
    assert got == httpx.Request("POST", fl.ANTHROPIC_API_URL, json=before).content


def test_brief_request_carries_the_playbook_as_system_when_a_skill_matches(monkeypatch):
    body = json.loads(_brief_request(monkeypatch, [MLB], True))
    assert body["system"].startswith('<playbook skills="mlb_game_lines">')
    assert "probable starter" in body["system"]
    # The per-event user message is unchanged by the playbook.
    off = json.loads(_brief_request(monkeypatch, [MLB], True, use_skills=False))
    assert "system" not in off and off["messages"] == body["messages"]


def test_skills_are_never_written_by_code():
    src = Path(skills.__file__).read_text(encoding="utf-8")
    assert "write_text" not in src and ".write(" not in src and "open(" not in src
