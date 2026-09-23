# What this project learned about Jev

Written 2026-09-23, when the project was paused. Four days of building and
measuring, one $100 account, four real orders, everything else in shadow or
paper mode. Every figure below says what it was measured on; most samples are
small, and none of them demonstrates a trading edge.

## The question

Jev (TypeSafe's System One model) takes a JSON state and typed questions and
returns probabilities in about 300 ms for about $0.00008. Where does that
profile help inside a real decision system, and where does it not?

## Where Jev earned its place

**1. As a cheap filter in front of expensive research.** Seven or eight typed
questions per market, in one request, decide whether a market deserves ~$0.10
of LLM research. A full evaluation costs roughly 1,500 Jev calls, so the filter
pays for itself many times over. Tier 1 cost never mattered; exchange rate
limits (about one quote per 1.2 s) were always the bottleneck.

**2. As a hard veto.** Four yes/no questions about politics, government policy,
defence and elections, vetoing on the maximum. On 17 labelled markets
(`tools/probe_restricted.py`) political ones scored 0.95-0.99 and everything
else 0.05 or below, including sports vocabulary like "defence" and "Patriots".
A probe also found a real hole: three US-scoped questions scored a UK election
at 0.02, which is why the fourth, country-agnostic question exists.

**3. As a recogniser, which is its strongest mode.** Given a brief that lists
possible events in advance ("the starting pitcher leaves before the 5th"), Jev
matched live-feed text to the right event 5 times out of 5, and correctly
declined a routine late pitching change and a mound visit
(`tools/probe_mlb_events.py`). Asked instead to judge the direction of each
market on its own, it was right 10 times out of 15. The design that follows:
an LLM writes the brief and prices each scenario offline, Jev recognises which
scenario happened, and code compares the brief's price with the book.

**4. As a relevance router.** Across baseball headlines, a roster move scored
0.95 for the game it concerned and 0.03-0.05 for the others; previews and
opinion pieces scored 0.05-0.10 as "new facts". Rewrites of the same injury
were recognised as repeats (0.88-0.93) against genuinely new items (0.02-0.07,
`tools/probe_dedup.py`).

## Where it did not

- **It reads literally.** A score level that said "exact margin" swept up point
  spreads and vetoed 33 of 40 game-result markets. A relevance question that
  said "team or player" scored Bitcoin news 0.43 for a Bitcoin market; reworded
  for any subject, 0.95.
- **It knows nothing it is not given.** "Puka Nacua ruled out", with no roster
  in the state, came back as good for the Rams. Nacua is a Ram. With a
  one-paragraph roster brief, the direction was right.
- **Questions that do not vary are constants with a weight.** Two of the three
  sports signals barely moved on real data (standard deviation 0.10 on a 0-2
  scale; 0.02 on 0-1). Question design is most of the work.
- **It is not a forecaster**, and was never used as one. Its calibration is
  about the question it was asked, not about who wins.

## What mattered more than the model

- **Reading what YES actually pays.** On underdog spread markets the exchange's
  title names the other team, and on NFL its settlement text does too. The
  three losing real orders were bought on the wrong side of a correct signal
  (Jev said the Giants' QB injury hurt the Giants; the Rams covered). Settling
  the spread ladder proved which side YES pays. `adapters.yes_side()` fixes it.
- **Fees.** About 4% of the stake on small orders near even money, fitted from
  the account's own fills (`fees.py`). Every past slow-lane signal fails once
  the fee is included.
- **Speed of information.** Headline feeds ran 15-20 minutes behind
  publication; the book had usually moved by then. MLB's live game feed was
  about 30 seconds behind in two spot checks (not yet measured in-game).
  Faster inputs, not a faster model, are the lever.
- **Which books trade.** On one baseball game, 360 of 415 markets were player
  props with empty books; game lines traded at 0.5-1 cent spreads.
- **Some categories are arithmetic.** Weather is priced from a forecast, not
  researched: the LLM tiers returned the market's own price four times for
  $0.20. Ensembles (including WeatherNext 2) need a week or two of settled days
  before their probabilities can be trusted; raw ones were badly biased at
  coastal stations.

## What was measured and what was not

| claim | evidence | verdict |
| --- | --- | --- |
| Jev filters cheaply and fast | every run | holds |
| Politics veto separates cleanly | 17 labelled markets | holds on that set |
| Scenario recognition beats direction guessing | 5 events vs 15 market calls, one game | promising, tiny sample |
| The fast lane's call on 9/21 was right | one game, settled ladder | one data point |
| Any strategy has an edge after fees | 21 events on two NFL days; paper lanes; 681 Kalshi pairs none positive | **not shown** |
| Favourites at 0.92-0.96 are overpriced | 15 events, two days, one sport | hypothesis only |

Markets inside one event settle together, so the honest sample size is events
(and days), not markets. A claim about returns needs roughly 100+ events over
10+ days; this project never ran long enough to get there.

## If this is picked up again

1. Schedule `run_daily.py` and the 10-minute `closer.py --capture`
   (`python run_daily.py --print-schedule`). Data only compounds if collected.
2. Run the fast lane with `--fast-sources` during live MLB and measure the
   game feed's real lag.
3. Add a free odds key (`reference.py`) to compare prices with sharp books.
4. After two weeks, read closing line value, paper equity per lane, the weather
   source scores and the Kalshi pair log. Decide from events, not bets.

The reusable pattern, beyond betting: **an LLM writes a brief offline (facts
plus scenarios, each priced or actioned in advance); Jev recognises in real
time which scenario the incoming text realises; code acts on the pre-decided
consequence.** No model sits on the fast path except the one built for it.
