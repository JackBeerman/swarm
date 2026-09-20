"""
swarm.py -- three-tier evaluation pipeline for the Polymarket US trading daemon.

    Tier 1  Jev (TypeSafe System One)   ~$0.0001   every candidate
    Tier 2  Claude Haiku 4.5 x3         ~$0.02     only past the gate
    Tier 3  Claude Opus 5                ~$0.05     only past the gate

The whole economic argument for this shape is that Tier 1 is effectively
free and Tiers 2/3 are not. The gate must therefore be *strict*: at a $100
treasury you can afford roughly 300-500 full evaluations, total, ever.

Two corrections to the original spec are baked in here and flagged inline:

  1. Astra does not size positions. It returns a probability and a
     confidence; size_from_signal() computes fractional Kelly in code.
  2. Sizing happens under a portfolio lock against a live bankroll, so
     three concurrent evaluations cannot each commit the same dollars.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence

import httpx
from litellm import acompletion
from pydantic import ValidationError

from adapters import derive_notionals, game_state
from questions import (
    ALL_TIER1_QUESTIONS,
    GateThresholds,
    StructuralLimits,
    is_sports_market,
    structural_filter,
    tier1_questions_for,
)
from schemas import (
    GatherRole,
    MarketFactSummary,
    PipelineResult,
    Side,
    SizedOrder,
    StageCost,
    TradeSignal,
    TriageVerdict,
)

log = logging.getLogger("swarm")

TYPESAFE_BASE_URL = os.getenv("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
TYPESAFE_MODEL = os.getenv("TYPESAFE_DEFAULT_MODEL", "jev-latest")

TIER2_MODEL = os.getenv("TIER2_MODEL", "anthropic/claude-haiku-4-5")
TIER3_MODEL = os.getenv("TIER3_MODEL", "anthropic/claude-opus-5")

# litellm prices Tiers 2 and 3 for us. It does not know about Jev, so triage
# is priced here. SET THESE from the TypeSafe console before going live --
# left at 0.0 the kill switch will under-count the always-on burn, which is
# the one cost that accrues whether or not you ever place a trade.
# Jev 1.13, per OpenRouter: $0.042 per 1M input tokens, output free.
# At ~1,600 input tokens per triage call that is $0.000067 -- about 15,000
# triage calls per dollar. Tier 1 is, for practical purposes, free; the
# reason to keep the gate tight is Tier 2/3 spend, not this.
TYPESAFE_USD_PER_MTOK_IN = float(os.getenv("TYPESAFE_USD_PER_MTOK_IN", "0.042"))
TYPESAFE_USD_PER_MTOK_OUT = float(os.getenv("TYPESAFE_USD_PER_MTOK_OUT", "0.0"))


def price_triage(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * TYPESAFE_USD_PER_MTOK_IN
        + output_tokens * TYPESAFE_USD_PER_MTOK_OUT
    ) / 1_000_000


# ==========================================================================
# Tunables
# ==========================================================================

@dataclass(frozen=True)
class RiskConfig:
    kelly_fraction: float = 0.25            # quarter Kelly, never full
    max_position_fraction: float = 0.08     # <=8% of bankroll in one market
    min_notional_usd: float = 2.00
    min_edge: float = 0.10                  # see note in size_from_signal()
    max_spread: float = 0.04                # skip anything wider than 4c


# ==========================================================================
# Injected dependencies
# ==========================================================================

class BudgetGuard(Protocol):
    """Implemented by risk_engine. Consulted before every paid stage."""

    async def can_spend(self, usd: float) -> bool: ...
    async def record_spend(self, cost: StageCost) -> None: ...
    async def available_bankroll(self) -> float: ...


SearchFn = Callable[[str], Awaitable[list[dict[str, str]]]]
"""
Injected web search. Return [{"title", "url", "snippet"}, ...].

