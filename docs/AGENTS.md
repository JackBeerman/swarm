# Agents: roles, skills, and the loop that improves them

Status: 2026-09-23. Role names match `models.py` `ROLES`; model tiers are the
`default` routing profile there (`docs/ROUTING.md`). Skills are the files
under `skills/`, selected by `skills.py`. Nothing here touches the order
path. No role outputs a size (CLAUDE.md).

## Principle

Each LLM call starts cold, so knowledge that is not written down is re-derived
or lost. A **skill** is a short, reviewed playbook for one market family
(where to look, what to trust, which facts matter, what has already gone
wrong). A **role** is one job with a fixed input, output schema, tool set and
model tier. Code picks the skills for a market from its tags and slug prefix.
No model picks them and no Jev call is spent on it. A market no skill matches
gets exactly the prompts it got before skills existed (tested).

Every skill line is marked `[general]` (domain knowledge), `[measured: ...]`
(observed in this repo, with the evidence), `[rule]` (operator rule) or
`[unverified: ...]`. The parser rejects an unmarked claim. The markers are
stripped before the text reaches a model.

## Roles

| role | kind | input | output | tools | skills loaded | model tier (default) | code |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `triage` | Jev (System One) | 5-field market state | Noul/Score/Choice answers -> `TriageVerdict` | none | none (questions.py is its playbook) | `jev-1.13.0`, ~$0.00008 | `swarm.JevTriage` |
| `skill_selector` | code | normalized market or Jev state | `list[Skill]`, at most 2 | none | n/a | none | `skills.select` |
| `gatherer_sleuth` | LLM | Jev state (JSON) | `MarketFactSummary` | `web_search`, 1 call | `select(state)`: Search, Trust, Facts, Pitfalls | Tier 2, Haiku 4.5 | `swarm._run_gatherer` |
| `gatherer_historian` (`GatherRole.QUANT`) | LLM | same | `MarketFactSummary` | `web_search`, 1 call | same | Tier 2, Haiku 4.5 | same |
| `gatherer_red_team` | LLM | same | `MarketFactSummary` | `web_search`, 1 call | same | Tier 2, Haiku 4.5 | same |
| `synthesis` | LLM | state + three compressed summaries | `TradeSignal` (probability, confidence; never a size) | none | none: it reads only what the gatherers wrote | Tier 3, Opus 5.5 | `swarm._synthesize` |
| `brief_writer` (sports variant) | LLM | event title + watched markets with YES side and price | brief: `teams`, `facts`, `scenarios[].affects` fair_yes | Anthropic `web_search`, max 3 | `select_for_event(markets)`: Facts, Pitfalls, Brief, as `system` | Haiku 4.5 (`SEARCH_MODEL`) | `fastlane.build_brief`, `_BRIEF_PROMPT` |
| `brief_writer` (general variant) | LLM | same | same | same | same | same | `fastlane.build_brief`, `_BRIEF_PROMPT_GENERAL` |
| `weather_pricer` | code | band market + NWS forecast or ensemble members | band probability | HTTP to NWS / Open-Meteo | `temperature_bands` says: do not research | none | `weather.py`, `weather_ensemble.py` |
| `scorer` | code | stored signals, verdicts, traces, forecasts + settlements | drift, Brier, calibration tables | sqlite, exchange quotes | n/a | none | `fastlane --score`, `traces --score`, `shadow --score/--calibrate`, `weather --score`, `closer --report` |
| `lesson_drafter` | LLM, **proposed** | scorer output for one family + that family's skill file | `LessonDraft` (family, lessons, evidence, n_events, is_hypothesis) + a skill-edit diff | read-only sqlite/CLI output; **no web search** | the skill it is revising, markers kept | Tier 3 (default route); bench in `tools/bench_roles.py` | not built |
| `question_drafter` | LLM, **proposed** | scorer output + the current question dict | `QuestionDraft` (question_id, proposed_text, rationale, >=2 probe cases) | read-only; runs no probes itself | none | Tier 3 | not built |

The two brief variants are one role in `models.py` (`brief_writer`); the
variant is chosen by `sport = any(tag in GAME_TAGS)` in `fastlane.run`.

## Skills

