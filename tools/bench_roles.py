"""
bench_roles.py -- replay stored inputs through candidate models, per role.

    python tools/bench_roles.py --dry-run                         # free: plan + stored-output scores
    python tools/bench_roles.py --roles synthesis --limit 3 \\
        --models anthropic/claude-opus-5-5 --max-usd 0.30         # paid, capped

What it replays, and through what:

| role               | inputs                                           | code path                     |
| ------------------ | ------------------------------------------------ | ----------------------------- |
| gatherer_*         | traces.db market + a FROZEN search corpus        | swarm._run_gatherer (real)    |
| synthesis          | traces.db market + the facts_json Tier 3 saw     | swarm._synthesize (real)      |
| brief_writer       | fastlane.db briefs: event, markets, frozen facts | fastlane prompt, search split |
| lesson_drafter     | fastlane.db signals, grouped by event            | prompt here, schema here      |
| question_drafter   | a small inline fixture                           | prompt here, schema here      |

The frozen corpus matters. The web search a gatherer ran is not stored,
only what it concluded, so a replay cannot re-see the same pages. Every
candidate gets the SAME corpus (built from the stored gatherer outputs for
that market) from a search function that ignores the query. That makes the
comparison fair on protocol -- does the model call the tool, and return
JSON that validates -- and says nothing about search quality, which stays
with search.py whatever model writes the summary.

Scores: schema validity (schemas.MarketFactSummary, schemas.TradeSignal,
fastlane._clean_brief, the drafter schemas below), latency, cost, and for
synthesis a Brier score against resolved outcomes beside the market mid's
Brier on the same rows. Few traces are resolved; the report says how many.

Spend: every paid call is pre-estimated from the registry price and skipped
if it would take the run past --max-usd. --dry-run imports no LLM client
and makes no network call at all. Never touches the order path: nothing
here sizes, and nothing imports daemon.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TYPESAFE_API_KEY", "bench-unused")   # swarm reads it at import

import models  # noqa: E402
from schemas import GatherRole, MarketFactSummary  # noqa: E402

log = logging.getLogger("bench")

GATHER_ROLES = {
    "gatherer_sleuth": GatherRole.SLEUTH,
    "gatherer_historian": GatherRole.QUANT,
    "gatherer_red_team": GatherRole.RED_TEAM,
}

#: Output tokens assumed when pre-estimating a call's cost. Generous on
#: purpose: the cap must hold even when a thinking model thinks.
EXPECTED_OUT = {"gather": 2 * 600, "synthesis": 2500, "brief_writer": 2500,
                "lesson_drafter": 900, "question_drafter": 900}


# --------------------------------------------------------------------------
# Drafter schemas -- the drafters are designed (docs/BRIEF.md), not built.
# A draft is text for a human; it carries no probability and no size.
# --------------------------------------------------------------------------

class LessonDraft(BaseModel):
    family: str
    lessons: str = Field(..., max_length=900)
    evidence: list[str] = Field(..., min_length=1)
    n_events: int = Field(..., ge=0)
    is_hypothesis: bool


class ProbeCase(BaseModel):
    state_hint: str
    expected: str


class QuestionDraft(BaseModel):
    question_id: str
    proposed_text: str
    rationale: str
    probe_cases: list[ProbeCase] = Field(..., min_length=2)


# --------------------------------------------------------------------------
# Loading stored inputs (read-only)
# --------------------------------------------------------------------------

def _ro(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_traces(path: str) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    with _ro(path) as conn:
        rows = conn.execute("SELECT * FROM evaluations ORDER BY id").fetchall()
    out = []
    for r in rows:
        facts = json.loads(r["facts_json"] or "[]")
        bid, ask = r["bid"], r["ask"]
        state = {
            "question": r["question"], "description": r["description"],
            "outcome": r["outcome"], "event": r["event_title"],
            "tags": json.loads(r["tags"] or "[]"), "slug": r["market_slug"],
            "best_bid": bid, "best_ask": ask,
            "spread": None if bid is None or ask is None else round(ask - bid, 4),
        }
        out.append({
            "id": f"trace-{r['id']}", "state": state, "facts": facts,
            "resolved": r["resolved_outcome"], "bid": bid, "ask": ask,
            "stored_signal": (r["signal_side"], r["signal_prob"]),
            "halted_at": r["halted_at"], "cost_usd": r["cost_usd"],
        })
    return out


def frozen_corpus(facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What the stored gatherers found, as search results. Same for every model."""
    items: list[dict[str, str]] = []
    for f in facts:
        srcs = f.get("sources") or [""]
        texts = [f.get("identified_catalyst"), f.get("historical_precedent"),
                 *(f.get("key_facts") or [])]
        for i, t in enumerate(x for x in texts if x):
            items.append({"title": f"stored {f.get('role')} finding",
                          "url": str(srcs[i % len(srcs)]), "snippet": str(t)[:400]})
    return items