Left as a seam on purpose: grounded-search billing is the single largest
line item in a Tier 2 call, and you will want to swap providers (Tavily,
Exa, Brave, Gemini grounding) while measuring cost per evaluation. Do not
hard-wire this.
"""


# ==========================================================================
# Tier 1 -- Jev triage
# ==========================================================================

class JevTriage:
    """
    Thin async client over POST /v1/systemone.

    Written against the raw HTTP contract rather than
    langchain_typesafe.experimental.middleware on purpose -- an experimental
    namespace two days after release is not where a money-handling daemon
    should take a dependency.

    Retries 429 and 529 with exponential backoff, as the API reference
    requires. The official SDKs do this for you; a hand-rolled client must
    do it explicitly or it will fall over the first time the daemon runs a
    burst of triage calls against a rate limit.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = TYPESAFE_MODEL,
        base_url: str = TYPESAFE_BASE_URL,
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        self._model = model
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def build_state(
        market: dict[str, Any],
        bbo: dict[str, Any],
        tracker: Any | None = None,
    ) -> dict[str, Any]:
        """
        The `state` Jev evaluates.

        `market` must already be normalized by adapters.normalize_market()
        and `bbo` by adapters.normalize_bbo(). The raw SDK shapes use
        different field names and wrap prices in Amount objects, so passing
        them through raw yields a state full of Nones -- and Jev will
        return confident-looking probabilities about nothing.

        Nested and structured on purpose: the questions in questions.py
        reference paths like `description` and `question` in backticks, and
        that only disambiguates if the state actually has that shape.

        Note what is NOT sent: no price history, no catalyst narrative, no
        news. Tier 1 judges the market's structure -- is this objective,
        self-contained, researchable, restricted -- not what is happening
        in the world. Asking a model about events it cannot see produces
        an answer drawn from weights, which is worse than no answer.
        """
        bid, ask = _f(bbo.get("bid")), _f(bbo.get("ask"))
        hours_to_close = _hours_until(market.get("closes_at"))
        # The event clock, distinct from the settlement clock. A game
        # played today settles ~332h later; `hours_to_close` measures the
        # payout wait, `hours_to_event` measures when the outcome is known.
        hours_to_event = _hours_until(market.get("event_at"))
        notionals = derive_notionals(bbo)

        return {
            "question": market.get("question"),
            "description": (market.get("description") or "")[:1500],
            "outcome": market.get("outcome"),
            "event": market.get("event_title"),
            "tags": market.get("tags") or [],
            "slug": market.get("slug"),
            # Numeric context is included for the record and for
            # structural_filter(), but no question asks about it -- those
            # comparisons happen in code.
            "best_bid": bid,
            "best_ask": ask,
            "spread": None if (bid is None or ask is None) else round(ask - bid, 4),
            "bid_depth": bbo.get("bid_depth"),
            "ask_depth": bbo.get("ask_depth"),
            # Share quantities. structural_filter gates on these; the
            # *_depth fields above are book-level counts and are kept only
            # for the record.
            "bid_shares": bbo.get("bid_shares"),
            "ask_shares": bbo.get("ask_shares"),
            # The API sends no volume or liquidity field, so these are
            # derived from the quote's share counts. See
            # adapters.derive_notionals.
            "volume_usd": market.get("volume_usd") or notionals["volume_usd"],
            "liquidity_usd": (market.get("liquidity_usd")
                              or notionals["liquidity_usd"]),
            "closes_at": market.get("closes_at"),
            "hours_to_close": hours_to_close,
            "hours_to_event": hours_to_event,
            # Live game state. structural_filter() reads these; no Tier 1
            # question asks about them, because "is this game over" is a
            # string comparison and belongs in code.
            "period": market.get("period"),
            "score": market.get("score"),
            "elapsed": market.get("elapsed"),
            "game_state": game_state(market),
        }

    @staticmethod
    def _model_state(state: dict[str, Any]) -> dict[str, Any]:
        """
        The subset actually sent to Jev.

        Numbers the questions never reference are stripped: they cost
        tokens, add nothing, and invite the model to reason about
        quantities that code has already decided on.
        """
        keys = ("question", "description", "outcome", "event", "tags")
        return {k: state[k] for k in keys if state.get(k) not in (None, "", [])}

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with exponential backoff on 429 / 529, per the API reference."""
        delay = 0.5
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.post("/v1/systemone", json=payload)
                if resp.status_code in (429, 529):
                    if attempt == self._max_retries:
                        resp.raise_for_status()
                    retry_after = resp.headers.get("retry-after")
                    wait = float(retry_after) if retry_after else delay
                    log.debug(
                        "jev %s, retrying in %.1fs (attempt %d)",
                        resp.status_code, wait, attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    delay *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last = exc
                if attempt == self._max_retries:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
        raise last or RuntimeError("jev request failed")

    async def evaluate(
        self,
        market: dict[str, Any],
        bbo: dict[str, Any],
        gate: GateThresholds | None = None,
        tracker: Any | None = None,
        limits: StructuralLimits | None = None,
    ) -> TriageVerdict:
        gate = gate or GateThresholds()
        limits = limits or StructuralLimits()
        state = self.build_state(market, bbo, tracker)
        slug = str(market.get("slug"))

        # Deterministic rejection first. Costs nothing, so it runs before
        # the model call rather than after it.
        reason = structural_filter(state, limits)
        if reason:
            return TriageVerdict(
                market_slug=slug,
                # Recorded even on a structural reject: an analysis of
                # shadow.db wants to know what the market WAS, and the
                # default would otherwise report every rejected sports
                # market as non-sports.
                is_sports=is_sports_market(market),
                structural_reject=reason,
                escalate=False,
                veto_reason=f"structural: {reason}",
            )

        # Which tractability block to ask is a tag lookup, so it is
        # decided here rather than by the model.
        sports = is_sports_market(market)
        questions = tier1_questions_for(market)

        t0 = time.perf_counter()
        body = await self._post(
            {
                "model": self._model,
                "state": self._model_state(state),
                "questions": questions,
            }
        )
        latency_ms = (time.perf_counter() - t0) * 1000

        a = body.get("answers", {})
        usage = body.get("usage", {})

        def noul(name: str) -> float:
            return float(a.get(name, {}).get("noul", 0.0))

        research = a.get("research_would_help", {})
        otype = a.get("outcome_type", {})
        aggregation = a.get("stat_aggregation", {})
        pregame = a.get("pregame_information_edge", {})
        smtype = a.get("sports_market_type", {})

        verdict = TriageVerdict(
            market_slug=slug,
            is_sports=sports,
            federal_policy_outcome=noul("federal_policy_outcome"),
            defense_or_military=noul("defense_or_military"),
            us_election_or_appointment=noul("us_election_or_appointment"),
            objective_resolution=noul("objective_resolution"),
            self_contained=noul("self_contained"),
            research_would_help=float(research.get("score", 0.0)),
            # Noul answers carry no confidence field -- only Choice and
            # Score do. Do not go looking for one on the nouls above.
            research_confidence=float(research.get("confidence", 0.0)),
            outcome_type=str(otype.get("choice", "unknown")),
            outcome_type_confidence=float(otype.get("confidence", 0.0)),
            stat_aggregation=float(aggregation.get("score", 0.0)),
            stat_aggregation_confidence=float(aggregation.get("confidence", 0.0)),
            pregame_information_edge=float(pregame.get("score", 0.0)),
            pregame_information_edge_confidence=float(
                pregame.get("confidence", 0.0)
            ),
            sports_market_type=str(smtype.get("choice", "unknown")),
            sports_market_type_confidence=float(smtype.get("confidence", 0.0)),
            latency_ms=latency_ms,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=body.get("model", self._model),
        )
        return apply_gate(verdict, gate)


def compute_gate_score(v: TriageVerdict, gate: GateThresholds) -> float:
    """
    Weighted arithmetic mean of the tractability signals, minus a penalty
    for the outcome type.

    Arithmetic, not geometric: a geometric mean is another way of writing
    an AND, since one near-zero term annihilates the product. Weakness on
    one axis should cost a market proportionally; the floors in
    GateThresholds handle genuine disqualifiers.

    Nothing here multiplies. An earlier version scaled this by both an
    outcome multiplier and a confidence term; because `raw` is bounded at
    1.0, the product could not reach min_gate_score for two of the four
    outcome types, and contested_event needed confidence of exactly 1.00.
    That is the ANDed-thresholds failure in another form, and sweeping
    min_gate_score would have shown 0% at every value for those types
    without revealing why. Confidence is now a routing decision in
    apply_gate(), not a factor here.

    This is TypeSafe's own Composite Scoring pattern -- independent
    probabilities combined by weights that live in code and can be
    reviewed, rather than blended inside a single broad question.
    """
    if v.is_sports:
        agg = min(max(v.stat_aggregation, 0.0) / 2.0, 1.0)
        pre = min(max(v.pregame_information_edge, 0.0) / 2.0, 1.0)
        w = (gate.w_stat_aggregation + gate.w_pregame_information_edge
             + gate.w_objective_resolution_sports)
        raw = (
            gate.w_stat_aggregation * agg
            + gate.w_pregame_information_edge * pre
            + gate.w_objective_resolution_sports * v.objective_resolution
        ) / w
        raw -= gate.sports_market_type_penalty.get(v.sports_market_type, 0.15)
        return round(max(raw, 0.0), 4)

    research = min(max(v.research_would_help, 0.0) / 2.0, 1.0)
    w = gate.w_research_would_help + gate.w_objective_resolution + gate.w_self_contained
    raw = (
        gate.w_research_would_help * research
        + gate.w_objective_resolution * v.objective_resolution
        + gate.w_self_contained * v.self_contained
    ) / w

    raw -= gate.outcome_type_penalty.get(v.outcome_type, 0.10)
    return round(max(raw, 0.0), 4)


def apply_gate(v: TriageVerdict, gate: GateThresholds) -> TriageVerdict:
    """Policy lives in code, not in the model. Jev judges; this decides."""

    # 1. Hard veto on the MAX of the restricted questions. Averaging would
    #    let two clean answers dilute one alarming one.
    if v.restricted_max > gate.max_restricted:
        which = max(
            ("federal_policy_outcome", v.federal_policy_outcome),
            ("defense_or_military", v.defense_or_military),
            ("us_election_or_appointment", v.us_election_or_appointment),
            key=lambda kv: kv[1],
        )
        v.escalate = False
        v.gate_score = 0.0
        v.veto_reason = f"restricted:{which[0]}={which[1]:.3f} > {gate.max_restricted}"
        return v

    # 2. Disqualifying floors. objective_resolution is asked in both
    #    question sets; the second floor differs, because
    #    research_would_help is never asked on a sports market and would
    #    sit at its 0.0 default and veto every one of them.
    if v.objective_resolution < gate.floor_objective_resolution:
        v.escalate = False
        v.gate_score = 0.0
        v.veto_reason = f"subjective_resolution={v.objective_resolution:.3f}"
        return v
    if v.is_sports:
        if v.stat_aggregation < gate.floor_stat_aggregation:
            v.escalate = False
            v.gate_score = 0.0
            v.veto_reason = f"too_discrete={v.stat_aggregation:.3f}"
            return v
    elif v.research_would_help < gate.floor_research_would_help:
        v.escalate = False
        v.gate_score = 0.0
        v.veto_reason = f"research_wont_help={v.research_would_help:.3f}"
        return v

    # 3. Confidence as a route, not a weight.
    #
    #    Low Score confidence means the levels were ambiguous or the state
    #    did not contain enough to judge -- TypeSafe's "do not act" branch.
    #    Escalation spends real money, so an unreadable market is skipped
    #    rather than scored down. Scoring it down was worse than skipping:
    #    it mixed "this market is unattractive" with "triage could not
    #    tell", and only one of those is information.
    #
    #    min_research_confidence ships at 0.0, so this is inert until a
    #    shadow run shows where the cut belongs.
    if v.research_confidence < gate.min_research_confidence:
        v.escalate = False
        v.gate_score = 0.0
        v.veto_reason = (
            f"low_confidence={v.research_confidence:.3f} "
            f"< {gate.min_research_confidence}"
        )
        return v

    # 4. The gate: one composite score, one threshold.
    v.gate_score = compute_gate_score(v, gate)
    if v.gate_score < gate.min_gate_score:
        v.escalate = False
        v.veto_reason = f"gate_score={v.gate_score:.3f} < {gate.min_gate_score}"
        return v

    v.escalate = True
    return v


# Back-compat alias; shadow.py and the tests import this name.
_apply_gate = apply_gate


# ==========================================================================
# Tier 2 -- gatherer swarm
# ==========================================================================

ROLE_BRIEFS: dict[GatherRole, str] = {
    GatherRole.SLEUTH: (
        "You establish what actually happened. Find the most recent concrete "
        "reporting bearing on this market's resolution criteria. Prefer "
        "primary sources and datestamped reporting. Name dates explicitly."
    ),
    GatherRole.QUANT: (
        "You establish the base rate. Find how comparable situations have "
        "historically resolved and how often. Give frequencies, not vibes. "
        "If no reference class exists, say so plainly -- that is a finding."
    ),
    GatherRole.RED_TEAM: (
        "You argue the market is correctly priced and any apparent edge is "
        "illusory. Find the strongest disconfirming evidence, the ambiguity "
        "in the resolution criteria, and the reason a sharp trader has "
        "already acted on what you are looking at."
    ),
}

_GATHER_SYSTEM = """You are a {role} analyst evaluating a prediction market.

