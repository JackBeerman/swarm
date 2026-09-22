"""
Pydantic contracts for the Polymarket US trading swarm.

Design rule carried through this module: models return *judgments*, code
computes *actions*. No LLM is ever asked for a dollar amount or a share
count -- it returns a probability and a confidence, and risk_engine turns
those into size. This is deliberate; see size_from_signal() in swarm.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class GatherRole(str, Enum):
    SLEUTH = "sleuth"          # what happened, what's the catalyst
    QUANT = "quant"            # base rates, historical precedent
    RED_TEAM = "red_team"      # why is the market right and we're wrong


# --------------------------------------------------------------------------
# Tier 1 -- Jev triage
# --------------------------------------------------------------------------

class TriageVerdict(BaseModel):
    """
    Output of the Jev System One call plus the deterministic filters.

    One field per atomic question. The decomposition is the point: when a
    market is rejected you can see which single judgment did it, and
    reweight that one signal rather than guessing at a blended score.
    """

    market_slug: str

    # --- restricted domain (any one firing is disqualifying) --------
    federal_policy_outcome: float = Field(0.0, ge=0.0, le=1.0)
    defense_or_military: float = Field(0.0, ge=0.0, le=1.0)
    us_election_or_appointment: float = Field(0.0, ge=0.0, le=1.0)
    politics_or_government: float = Field(0.0, ge=0.0, le=1.0)

    # --- tractability ------------------------------------------------
    objective_resolution: float = Field(0.0, ge=0.0, le=1.0)
    self_contained: float = Field(0.0, ge=0.0, le=1.0)
    research_would_help: float = Field(0.0, ge=0.0, le=2.0)
    research_confidence: float = Field(0.0, ge=0.0, le=1.0)

    outcome_type: str = "unknown"
    outcome_type_confidence: float = Field(0.0, ge=0.0, le=1.0)

    # --- sports tractability (asked instead of the above on sports) --
    # Defaults of 0.0 mean "not asked", which is why is_sports below
    # carries the routing rather than inferring it from these.
    stat_aggregation: float = Field(0.0, ge=0.0, le=2.0)
    stat_aggregation_confidence: float = Field(0.0, ge=0.0, le=1.0)
    pregame_information_edge: float = Field(0.0, ge=0.0, le=2.0)
    pregame_information_edge_confidence: float = Field(0.0, ge=0.0, le=1.0)
    sports_market_type: str = "unknown"
    sports_market_type_confidence: float = Field(0.0, ge=0.0, le=1.0)
    is_sports: bool = False

    # --- decisions ---------------------------------------------------
    structural_reject: str | None = None
    escalate: bool = False
    veto_reason: str | None = None
    gate_score: float = Field(0.0, ge=0.0, le=1.0)

    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def restricted_max(self) -> float:
        """Max, not mean: alternative routes to the same problem."""
        return max(
            self.federal_policy_outcome,
            self.defense_or_military,
            self.us_election_or_appointment,
            self.politics_or_government,
        )


# --------------------------------------------------------------------------
# Tier 2 -- gatherer compression
# --------------------------------------------------------------------------

class MarketFactSummary(BaseModel):
    """
    Hard compression boundary. Raw HTML, order book dumps and model
    chit-chat die here; only this crosses into Tier 3.
    """

    # Length limits TRUNCATE; they do not reject. On the first paper run
    # (2026-09-21) four of five evaluations were discarded because a
    # gatherer wrote 450 characters into a 400-character field or a
    # seventh key fact. The research was already paid for. A boundary
    # that throws away what crossed it is a leak, not a boundary.
    role: GatherRole
    identified_catalyst: str
    historical_precedent: str
    key_facts: list[str] = Field(default_factory=list)
    contradicting_evidence: str | None = None
    data_confidence: float = Field(..., ge=0.0, le=1.0)
    sources: list[str] = Field(default_factory=list)

    @field_validator("identified_catalyst", "historical_precedent",
                     "contradicting_evidence", mode="before")
    @classmethod
    def _clip_text(cls, v):
        return v[:400] if isinstance(v, str) else v

    @field_validator("key_facts", "sources", mode="before")
    @classmethod
    def _clip_list(cls, v):
        if not isinstance(v, list):
            return v
        return [str(x)[:200] for x in v[:6]]


# --------------------------------------------------------------------------
# Tier 3 -- synthesis
# --------------------------------------------------------------------------

class TradeSignal(BaseModel):
    """
    What Astra returns. Note what is NOT here: quantity, dollar amount,
    Kelly fraction. Those are computed downstream from `probability`.
    """

    market_slug: str
    side: Side
    probability: float = Field(
        ..., ge=0.0, le=1.0,
        description="P(the chosen side resolves true), post-aggregation.",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Astra's own calibration on `probability`. Modulates size.",
    )
    reasoning: str
    disqualifiers: list[str] = Field(default_factory=list)
    abstain: bool = False

    @field_validator("reasoning", mode="before")
    @classmethod
    def _clip_reasoning(cls, v):
        return v[:800] if isinstance(v, str) else v

    @field_validator("disqualifiers", mode="before")
    @classmethod
    def _clip_disqualifiers(cls, v):
        return [str(x)[:200] for x in v[:4]] if isinstance(v, list) else v


class SizedOrder(BaseModel):
    """Code-computed. This is the only thing execution is allowed to act on."""

    market_slug: str
    side: Side
    limit_price: float = Field(..., gt=0.0, lt=1.0)
    quantity: int = Field(..., gt=0, description="Whole shares; the exchange accepts int only.")
    notional_usd: float = Field(..., gt=0.0)

    raw_kelly: float
    applied_fraction: float
    edge: float
    bankroll_at_size: float

    signal: TradeSignal


# --------------------------------------------------------------------------
# Pipeline bookkeeping
# --------------------------------------------------------------------------

class StageCost(BaseModel):
    stage: Literal["triage", "gather", "synthesize"]
    model: str
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class PipelineResult(BaseModel):
    market_slug: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    triage: TriageVerdict | None = None
    facts: list[MarketFactSummary] = Field(default_factory=list)
    signal: TradeSignal | None = None
    order: SizedOrder | None = None
    costs: list[StageCost] = Field(default_factory=list)
    halted_at: str | None = None

    @property
    def total_cost_usd(self) -> float:
        return sum(c.usd for c in self.costs)
