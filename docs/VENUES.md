# New venues: Kalshi cross-venue arbitrage and a crypto assessment

Status: **shadow only.** Nothing here has an account, a key or an order
path. Researched and built 2026-09-23. Items marked *(unverified)* were
not confirmed against a primary source.

## TL;DR, ranked by expected value per unit of risk and effort

| rank | idea | expected value | risk | effort | verdict |
|---|---|---|---|---|---|
| 1 | **Keep `xvenue.py` running in shadow** (Polymarket US vs Kalshi, MLB/NFL game lines and daily highs) | ~0 today: the first live scan found no pair positive after fees (see [Measured](#measured-2026-09-23)) | low: read-only, public data | done | Record twice a day for 2+ weeks. Open a Kalshi account only if positive-after-fee pairs show up repeatedly *with depth* |
| 2 | Weather pairs specifically | same as above, and settles daily | publisher mismatch (TWC vs NWS CLI) | done | Watch, and score how often the two publishers disagree once results land |
| 3 | Cross-exchange spot crypto gaps (Coinbase vs Kraken) | negative: gaps ~1 bp vs ~170 bp round-trip fees | medium | n/a | **Do not build** |
| 4 | Meme coins (any platform) | negative expected value for a $100 book | high: rug pulls, pump-and-dumps, spreads | n/a | **Do not build** |
| 5 | Polymarket crypto ladders vs Kalshi / options-implied probabilities | unknown, model-dependent | settlement-source and model risk | medium | Not an arbitrage; possible later as a *reference-price* study, not a trade |

The honest summary: cross-venue arbitrage is the only idea here whose
profit does not depend on being right about anything, and it is now
measured automatically. Whether it ever pays is an empirical question the
recorder will answer; the first scan says the two books track each other
closely (edges of about -2 to -4 cents per $1 set after fees).

---

## A. Kalshi

### API (verified 2026-09-23, docs plus live GETs paced >= 2 s)

- Base URL: the docs' market-data quick start uses
  `https://external-api.kalshi.com/trade-api/v2`; the older
  `https://api.elections.kalshi.com/trade-api/v2` returned identical
  results (both 200 on `/series/KXHIGHNY`). The hostname says
  "elections" but serves every category.
  [quick start](https://docs.kalshi.com/getting_started/quick_start_market_data)
- **Reads need no authentication:** "No authentication headers are
  required for the endpoints in this guide" (series, events, markets,
  orderbook). The WebSocket *does* require keys, so live streaming would
  need an account. [docs index](https://docs.kalshi.com/llms.txt)
- Endpoints used: `GET /series/{ticker}`,
  `GET /events?series_ticker=&status=open&limit=200&with_nested_markets=true`
  (cursor pagination), `GET /markets/{ticker}/orderbook?depth=N`.
- Wire facts: prices are dollar strings in `*_dollars` (`"0.4600"`),
  sizes fixed-point strings in `*_fp` (`"3230.50"`), fractional contracts
  allowed (min 0.01). The listed market carries top-of-book sizes
  (`yes_bid_size_fp`, `yes_ask_size_fp`).
- **The orderbook returns bids only**: `orderbook_fp.yes_dollars` and
  `no_dollars`, ascending, best bid last; a YES ask at p is a NO bid at
  1 - p. [orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses)
- Rate limits are token buckets per account tier; 429 carries no
  Retry-After; "apply exponential backoff". Limits for unauthenticated
  reads are not documented, so `kalshi.py` paces at >= 1.5 s and backs off.
  [rate limits](https://docs.kalshi.com/getting_started/rate_limits)

### Fees

- **General formula** (Kalshi Fee Schedule, 2nd iteration, 2022-09-22, as
  filed with the CFTC): `fees = round up(0.07 x C x P x (1-P))`, rounded
  up to the next cent; no settlement, membership or ACH deposit fee; $2
  per withdrawal. [CFTC filing](https://www.cftc.gov/sites/default/files/filings/orgrules/22/09/rule091222kexdcm003.pdf).
  The 2022 table (100 contracts at $0.50 -> $1.75; at $0.10 -> $0.63) is
  pinned in `tests/test_kalshi.py`.
- **Per-series fee fields** in the live API: `fee_type` and
  `fee_multiplier`. Seen 2026-09-23: daily highs `quadratic` x1; MLB
  moneyline `quadratic_with_maker_fees` x0.5; MLB spread and total
  `quadratic` x0.5; NFL moneyline `quadratic_with_maker_fees` x1; BTC
  hourly `quadratic` x1. The schema says fee types are defined in the fee
  schedule PDF and the multiplier is "applied to the fee calculations".
  [series schema](https://docs.kalshi.com/api-reference/market/get-series)
- *(unverified)* That the multiplier scales the 0.07 taker coefficient
  (so MLB spreads cost 0.035 x P(1-P)). The current PDF
  (`kalshi.com/docs/kalshi-fee-schedule.pdf`) returned HTTP 429 to every
  fetch; secondary 2026 write-ups give `M x 0.07 x C x P x (1-P)` taker
  and `M x 0.0175 x ...` maker
  ([marketmath](https://marketmath.io/platforms/kalshi)). **Because of
  this, every pair is also priced with the multiplier forced to >= 1**
  (`edge_*_m1`), and depth is only walked for pairs positive on that
  conservative number.
- Rounding: fees are computed to $0.000001 and balances of non-direct
  members are aligned to $0.01 with a per-order accumulator that rebates
  over-rounding. [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding).
  `kalshi.taker_fee()` rounds up to the cent per fill, which can only
  overstate cost.
- Polymarket US side: the listed market's `feeCoefficient` (0.0695 on
  every market seen) with `fees.fee_usd` (p(1-p), rounded up per fill).

### Settlement differences: this is the entire risk

Matching removes differences you can see in structure (teams, date, line,
band, period). What remains is written on every `xvenue.db` row (`risk`):

| category | Kalshi rule (rules_primary / contract terms) | Polymarket US rule (market description) | residual risk |
|---|---|---|---|
| **MLB, all game lines** | Postponed: open if started within **48 h**, else "last fair price as determined by the Exchange". Shortened but official: settles on the official result. Extra innings included. ([BASEBALLGAMEWIN](https://assets.kalshi.com/contract_terms/BASEBALLGAMEWIN.pdf)) | "delayed, postponed, or suspended and not rescheduled to a date within **two weeks**... settle to the last fair market price". Extra innings included; shortened official games settle on the result. | A game moved 3-14 days: Kalshi settles at its own fair price, Polymarket waits for the game. The hedge then becomes two independent bets. Both venues' "fair price" is set by each venue separately. |
| **NFL moneyline** | Overtime included; **tie resolves $0.50 per team**; postponed beyond 48 h -> fair price. ([FOOTBALLGAMEWIN](https://assets.kalshi.com/contract_terms/FOOTBALLGAMEWIN.pdf)) | Overtime included; "If the game ends in a tie, the market will settle to $0.50"; two days. | Close to identical. Fair-price settlements are independent. |
| **NFL spread / total** | Kalshi lists only "X wins by more than S" and "Over T"; overtime included by default. | Full-game spread: **two weeks**; full-game total: two days; overtime included. On underdog ("pos") spreads the description names the other team (see CLAUDE.md), so by default those are **skipped** (`--trust-sides` admits them). | Postponement window mismatch on spreads. |
| **Daily high temperature** | Station named in rules, e.g. "New York City (CLINYC)", "Chicago (CLIMDW)"; value "according to **The Weather Company**" (weather.com/kalshi); contract terms list NWS as Source Agency; material-error holds; "no data -> last fair price". ([GLOBALTEMPERATURE](https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf)) | "Central Park (KNYC) ... as reported by the **National Weather Service's Climatological Report (Daily)**". | Same station and the same CLI number in principle, published by two parties. A rounding or revision difference on a band edge breaks the hedge. Not yet measured. |
| Crypto (Kalshi KXBTCD etc.) | Hourly/daily levels on **CF Benchmarks BRTI** averages. | Polymarket US crypto markets seen are year-long "hit price" markets. | Different proposition, not matched. |
| Economics, Fed, politics | not read | not read | Excluded (operator rule; federal policy is restricted). |

### What was built

- **`kalshi.py`**: paced read-only GET client with a hard wall-clock
  deadline; `market_quote`, `ladders` (bids -> buy ladders),
  `taker_fee`, `series_blocked` (category deny-list plus
  `questions.political_tag`). A test greps the module so it can never
  grow a write verb, `/portfolio`, `/orders` or key handling.
- **`xvenue.py`**: reads an **allow-list** of Kalshi series (MLB and NFL
  game/spread/total; NYC, Chicago, LA, Miami, SF daily highs), three
  Polymarket `events.list` calls, proves pairs, prices both hedge
  directions at the touch after both fees, walks both books only for
  pairs positive on the conservative fee (at most 6 Polymarket book calls),
  and records to `xvenue.db` (`scans`, `pairs`, `skips`).
  - Sports equivalence: same sport, same teams (Kalshi ticker
    abbreviations -> a verified table -> `reference.TEAMS`; the Kalshi
    event's team string must split into two known teams exactly one way),
    same originally scheduled date, start within 30 min when Kalshi's
    ticker gives a time, same kind and full-game period, same half-point
    line. Polymarket's YES side comes from `reference.parse_market`
    (marketSides, i.e. `adapters.yes_side` logic), never the title.
    Kalshi's team is read from the ticker, checked against
    `yes_sub_title`, and on spreads the team id in `custom_strike` must
    equal the id on that game's moneyline market. Rule text must agree
    with the strike. Complements are handled explicitly: Polymarket
    "White Sox +1.5" pairs with Kalshi "Royals win by more than 1.5"
    as YES+YES.
  - Weather equivalence: station from Polymarket's "(KNYC)" and Kalshi's
    "(CLINYC)", date from both slug/ticker and rule text, band from
    Polymarket's slug and Kalshi's strikes cross-checked against its label.
  - Skipped with a counted reason: doubleheaders, rescheduled games,
    first-five and team totals, lines Kalshi does not list, NFL
    text-conflict spreads (default), unknown teams, political tags.
- Tests: `tests/test_kalshi.py`, `tests/test_xvenue.py` on trimmed real
  responses in `tests/fixtures/xvenue/` (both venues, captured
  2026-09-23). No network, no keys.

```bash
python xvenue.py                  # one scan, hard 240 s cap, then report
python xvenue.py --report
```

Not added to `run_daily.py` (kept the edit surface minimal); adding
`("xvenue", ["xvenue.py"], 300)` to `STEPS` and `"xvenue.py"` to
`EXCHANGE_HEAVY` is the one-line change once the operator wants it daily.

### Measured (2026-09-23)

One live scan, 2026-09-23 20:51 UTC, 46 s, **3 Polymarket calls and 22
Kalshi calls** (no book calls were needed, since nothing cleared fees):

- Claims parsed: Polymarket 1,039 full-game lines and temperature bands;
  Kalshi 1,241 allow-listed markets.
- **Matched pairs: 681** (MLB 169, NFL 452, weather 60). Every
  Polymarket temperature band on both listed days (5 cities x 2 days x 6
  bands) had an identical Kalshi band at the same station.
- **Positive after fees: 0**, on the reported fee multiplier and on the
  conservative one.
- Best edge per $1 set, after fees: median **-3.1 cents** (MLB -3.0,
  NFL -3.2, weather -2.0); best -0.07 cents. 33 of 661 priced pairs were
  within 1 cent of break-even, and they are all tails (prices near 0.04
  or 0.99, where fees vanish and one tick is the whole gap), so even a
  positive print there would be worth cents.
- Near-misses (a proven game with no Kalshi line at Polymarket's number):
  MLB 35, NFL 364. Skipped on the Polymarket side: first-five (175),
  team totals and all period markets (Kalshi lists none of these in the
  allow-listed series), and **336 NFL underdog spreads whose exchange text
  names the other side** (admitted only with `--trust-sides`).
- Read: the two venues price the same game lines within about a tick of
  each other, and the ~3-cent median is roughly both venues' fees plus
  half-spreads. An arbitrage here, if one appears, will be brief (around
  news or line moves), so it needs frequent scans, which is exactly what
  the Polymarket rate limit makes expensive. That is the main reason this
  is ranked "measure, don't fund".

### What the operator would need to trade it (not done, not recommended yet)

- A Kalshi account: US resident, KYC (identity documents, SSN), bank
  link; API keys (RSA key pair created in account settings) for the
  WebSocket and any order. Funds split across two venues: at $100 total,
  each leg gets ~$50, which caps a hedge at roughly 50-100 contracts.
- Both legs must fill. Legging risk (one side fills, the other moves) is
  real at these sizes and is not modelled; the recorder assumes both
  touches are hit simultaneously.
- Capital is locked until the later of the two settlements; Kalshi pays
  within minutes, Polymarket US within minutes to hours.

---

## B. Crypto and "meme coins"

### Platforms with an official trading API

| platform | API | reads without a key? | fees at the lowest tier | account / KYC | automation |
|---|---|---|---|---|---|
| Robinhood Crypto | Crypto Trading API (v1, v2 with fee tiers) | *(unverified)* docs show keyed endpoints for quotes; public market data not documented | 0.00%-0.95% by tier; v2 API orders "charged the taker rate until maker/taker is fully rolled out" ([fee tiers](https://robinhood.com/us/en/support/articles/crypto-fee-tiers)) | Robinhood Crypto account, US only; keys created in web settings ([API](https://robinhood.com/us/en/support/articles/crypto-api)) | Permitted via the API; no stated restriction found |
| Coinbase Advanced | Advanced Trade API | yes (public product/ticker endpoints; used below) | US entry tier **0.50% maker / 0.90% taker** (announced 2026-09-16; [securities.io](https://www.securities.io/coinbase-lowers-advanced-trading-fees-with-tiers-starting-at-10-000/); primary page returned 403) | Coinbase account, KYC | API keys, permitted |
| Kraken Pro | REST + WebSocket | yes (public Ticker; used below) | **0.40% maker / 0.80% taker** at $0+ ([fee schedule](https://www.kraken.com/features/fee-schedule)) | Kraken account, KYC | API keys, permitted |

### Is there a testable, low-risk edge for $100?

**Cross-venue spot gaps on liquid coins: no.** One public-ticker probe,
2026-09-23 20:51 UTC, no keys:

| pair | Coinbase bid / ask | Kraken bid / ask | best cross gap |
|---|---|---|---|
| BTC-USD | 84,305.16 / 84,305.17 | 84,298.40 / 84,298.50 | ~0.8 bp (buy Kraken, sell Coinbase) |
| DOGE-USD | 0.09198 / 0.09201 | 0.0919779 / 0.0919780 | ~0.2 bp |

Round-trip taker cost at entry tiers is ~0.90% + 0.80% = **~170 bp**,
two orders of magnitude above the gap, before withdrawal/transfer time
and fees to rebalance inventory between exchanges. Professional market
makers keep these gaps closed; a $100 account on retail tiers cannot
compete. One snapshot, but the fee gap is structural.

**Meme coins: no, and plainly so.** The documented base rate is hostile:
Solidus Labs found **98.6% of tokens launched on Pump.fun** collapsed into
pump-and-dump patterns and ~93% of 388,000 Raydium pools showed soft
rug-pull traits (Jan 2024-Mar 2025,
[report](https://www.soliduslabs.com/reports/solana-rug-pulls-pump-dumps-crypto-compliance));
academic work finds even established memecoins (DOGE, SHIB, PEPE) carry
intermediate-to-high fragility and politically themed coins the highest
([arXiv 2512.00377](https://arxiv.org/abs/2512.00377)). Politically themed
coins are also off-limits under the operator's no-politics rule. The
listed memecoins on regulated US apps (DOGE, SHIB, PEPE, BONK, ...) are
the survivors; trading them is a directional bet on sentiment with 1-2%
round-trip cost at $100 scale. There is no "shadow" version of that
which measures an edge rather than a gamble, so nothing was built.

**Polymarket crypto ladders vs Kalshi or options: not an arbitrage.**
Kalshi's BTC markets settle on CF Benchmarks' BRTI at a stated hour
(series `KXBTCD`, hourly, quadratic x1); the Polymarket US crypto markets
seen in `shadow.db` are year-long "how high/low will Bitcoin get" touch
markets. Different propositions and sources, so no provable pair exists.
Comparing either to an options-implied probability (e.g. Deribit implied
vol, public without a key) is a *model* (barrier probability under a vol
assumption), not a hedge; it could be a later reference-price study in
the style of `reference.py`, scored on outcomes, but it is not low-risk
profit and it parks capital for months.

**Decision: no crypto recorder or bot was built.** The only defensible
crypto-adjacent work is already covered: if Kalshi and Polymarket ever
list the same crypto proposition on the same source, `xvenue.py` is where
it belongs.