{brief}

You have one web_search tool call available. Use it once, with a precise query.

Then respond with ONLY a JSON object matching this schema. No preamble, no
markdown fences, no commentary:

{{"identified_catalyst": str,
  "historical_precedent": str,
  "key_facts": [str],
  "contradicting_evidence": str or null,
  "data_confidence": float between 0 and 1,
  "sources": [str]}}

data_confidence is your calibration on the facts above, not your enthusiasm.
If your search returned nothing useful, return low confidence and say so in
identified_catalyst. Fabricating a catalyst is the worst outcome available
to you."""


async def _run_gatherer(
    role: GatherRole,
    state: dict[str, Any],
    search: SearchFn,
    costs: list[StageCost],
) -> MarketFactSummary | None:
    search_tool = {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the public web. One call only.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _GATHER_SYSTEM.format(
                role=role.value, brief=ROLE_BRIEFS[role]
            ),
        },
        {"role": "user", "content": json.dumps(state, default=str)},
    ]

    try:
        first = await acompletion(
            model=TIER2_MODEL,
            messages=messages,
            tools=[search_tool],
            **sampling_kwargs(TIER2_MODEL, 0.3),
            max_tokens=900,
        )
        costs.append(_cost_of(first, "gather", TIER2_MODEL))

        msg = first.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls:
            messages.append(msg.model_dump())
            for call in tool_calls[:1]:  # one search, hard cap
                args = json.loads(call.function.arguments or "{}")
                results = await search(args.get("query", state.get("question", "")))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(results[:5])[:6000],
                    }
                )
            second = await acompletion(
                model=TIER2_MODEL,
                messages=messages,
                **sampling_kwargs(TIER2_MODEL, 0.3),
                max_tokens=900,
            )
            costs.append(_cost_of(second, "gather", TIER2_MODEL))
            raw = second.choices[0].message.content
        else:
            raw = msg.content

        payload = _loads_loose(raw)
        payload["role"] = role.value
        return MarketFactSummary.model_validate(payload)

    except (ValidationError, json.JSONDecodeError, KeyError, AttributeError) as exc:
        log.warning("gatherer %s produced unusable output: %s", role.value, exc)
        return None
    except Exception as exc:  # network, rate limit, provider outage
        log.warning("gatherer %s failed: %s", role.value, exc)
        return None


# ==========================================================================
# Tier 3 -- Astra synthesis
# ==========================================================================

_SYNTH_SYSTEM = """You aggregate independent analyst reports on a prediction
market into a single calibrated probability.

