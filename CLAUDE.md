# CLAUDE.md

Autonomous trading swarm on Polymarket US. Tier 1 Jev triage → Tier 2
gatherers → Tier 3 synthesis → sizing in code.

**Treasury is $100.** Every design decision below follows from that
number. At this size inference cost is a material fraction of position
notional, so the gate must stay tight and Tier 2/3 spend is the only cost
that matters.

---

## Hard rules

- **Never place an order outside `daemon.py`.** Sizing produces a
  `SizedOrder`; only the daemon acts on one.
- **Never let an LLM output a dollar amount, share count, or Kelly
  fraction.** Models return probability + confidence. `size_from_signal()`
  computes size in code. This is deliberate and not negotiable.
- **Never widen `max_restricted` or remove the restricted-domain veto.**
  A standing operator constraint: markets touching federal policy,
  defense, or US elections are off-limits regardless of edge. The veto is
  on the **max** of the three questions, never the mean.
- **Never default `SWARM_MODE` to anything but `shadow`.**
- **Never make the kill switch exit 0** or auto-clear the `.halted`
  sentinel. A human deletes it. That manual step is the point.

## Edit here first

`questions.py` holds every Jev question and threshold. That file is what a
human reviews; the rest is plumbing. Do not scatter thresholds into
application code. Per TypeSafe's own guidance, agents are weak at writing
these questions — propose changes, don't apply them unilaterally.

---

## Pitfalls that have already bitten us

### polymarket_us SDK (verified against 0.1.2)

An earlier version of this table was read off the SDK's TypedDicts. **Do
not trust them.** They are `total=False`, so they assert nothing at
runtime, and they declare three fields the gateway never sends. The table
below came off live responses (2026-09-19, sample of 10,771 open markets).
Where the stubs and the wire disagree, the wire wins.

| stub / spec says | actually on the wire |
|---|---|
| `volume`, `liquidity` | **do not exist**, on markets or events |
| `volumeMin` (filter) | accepted and **silently ignored** |
| `Event["endTime"]` | `endDate`; 0/50 events had `endTime` |
| `question` → `title` | **both exist, 100%**, and differ |
| `Market["outcome"]` | does not exist; the leg is `title` |
| `bbo["bestBid"]` | `bbo["marketData"]["bestBid"]`, a decimal **string** |
| `resolutionCriteria`, `oneHourPriceChange` | do not exist |

- **`active` and `closed` are orthogonal.** A settled market is
  `active=True, closed=True, status="MARKET_STATUS_RESOLVED"`. Querying
  events with `{"active": True}` returns almost nothing but resolved
  markets — 100/100 in the sample that first caught this. **Filter on
  `closed: False`.** All three call sites once sent `active: True` and
  collected zero markets while logging a clean, selective-looking run.
- **Close time is on the Market**, as `endDate`, and the parent event's
  `endDate` can be months earlier (event 2026-11-03 vs. leg 2027-02-01).
  Prefer the market's; that gap *is* the capital-parked check.
- **Volume and liquidity must be derived.** `adapters.derive_notionals()`
  multiplies the quote's `sharesTraded` / `openInterest` (share counts, as
  decimal strings) by mid. It is a proxy, and **the floors in
  `questions.py` were never tuned against its distribution.**

- `Amount` is `{"value": "0.55", "currency": "USD"}`. Parse at the
  boundary in `adapters.py`; never let the raw string reach sizing.
- **`GetUserPositionsResponse.positions` is a dict keyed by slug**, not a
  list. Iterating it as a list yields bare strings and marks every
  position at zero — which drives NLV down and trips the kill switch on a
  healthy account.
- **`CreateOrderParams.quantity` is an `int`.** Whole shares only. Floor,
  never round up — rounding up breaches the position cap.
- There is **no price-change field anywhere in REST**. Movement must come
  from the websocket or `adapters.PriceTracker`.

**Always go through `adapters.normalize_*`.** Raw SDK objects passed to
`build_state()` produce a state with no prices and no volume, and Jev will
return confident-looking probabilities about nothing. Worse than it
sounds: the raw market *does* carry `question`, so the bad state reads
like a normal market with a missing quote rather than an obvious blank.
The prices are the tell. There is a test asserting the broken path stays
broken.

**A test fixture is not evidence.** The 84 tests that shipped were green
against fixtures built from the same wrong stubs as the code — mock and
parser agreed, and neither matched the API. Before trusting any field
name here, check it against a live response, not against
`polymarket_us/types/`.

### Jev / TypeSafe

- **Noul answers carry no `confidence` field.** Only Choice and Score do.
- `jev-latest` floats, **and so does the family slug `jev-1.13`** —
  OpenRouter states it always redirects to the newest model. An unpinned
  model silently recalibrates every threshold in `questions.py`. Config
  refuses live mode with an unpinned id.
