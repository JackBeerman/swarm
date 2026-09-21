# The brief: giving Jev its information

Status: design, 2026-09-21. The v1 brief (rosters only) ships in
`fastlane.py`. v2 below is the next build.

## Principle

Jev reads; it does not know. Every wrong fast-lane answer found so far came
from a fact missing in the state ("Nacua ruled out" with no roster gave the
wrong direction), never from Jev misreading what was there. So the design
question is not "how do we ask Jev to forecast" but "what document do we hand
it so the decision becomes recognition."

Jev cannot produce a probability of an outcome, cannot do arithmetic, and
does not know the world. It is calibrated on the question asked, against the
text it is shown. The number a slip needs must therefore already be in the
brief; Jev's job is to say which number applies.

## The brief (v2), one per event, written offline by an LLM

```json
{
  "event": "NY Giants vs LA Rams",
  "written_at": "2026-09-21T22:10:00Z",
  "teams": {"Los Angeles Rams": ["Matthew Stafford (QB)", "Puka Nacua (WR)", "..."]},
  "facts": [
    {"fact": "Nacua listed questionable (ankle)", "as_of": "2026-09-21T18:00Z", "source": "..."}
  ],
  "scenarios": [
    {"id": "nacua_out", "trigger": "Puka Nacua ruled out or inactive",
     "affects": {"rams-total-27pt5": {"fair_yes": 0.41}, "rams-cover-3pt5": {"fair_yes": 0.47}}},
    {"id": "dart_out", "trigger": "Jaxson Dart does not start",
     "affects": {"giants-total-22pt5": {"fair_yes": 0.35}, "rams-cover-3pt5": {"fair_yes": 0.63}}}
  ],
  "lessons": "Inactives publish ~90 min before kickoff and the book reprices them within ~2 min. A single early score moves totals less than its headline suggests."
}
```

- `facts` and `scenarios` come from LLM research with web search (the slow
  lane, ahead of time; ~$0.05 per event).
- `fair_yes` is the LLM's number, produced offline where a minute of
  reasoning is fine. It is the only place a model-written probability
  exists, and it is written before any headline, so it cannot be steered by
  one.
- `lessons` is the history layer: a paragraph distilled by an LLM from
  scored records (`fastlane.py --score`, `traces.py --score`), reviewed by
  a human, per market family.

Size matters. Jev's accuracy falls as the state grows with content that is
not about the decision, and the hard limit is 32k tokens for state plus the
longest question. A brief is per event and curated. Lessons are a paragraph.

## Jev's questions: recognition, not forecasting

Per headline (or per pre-game slip), one request:

| question | primitive | what it asks |
| --- | --- | --- |
| `reports_new_fact`, `concerns_event`, `headline_political` | Noul | as today |
| `scenario` | Choice over `scenarios[].trigger` + `none_of_these` | which pre-written scenario the headline realises |
| `contradicts_brief` | Noul | does the headline contradict a `facts[]` entry |
| `brief_stale` | Noul | does the headline describe a change after `written_at` that the brief does not cover |

Then code: `fair = scenarios[chosen].affects[market].fair_yes`,
`edge = fair - ask`, sized by `size_from_signal()` under the same caps and
vetoes as the slow lane. No model output is ever a size. A `contradicts_brief`
or `brief_stale` above threshold means stand down and re-brief, not trade.

## The loop

1. Brief written before the event (LLM, offline).
2. Headlines judged against it (Jev, ~300 ms).
3. Quotes recorded at the signal and +1/+5/+30 min; outcomes backfilled.
4. Scoring per question and per scenario: did the book move toward
   `fair_yes`? Was `fair_yes` closer to the settled outcome than the price?
5. An LLM reads the scored records and drafts revised lessons, scenario
   templates and question wording. A human applies them (`questions.py` is
   never edited by a model). Probe before adopting: `tools/probe_fastlane.py`.

Steps 4-5 are what make it recursive. The reward arrives in minutes (drift)
and days (settlement), and the unit of evidence is events and days, never a
single slip. A lesson drawn from one Sunday is a hypothesis.

## Risks that stay in code

- **Poisoned brief.** The brief is built from web content. A wrong fact
  becomes a confident wrong decision, because Jev judges against the brief.
  Source allowlist for research, the tight-book filter, position caps, the
  per-event cap and the politics veto do not depend on the brief.
- **Feed lag.** Measured 7-15 min on RSS in the first smoke test. If that
  holds, the fast lane needs a faster source before any of this matters.
- **Stale brief.** `written_at` is in the state and `brief_stale` is asked
  on every headline. A brief older than the event's inactives deadline is
  re-written, not trusted.

## Build order

1. Brief v2 writer (`fastlane.py build_brief`): facts, scenarios with
   `fair_yes`, `written_at`. Store briefs in `fastlane.db`.
2. `scenario`, `contradicts_brief`, `brief_stale` questions in
   `questions.py`; probe cases in `tools/probe_fastlane.py`.
3. Scoring per scenario: drift toward `fair_yes`, and `fair_yes` vs outcome.
4. Lessons: a per-family text block, LLM-drafted from scored records into
   `docs/PROPOSALS.md`, human-applied.
5. Order path, only after drift beats the spread over several days.