def load_briefs(path: str) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    with _ro(path) as conn:
        briefs = conn.execute("SELECT * FROM briefs ORDER BY id").fetchall()
        sig = conn.execute("SELECT DISTINCT market_slug, question, outcome FROM signals").fetchall()
    known = {r["market_slug"]: r for r in sig}
    out = []
    for b in briefs:
        brief = json.loads(b["brief_json"])
        prices: dict[str, list[float]] = {}
        for sc in brief.get("scenarios") or []:
            for k, v in (sc.get("affects") or {}).items():
                prices.setdefault(k, []).append(float(v))
        # The prompt's markets were not stored. Rebuild them: question text
        # from signals where the market was watched, else the slug; price as
        # the median of the brief's own scenario prices (the prompt told the
        # writer to repeat the current price for unaffected markets).
        markets, quotes = [], {}
        for k, ps in prices.items():
            q = known.get(k)
            markets.append({"slug": k, "question": q["question"] if q else k,
                            "outcome": q["outcome"] if q else ""})
            mid = statistics.median(ps)
            quotes[k] = {"bid": mid, "ask": mid}
        out.append({"id": f"brief-{b['id']}", "event": b["event_slug"], "stored": brief,
                    "markets": markets, "quotes": quotes,
                    "sport": b["event_slug"].split("-")[0] in ("mlb", "nfl", "nba", "nhl"),
                    "corpus": [{"title": "stored brief fact", "url": "", "snippet": f}
                               for f in brief.get("facts") or []]})
    return out


def load_lesson_items(path: str) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    with _ro(path) as conn:
        rows = conn.execute(
            """SELECT event_slug, market_slug, effect, effect_conf, acted, scenario,
                      fair_yes, bid0, ask0, mid_1m, mid_5m, mid_30m
               FROM signals WHERE acted = 1 ORDER BY event_slug, id""").fetchall()
    by_family: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        fam = (r["event_slug"] or "unknown").split("-")[0]
        by_family.setdefault(fam, []).append(dict(r))
    return [{"id": f"lessons-{fam}", "family": fam, "records": recs[:40],
             "n_events": len({x["event_slug"] for x in recs})}
            for fam, recs in by_family.items()]


