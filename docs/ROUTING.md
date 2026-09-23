# Model routing: which model does which job

Status: 2026-09-23. Registry, hook and benchmark shipped. **The default
routing is unchanged**: Tier 2 gatherers on Claude Haiku 4.5, Tier 3 on
Claude Opus 5.5, no fallbacks. Everything below that is not "measured" is a
proposal.

## Why

At a $100 treasury, Tier 2/3 spend is the cost that matters (CLAUDE.md).
Every LLM role today is Anthropic. Open-weight models reached through
litellm (OpenRouter, Together, Groq, local Ollama) are now 7-10x cheaper
than Haiku for the same token count, with 1M-token context and tool
calling. The question per role is whether they are as good *here*, and
the only honest way to answer it is to replay this repo's own inputs.

## Design

### `models.py`: the registry

- **`MODELS`**: one `ModelSpec` per litellm id: price per MTok (in/out),
  context limit, `accepts_temperature`, `tool_calling`,
  `native_web_search`, `open_weights`, `json_reliability` (measured in
  this repo, or `unmeasured`), the env key it needs, extra litellm kwargs
  (e.g. OpenRouter provider routing), source URL and date priced.
- **Roles**: `gatherer_sleuth`, `gatherer_historian` (the `quant`
  GatherRole: base rates and precedent), `gatherer_red_team`,
  `synthesis`, `brief_writer`, `lesson_drafter`, `question_drafter`.
- **Routes**: per role, a primary and ordered fallbacks.
- **Profiles**: `default` is today, read at call time from `TIER2_MODEL` /
  `TIER3_MODEL` / `SEARCH_MODEL` so those env vars still win.
  `recommended` is the proposal below. It is only active if selected.

Overrides, most specific first:

```text
SWARM_ROUTE_<ROLE>=model[,fallback,...]    e.g. SWARM_ROUTE_GATHERER_RED_TEAM=openrouter/z-ai/glm-5.3-flash
SWARM_ROUTING_FILE=routing.toml            see routing.example.toml
SWARM_ROUTING_PROFILE=recommended          built-in profile
```

A malformed override (unknown profile, unknown role in the file, empty
chain) raises. It does not fall back to some other model.

### The hook in `swarm.py`

The whole coupling is one block, "Model routing hook", and three call-site
changes:

1. `_run_gatherer` and `_synthesize` ask `_route(role, TIER2_MODEL |
   TIER3_MODEL)` for the model, and add `**_route_kwargs(model)` to the
   litellm call. **With no routing configuration, `_route` returns the
   default it was given and `_route_kwargs` returns `{}`.** The requests
   are the same as before; `tests/test_models.py` asserts the exact keyword
   set sent to litellm for both tiers.
2. The routing call sits inside each function's existing `try`, so a bad
   override fails that one evaluation before any spend and logs why.
3. `_cost_of` falls back to the registry price when litellm returns $0.00
   for a response that used tokens. Reason: litellm prices a model only if
   its cost map knows it, and a $0.00 spend never reaches the kill switch.
   The default models are priced by litellm, so for them this line is
   never reached.

Not wired: fallback chains inside the pipeline (swarm still makes one call
and treats a failure as a missing report), and `fastlane.build_brief`
(it posts to Anthropic directly with the server search tool). Both are
listed under "Next" below.

### Adding a model

1. Add a `ModelSpec` to `_TABLE` in `models.py`: litellm id, price from the
   provider's page or `https://openrouter.ai/api/v1/models` (public, no
   key), context, and flags. Set `accepts_temperature=False` for any
   Claude 4.6+ model (tested against `swarm.sampling_kwargs`). Leave
   `json_reliability="unmeasured"` until the bench has run it.
2. For OpenRouter open models, keep `extra=_NO_FP4` unless you have
   measured the fp4 upstreams. The cheapest OpenRouter upstream is often
   fp4-quantised; that is how DeepSeek V4.1 Flash is listed at $0.10/$0.50
   while its fp8 upstreams charge $0.14/$0.42.
3. Run `python tools/bench_roles.py --dry-run --models <id> ...` and then a
   capped paid run. Only then name it in a route.

The bench refuses to spend on a model that is not in the registry, because
it cannot cost the call in advance.

## Benchmark: `tools/bench_roles.py`

Replays stored inputs through candidate models and scores them.