- Pricing: **$0.042/M input, $0 output, 32K context.** ~$0.000067 per
  triage call. Tier 1 is effectively free; do not optimize it.
- 429 and 529 require exponential backoff. Hand-rolled client does this in
  `JevTriage._post`; the official SDKs would do it for you.

### Design rules from TypeSafe's build guide

1. **Deterministic work stays in code.** An earlier version asked Jev "is
   the spread tight enough to trade?" Spread, depth, volume and
   time-to-close are arithmetic → `questions.structural_filter()`, which
   runs *before* the model call. Never regress this.
2. **Never ask about what the model cannot see.** An earlier version asked
   "does a fresh catalyst explain this move?" while supplying no news —
   answerable only from weights. Catalyst questions belong at Tier 2,
   after a gatherer has searched. Tier 1 judges market *structure* only.
3. **Decompose.** Atomic questions, structured `instructions`/`criteria`
   objects, backticked state paths. Broad questions hide several
   judgments behind one number.
4. **Compose in code** with weighted sums, not inside one broad question.

---

## Bugs fixed — do not reintroduce

- **ANDed thresholds.** Six independent gates with marginals of
  75/8/7/56/51/68% multiplied to a **0.1% joint escalation rate** — the
  daemon would run forever and trade nothing. Worse, it failed silently:
  sweeping any one threshold showed 0% at every value because the other
  five still bound. Now: one hard veto, two floors, one composite score,
  one threshold. Keep it that way.
- **ANDed thresholds, second form: multiplied factors.** The fix above was
  undone in spirit by `raw × outcome_multiplier × (0.5 + 0.5·confidence)`.
  Since `raw` is bounded at 1.0, that product could not reach a 0.60
  threshold for two of four outcome types, and `contested_event` needed
  confidence of exactly **1.00** — unreachable. It failed the same silent
  way, and a sweep of `min_gate_score` would have shown 0% at every value.
  **Do not multiply independent factors together against a fixed
  threshold.** Penalties subtract; routing decisions are branches.
- **Structural floors tuned on synthetic data.** The shipped floors passed
  **0 of 176** live markets. `min_volume_usd` was 50,000 against a live
  median of $34. Any floor here must be set from a measured distribution,
  and checked as a *joint* pass rate — marginals hide interactions.
- **Comparing a level count to a share floor.** `min_depth_shares` was
  compared against `bidDepth`/`askDepth`, which count book levels (range
  1–16), not shares. At a floor of 200 it rejected 100% of markets, and it
  was invisible because the volume check returned first. When a filter
  short-circuits, every check after the first is untested by the reject
  reasons — evaluate each one independently before trusting any of them.
- **Concurrent Kelly.** Without `PortfolioLock`, 30 concurrent
  evaluations on a $100 bankroll committed **$170.70**. Each pipeline read
  the same unreserved bankroll.
- **Leaked reservations.** `release()` existed and was never called;
  `reserved_total` grew without bound until sizing silently stopped
  producing orders. Release on every path; the TTL is a safety net, not
  the mechanism.
- **Unfetched risk snapshot.** A fresh `RiskSnapshot` timestamped `now()`
  made the staleness check short-circuit, so every consumer saw an
  all-zeros account. `taken_at=0.0` means never fetched.
- **NLV from cash alone.** $40 cash + $55 positions is healthy, not near a
  $10 floor. Use `assetNotional` / `cashValue`, and take the more
  conservative of the two marks.
- **Triage cost recorded after the gate.** ~95% of markets are rejected
  there, so the always-on burn never reached the ledger.

---

## Testing

`make test` — 84 tests, no network, no API key. `respx` mocks httpx at the
transport layer; litellm is monkeypatched.

A passing test is only worth something if it fails when the code breaks.
Several tests here deliberately assert the *broken* behavior of a removed
design (raw SDK shapes, leaked reservations, cash-only NLV). Don't delete
them as redundant — they are regression guards.

---

## Current state — read before claiming anything works

- **`web_search` is a stub.** `daemon._unconfigured_search` returns `[]`,
  so gatherers produce empty facts. Paper trading is meaningless until a
  provider is wired. This is also the largest per-evaluation cost, so
  choose deliberately.
- **Every threshold is a guess** tuned against a synthetic distribution,
  not real markets. `min_gate_score` especially.
- **Nothing has been scored against a real resolution.** `shadow.db` has a
  `resolved_outcome` column nothing fills. Until it does, we are
  calibrating escalation *rate*, not escalation *quality*.
- **`reflexion.py` does not exist.** Needs a few hundred resolved markets.
- The seven Tier 1 questions are a first draft. `research_would_help` is
  the weakest — it asks Jev to judge whether public evidence exists for a
  market it knows nothing about.

The infrastructure is well-tested. The judgment inside it is unvalidated.
Do not describe this as working; describe it as ready to find out.