QUESTION_FIXTURES = [
    {"id": "q-research_would_help", "question_id": "research_would_help",
     "current": "Would public research plausibly change an informed estimate of "
                "this market's probability?",
     "audit": "Asked on general markets only. On 830 triage verdicts it is the "
              "weakest Tier 1 signal: Jev judges whether public evidence exists "
              "for a market it knows nothing about. Near-constant on sports-like "
              "wording."},
    {"id": "q-stat_aggregation", "question_id": "stat_aggregation",
     "current": "Is the outcome decided by one play, or by many aggregated events?",
     "audit": "Sports block. sd 0.10 on a 0-2 scale over real markets: a constant "
              "with a weight on it."},
]


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass
class Result:
    role: str
    model: str
    item: str
    valid: bool = False
    error: str = ""
    latency_s: float = 0.0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class _LastWarning(logging.Handler):
    """swarm swallows gatherer/synthesis errors into a log line; keep it."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.last = ""

    def emit(self, record: logging.LogRecord) -> None:
        self.last = record.getMessage()[:300]


class Budget:
    def __init__(self, cap: float) -> None:
        self.cap, self.spent, self.skipped = cap, 0.0, 0

    def allow(self, model: str, prompt_chars: int, out_tokens: int) -> bool:
        s = models.spec(model)
        if s is None:
            log.warning("%s is not in the registry; refusing to spend on an unpriced model",
                        model)
            self.skipped += 1
            return False
        est = s.cost_usd(prompt_chars // 3, out_tokens)
        if self.spent + est > self.cap:
            self.skipped += 1
            return False
        return True


def _with_route(role: str, model: str):
    """Point one role at one model for the duration of a call."""
    key = f"SWARM_ROUTE_{role.upper()}"

    class _Ctx:
        def __enter__(self):
            self.old = os.environ.get(key)
            os.environ[key] = model

        def __exit__(self, *a):
            if self.old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = self.old

    return _Ctx()


# --------------------------------------------------------------------------
# Runners (paid)
# --------------------------------------------------------------------------

async def run_gatherer(role: str, model: str, item: dict[str, Any], budget: Budget) -> Result:
    import swarm as sw

    res = Result(role, model, item["id"])
    corpus = frozen_corpus(item["facts"])
    prompt_chars = len(json.dumps(item["state"])) + len(json.dumps(corpus)) + 2500
    if not budget.allow(model, prompt_chars, EXPECTED_OUT["gather"]):
        res.error = "skipped: budget"
        return res
    searched = {"n": 0}

    async def search(query: str) -> list[dict[str, str]]:
        searched["n"] += 1
        return corpus

    grab = _LastWarning()
    logging.getLogger("swarm").addHandler(grab)
    costs: list = []
    t0 = time.perf_counter()
    try:
        with _with_route(role, model):
            out = await sw._run_gatherer(GATHER_ROLES[role], item["state"], search, costs)
    finally:
        logging.getLogger("swarm").removeHandler(grab)
    res.latency_s = time.perf_counter() - t0
    res.cost_usd = sum(c.usd for c in costs)
    res.tokens_in = sum(c.input_tokens for c in costs)
    res.tokens_out = sum(c.output_tokens for c in costs)
    budget.spent += res.cost_usd
    res.valid = out is not None
    res.error = "" if out is not None else grab.last
    res.extra = {"tool_called": searched["n"] > 0, "calls": len(costs)}
    if out is not None:
        res.extra.update(data_confidence=out.data_confidence, n_sources=len(out.sources))
    return res


def _p_yes(side: str | None, p: float | None) -> float | None:
    if side is None or p is None:
        return None
    return p if side == "YES" else 1 - p


async def run_synthesis(model: str, item: dict[str, Any], budget: Budget) -> Result:
    import swarm as sw

    res = Result("synthesis", model, item["id"])
    facts = []
    for f in item["facts"]:
        try:
            facts.append(MarketFactSummary.model_validate(f))
        except ValidationError:
            pass
    if len(facts) < 2:
        res.error = "skipped: <2 stored reports (Tier 3 never runs on these)"
        return res
    prompt_chars = len(json.dumps(item["state"])) + len(json.dumps(item["facts"])) + 2000
    if not budget.allow(model, prompt_chars, EXPECTED_OUT["synthesis"]):
        res.error = "skipped: budget"
        return res
    grab = _LastWarning()
    logging.getLogger("swarm").addHandler(grab)
    costs: list = []
    t0 = time.perf_counter()
    try:
        with _with_route("synthesis", model):
            sig = await sw._synthesize(item["state"], facts, costs)
    finally:
        logging.getLogger("swarm").removeHandler(grab)
    res.latency_s = time.perf_counter() - t0
    res.cost_usd = sum(c.usd for c in costs)
    res.tokens_in = sum(c.input_tokens for c in costs)
    res.tokens_out = sum(c.output_tokens for c in costs)
    budget.spent += res.cost_usd
    res.valid = sig is not None
    res.error = "" if sig is not None else grab.last
    if sig is not None:
        p = _p_yes(sig.side.value, sig.probability)
        mid = (item["bid"] + item["ask"]) / 2 if item["bid"] is not None else None
        res.extra = {"p_yes": p, "confidence": sig.confidence, "abstain": sig.abstain,
                     "mid": mid, "resolved": item["resolved"]}
    return res


def _brief_prompt(item: dict[str, Any]) -> str:
    import fastlane

    tmpl = fastlane._BRIEF_PROMPT if item["sport"] else fastlane._BRIEF_PROMPT_GENERAL
    body = tmpl.format(event=item["event"],
                       markets=fastlane._markets_block(item["markets"], item["quotes"]))
    corpus = "\n".join(f"- {c['snippet']}" for c in item["corpus"]) or "- (none)"
    return (body + "\n\nYou have no search tool. The search step has already run; "
            "these are its results:\n" + corpus)


async def run_text_role(role: str, model: str, item: dict[str, Any], prompt: str,
                        check, budget: Budget) -> Result:
    import swarm as sw

    res = Result(role, model, item["id"])
    if not budget.allow(model, len(prompt), EXPECTED_OUT[role]):
        res.error = "skipped: budget"
        return res
    t0 = time.perf_counter()
    try:
        resp, used = await models.complete(role, [{"role": "user", "content": prompt}],
                                           temperature=0.2, chain=(model,), max_tokens=4000)
    except Exception as exc:  # noqa: BLE001
        res.latency_s = time.perf_counter() - t0
        res.error = f"call failed: {exc.__cause__ or exc}"[:300]
        return res
    res.latency_s = time.perf_counter() - t0
    c = sw._cost_of(resp, "synthesize", used)
    res.cost_usd, res.tokens_in, res.tokens_out = c.usd, c.input_tokens, c.output_tokens
    budget.spent += res.cost_usd
    try:
        raw = sw._loads_loose(resp.choices[0].message.content)
        res.valid, res.extra = check(raw)
    except Exception as exc:  # noqa: BLE001
        res.error = f"unparseable: {exc}"[:300]
    return res


def check_brief(item: dict[str, Any]):
    import fastlane

    keys = {m["slug"] for m in item["markets"]}

    def _check(raw: Any) -> tuple[bool, dict[str, Any]]:
        b = fastlane._clean_brief(raw, keys)
        pairs = sum(len(s["affects"]) for s in b.get("scenarios", []))
        n_sc = len(b.get("scenarios", []))
        cov = pairs / (n_sc * len(keys)) if n_sc and keys else 0.0
        return (n_sc >= 4 and len(b.get("facts", [])) >= 4), {
            "n_facts": len(b.get("facts", [])), "n_scenarios": n_sc,
            "coverage": round(cov, 3)}
    return _check


def _schema_check(schema: type[BaseModel]):
    def _check(raw: Any) -> tuple[bool, dict[str, Any]]:
        try:
            schema.model_validate(raw)
            return True, {}
        except ValidationError as exc:
            return False, {"schema_error": str(exc)[:200]}
    return _check


def lesson_prompt(item: dict[str, Any]) -> str:
    return (
        "You draft lessons for a prediction-market research system. Below are scored "
        f"fast-lane records for the market family '{item['family']}': each is a headline "
        "the system acted on, the direction it expected (effect), the price when it "
        "acted (bid0/ask0) and the mid 1, 5 and 30 minutes later.\n\n"
        + json.dumps(item["records"], default=str)
        + "\n\nWrite at most one paragraph of lessons a human will review. Evidence "
        "counts in events, not rows; a lesson from one day is a hypothesis. Never "
        "recommend a position size. Reply with ONLY this JSON: "
        '{"family": str, "lessons": str, "evidence": [str], "n_events": int, '
        '"is_hypothesis": bool}')


def question_prompt(item: dict[str, Any]) -> str:
    return (
        "You propose rewrites of a classifier question for a human to review; you "
        "never apply them. The classifier reads a market's question, outcome, "
        "description and tags, and answers the question below as a probability.\n\n"
        f"question_id: {item['question_id']}\ncurrent wording: {item['current']}\n"
        f"audit: {item['audit']}\n\nPropose one atomic rewrite that would vary across "
        "real markets, and at least two probe cases (a market description and the "
        "answer you expect). Reply with ONLY this JSON: "
        '{"question_id": str, "proposed_text": str, "rationale": str, '
        '"probe_cases": [{"state_hint": str, "expected": str}]}')


# --------------------------------------------------------------------------
# Free: score what is already stored
# --------------------------------------------------------------------------

def score_stored(traces: list[dict[str, Any]], briefs: list[dict[str, Any]]) -> None:
    print("\nStored outputs (no calls):")
    n = ok = 0
    for t in traces:
        for f in t["facts"]:
            n += 1
            try:
                MarketFactSummary.model_validate(f)
                ok += 1
            except ValidationError:
                pass
    print(f"  gatherer reports stored: {n}, re-validate against MarketFactSummary: {ok}")
    halts: dict[str, int] = {}
    for t in traces:
        halts[t["halted_at"] or "sized"] = halts.get(t["halted_at"] or "sized", 0) + 1
    print(f"  evaluations: {len(traces)}  halts: {halts}")
    costs = [t["cost_usd"] for t in traces if t["cost_usd"]]
    if costs:
        print(f"  recorded LLM cost per evaluation: mean ${statistics.mean(costs):.4f}, "
              f"range ${min(costs):.4f}-${max(costs):.4f} (search fees not included)")
    rows = [(t, _p_yes(*t["stored_signal"])) for t in traces]
    rows = [(t, p) for t, p in rows if p is not None and t["resolved"] is not None
            and t["bid"] is not None]
    if rows:
        bm = statistics.mean((p - float(t["resolved"])) ** 2 for t, p in rows)
        bk = statistics.mean(((t["bid"] + t["ask"]) / 2 - float(t["resolved"])) ** 2
                             for t, _ in rows)
        print(f"  stored Tier 3 Brier {bm:.4f} vs market mid {bk:.4f} on {len(rows)} "
              "resolved evaluation(s) -- too few to mean anything")
    else:
        print("  no resolved evaluation carries a Tier 3 signal")
    for b in briefs:
        s = b["stored"]
        print(f"  stored brief {b['event']}: {len(s.get('facts') or [])} facts, "
              f"{len(s.get('scenarios') or [])} scenarios")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def _models_for(role: str, arg: str | None) -> list[str]:
    if arg:
        return [m.strip() for m in arg.split(",") if m.strip()]
    return [models.route(role).primary]


def summarize(results: list[Result]) -> None:
    print(f"\n{'role':20s} {'model':44s} {'n':>3s} {'valid':>6s} {'p50 s':>6s} "
          f"{'$/call':>8s} {'brier':>7s} {'mkt':>7s}")
    groups: dict[tuple[str, str], list[Result]] = {}
    for r in results:
        groups.setdefault((r.role, r.model), []).append(r)
    for (role, model), rs in groups.items():
        ran = [r for r in rs if not r.error.startswith("skipped")]
        if not ran:
            print(f"{role:20s} {model:44s} {0:3d}  (all skipped: {rs[0].error})")
            continue
        valid = sum(r.valid for r in ran)
        lat = statistics.median(r.latency_s for r in ran)
        cost = statistics.mean(r.cost_usd for r in ran)
        brier = mkt = "-"
        scored = [r for r in ran if r.extra.get("resolved") is not None
                  and r.extra.get("p_yes") is not None]
        if scored:
            brier = f"{statistics.mean((r.extra['p_yes'] - float(r.extra['resolved'])) ** 2 for r in scored):.4f}"
            mkt = f"{statistics.mean((r.extra['mid'] - float(r.extra['resolved'])) ** 2 for r in scored):.4f}"
            brier += f"/{len(scored)}"
        print(f"{role:20s} {model:44s} {len(ran):3d} {valid:3d}/{len(ran):<2d} "
              f"{lat:6.1f} {cost:8.4f} {brier:>7s} {mkt:>7s}")
        for r in ran:
            if r.error:
                print(f"    {r.item}: {r.error[:140]}")


async def main_async(args: argparse.Namespace) -> int:
    traces = load_traces(args.traces)
    briefs = load_briefs(args.fastlane)
    lessons = load_lesson_items(args.fastlane)
    roles = [r.strip() for r in args.roles.split(",")]
    if "gatherer" in roles:
        roles = [r for r in roles if r != "gatherer"] + list(GATHER_ROLES)
    unknown = [r for r in roles if r not in models.ROLES]
    if unknown:
        print(f"unknown role(s): {unknown}; choose from {models.ROLES}")
        return 2

    items_for = {
        **{r: traces for r in GATHER_ROLES},
        "synthesis": [t for t in traces if len(t["facts"]) >= 2],
        "brief_writer": briefs,
        "lesson_drafter": lessons,
        "question_drafter": QUESTION_FIXTURES,
    }
    print(f"inputs: {len(traces)} traces ({sum(t['resolved'] is not None for t in traces)} "
          f"resolved), {len(briefs)} briefs, {len(lessons)} lesson families, "
          f"{len(QUESTION_FIXTURES)} question fixtures")

    plan = []
    for role in roles:
        for model in _models_for(role, args.models):
            for item in items_for[role][: args.limit]:
                plan.append((role, model, item))

    if args.dry_run:
        print(f"\nDRY RUN -- no LLM client imported, no network. Plan: {len(plan)} replays.")
        est_total = 0.0
        need: set[str] = set()
        for role, model, item in plan:
            s = models.spec(model)
            chars = len(json.dumps(item, default=str)) + 2500
            kind = "gather" if role in GATHER_ROLES else role
            est = s.cost_usd(chars // 3, EXPECTED_OUT[kind]) if s else float("nan")
            est_total += 0 if s is None else est
            if s and s.env_key:
                need.add(s.env_key)
            print(f"  {role:20s} {model:44s} {item['id']:34s} "
                  f"est ${est:.4f}" + ("" if s else "  (NOT IN REGISTRY: would be refused)"))
        print(f"\n  estimated worst case ${est_total:.3f} against cap ${args.max_usd:.2f}")
        for k in sorted(need):
            print(f"  key {k}: {'present' if os.getenv(k) else 'ABSENT'}")
        score_stored(traces, briefs)
        return 0

    missing = models.missing_keys([m for _, m, _ in plan])
    if missing:
        print(f"missing keys (names only): {missing}. Add them to .env or use --dry-run.")
        return 3

    budget = Budget(args.max_usd)
    results: list[Result] = []
    for role, model, item in plan:
        if role in GATHER_ROLES:
            r = await run_gatherer(role, model, item, budget)
        elif role == "synthesis":
            r = await run_synthesis(model, item, budget)
        elif role == "brief_writer":
            r = await run_text_role(role, model, item, _brief_prompt(item),
                                    check_brief(item), budget)
        elif role == "lesson_drafter":
            r = await run_text_role(role, model, item, lesson_prompt(item),
                                    _schema_check(LessonDraft), budget)
        else:
            r = await run_text_role(role, model, item, question_prompt(item),
                                    _schema_check(QuestionDraft), budget)
        results.append(r)
        print(f"  {r.role:20s} {r.model:40s} {r.item:28s} valid={r.valid!s:5s} "
              f"{r.latency_s:5.1f}s ${r.cost_usd:.4f}  total ${budget.spent:.4f}")
    summarize(results)
    print(f"\nspent ${budget.spent:.4f} of ${args.max_usd:.2f}; {budget.skipped} skipped for budget")
    if args.out:
        with open(args.out, "a", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(asdict(r), default=str) + "\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay stored inputs through candidate models.")
    ap.add_argument("--roles", default="synthesis,gatherer,brief_writer",
                    help="comma list of roles; 'gatherer' means all three")
    ap.add_argument("--models", default=None,
                    help="comma list of litellm ids; default: each role's routed primary")
    ap.add_argument("--limit", type=int, default=3, help="items per role")
    ap.add_argument("--max-usd", type=float, default=0.25, help="hard cap for this run")
    ap.add_argument("--traces", default=os.getenv("TRACES_DB", "traces.db"))
    ap.add_argument("--fastlane", default=os.getenv("FASTLANE_DB", "fastlane.db"))
    ap.add_argument("--env", default=".env", help="dotenv file for keys (values never printed)")
    ap.add_argument("--out", default=None, help="append results as JSON lines")
    ap.add_argument("--dry-run", action="store_true", help="no calls, no spend")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s %(message)s")
    # Loaded in both modes so the dry run can say which keys exist. Only
    # names are ever printed.
    from config import load_dotenv_if_present

    load_dotenv_if_present(args.env)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