| id | family | selected when | research |
| --- | --- | --- | --- |
| `nfl_game_lines` | sports | tag or slug league `nfl` and kind in aec, asc, tsc, cks | yes |
| `mlb_game_lines` | sports | `mlb`/`baseball` and kind in aec, asc, tsc, atc, cks (f5 lines included) | yes |
| `thin_props_periods` | sports | a league tag and kind `astatc`, or a period line (same rule as `fastlane.is_period_market`) | yes, one search on the game |
| `ai_releases_rankings` | tech | kind `aimc` or `aimrc` | yes |
| `charts` | music | `music`/`sptfy`/`bilbrd` and kind `ccrc` or `ccpc` | yes |
| `price_ladders` | crypto | `crypto`/`btc`/`eth`/`sol` and kind `cpc` | one query: spot and volatility |
| `temperature_bands` | weather | kind `tc-temp` | **no**: priced in code |

Rules in `skills.select`:

- Tags are the market's tags plus the slug's 2nd and 3rd tokens, since many
  rows arrive untagged (`aec-mlb-...`, `cpc-btc-...`, `ccpc-bilbrd-...`).
- Kind is the slug prefix (`tc-temp` for weather bands).
- A restricted market selects nothing: `political_tag(tags)`, or a restricted
  word (election, governor, legislation, law, fed, tariff, ...) in its title,
  leg, event or slug. For a brief, one restricted market empties the event.
  This is an extra layer; the veto proper is unchanged.
- Order is `(priority, id)`, at most `MAX_SKILLS = 2` per market.

Where the text goes:

- **Gatherers:** appended to the end of the system prompt inside
  `<playbook skills="..."> ... </playbook>`, after the fixed role text, with
  a preamble that says to ignore what does not fit and that the notes are
  not a source.
- **Brief writer:** sent as the request's `system` field. It is the same for
  every event of a family, and it sits ahead of the per-event user message.
  So `tools + system` form a stable prefix. At ~500-900 tokens it is below
  the minimum cacheable length for Haiku, so no `cache_control` is set. That
  becomes worth adding if the playbooks grow.
- `skills.research_allowed(market)` returns False for weather. It is **not
  wired** into the daemon (see PROPOSALS 6a). Today the weather playbook
  tells the gatherers not to search and to return `data_confidence` 0.0.

## The learning loop

1. **Record.** Fast lane: signals with the quote at +1/+5/+30 min
   (`fastlane.db`). Slow lane: what Tiers 2/3 believed (`traces.db`).
   Triage: `shadow.db`. Weather: `weather.db`. Closing lines: `closer.py`.
2. **Score (code).** `fastlane.py --score` (signed drift vs control, per
   headline), scenario scoring (gap closed toward `fair_yes`),
   `traces.py --score` (Tier 3 Brier vs the market's), `shadow.py --calibrate`
   (price vs frequency, counted by event), `weather.py --score`.
3. **Draft (lesson_drafter, offline).** One family at a time, it reads only
   the scorer output and the current skill file. It never reads the web:
   betting knowledge comes from scored history, not internet betting content.
   It writes a proposal into `docs/PROPOSALS.md` in this form:

   ```text
   ### Skill edit: <skill id> / <section>
   - current:  <line, with its marker>
   - proposed: <line> [measured: <query or command>, <n> events over <d> days, <dates>]
   - evidence: <the numbers>
   - status:   hypothesis | measured
   ```

   A line may carry `[measured: ...]` only with at least 10 events over at
   least 3 days. Below that it is `[unverified: hypothesis from ...]`. Markets
   inside one event resolve together, so the unit is events, not markets.
4. **Apply (human).** A person edits `skills/*.md`. Code only reads skills:
   `skills.py` contains no write path, and a test checks that. The parser
   rejects an unmarked claim, an unknown section, or an id that does not
   match its file name.
5. **Check.** Re-run the relevant probe (`tools/probe_fastlane.py`,
   `tools/bench_roles.py` for the brief writer) before and after. A skill edit
   that changes a brief's scenarios is compared on the same events.

`question_drafter` follows the same path into `docs/PROPOSALS.md` for
`questions.py`, which is never edited by a model (CLAUDE.md).

## What is not claimed

- No skill has been shown to improve a score. The one brief comparison run so
  far is PROPOSALS 6f, on one event. With the skill the facts improved: both
  starters were named. The scenarios did not, and one was priced on the wrong
  side.
- `[general]` lines are domain knowledge that nobody in this repo has
  measured, including every number in them (the 3/7 margin frequencies and
  the ~30% one-run games).