| role | inputs | path |
| --- | --- | --- |
| gatherers | `traces.db` market state + a **frozen search corpus** | `swarm._run_gatherer`, unchanged |
| synthesis | `traces.db` market state + the `facts_json` Tier 3 saw | `swarm._synthesize`, unchanged |
| brief writer | `fastlane.db` briefs: event, markets, facts as corpus | fastlane's prompt, search split out |
| lesson drafter | `fastlane.db` acted signals, per family | prompt + `LessonDraft` schema |
| question drafter | two inline fixtures (`research_would_help`, `stat_aggregation`) | prompt + `QuestionDraft` schema |

Scores: schema validity (`MarketFactSummary`, `TradeSignal`,
`fastlane._clean_brief`, drafter schemas), latency, cost, whether a
gatherer called its search tool, and for synthesis a Brier score against
resolved outcomes beside the market mid's Brier on the same rows.

Limits, stated plainly:

- **The search a gatherer ran is not stored**, only what it concluded. So
  every candidate gets the same corpus, built from the stored gatherer
  outputs for that market, from a search function that ignores the query.
  This tests protocol (tool call, valid JSON, calibrated `data_confidence`
  on thin evidence) and nothing about search quality.
- **Brief inputs are reconstructed.** The markets block a brief was written
  from was not stored. It is rebuilt from the slugs in the brief, with
  question text where `fastlane.db` has it, and a price from the median of
  the brief's own scenario prices. Fine for "does it return the shape";
  not a fair test of fair-price quality.
- **Resolved traces: 5 of 9, all from one NFL event**, and one weather
  event pending. A Brier comparison on one event is noise.

```bash
python tools/bench_roles.py --dry-run                              # free
python tools/bench_roles.py --roles gatherer --limit 3 \
    --models openrouter/deepseek/deepseek-v4.1-flash,anthropic/claude-haiku-4-5 \
    --max-usd 0.10 --out bench.jsonl
```

`--dry-run` imports no LLM client and makes no network call. It prints
the plan with a worst-case cost per replay, which keys are present (names
only), and scores what is already stored.

## Results so far

Keys: `ANTHROPIC_API_KEY` present, `OPENROUTER_API_KEY` **absent** (also
absent: Together, Groq, DeepSeek, Mistral). So no open model has been run;
open-model rows below are dry-run estimates. Total paid spend for this
work is at the end of this section.

**Stored outputs** (free): 21 stored gatherer reports all re-validate
against `MarketFactSummary`; 9 evaluations halted as
`no_edge_after_sizing` x5, `synthesis_failed` x2,
`insufficient_gatherer_output` x2. Recorded LLM cost per evaluation mean
$0.050 (range $0.018-$0.096), search fees not included.

**Baseline, current models, paid** (2026-09-23, 15 replays, $0.1275):

| role | model | n | valid | p50 latency | $/call | tokens in/out |
| --- | --- | --- | --- | --- | --- | --- |
| synthesis | Opus 5.5 | 3 | 3/3 | 7.9 s | $0.0202 | ~1.9k / ~640 |
| gatherer_sleuth | Haiku 4.5 | 3 | 3/3 | 3.9 s | $0.0046 | ~2.5k / ~430 (2 calls) |
| gatherer_historian | Haiku 4.5 | 3 | 3/3 | 7.3 s | $0.0050 | ~2.5k / ~500 |
| gatherer_red_team | Haiku 4.5 | 3 | 3/3 | 8.2 s | $0.0056 | ~2.5k / ~620 |
| brief_writer (writing only) | Haiku 4.5 | 3 | 3/3 | 6.6 s | $0.0071 | ~0.9k / ~1.2k |

- Every gatherer called its search tool. Every brief had 5-6 scenarios
  with full market coverage.
- The two traces whose synthesis originally failed (`synthesis_failed`,
  $0.08-$0.10 each) returned valid signals on replay. Their failure was
  probably the old 1,200 `max_tokens` ceiling truncating Opus's thinking
  (since raised to 4,000); unverified, the cause was not recorded.
- Opus 5.5 abstained on all three resolved replays: its probabilities
  (0.85, 0.80, 0.89) sat within a few points of the mid (0.865-0.875). Brier
  0.0249 vs the market's 0.0174, n=3 markets from **one event**. That says
  nothing about calibration.

**Synthesis, Opus 5.5 vs Sonnet 5, paid** (2026-09-23, all 7 traces with
>= 2 reports, $0.2035):

| model | n | valid | p50 latency | $/call | abstained | Brier (n=3, one event) | market Brier |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Opus 5.5 | 7 | 7/7 | 7.3 s | $0.0205 | 7/7 | 0.0267 | 0.0174 |
| Sonnet 5 | 7 | 7/7 | 5.2 s | $0.0086 | 6/7 | 0.0161 | 0.0174 |

