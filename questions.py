"""
questions.py -- every Jev question and every threshold, in one file.

Kept separate on TypeSafe's own advice: questions and thresholds are the
part a human must actually review, and they should not be scattered through
application code. Jack: this is the file to edit. The rest of the pipeline
is plumbing.

Three corrections from the TypeSafe build guide, all of which changed the
design materially:

1. DETERMINISTIC WORK BELONGS IN CODE. The first draft asked Jev
   "is the bid-ask spread tight enough to trade?" -- but spread, depth,
   volume and time-to-close are arithmetic. They are now computed in
   structural_filter() below and never sent to a model. Paying an LLM to
   compare two floats is the exact anti-pattern the guide opens with.

2. DO NOT ASK THE MODEL ABOUT THINGS IT CANNOT SEE. The first draft asked
   "does a fresh catalyst explain this price move?" while supplying no
   news. Jev could only answer from weights, which the guide explicitly
   warns against. Catalyst questions are removed from Tier 1 entirely;
   they belong at Tier 2, where a gatherer has actually searched. What
   Tier 1 can legitimately judge is the market's *structure* -- is this
   resolvable, objective, and researchable at all.

3. DECOMPOSE. The guide calls this the most important idea in it. Broad
   questions hide several judgments behind one number. Each question below
   evaluates exactly one property, uses structured instructions rather
   than dense prose, and points at a specific state path in backticks.

Output tokens are free and input is $0.042/M, so asking twelve questions
instead of five costs fractions of a cent. There is no reason to be
stingy here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ==========================================================================
# Structural filters -- pure arithmetic, no model call
# ==========================================================================

@dataclass(frozen=True)
class StructuralLimits:
    # Calibrated against 176 quoted live markets (2026-09-19), sampled from
    # ~10,800 open. The previous values were tuned on synthetic data and
    # passed 0 of 176 jointly. Polymarket US markets are small: median
    # traded notional $34, median ask-side depth $5,875.
    #
    # These floors are a "is this market real" test, NOT a tradability
    # test. On a $100 treasury tradability never binds -- only 2 of 176
    # markets could not absorb a $10 position. Spend is controlled by the
    # escalation budget, not here.
    min_volume_usd: float = 250.0         # 50k passed 0.6% alone
    min_liquidity_usd: float = 100.0      # 10k passed 1.7% alone
    max_spread: float = 0.04              # passes 77%; unchanged
    # Compared against bid_shares/ask_shares (a share count), NOT
    # bid_depth/ask_depth, which are counts of book levels and run 1-16.
    # Non-binding on current data: 200 and 5,000 give identical joint
    # results. Kept as a guard against an empty book.
    min_depth_shares: int = 500
    # Measured against the EVENT clock, not the settlement deadline.
    min_hours_to_close: float = 6.0       # no time for research to pay off
    max_days_to_close: float = 120.0      # capital parked too long

    # Live sports. Both default OFF, so behaviour is unchanged until a
    # human turns them on.
    #
    # in_play: trading a game in progress means reacting to score changes
    # faster than the book does. On REST polling, against sportsbooks, that
    # is a race you lose; it wants the markets websocket first.
    #
    # finished: the outcome is known and the quotes reflect it. Measured on
    # finished college football games -- spreads of 0.28-0.49 and 0 shares
    # on the ask of the one market near $1.00.
    allow_in_play: bool = False
    allow_finished_games: bool = False
    min_price: float = 0.05               # avoid lottery-ticket tails
    max_price: float = 0.95


POLITICAL_TAG_WORDS = (
    "politic", "election", "geopolitic", "government", "congress", "senate",
    "president", "parliament", "white-house", "supreme-court", "legislation",
    "diplomacy", "sanction", "war", "military", "trump", "biden",
)


def political_tag(tags: Any) -> str | None:
    """
    First tag that marks a market as political, else None.

    Substring match on purpose: the wire's tag vocabulary has never been
    recorded (shadow.db `tags` is NULL on every row so far), so an exact
    set would miss `us-politics` or `elections-2028`. A false positive
    costs one skipped market; a false negative breaks Jack's rule.
    """
    for t in tags or []:
        slug = str(t.get("slug") or t.get("label") or "") if isinstance(t, dict) else str(t)
        slug = slug.lower().replace(" ", "-")
        for w in POLITICAL_TAG_WORDS:
            # "war" must not match "warriors" or "award".
            if w == "war":
                if slug == "war" or slug.startswith("war-") or slug.endswith("-war"):
                    return slug
            elif w in slug:
                return slug
    return None


def structural_filter(
    state: dict[str, Any], limits: StructuralLimits = StructuralLimits()
) -> str | None:
    """
    Reject on measurable facts before spending anything at all.

    Returns a rejection reason, or None if the market passes. Runs before
    the Jev call, not after -- a market with a 12-cent spread is
    untradeable regardless of how interesting it is, and that judgment
    costs zero.
    """
    # The exchange's own label. Deterministic, so it is code and not a
    # question; the politics_or_government Noul is the second layer, for
    # political markets filed under some other tag.
    banned = political_tag(state.get("tags"))
    if banned:
        return f"political_tag={banned}"

    bid, ask = state.get("best_bid"), state.get("best_ask")
    if bid is None or ask is None:
        return "no_quote"
    if not (0 < bid < ask < 1):
        return f"crossed_or_invalid_book bid={bid} ask={ask}"

    spread = ask - bid
    if spread > limits.max_spread:
        return f"spread={spread:.3f} > {limits.max_spread}"

    mid = (bid + ask) / 2
    if not (limits.min_price <= mid <= limits.max_price):
        return f"mid={mid:.3f} outside [{limits.min_price}, {limits.max_price}]"

    vol = state.get("volume_usd") or 0
    if vol < limits.min_volume_usd:
        return f"volume={vol:.0f} < {limits.min_volume_usd:.0f}"

    liq = state.get("liquidity_usd") or 0
    if liq < limits.min_liquidity_usd:
        return f"liquidity={liq:.0f} < {limits.min_liquidity_usd:.0f}"

    # bid_shares/ask_shares, not bid_depth/ask_depth. The latter count book
    # LEVELS (observed range 1-16) and comparing them to a share floor of
    # 200 rejected 100% of live markets -- invisibly, because the volume
    # check above returned first.
    for side in ("bid_shares", "ask_shares"):
        d = state.get(side)
        if d is not None and d < limits.min_depth_shares:
            return f"{side}={d:,.0f} < {limits.min_depth_shares:,}"

    # Live sports. `period` tells us where the game is; it is a string
    # comparison, so it runs here rather than in a question.
    gs = state.get("game_state")
    if gs == "finished" and not limits.allow_finished_games:
        # The outcome is known and the books know it. Measured on finished
        # college football: spreads of 0.28-0.49, and 0 shares on the ask
        # of the one market quoting near $1.00. There is no settlement-lag
        # trade, only a wide spread and a two-week wait.
        return f"game over (period={state.get('period')}), no edge left"
    if gs == "in_play" and not limits.allow_in_play:
        return f"in play (period={state.get('period')}), not configured for it"

    # TWO CLOCKS. `hours_to_event` is when the outcome is known;
    # `hours_to_close` is when the payout lands, and for a game played
    # today that is ~332h away. Gating research time on the settlement
    # deadline is what made every short-dated market look like a
    # two-week hold.
    # Only when the event is genuinely ahead of us. On a season future
    # `startTime` is when the SEASON began -- 307h in the past for an
    # in-progress MLB series whose market settles 1,149h out. Treating that
    # as a research deadline rejected every futures market as "too soon",
    # which is the same class of mistake as reading the settlement deadline
    # as the event time, in the opposite direction.
    ev_hrs = state.get("hours_to_event")
    research_hrs = (ev_hrs if (ev_hrs is not None and ev_hrs > 0)
                    else state.get("hours_to_close"))
    if research_hrs is not None and research_hrs < limits.min_hours_to_close:
        return f"event in {research_hrs:.1f}h, too soon to research"

    hrs = state.get("hours_to_close")
    if hrs is not None and hrs > limits.max_days_to_close * 24:
        return f"settles in {hrs / 24:.0f}d, capital parked too long"

    return None


# ==========================================================================
# Tier 1 questions -- judgment over unstructured text only
# ==========================================================================
#
# Every question below is answerable from the market's own description and
# title. None of them require news, prices, or outside knowledge.

RESTRICTED_QUESTIONS: dict[str, dict[str, Any]] = {
    # Decomposed from one broad "restricted_domain" question. Each names a
    # distinct way a market could fall in a domain the operator does not
    # trade on. Any one of them firing is disqualifying.
    "federal_policy_outcome": {
        "type": "noul",
        "instructions": {
            "question": (
                "Does resolution depend on a decision, action, or "
                "announcement by the US federal government?"
            ),
            "inspect": "`question`, `outcome`, `event`, `description` and `tags`",
            "focus": (
                "Federal agencies, regulators, Congress, the White House, "
                "or federal courts deciding the outcome."
            ),
        },
        "criteria": {
            "true": {
                "what": "A federal body's action determines resolution",
                "examples": [
                    "Will the FDA approve X by December?",
                    "Will Congress pass the appropriations bill?",
                    "Will the SEC approve a spot ETF?",
                ],
            },
            "false": {
                "what": "Resolution is independent of federal action",
                "not_for": "Markets merely mentioning the US in passing",
                "examples": [
                    "Will Bitcoin close above $120k?",
                    "Will Film X win Best Picture?",
                ],
            },
        },
    },
    "defense_or_military": {
        "type": "noul",
        "instructions": {
            "question": (
                "Does this market concern defense, the military, "
                "intelligence, or an armed conflict?"
            ),
            "inspect": "`question`, `outcome`, `event`, `description` and `tags`",
            "focus": (
                "Armed forces of any nation, defense procurement or budgets, "
                "intelligence services, active or threatened hostilities."
            ),
        },
        "criteria": {
            "true": {
                "what": "Military, intelligence, or armed conflict outcomes",
                "examples": [
                    "Will there be a ceasefire in X by March?",
                    "Will country Y conduct a missile test this quarter?",
                    "Will the defense budget exceed $900B?",
                ],
            },
            "false": {
                "what": "No military, intelligence, or conflict dimension",
                "examples": ["Will the Fed cut rates?", "Who wins the league?"],
            },
        },
    },
    "us_election_or_appointment": {
        "type": "noul",
        "instructions": {
            "question": (
                "Does resolution depend on a US election, nomination, "
                "confirmation, or officeholder's tenure?"
            ),
            "inspect": "`question`, `outcome`, `event`, `description` and `tags`",
        },
        "criteria": {
            "true": {
                "what": "US electoral or appointment outcomes at any level",
                "examples": [
                    "Who wins the 2028 Republican nomination?",
                    "Will Secretary X leave office before June?",
                ],
            },
            "false": {
                "what": "No US election or appointment dimension",
                "not_for": "Elections in other countries",
                "examples": ["Will the UK call a snap election?"],
            },
        },
    },
    # Jack's instruction, 2026-09-21: no politics on Polymarket, at all.
    # Broader than the three above on purpose -- any country, any level,
    # and political figures as subjects. The question above still says
    # foreign elections are "false" for IT; this one catches them.
    "politics_or_government": {
        "type": "noul",
        "instructions": {
            "question": (
                "Is this market about politics, government, or a political "
                "figure, in any country?"
            ),
            "inspect": "`question`, `outcome`, `event`, `description` and `tags`",
            "focus": (
                "Elections, parties, politicians, heads of state, "
                "legislation, government policy, courts ruling on political "
                "matters, referendums, diplomacy, sanctions, and relations "
                "between countries. A politician named in `outcome` counts "
                "even when `question` looks neutral."
            ),
        },
        "criteria": {
            "true": {
                "what": (
                    "A political actor, political process, or government "
                    "decision is the subject or decides the result"
                ),
                "examples": [
                    "Will the UK call a snap election?",
                    "Who will win the French presidential election?",
                    "Will the Prime Minister resign by March?",
                    "What will the President say during the address?",
                    "Will the bill pass the Senate?",
                    "Will country X impose sanctions on country Y?",
                    "TIME Person of the Year, outcome: a sitting head of state",
                ],
            },
            "false": {
                "what": "No political actor, process, or government decision",
                "not_for": (
                    "Sports, entertainment, company results, crypto prices, "
                    "or weather that merely take place in some country"
                ),
                "examples": [
                    "Total points over 47.5",
                    "Will Bitcoin close above $120k?",
                    "Will Film X win Best Picture?",
                    "Will Tesla deliver 500k vehicles this quarter?",
                ],
            },
        },
    },
}

TRACTABILITY_QUESTIONS: dict[str, dict[str, Any]] = {
    # Decomposed from one broad "tractability" score. Each isolates one
    # reason a market might or might not reward research.
    "objective_resolution": {
        "type": "noul",
        "instructions": {
            "question": (
                "Is the resolution condition objective enough that two "
                "careful readers would agree on the outcome?"
            ),
            "inspect": "`description`",
            "focus": (
                "A named, checkable source or an unambiguous numeric "
                "threshold, rather than a judgment call."
            ),
        },
        "criteria": {
            "true": {
                "what": "Names a specific source or a precise threshold",
                "examples": [
                    "Per the official BLS release",
                    "Closing price above $100 on Dec 31",
                ],
            },
            "false": {
                "what": "Requires subjective interpretation to settle",
                "examples": [
                    "Will the policy be considered a success?",
                    "Will the relationship deteriorate significantly?",
                ],
            },
        },
    },
    "self_contained": {
        "type": "noul",
        "instructions": {
            "question": (
                "Can the resolution condition be understood entirely from "
                "the supplied text, without outside context?"
            ),
            "compare": ["`question`", "`description`"],
            "focus": (
                "Undefined terms, unstated baselines, or references to "
                "external documents make this false."
            ),
        },
        "criteria": {
            "true": {"what": "Fully specified in the text provided"},
            "false": {
                "what": "Depends on definitions or documents not included",
                "examples": ["Will the agreed targets be met?"],
            },
        },
    },
    "research_would_help": {
        "type": "score",
        "instructions": {
            "question": (
                "How much could careful public-source research improve an "
                "estimate of this outcome beyond an uninformed guess?"
            ),
            "inspect": "`question` and `description`",
            "focus": (
                "Judge whether relevant evidence plausibly EXISTS in public "
                "sources. Do not judge whether the market has already "
                "priced it -- that is a separate question."
            ),
        },
        "criteria": [
            {
                "what": "Nothing to find; outcome is irreducibly uncertain",
                "signals": [
                    "Coin-flip or near-random",
                    "Depends on one person's undisclosed private decision",
                    "Live sporting event in progress",
                ],
            },
            {
                "what": "Some public evidence exists but is thin or indirect",
                "signals": ["Sparse reporting", "Only loosely related precedent"],
            },
            {
                "what": "Substantial checkable public evidence plausibly exists",
                "signals": [
                    "Published statistics or filings bear directly on it",
                    "A clear historical reference class exists",
                    "Scheduled disclosures precede resolution",
                ],
            },
        ],
    },
    "outcome_type": {
        "type": "choice",
        "instructions": {
            "question": "What kind of process determines this outcome?",
            "focus": "Classify the generating process, not the topic.",
        },
        "criteria": {
            "scheduled_disclosure": {
                "what": "A dated release, report, filing, or official decision",
                "not_for": "Ongoing continuous measures",
                "examples": ["FOMC decision", "quarterly earnings", "CPI print"],
            },
            "continuous_metric": {
                "what": "A price, index, or count crossing a threshold",
                "not_for": "One-off announcements",
                "examples": ["BTC above $120k", "temperature record"],
            },
            "contested_event": {
                "what": "A competition or adversarial process",
                "examples": ["sports result", "award show", "election"],
            },
            "discretionary_action": {
                "what": "An individual or firm choosing to act",
                "examples": ["Will CEO X resign?", "Will Y announce a merger?"],
            },
        },
    },
}

# ==========================================================================
# Tier 1 questions -- sports
# ==========================================================================
#
# The general tractability set cannot be pointed at sports. Its
# `research_would_help` has "Live sporting event in progress" as the
# bottom Score level, so every sports market scores near 0, hits
# floor_research_would_help, and is vetoed. `outcome_type` has no category
# that fits a prop, so everything falls to `contested_event` and takes the
# largest penalty. Sports markets were rejected by design.
#
# These replace the tractability block for sports-tagged markets. The
# restricted questions below are unchanged and always asked -- the
# operator constraint does not care what sport it is.
#
# Nothing here asks about game state. Tier 1 sees only question,
# description, outcome, event and tags; period and score are handled by
# adapters.game_state() in code.

SPORTS_QUESTIONS: dict[str, dict[str, Any]] = {
    # The single best predictor of whether any estimate can beat noise on
    # a prop. "Team total first downs over 23.5" accumulates over ~60
    # plays, so the central limit theorem does most of the work and a
    # small edge in expected pace is recoverable. "First player to score"
    # is one draw from a wide distribution; no research narrows it enough
    # to beat the vig.
    "stat_aggregation": {
        "type": "score",
        "instructions": {
            "question": (
                "Is the quantity being priced an aggregate over many "
                "opportunities, or a single discrete occurrence?"
            ),
            "inspect": "`question` and `outcome`",
            "focus": (
                "Count how many independent chances contribute to the "
                "result. Judge the structure of the quantity, not whether "
                "the line is set well and not who is likely to win."
            ),
        },
        "criteria": [
            {
                "what": "One discrete occurrence; the outcome is a single draw",
                "signals": [
                    "First player to score",
                    "Exact final score or exact margin",
                    "Whether a specific one-off event happens at all",
                ],
            },
            {
                "what": "A small count with few contributing opportunities",
                "signals": [
                    "One player's touchdowns or home runs in a game",
                    "Field goals made by a single kicker",
                ],
            },
            {
                "what": "An aggregate accumulated over many plays or possessions",
                "signals": [
                    "Team total yards, first downs, or total points",
                    "Combined score of both teams",
                    "A count that rises steadily through the game",
                ],
            },
        ],
    },
    # The sports replacement for research_would_help. Not "can this be
    # researched" in the abstract, but whether SCHEDULED, PUBLISHED
    # information bears on this quantity -- the only edge a gatherer with
    # a web search can actually fetch.
    "pregame_information_edge": {
        "type": "score",
        "instructions": {
            "question": (
                "Would published pre-game information plausibly move a "
                "careful estimate of this quantity?"
            ),
            "inspect": "`question`, `description` and `event`",
            "focus": (
                "Injury and inactive reports, confirmed starters, weather "
                "at an outdoor venue, rest days, travel. Judge whether "
                "such information BEARS on this quantity -- not whether "
                "the market has already priced it, which is a separate "
                "judgment and not answerable from this text."
            ),
        },
        "criteria": [
            {
                "what": "No published pre-game information bears on it",
                "signals": [
                    "Depends on in-game randomness alone",
                    "Turns on a single official's discretionary call",
                ],
            },
            {
                "what": "Published information bears on it only indirectly",
                "signals": [
                    "Team-level form is relevant but no specific report is",
                    "Weather matters slightly at an indoor venue",
                ],
            },
            {
                "what": "Scheduled, published information bears on it directly",
                "signals": [
                    "A named starter's availability drives the quantity",
                    "Wind or precipitation at an outdoor kicking prop",
                    "An injury report due before the market closes",
                    "Confirmed lineup or starting pitcher",
                ],
            },
        ],
    },
    "sports_market_type": {
        "type": "choice",
        "instructions": {
            "question": "What kind of quantity does this market price?",
            "focus": (
                "Classify the quantity, not the sport and not the teams."
            ),
        },
        "criteria": {
            "team_aggregate_stat": {
                "what": "A team-level total accumulated over the game",
                "examples": ["Team total first downs over 23.5",
                             "Team total rushing yards over 100.5"],
            },
            "game_aggregate_stat": {
                "what": "A combined total across both teams",
                "examples": ["Total points over 47.5",
                             "Combined runs under 8.5"],
            },
            "player_stat": {
                "what": "A single player's production",
                "examples": ["Passing yards over 249.5",
                             "Player total bases"],
            },
            "game_result": {
                "what": "Who wins, or the margin",
                "examples": ["Moneyline", "Wins by over 5.5"],
            },
            "discrete_event": {
                "what": "A single occurrence that either happens or does not",
                "examples": ["First touchdown scorer",
                             "Any player to hit 2 home runs"],
            },
        },
    },
    # Carried over unchanged: a stat line is about as objective as a
    # market gets, but the question still discriminates on vague props.
    "objective_resolution": TRACTABILITY_QUESTIONS["objective_resolution"],
}

#: Tags that route a market to the sports question set.
SPORTS_TAGS = frozenset({
    "sports", "mlb", "nfl", "nba", "nhl", "baseball", "football",
    "basketball", "hockey", "soccer", "mls", "ufc", "mma", "boxing",
    "tennis", "golf", "pga", "motorsports", "f1", "nascar", "cfb",
    "ncaab", "cricket", "rugby", "esports", "lol",
})


def is_sports_market(market: dict[str, Any]) -> bool:
    """
    Route on tags, in code. Deterministic, so it is not a question.

    Reads the normalized market (or a built state) -- both carry `tags`.
    """
    tags = market.get("tags") or []
    return any(str(t).lower() in SPORTS_TAGS for t in tags)


def tier1_questions_for(market: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    The question set for this market. Restricted questions are always
    asked; only the tractability block swaps.
    """
    block = SPORTS_QUESTIONS if is_sports_market(market) else TRACTABILITY_QUESTIONS
    return {**RESTRICTED_QUESTIONS, **block}


