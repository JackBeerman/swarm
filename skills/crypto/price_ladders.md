---
id: price_ladders
title: Crypto price ladders
tags: crypto, btc, eth, sol
kinds: cpc
research: yes
priority: 10
---
Evidence markers: [general] domain knowledge, [measured: ...] observed in
this repo, [rule] operator rule, [unverified: ...] unchecked lead.

## Applies when
- Price-range and hit-price ladders on a coin (slug prefix cpc: "Bitcoin price at the end of 2026", "How low will Bitcoin get this year"). [measured: shadow.db tags survey]

## Search
- <coin> spot price now and implied volatility (one query; nothing else)

## Trust
- Exchange spot prices and an options-implied volatility index for the level and the spread of outcomes. [general]
- Never use price predictions, analyst targets or trading-signal content. [rule]

## Facts that matter
- A ladder is a question about the distribution of a random walk: current spot, time left and volatility decide it. News matters only through the price it has already moved. [general]
- Spot and perpetual futures trade around the clock and move together; ETF-flow and funding headlines report moves that already happened. [general]
- Under a driftless random walk a "touches X by date" market is roughly twice as likely as "ends above X on date" for the same level. [general]
- Rungs must be coherent: P(touch a higher level) cannot exceed P(touch a lower one), and range buckets sum to one; arb.py scans complete sets for that. [general]

## Pitfalls
- Most crypto headlines are regulatory or political and scored about 0.9 on the political veto, so they are removed before anything acts on them. [measured: README smoke test 2026-09-22]
- A sports-worded concerns_event scored Bitcoin news at 0.43 for a Bitcoin market; reworded, 0.95. [measured: tools/probe_concerns.py]
- An RSS headline arrives minutes after the market it describes has moved (15.6 min median on NFL feeds); a 24-hour spot market has repriced by then. [measured: NFL feeds; not measured on crypto feeds]

## Brief
- facts: spot price with timestamp, a volatility figure with its source and date, days to resolution. Nothing regulatory. [rule]
- scenarios: spot moves up or down by a stated percentage (for example 5% and 10%) from the timestamped level, with fair_yes for every rung; no scenario about a regulator, court, law or government. [rule]
