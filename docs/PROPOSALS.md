# Proposed changes awaiting a human decision

From the 2026-09-21 audit of this repo against the TypeSafe docs corpus.
Nothing here is applied. Everything in `questions.py` is the operator's call
(see CLAUDE.md). Every question rewrite recalibrates its thresholds: re-measure
floors and `min_gate_score` on a fresh shadow run afterwards. `max_restricted`
stays at 0.12 throughout.

Evidence base: shadow.db, 602 verdicts, 170 reached Jev (105 sports, 65
general), 14 resolved events, all NFL, all 2026-09-20. Indicative only.

## 1. Safety: the restricted veto does not read `outcome` or `event`

`question` is the event proposition ("Who leaves first?"); `outcome` is the leg
("Secretary of Defense"). All three restricted Nouls inspect `question` and
`description` only, so restricted content in the leg is outside what the
question points at. jev-1.13 reads literally ("answers the question you wrote").

Proposed: in all three, `"inspect": "`question`, `outcome`, `event`,
`description` and `tags`"`. Can only raise recall. After the change confirm the
clean-market floor (0.01-0.03 on all 105 sports rows today) stays far below 0.12.

Also proposed for `defense_or_military`: a `focus` line and `false.not_for`
saying sports vocabulary (defense, sacks, blitz) and team names (Patriots,
Commanders, Raiders) are not military, with real NFL strings as `false`
examples. Not urgent: rows naming those teams scored 0.01. Build a fixed
regression set of ~10 known-restricted markets and assert each stays > 0.12
after ANY criteria edit. Needs a decision: Army-Navy game.

## 2. Questions that do not discriminate

| question | measured | problem |
| --- | --- | --- |
| `pregame_information_edge` | sd 0.10 on 0-2, range 1.37-1.80 | yes/no instruction answered with 3 levels; 35% weight on a constant |
| sports `objective_resolution` | sd 0.02, range 0.83-0.94 | a constant; keep as floor, drop from the composite |
| `research_would_help` | median confidence 0.53, p10 0.17 | levels are degree words ("thin"); signals cite things not in state |
| `stat_aggregation` | sd 0.63 (works) | "exact margin" signal catches point spreads: 33/40 `game_result` markets vetoed `too_discrete`, then penalised again by `sports_market_type` |
| `objective_resolution` (general) | cluster 0.57-0.64 | a Noul about a degree; 0.6 means "unsure", the composite reads "medium". Convert to a 3-level Score or make the Noul literal |

Net effect today: the general composite is decided by `outcome_type` alone
(`contested_event` 0/44 escalated, `continuous_metric` 8/8); the sports
composite is roughly `0.47 + 0.225*agg - penalty`.

Also: "Count how many independent chances" in `stat_aggregation.focus` — the
docs say jev-1.13 does not count reliably.

## 3. Option and routing holes

- Neither Choice has an `other` option (docs: add one when the list may not
  cover the input). "TIME Person of the Year" was classed `scheduled_disclosure`.
- Sports futures and awards get game-prop questions. `period` is absent on
  season futures, so route in code: SPORTS_QUESTIONS only when sports-tagged
  AND `period` is present.
- `SPORTS_TAGS` is set membership over 26 leaf slugs; a league missing from it
  falls to the general block, whose bottom Score level vetoes live sport.
  Replace with a root-tag route table plus one fallback `market_domain` Choice.

## 4. State naming

The state key `question` collides with every instruction's own `question`
field, and backticked paths resolve against both. `outcome` collides with the
ordinary word. Proposed rename in `_model_state` only: `question`->`market_title`,
`outcome`->`leg`, `description`->`rules`. Also send a visible placeholder when
`description` is empty instead of dropping the key the questions point at.

## 5. Open design questions

- `gate_score` multiplies into Kelly sizing (swarm.py `size_from_signal`). The
  docs call Score expectations "weak in numerical calibration"; consider using
  the gate as a threshold only.
- Choice penalties use the argmax and ignore the recorded confidence. An
  expected penalty `sum(p_i * penalty_i)` adds no threshold; needs
  `probabilities` stored.

The full proposed question dicts are in the audit report; ask Claude to
regenerate them against the current `questions.py` before applying.

## 6. Skills and roles (2026-09-23, docs/AGENTS.md)

`skills.py` and `skills/` are in place: seven playbooks, selected in code and
injected only where one matches. Unmatched markets get byte-identical prompts
(tested). These follow-ups need a decision:

a. **[APPLIED 2026-09-23] Weather skips Tier 2/3 entirely.** `skills.research_allowed(market)` is
   False for `tc-temp`. Today the playbook only tells the gatherers not to
   search; each weather escalation still pays for three gatherer calls and
   synthesis. Proposed: in `Swarm.research`, halt with `priced_in_code` before
   the budget check when `research_allowed` is False. That is a pipeline
   change, so it is not made here.
b. **Crypto ladders to code, like weather.** A touch or range probability from
   spot, time left and implied volatility is arithmetic. The `price_ladders`
   playbook limits research to one spot/volatility query in the meantime.
   Measure first: compare the ladder's prices to a lognormal with DVOL on
   stored `cpc` markets. Unmeasured.
c. **MLB wording in the fast-lane questions (questions.py, not applied).**
   Every example in `effect_{i}` and `size_{i}` is NFL ("starting
   quarterback ruled out"). The MLB analogue of the decisive factor is "the
   starting pitcher is scratched" and "a regular is out of the lineup".
   Proposed: add one MLB example to `size_{i}` level 2 signals and one to
   `effect_{i}.lowers`. Probe on MLB headlines before and after (a new
   `probe_fastlane` case set). Adding examples recalibrates the Score.
d. **The lesson_drafter output format** in docs/AGENTS.md ("Skill edit" block,
   with >= 10 events over >= 3 days to mark a line measured). Needs the
   operator's agreement before anything is drafted against it.
e. **Stored-brief audit, MLB 2026-09-22 (fastlane.db, 7 briefs).** 18 of 58
   facts were season-long absences, 4 of 47 scenarios were pregame or weather
   triggers, 7 were improbable early blowouts and 7 were one player's stat line.
   These numbers are in the MLB skill. Re-count on briefs written with the
   skill before claiming it helped.
f. **[Ladder check APPLIED 2026-09-23 as fastlane.coherent()] One A/B brief, Blue Jays vs Orioles, 2026-09-22 (Haiku 4.5, same
   markets and prices, one run each).**
   - Without the skill: 8 facts, 7 scenarios. No probable starter was named
     (a scenario guessed "Yesavage starts"), 2 facts were long-term IL, and
     the weather was given for Toronto without saying where the game is played.
   - With the skill: 5 facts, 5 scenarios. Both probable starters were named
     and dated (the fact the skill ranks first), there were no season-long IL
     facts, and Cease was reported as skipping his start.
   - The scenarios did not improve. Two were 14-run blowouts, which the skill
     says not to write, and one of them was priced **on the wrong side**:
     "Baltimore builds a 14-run lead" set Toronto -1.5 to 0.98 and the Toronto
     moneyline to 0.05.
   One run each is an anecdote, and a model reads a prompt instruction as
   advice. Proposed, in code: `_clean_brief` drops a scenario whose prices
   break the ladder for one team, P(-1.5) <= P(win) <= P(+1.5). Those prices
   are read from `yes_team` and the line, so this is arithmetic, not a
   question. Not applied, because it changes which scenarios survive.