You will receive the market state and three compressed analyst summaries
(sleuth, quant, red_team). You will NOT receive raw web pages or order book
data. Work only from what you are given.

Method:
1. Start from the quant's base rate as your prior. If there is no usable
   reference class, start from the market price and require strong evidence
   to move.
2. Update on the sleuth's catalyst only to the extent it is datestamped and
   specific.
3. Take the red team seriously. The market price is the aggregate of people
   with money at risk; you are one model with three web searches.
4. Weight each report by its data_confidence.

Respond with ONLY this JSON object, no fences or commentary:

{"side": "YES" or "NO",
 "probability": float,
 "confidence": float,
 "reasoning": str,
 "disqualifiers": [str],
 "abstain": bool}

`probability` is P(your chosen side resolves true) -- not P(YES) unless you
chose YES. `confidence` is your calibration on that number.

Set abstain=true and explain when the reports conflict irreconcilably, when
the resolution criteria are ambiguous, or when your probability lands within
a few points of the market price. Abstaining is free. Being wrong is not.
You are NOT asked for a position size; do not suggest one."""


async def _synthesize(
    state: dict[str, Any],
    facts: Sequence[MarketFactSummary],
    costs: list[StageCost],
) -> TradeSignal | None:
    payload = {
        "market": state,
        "reports": [f.model_dump(mode="json") for f in facts],
    }
    try:
        resp = await acompletion(
            model=TIER3_MODEL,
            messages=[
                {"role": "system", "content": _SYNTH_SYSTEM},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
            **sampling_kwargs(TIER3_MODEL, 0.1),
            max_tokens=1200,
        )
        costs.append(_cost_of(resp, "synthesize", TIER3_MODEL))
        data = _loads_loose(resp.choices[0].message.content)
        data["market_slug"] = state.get("slug")
        return TradeSignal.model_validate(data)
    except (ValidationError, json.JSONDecodeError, AttributeError) as exc:
        log.warning("synthesis produced unusable output: %s", exc)
        return None
    except Exception as exc:
        log.warning("synthesis failed: %s", exc)
        return None


# ==========================================================================
# Sizing -- in code, under a lock
# ==========================================================================

class PortfolioLock:
    """
    Serializes sizing across concurrent evaluations.

    Without this, three pipelines that clear the gate in the same second
    each compute Kelly against the same bankroll and collectively commit
    ~3x what any one of them thought it was committing. This is the single
    most likely way to blow the account in week one.

    Reservations carry a TTL. An earlier version had release() defined but
    never called, so reservations accumulated forever: reserved_total grew
    monotonically, available bankroll drifted to zero, and the daemon
    silently stopped producing orders with no error anywhere. Explicit
    release is still correct and the caller should do it -- the TTL only
    means a missed release degrades rather than wedges.

    The TTL should comfortably exceed the time between sizing an order and
    it appearing in the exchange's own balances, since at that point the
    capital is accounted for on the other side and double-counting it here
    would under-size everything that follows.
    """

    def __init__(self, ttl_seconds: float = 120.0) -> None:
        self._lock = asyncio.Lock()
        self._reserved: dict[str, tuple[float, float]] = {}  # slug -> (usd, at)
        self.ttl = ttl_seconds

    def __call__(self) -> asyncio.Lock:
        return self._lock

    def _expire(self) -> None:
        cutoff = time.time() - self.ttl
        stale = [s for s, (_, at) in self._reserved.items() if at < cutoff]
        for s in stale:
            log.debug("reservation for %s expired after %.0fs", s, self.ttl)
            del self._reserved[s]

    def reserve(self, slug: str, usd: float) -> None:
        self._expire()
        prior = self._reserved.get(slug, (0.0, 0.0))[0]
        self._reserved[slug] = (prior + usd, time.time())

    def release(self, slug: str) -> None:
        """Call once the order has resolved, filled or rejected alike."""
        self._reserved.pop(slug, None)

    @property
    def reserved_total(self) -> float:
        self._expire()
        return sum(usd for usd, _ in self._reserved.values())

    @property
    def open_reservations(self) -> int:
        self._expire()
        return len(self._reserved)


def size_from_signal(
    signal: TradeSignal,
    bbo: dict[str, Any],
    bankroll: float,
    gate_score: float,
    cfg: RiskConfig,
) -> SizedOrder | None:
    """
    Fractional Kelly for a binary contract, computed here rather than by
    the model.

    For a YES buy at ask price c with true probability p:
        f* = (p - c) / (1 - c)
    For a NO buy (implied NO price 1-c, true prob 1-p):
        f* = (c - p) / c

    The min_edge floor is not risk aversion, it is arithmetic: at a $100
    bankroll an evaluation costs ~$0.15-0.25 and a $5 position therefore
    carries a 3-5% inference drag before the spread. An edge below ~10% is
    not an edge, it is a rounding error you are paying Astra to find.
    """
    if signal.abstain:
        return None

    bid, ask = _f(bbo.get("bid")), _f(bbo.get("ask"))
    if bid is None or ask is None or not (0 < bid < ask < 1):
        return None
    if (ask - bid) > cfg.max_spread:
        return None

    p = signal.probability
    if signal.side is Side.YES:
        price = ask
        kelly = (p - price) / (1 - price)
    else:
        price = 1.0 - bid           # cost of a NO share
        p_yes = 1.0 - p
        kelly = (bid - p_yes) / bid

    # Both branches reduce to the same thing: edge is the probability gap
    # (true prob minus price paid), and kelly = edge / (1 - price).
    edge = kelly * (1.0 - price)
    if kelly <= 0 or edge < cfg.min_edge:
        return None

    # Three independent haircuts: quarter Kelly, Astra's own calibration,
    # and how convincingly Tier 1 let this through in the first place.
    fraction = kelly * cfg.kelly_fraction * signal.confidence * gate_score
    fraction = min(fraction, cfg.max_position_fraction)

    # CreateOrderParams.quantity is an int -- the exchange trades whole
    # shares only. Floor rather than round: rounding up can push notional
    # above the position cap, which is the one number sizing exists to
    # respect. At a $100 bankroll this granularity is not cosmetic --
    # a $2.40 target at $0.90 is 2 shares, a 25% cut.
    target_notional = bankroll * fraction
    quantity = int(target_notional / price)
    if quantity < 1:
        return None

    notional = round(quantity * price, 2)
    if notional < cfg.min_notional_usd:
        return None

    return SizedOrder(
        market_slug=signal.market_slug,
        side=signal.side,
        limit_price=round(price, 3),
        quantity=quantity,
        notional_usd=notional,
        raw_kelly=round(kelly, 4),
        applied_fraction=round(notional / bankroll, 4) if bankroll else 0.0,
        edge=round(edge, 4),
        bankroll_at_size=bankroll,
        signal=signal,
    )


# ==========================================================================
# Orchestrator
# ==========================================================================

@dataclass
class Swarm:
    jev: JevTriage
    search: SearchFn
    budget: BudgetGuard
    gate: GateThresholds = field(default_factory=GateThresholds)
    risk: RiskConfig = field(default_factory=RiskConfig)
    limits: StructuralLimits = field(default_factory=StructuralLimits)
    portfolio: PortfolioLock = field(default_factory=PortfolioLock)
    max_concurrent_evaluations: int = 2

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.max_concurrent_evaluations)

    # Budget headroom required before opening a Tier 2/3 evaluation.
    ESCALATION_COST_ESTIMATE_USD = 0.25

    async def evaluate(
        self,
        market: dict[str, Any],
        bbo: dict[str, Any],
        tracker: Any | None = None,
    ) -> PipelineResult:
        slug = str(market.get("slug"))
        result = PipelineResult(market_slug=slug)

        # ---- Tier 1 -------------------------------------------------
        try:
            verdict = await self.jev.evaluate(
                market, bbo, self.gate, tracker, self.limits
            )
        except Exception as exc:
            log.warning("triage failed for %s: %s -- skipping", slug, exc)
            result.halted_at = "triage_error"
            return result

        result.triage = verdict
        triage_cost = StageCost(
            stage="triage",
            model=TYPESAFE_MODEL,
            usd=price_triage(verdict.input_tokens, verdict.output_tokens),
            input_tokens=verdict.input_tokens,
            output_tokens=verdict.output_tokens,
        )
        result.costs.append(triage_cost)
        # Record BEFORE the gate check. The overwhelming majority of markets
        # are rejected here, and Tier 1 runs on every one of them -- if the
        # ledger only sees escalated markets it misses the always-on burn,
        # which is precisely the cost Tier 1 was introduced to control.
        await self.budget.record_spend(triage_cost)

        if not verdict.escalate:
            result.halted_at = f"gate: {verdict.veto_reason}"
            return result

        # ---- Budget check before spending anything real -------------
        if not await self.budget.can_spend(self.ESCALATION_COST_ESTIMATE_USD):
            result.halted_at = "budget_exhausted"
            log.warning("gate cleared for %s but budget refused escalation", slug)
            return result

        async with self._sem:
            state = JevTriage.build_state(market, bbo, tracker)

            # ---- Tier 2 ---------------------------------------------
            mark = len(result.costs)
            gathered = await asyncio.gather(
                *(
                    _run_gatherer(role, state, self.search, result.costs)
                    for role in GatherRole
                )
            )
            result.facts = [f for f in gathered if f is not None]
            # Only the costs this stage added -- triage is already on the
            # ledger, and re-recording it would double-count the burn.
            for c in result.costs[mark:]:
                await self.budget.record_spend(c)

            if len(result.facts) < 2:
                result.halted_at = "insufficient_gatherer_output"
                return result

            # ---- Tier 3 ---------------------------------------------
            pre = len(result.costs)
            signal = await _synthesize(state, result.facts, result.costs)
            for c in result.costs[pre:]:
                await self.budget.record_spend(c)

            if signal is None:
                result.halted_at = "synthesis_failed"
                return result
            result.signal = signal

            # ---- Sizing, serialized ---------------------------------
            async with self.portfolio():
                bankroll = await self.budget.available_bankroll()
                bankroll -= self.portfolio.reserved_total
                order = size_from_signal(
                    signal, bbo, bankroll, verdict.gate_score, self.risk
                )
                if order is None:
                    result.halted_at = "no_edge_after_sizing"
                    return result
                self.portfolio.reserve(slug, order.notional_usd)

            result.order = order
            return result


# ==========================================================================
# helpers
# ==========================================================================

def _hours_until(iso_ts: str | None) -> float | None:
    """Hours from now until an ISO-8601 timestamp. None if unparseable."""
    if not iso_ts:
        return None
    try:
        t = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (t - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _loads_loose(raw: str | None) -> dict[str, Any]:
    """Models still fence JSON sometimes, whatever the system prompt says."""
    if not raw:
        raise json.JSONDecodeError("empty response", "", 0)
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise json.JSONDecodeError("no JSON object found", s, 0)
    return json.loads(s[start : end + 1])


def sampling_kwargs(model: str, temperature: float) -> dict[str, float]:
    """
    `temperature` where the model still accepts it, nothing where it does not.

    Anthropic removed the sampling parameters on Claude 4.6 and later:
    Opus 5, Sonnet 5, Opus 4.8/4.7 and the Fable family return a **400** if
    `temperature`, `top_p` or `top_k` is present. Haiku 4.5 and older models
    still take them, as do the OpenAI and Gemini models.

    litellm passes the parameter straight through, so this is not something
    the router absorbs -- an unguarded temperature turns every Tier 2/3 call
    into a hard failure the moment the model is switched to Claude.
    """
    m = model.lower()
    if "claude" in m or "anthropic" in m:
        no_sampling = ("opus-5", "opus-4-8", "opus-4-7", "sonnet-5",
                       "fable", "mythos")
        if any(tag in m for tag in no_sampling):
            return {}
    return {"temperature": temperature}


def _cost_of(resp: Any, stage: str, model: str) -> StageCost:
    usd = 0.0
    try:
        usd = float(resp._hidden_params.get("response_cost") or 0.0)
    except Exception:
        try:
            from litellm import completion_cost

            usd = float(completion_cost(completion_response=resp) or 0.0)
        except Exception:
            log.debug("could not price a %s call on %s", stage, model)
    usage = getattr(resp, "usage", None)
    return StageCost(
        stage=stage,  # type: ignore[arg-type]
        model=model,
        usd=usd,
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )
