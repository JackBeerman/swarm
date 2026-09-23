"""
models.py -- which model does which job, and what each one costs.

Every LLM role in the swarm has a Route: a primary model and an
ordered list of fallbacks. Every model is a row in MODELS: its litellm id,
price, context limit, whether it accepts `temperature`, whether it can call
tools, whether it can search the web by itself, and how reliably it has
returned schema-valid JSON *in this repo* (not on a leaderboard).

Nothing here changes behaviour by default. The shipped profile is
`default`, which is today's routing exactly: Tier 2 gatherers on
TIER2_MODEL (Claude Haiku 4.5), Tier 3 on TIER3_MODEL (Claude Opus 5.5),
no fallbacks. swarm.py asks `resolve_model(role, default)` and, with no
routing configuration set, gets back the `default` it passed in. See
docs/ROUTING.md.

Overrides, most specific first:

    SWARM_ROUTE_<ROLE>=model[,fallback,...]    one role, from the environment
    SWARM_ROUTING_FILE=routing.toml            a file (see routing.example.toml)
    SWARM_ROUTING_PROFILE=recommended          a built-in profile

Hard rule, unchanged: a model here returns probability and confidence, or
facts, or text for a human. It never returns a size. Nothing in this module
touches the order path.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("models")

JsonReliability = Literal["measured_ok", "measured_flaky", "unmeasured"]

#: Every role that calls an LLM, or will. The last three are not in the
#: slow lane: the brief writer is fastlane.build_brief (not wired through
#: here yet), and the two drafters are designed in docs/BRIEF.md, not built.
ROLES: tuple[str, ...] = (
    "gatherer_sleuth",
    "gatherer_historian",     # GatherRole.QUANT: base rates, precedent
    "gatherer_red_team",
    "synthesis",
    "brief_writer",
    "lesson_drafter",
    "question_drafter",
)

#: Roles whose default follows TIER2_MODEL / TIER3_MODEL, as swarm.py does.
_TIER2_ROLES = ("gatherer_sleuth", "gatherer_historian", "gatherer_red_team")


@dataclass(frozen=True)
class ModelSpec:
    """
    One model as litellm reaches it.

    Prices are USD per million tokens at the route named in `source`, on
    `priced_on`. They are the ledger's fallback when litellm cannot price a
    response (a model newer than its cost map returns $0.00, and a $0.00
    spend never trips the kill switch). OpenRouter prices vary by upstream
    provider; the figure here is the one the route below is pinned to, or
    the list price where it is not.
    """

    id: str                               # litellm model string
    provider: str                         # anthropic | openrouter | groq | together_ai | ollama
    usd_in_mtok: float
    usd_out_mtok: float
    context: int                          # input tokens
    accepts_temperature: bool             # Claude 4.6+ return 400 if it is sent
    tool_calling: bool                    # OpenAI-style function tools via litellm
    native_web_search: bool = False       # can search without search.py
    open_weights: bool = False
    json_reliability: JsonReliability = "unmeasured"
    env_key: str = ""                     # the key litellm needs
    #: Extra litellm kwargs, e.g. OpenRouter provider routing. Kept here so
    #: a quantised or flaky upstream is excluded by config, not by luck.
    extra: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    priced_on: str = ""
    notes: str = ""

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.usd_in_mtok
                + output_tokens * self.usd_out_mtok) / 1_000_000


@dataclass(frozen=True)
class Route:
    role: str
    primary: str
    fallbacks: tuple[str, ...] = ()

    @property
    def chain(self) -> tuple[str, ...]:
        return (self.primary, *self.fallbacks)


# --------------------------------------------------------------------------
# The model table
# --------------------------------------------------------------------------

_OR = "https://openrouter.ai/api/v1/models"
#: OpenRouter's cheapest upstreams for open models are often fp4. JSON and
#: tool-call reliability is the whole job for a gatherer, so pin to fp8 or
#: better and let OpenRouter pick among those. Unverified that fp4 is worse
#: *here*; the bench can say.
_NO_FP4 = {"extra_body": {"provider": {"quantizations": ["fp8", "bf16", "fp16"],
                                       "require_parameters": True}}}

_TABLE: tuple[ModelSpec, ...] = (
    # ---- Anthropic, direct (today's routing) ---------------------------
    ModelSpec(
        "anthropic/claude-haiku-4-5", "anthropic", 1.00, 5.00, 200_000,
        accepts_temperature=True, tool_calling=True, native_web_search=True,
        json_reliability="measured_flaky", env_key="ANTHROPIC_API_KEY",
        source="claude-api model table; litellm cost map", priced_on="2026-09-22",
        notes="Tier 2 today. traces.db: 2 of 9 evaluations halted on "
              "insufficient_gatherer_output (before the truncate-not-reject "
              "schema fix). Server web search via search.py, $10/1k searches."),
    ModelSpec(
        "anthropic/claude-opus-5-5", "anthropic", 4.00, 20.00, 1_000_000,
        accepts_temperature=False, tool_calling=True, native_web_search=True,
        json_reliability="measured_flaky", env_key="ANTHROPIC_API_KEY",
        source="claude-api model table; litellm cost map", priced_on="2026-09-22",
        notes="Tier 3 today. Thinking cannot be disabled; effort defaults to "
              "medium. traces.db: 2 of 7 synthesis calls with >=2 reports "
              "halted synthesis_failed (cause not recorded)."),
    ModelSpec(
        "anthropic/claude-sonnet-5", "anthropic", 2.00, 10.00, 1_000_000,
        accepts_temperature=False, tool_calling=True, native_web_search=True,
        env_key="ANTHROPIC_API_KEY",
        source="claude-api model table", priced_on="2026-09-22",
        notes="Half Opus 5.5's price. Candidate challenger for synthesis; "
              "no evidence yet."),

    # ---- Open weights via OpenRouter -----------------------------------
    ModelSpec(
        "openrouter/deepseek/deepseek-v4.1-flash", "openrouter", 0.14, 0.42, 1_048_576,
        accepts_temperature=True, tool_calling=True, open_weights=True,
        env_key="OPENROUTER_API_KEY", extra=_NO_FP4,
        source=_OR + " (DeepInfra fp8 endpoint)", priced_on="2026-09-22",
        notes="deepseek-ai/DeepSeek-V4.1-Flash. List price $0.10/$0.50 is an "
              "fp4 upstream; Together lists $0.30/$1.20. 1M context."),
    ModelSpec(
        "openrouter/openai/gpt-oss-120b", "openrouter", 0.15, 0.60, 131_072,
        accepts_temperature=True, tool_calling=True, open_weights=True,
        env_key="OPENROUTER_API_KEY", extra=_NO_FP4,
        source=_OR, priced_on="2026-09-22",
        notes="Apache-2.0 weights; 24 upstreams incl. Groq and Cerebras. "
              "bf16 upstreams from $0.03/$0.17."),
    ModelSpec(
        "openrouter/z-ai/glm-5.3-flash", "openrouter", 0.15, 0.50, 1_048_576,
        accepts_temperature=True, tool_calling=True, open_weights=True,
        env_key="OPENROUTER_API_KEY", extra=_NO_FP4,
        source=_OR, priced_on="2026-09-22",
        notes="zai-org/GLM-5.3-Flash. Together lists the same $0.15/$0.50."),
    ModelSpec(
        "openrouter/qwen/qwen3.8-flash", "openrouter", 0.15, 0.47, 1_000_000,
        accepts_temperature=True, tool_calling=True, open_weights=True,
        env_key="OPENROUTER_API_KEY",
        source=_OR, priced_on="2026-09-22",
        notes="Single upstream (Alibaba) on OpenRouter; Together lists "
              "$0.09/$0.28."),
    ModelSpec(
        "openrouter/deepseek/deepseek-v4-pro-0813", "openrouter", 0.99, 2.97, 1_048_576,
        accepts_temperature=True, tool_calling=True, open_weights=True,
        env_key="OPENROUTER_API_KEY", extra=_NO_FP4,
        source=_OR + " (Novita fp8 endpoint)", priced_on="2026-09-22",
        notes="Large reasoning model. Synthesis challenger at ~1/5 of "
              "Opus 5.5. Together lists $1.32/$3.96."),
    ModelSpec(
        "openrouter/z-ai/glm-5.3", "openrouter", 0.84, 2.64, 1_048_576,
        accepts_temperature=True, tool_calling=True, open_weights=True,
        env_key="OPENROUTER_API_KEY", extra=_NO_FP4,
        source=_OR, priced_on="2026-09-22",
        notes="Synthesis challenger. Together lists $1.40/$4.40."),
    ModelSpec(
        "openrouter/moonshotai/kimi-k3", "openrouter", 3.00, 15.00, 1_048_576,
        accepts_temperature=True, tool_calling=True, open_weights=True,
        env_key="OPENROUTER_API_KEY",
        source=_OR, priced_on="2026-09-22",
        notes="2.8T open weights; near Opus price, so only worth it on evidence."),
    ModelSpec(
        "openrouter/anthropic/claude-haiku-4.5", "openrouter", 1.00, 5.00, 200_000,
        accepts_temperature=True, tool_calling=True, native_web_search=True,
        env_key="OPENROUTER_API_KEY",
        source=_OR, priced_on="2026-09-22",
        notes="Same model through OpenRouter: a fallback when Anthropic is down."),

    # ---- Direct fast inference ------------------------------------------
    ModelSpec(
        "groq/openai/gpt-oss-120b", "groq", 0.15, 0.60, 131_072,
        accepts_temperature=True, tool_calling=True, native_web_search=True,
        open_weights=True, env_key="GROQ_API_KEY",
        source="https://console.groq.com/docs/models", priced_on="2026-09-22",
        notes="~500 tok/s. Groq's browser_search tool exists for gpt-oss but "
              "is incompatible with structured outputs and its price is "
              "unverified; the swarm does not use it."),

    # ---- Local ----------------------------------------------------------
    ModelSpec(
        "ollama/qwen3.8:27b", "ollama", 0.0, 0.0, 131_072,
        accepts_temperature=True, tool_calling=True, open_weights=True,
        source="local", priced_on="2026-09-22",
        notes="Qwen/Qwen3.8-27B on local hardware. $0 marginal, needs a GPU "
              "with ~20 GB at q4. Context set by the Ollama model file."),
)

MODELS: dict[str, ModelSpec] = {m.id: m for m in _TABLE}


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

def _default_routes() -> dict[str, Route]:
    """Today's behaviour, exactly. Read at call time so TIER2/3 env wins."""
    t2 = os.getenv("TIER2_MODEL", "anthropic/claude-haiku-4-5")
    t3 = os.getenv("TIER3_MODEL", "anthropic/claude-opus-5-5")
    search = "anthropic/" + os.getenv("SEARCH_MODEL", "claude-haiku-4-5")
    r = {role: Route(role, t2) for role in _TIER2_ROLES}
    r["synthesis"] = Route("synthesis", t3)
    r["brief_writer"] = Route("brief_writer", search)
    # Not built yet. Human-reviewed text; defaults to the Tier 3 model.
    r["lesson_drafter"] = Route("lesson_drafter", t3)
    r["question_drafter"] = Route("question_drafter", t3)
    return r


#: The proposal in docs/ROUTING.md. NOT active unless selected. Synthesis
#: stays on Opus 5.5: there is no evidence yet that anything cheaper is as
#: calibrated, and synthesis is the only model-written probability in the
#: slow lane.
_RECOMMENDED: dict[str, tuple[str, ...]] = {
    "gatherer_sleuth": ("openrouter/deepseek/deepseek-v4.1-flash",
                        "openrouter/z-ai/glm-5.3-flash",
                        "anthropic/claude-haiku-4-5"),
    "gatherer_historian": ("openrouter/deepseek/deepseek-v4.1-flash",
                           "openrouter/openai/gpt-oss-120b",
                           "anthropic/claude-haiku-4-5"),
    "gatherer_red_team": ("openrouter/z-ai/glm-5.3-flash",
                          "openrouter/deepseek/deepseek-v4.1-flash",
                          "anthropic/claude-haiku-4-5"),
    "synthesis": ("anthropic/claude-opus-5-5",
                  "openrouter/anthropic/claude-haiku-4.5"),
    "brief_writer": ("anthropic/claude-haiku-4-5",),
    "lesson_drafter": ("openrouter/deepseek/deepseek-v4.1-flash",
                       "anthropic/claude-sonnet-5"),
    "question_drafter": ("anthropic/claude-opus-5-5",),
}


def _profile(name: str) -> dict[str, Route]:
    if name == "default":
        return _default_routes()
    if name == "recommended":
        return {role: Route(role, chain[0], tuple(chain[1:]))
                for role, chain in _RECOMMENDED.items()}
    raise ValueError(f"unknown routing profile {name!r} (default | recommended)")


def _parse_chain(value: str) -> tuple[str, ...]:
    chain = tuple(s.strip() for s in value.split(",") if s.strip())
    if not chain:
        raise ValueError("empty model chain")
    return chain


def _load_file(path: str) -> dict[str, tuple[str, ...]]:
    """
    [roles.synthesis]
    primary = "anthropic/claude-opus-5-5"
    fallbacks = ["anthropic/claude-sonnet-5"]
    """
    with open(Path(path), "rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, tuple[str, ...]] = {}
    for role, spec in (data.get("roles") or {}).items():
        if role not in ROLES:
            raise ValueError(f"{path}: unknown role {role!r}")
        if not isinstance(spec, dict) or not spec.get("primary"):
            raise ValueError(f"{path}: roles.{role} needs a primary")
        out[role] = (str(spec["primary"]),
                     *(str(f) for f in spec.get("fallbacks") or ()))
    return out


def routes() -> dict[str, Route]:
    """
    The effective routing table, after every override.

    Raises ValueError on a malformed override. A typo in routing config
    must not silently fall back to some other model with other costs.
    """
    table = _profile(os.getenv("SWARM_ROUTING_PROFILE", "default").strip() or "default")
    path = os.getenv("SWARM_ROUTING_FILE", "").strip()
    if path:
        for role, chain in _load_file(path).items():
            table[role] = Route(role, chain[0], chain[1:])
    for role in ROLES:
        raw = os.getenv(f"SWARM_ROUTE_{role.upper()}", "").strip()
        if raw:
            chain = _parse_chain(raw)
            table[role] = Route(role, chain[0], chain[1:])
    return table


def is_configured() -> bool:
    """True when any routing override is set."""
    return bool(
        os.getenv("SWARM_ROUTING_PROFILE", "default").strip() not in ("", "default")
        or os.getenv("SWARM_ROUTING_FILE", "").strip()
        or any(os.getenv(f"SWARM_ROUTE_{r.upper()}", "").strip() for r in ROLES)
    )


def route(role: str) -> Route:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    return routes()[role]


def resolve_model(role: str, default: str) -> str:
    """
    The hook swarm.py calls. With no routing configuration it returns
    `default` untouched -- which is what makes the shipped behaviour
    identical to the code before this module existed.
    """
    if not is_configured():
        return default
    return route(role).primary


# --------------------------------------------------------------------------
# Per-model helpers
# --------------------------------------------------------------------------

def spec(model: str) -> ModelSpec | None:
    """Registry entry for a litellm id; tolerant of '.' vs '-' in versions."""
    if model in MODELS:
        return MODELS[model]
    norm = model.replace(".", "-")
    for k, v in MODELS.items():
        if k.replace(".", "-") == norm:
            return v
    return None


def price_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Registry price, 0.0 for a model not in the table."""
    s = spec(model)
    return s.cost_usd(input_tokens, output_tokens) if s else 0.0


def sampling_kwargs(model: str, temperature: float) -> dict[str, float]:
    """
    Registry first; swarm.sampling_kwargs' name heuristic for anything
    unlisted. The two must agree on every listed Claude model (tested).
    """
    s = spec(model)
    if s is not None:
        return {"temperature": temperature} if s.accepts_temperature else {}
    from swarm import sampling_kwargs as heuristic   # lazy: swarm imports us

    return heuristic(model, temperature)


def call_kwargs(model: str) -> dict[str, Any]:
    """Extra litellm kwargs a model needs (provider routing, api_base)."""
    s = spec(model)
    return dict(s.extra) if s else {}


def missing_keys(models: list[str] | tuple[str, ...]) -> list[str]:
    """Env vars a set of models needs and the environment lacks. Names only."""
    need = {s.env_key for m in models if (s := spec(m)) and s.env_key}
    return sorted(k for k in need if not os.getenv(k))


async def complete(role: str, messages: list[dict[str, Any]], *,
                   temperature: float = 0.0, chain: tuple[str, ...] | None = None,
                   **kw: Any) -> tuple[Any, str]:
    """
    Call a role's chain in order; return (response, model_used).

    Used by tools/bench_roles.py. Not wired into swarm.py: the pipeline
    keeps its own single call and its own failure handling, so enabling
    fallbacks there is a separate, reviewed change (docs/ROUTING.md).
    """
    from litellm import acompletion

    models = chain or route(role).chain
    last: Exception | None = None
    for model in models:
        try:
            resp = await acompletion(
                model=model, messages=messages,
                **sampling_kwargs(model, temperature), **call_kwargs(model), **kw)
            return resp, model
        except Exception as exc:  # noqa: BLE001 -- next in chain
            log.warning("%s on %s failed: %s", role, model, exc)
            last = exc
    raise RuntimeError(f"every model in the {role} chain failed") from last