ALL_TIER1_QUESTIONS: dict[str, dict[str, Any]] = {
    **RESTRICTED_QUESTIONS,
    **TRACTABILITY_QUESTIONS,
}


# ==========================================================================
# Thresholds
# ==========================================================================

@dataclass(frozen=True)
class GateThresholds:
    """
    The reviewable constants. Every number here is currently a guess and
    must be calibrated against shadow data before it means anything.
    """

    # --- hard veto --------------------------------------------------
    # Applied to the MAXIMUM of the three restricted questions, not their
    # average: these are alternative routes to the same problem, and one
    # firing is enough. Averaging would let two clean answers dilute one
    # alarming one, which is precisely the wrong behaviour here.
    max_restricted: float = 0.12

    # --- disqualifying floors ---------------------------------------
    floor_objective_resolution: float = 0.45
    floor_research_would_help: float = 0.60     # on the 0-2 scale

    # --- sports composite -------------------------------------------
    # Used instead of the three weights below when the market is sports.
    # stat_aggregation carries the most weight because it is the judgment
    # that decides whether an estimate can beat variance at all.
    #
    # self_contained is deliberately absent: a stat line is self-contained
    # essentially always, so including it would add a constant rather than
    # information.
    #
    # Starting points, no evidence behind them -- same status as every
    # other number here until something scores against a resolved outcome.
    floor_stat_aggregation: float = 0.60          # on the 0-2 scale
    w_stat_aggregation: float = 0.45
    w_pregame_information_edge: float = 0.35
    w_objective_resolution_sports: float = 0.20

    sports_market_type_penalty: dict[str, float] = None  # __post_init__

    # --- the composite gate -----------------------------------------
    min_gate_score: float = 0.60

    w_research_would_help: float = 0.45
    w_objective_resolution: float = 0.30
    w_self_contained: float = 0.25

    # Outcome types that historically reward research, applied as a
    # SUBTRACTED penalty, not a multiplier. A contested event is not
    # disqualifying -- it is simply a harder place to find edge.
    #
    # Why additive: as a multiplier this compounded with the confidence
    # scale, and since `raw` is bounded at 1.0 the product could not reach
    # min_gate_score at all for two of the four types. contested_event
    # required confidence >= 1.00 exactly, so it could never escalate --
    # the same silent-AND failure as the six ANDed gates, and equally
    # invisible to a sweep of min_gate_score. Subtracting keeps every type
    # reachable and keeps one threshold rather than four.
    #
    # These four numbers preserve the ordering of the multipliers they
    # replace. They are not evidence-based and want revisiting once
    # resolved outcomes exist.
    outcome_type_penalty: dict[str, float] = None  # set in __post_init__

    # Minimum Score confidence on research_would_help, below which the
    # market is skipped rather than scored down. TypeSafe's guidance is
    # that low Score confidence means the levels were ambiguous or the
    # state was insufficient -- a routing signal ("do not act"), not a
    # magnitude. Scaling the composite by it, as the previous version did,
    # systematically suppressed exactly the markets triage could not read,
    # and dragged every score down because Tier 1 sends deliberately
    # minimal state.
    #
    # Deliberately 0.0 -- NON-BINDING. shadow.py already records
    # research_confidence per market, so set this from the observed
    # histogram after a collection run rather than guessing it now. A
    # guessed value here would silently filter markets before there is
    # any data showing where the cut belongs.
    min_research_confidence: float = 0.0

    def __post_init__(self) -> None:
        if self.sports_market_type_penalty is None:
            object.__setattr__(
                self,
                "sports_market_type_penalty",
                {
                    # Subtracted, never multiplied -- see the note on
                    # outcome_type_penalty. Every type stays reachable:
                    # the worst ceiling is 0.75 against a 0.60 threshold.
                    "team_aggregate_stat": 0.00,
                    "game_aggregate_stat": 0.00,
                    "player_stat": 0.08,
                    "game_result": 0.10,
                    "discrete_event": 0.25,
                },
            )
        if self.outcome_type_penalty is None:
            object.__setattr__(
                self,
                "outcome_type_penalty",
                {
                    "scheduled_disclosure": 0.00,
                    "discretionary_action": 0.05,
                    "continuous_metric": 0.08,
                    "contested_event": 0.20,
                },
            )