- Both models stayed within 0.08 of the mid on every market, and within
  0.02 on six of seven. On the four weather markets both returned the
  price (0.48/0.49 vs 0.485, and so on). That matches the README's weather
  finding: without a forecast in the reports, the research tiers give back
  the market price.
- Opus was repeatable: its three re-run probabilities moved 0.00-0.01
  from the first replay.
- Sonnet's lower Brier is an artefact. All three resolved markets are
  favourites from one NFL game that all resolved YES, and Sonnet sat a
  little closer to the price. **This is not evidence that Sonnet 5 is
  better calibrated, or as good.** It is evidence that Sonnet 5 returns
  valid `TradeSignal` JSON at ~40% of Opus's cost per call and is quicker.
  It stays a challenger until the bench has resolved rows from many
  events.

**Paid spend for this work: $0.331** ($0.1275 baseline + $0.2035
synthesis comparison) against a $1.00 cap. No OpenRouter, exchange or
order-path calls were made.

## Options litellm can reach (2026-09-22)

Prices USD per MTok, from OpenRouter's public model API unless noted.
"Open" = weights on Hugging Face (OpenRouter's `hugging_face_id`). None of
the open models has provider-native web search on OpenRouter (its
`pricing.web_search` is empty for all of them), so **for every open model
the search step stays separate: `search.py`**. That is already the
gatherer design: the gatherer model only calls a `web_search` *function*,
which `search.py` serves, so a gatherer model needs tool calling, not
search.

| model (litellm id) | open | in / out | context | tools | notes |
| --- | --- | --- | --- | --- | --- |
| `anthropic/claude-haiku-4-5` | no | 1.00 / 5.00 | 200k | yes | today's Tier 2; server web search $10/1k |
| `anthropic/claude-sonnet-5` | no | 2.00 / 10.00 | 1M | yes | rejects temperature |
| `anthropic/claude-opus-5-5` | no | 4.00 / 20.00 | 1M | yes | today's Tier 3; rejects temperature |
| `openrouter/deepseek/deepseek-v4.1-flash` | yes | 0.14 / 0.42 (fp8) | 1M | yes | list $0.10/$0.50 is fp4; Together $0.30/$1.20 |
| `openrouter/z-ai/glm-5.3-flash` | yes | 0.15 / 0.50 | 1M | yes | Together same price |
| `openrouter/qwen/qwen3.8-flash` | yes | 0.15 / 0.47 | 1M | yes | one upstream (Alibaba); Together $0.09/$0.28 |
| `openrouter/openai/gpt-oss-120b` | yes | 0.15 / 0.60 | 131k | yes | 24 upstreams incl. Groq, Cerebras |
| `groq/openai/gpt-oss-120b` | yes | 0.15 / 0.60 | 131k | yes | ~500 tok/s; Groq `browser_search` exists but not with structured output, price unverified |
| `openrouter/deepseek/deepseek-v4-pro-0813` | yes | 0.99 / 2.97 (fp8) | 1M | yes | synthesis challenger |
| `openrouter/z-ai/glm-5.3` | yes | 0.84 / 2.64 | 1M | yes | synthesis challenger; Together $1.40/$4.40 |
| `openrouter/moonshotai/kimi-k3` | yes | 3.00 / 15.00 | 1M | yes | near Opus price |
| `ollama/qwen3.8:27b` | yes | 0 / 0 | local | yes | needs ~20 GB GPU at q4 |

Also seen and not tabled: Llama 4 Maverick/Scout (April 2025, now
outclassed at the same price), Mistral Small 4 (`mistral-small-2603`,
open, $0.15/$0.60, one upstream), Gemma 4 31B (open, $0.09/$0.34),
NVIDIA Nemotron 3 Super/Ultra (open), MiniMax M3 (open, $0.30/$1.20).

OpenRouter can add search to any model (`:online` / web plugin, Exa,
$0.007 per request up to 10 results). Cheaper per query than Anthropic's
$0.01, but it returns Exa highlights rather than the cited quotes
`search.py` extracts. Not evaluated.

Sources (fetched 2026-09-22):

- <https://openrouter.ai/api/v1/models> and `/api/v1/models/{id}/endpoints`
- <https://www.together.ai/pricing>
- <https://console.groq.com/docs/models>, <https://console.groq.com/docs/browser-search>
- <https://openrouter.ai/docs/guides/features/plugins/web-search>
- <https://docs.litellm.ai/docs/providers/openrouter>
- Anthropic prices: the Claude API model table (Haiku 4.5 $1/$5, Sonnet 5
  $2/$10, Opus 5.5 $4/$20), matching litellm's cost map.

## Recommendation

Per evaluation, from the measured token counts above (search not included;
see below):

| role | today | recommended | $/eval today | $/eval recommended |
| --- | --- | --- | --- | --- |
| 3 gatherers | Haiku 4.5 | DeepSeek V4.1 Flash (sleuth, historian), GLM-5.3 Flash (red team), Haiku last fallback | $0.015 | ~$0.002 (estimate) |
| synthesis | Opus 5.5 | **Opus 5.5, unchanged** | $0.020 | $0.020 |
| **LLM total** | | | **$0.035** | **~$0.022** |
| search (3 gatherer searches) | Haiku + Anthropic web search | unchanged | not measured here; $0.03-0.09 in fees at 1-3 searches per gatherer, plus Haiku tokens | same |
| brief writer (per event) | Haiku 4.5 + search | unchanged | ~$0.03 (fastlane's recorded figure) | same |
| lesson drafter (rare) | not built | DeepSeek V4.1 Flash (1M context), Sonnet 5 fallback | - | <$0.01 per draft (estimate) |
| question drafter (rare) | not built | Opus 5.5 | - | ~$0.02 per draft (estimate) |

Reasoning:

- **Gatherers** are the clean case: compression of a handful of search
  results into a fixed schema, three per evaluation, human-irrelevant
  prose. The saving is ~$0.013 per evaluation, about 37% of LLM spend.
  **Condition: the bench must show >= Haiku's validity and tool-call rate
  on the same replays before the profile is switched.** Different families
  per gatherer (DeepSeek, GLM) keep the three reports from sharing one
  model's blind spots, which is the point of having a red team.
- **Synthesis stays on Opus 5.5.** It writes the only model probability in
  the slow lane. The one comparison possible today is on one event.
  Sonnet 5 (measured $0.0086 vs $0.0205 per call, 7/7 valid) and DeepSeek
  V4 Pro / GLM-5.3 (~1/5 of Opus's price, unmeasured) are the challengers
  to replay once there are resolved traces across dozens of events.
- **The larger lever is search, not models.** The LLM part of an
  evaluation is ~$0.035; search fees alone can be as much again. Cheaper
  search (OpenRouter/Exa $0.007 per request; Serper/Brave per `search.py`'s
  notes) is a separate decision with a quality question of its own.
- **Brief writer stays on Haiku + server search.** It runs once per event,
  and the search call is most of its cost; splitting search from writing
  saves well under a cent per event.
- **Lesson drafter** is long-context bulk summarisation that a human
  reviews: the case where a 1M-context open model at $0.14/MTok fits.
- **Question drafter** is rare and its output recalibrates thresholds if a
  human adopts it; cost is irrelevant, quality is not.

Expected cost per full evaluation (LLM + search): today roughly
$0.07-0.13, recommended roughly $0.05-0.11. Estimate, not measured; search
fees were not part of this benchmark.

## What the operator must do

1. Add `OPENROUTER_API_KEY` to `.env` (<https://openrouter.ai/keys>). Put
   a credit limit on the key. Optional: `GROQ_API_KEY` for gpt-oss at
   ~500 tok/s; `TOGETHER_API_KEY` as a second upstream.
2. Run the open-model bench, capped:

   ```bash
   python tools/bench_roles.py --roles gatherer --limit 5 --max-usd 0.10 \
       --models openrouter/deepseek/deepseek-v4.1-flash,openrouter/z-ai/glm-5.3-flash,openrouter/openai/gpt-oss-120b,anthropic/claude-haiku-4-5
   ```

3. If they match Haiku on validity and tool calls, run one paper cycle
   with `SWARM_ROUTING_FILE=routing.example.toml` and compare
   `traces.py --score` and the ledger against a Haiku cycle on similar
   markets.
4. Leave synthesis alone until `traces.py --score` has resolved rows from
   many events.

## Next

- Fallback chains inside the pipeline: have `_run_gatherer` walk
  `models.route(role).chain` on provider errors (not on invalid JSON: that
  is a quality signal, not an outage). Needs its own tests.
- Route `fastlane.build_brief` through the registry with search split out
  (`search.py` for the search, the routed model for the writing).
- Store each gatherer's raw search results in `traces.db`, so a replay can
  show the same pages instead of a frozen summary.
