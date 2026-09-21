"""
Which escalated markets get paid research. Pure function, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from daemon import select_for_research
from schemas import PipelineResult, TriageVerdict


def _c(slug, event, gate, escalate=True, hours=48):
    at = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    result = PipelineResult(
        market_slug=slug,
        triage=TriageVerdict(market_slug=slug, escalate=escalate, gate_score=gate),
    )
    return result, {"slug": slug, "event_slug": event, "event_at": at}, {}


def _slugs(chosen):
    return [r.market_slug for r, _, _ in chosen]


def test_one_market_per_event_best_gate_first():
    """
    Measured 2026-09-21: one event escalated six net-worth thresholds at
    once. They resolve together and share one set of facts.
    """
    chosen = select_for_research([
        _c("musk-600b", "musk-net-worth", 0.76),
        _c("musk-700b", "musk-net-worth", 0.77),
        _c("musk-1t", "musk-net-worth", 0.77),
        _c("tesla-q3", "tesla-deliveries", 0.87),
        _c("nyc-temp", "temp-nyc", 0.64),
    ], max_research=5, per_event=1)
    assert _slugs(chosen)[0] == "tesla-q3"
    assert len([s for s in _slugs(chosen) if s.startswith("musk")]) == 1
    assert len(chosen) == 3


def test_cap_bounds_spend_regardless_of_how_many_escalate():
    many = [_c(f"m{i}", f"e{i}", 0.6 + i / 1000) for i in range(50)]
    assert len(select_for_research(many, max_research=5)) == 5


def test_ties_go_to_the_market_that_resolves_sooner():
    chosen = select_for_research([
        _c("december", "e1", 0.80, hours=2400),
        _c("tomorrow", "e2", 0.80, hours=20),
    ], max_research=1)
    assert _slugs(chosen) == ["tomorrow"]


def test_never_researches_a_market_the_gate_rejected():
    chosen = select_for_research([
        _c("vetoed", "e1", 0.0, escalate=False),
        _c("ok", "e2", 0.61),
    ], max_research=5)
    assert _slugs(chosen) == ["ok"]


def test_zero_budget_researches_nothing():
    assert select_for_research([_c("a", "e", 0.9)], max_research=0) == []


# --- the paper-mode cycle, end to end with fakes ---------------------------

class _FakeEngine:
    async def check_floors(self):
        return None


class _FakeQuotes:
    def __init__(self, *a, **kw):
        pass

    async def bbo(self, slug):
        return {"bid": 0.50, "ask": 0.52}


class _FakeSwarm:
    """Escalates everything; records what was researched."""

    def __init__(self):
        from questions import StructuralLimits
        self.limits = StructuralLimits()
        self.portfolio = None
        self.researched: list[str] = []

    async def triage(self, market, bbo, tracker=None):
        gate = {"a1": 0.70, "a2": 0.90, "b1": 0.80, "c1": 0.65}[market["slug"]]
        return PipelineResult(
            market_slug=market["slug"],
            triage=TriageVerdict(market_slug=market["slug"], escalate=True,
                                 gate_score=gate))

    async def research(self, result, market, bbo, tracker=None):
        self.researched.append(result.market_slug)
        result.halted_at = "no_edge_after_sizing"
        return result


async def test_paper_cycle_triages_all_then_researches_the_best(monkeypatch, tmp_path):
    import daemon as d
    from config import Config

    pairs = [({"slug": s, "outcomePrices": '["0.50","0.52"]'}, {"slug": ev})
             for s, ev in (("a1", "A"), ("a2", "A"), ("b1", "B"), ("c1", "C"))]

    async def fake_fetch(pm, **kw):
        return []

    monkeypatch.setattr(d, "fetch_events_across_tags", fake_fetch)
    monkeypatch.setattr(d, "iter_event_markets", lambda page: pairs)
    monkeypatch.setattr(d, "QuoteFetcher", _FakeQuotes)
    monkeypatch.setattr(
        d, "normalize_market",
        lambda m, ev: {"slug": m["slug"], "event_slug": ev["slug"]})
    monkeypatch.setattr(d.traces, "connect", lambda: None)
    monkeypatch.setattr(d.traces, "record", lambda *a, **kw: None)

    cfg = Config(typesafe_api_key="k", polymarket_key_id="i",
                 polymarket_secret_key="s", mode="paper")
    dm = d.Daemon(cfg, once=True, tags=("nfl",), research_per_cycle=2)
    swarm = _FakeSwarm()
    await dm._cycle(pm=None, swarm=swarm, engine=_FakeEngine())

    assert dm.stats["triaged"] == 4 and dm.stats["escalated"] == 4
    # a2 (0.90) beats a1 in event A; then b1 (0.80). c1 is over the cap.
    assert swarm.researched == ["a2", "b1"]
