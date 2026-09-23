---
id: mlb_game_lines
title: MLB game lines
tags: mlb, baseball
kinds: aec, asc, tsc, atc, cks
research: yes
priority: 10
---
Every bullet outside "Search" carries its evidence: [general] is domain
knowledge, [measured: ...] was observed in this repo, [rule] is an operator
rule, [unverified: ...] is a lead nobody has checked. Selection is in
skills.py; this file is read, never written, by code.

## Applies when
- Full-game and first-five-innings MLB lines: moneyline (aec), run line (asc), total (tsc), inning outcomes (atc). Player props (astatc) and futures (tec) are not covered. [rule]
- `cks` is included because fastlane.KIND_RANK ranks it with spreads. [unverified: what market type cks is has not been recorded]

## Search
- <away team> at <home team> probable pitchers <date>
- <team> starting lineup <date>
- <team> bullpen usage last three days <closer name>
- <ballpark> weather forecast <date> wind first pitch
- <team> injured list moves <date>

## Trust
- MLB.com probable pitchers and the teams' own lineup posts are the primary sources for who starts. [general]
- Established beat writers and lineup aggregators (RotoWire, team beat reporters at major outlets) for scratches and rest days. [general]
- National Weather Service forecasts for wind and rain at open-air parks. [general]
- Never use picks, predictions or "best bets" articles: betting knowledge here comes only from this repo's scored history. [rule]

## Facts that matter
- The two starting pitchers are the largest single input to a moneyline and to every first-five-innings (f5) line; a late scratch reprices both. [general]
- Lineups post a few hours before first pitch; a rested regular (day game after a night game, a clinched or eliminated team in September) is the most common pregame news. [general]
- Relievers who pitched on consecutive days are often unavailable; this matters for full-game lines, not f5. [general]
- Wind direction at open parks (Wrigley especially) and altitude (Coors) move totals; a closed roof removes weather. [general]
- Roughly 30% of MLB games are decided by one run, so the -1.5 run line is far less likely than the moneyline for the same team. [general]
- Late season, a clinched team caps starter pitch counts and rests regulars; that is news only on the day it is announced. [general]

## Pitfalls
- Read what YES pays from the market's YES side (the `outcome` leg, set by adapters.yes_side from the exchange's structured side), never from the title. On underdog "pos" run lines the title names the other team; YES pays the underdog plus the line. [measured: settled NFL ladder 2026-09-23; three real orders went on the wrong side before the fix]
- Moneyline titles read "Team A vs Team B" and name no side; the YES leg now reads "<team> wins". [measured: fastlane.db signals 2026-09-22]
- In the seven MLB briefs stored on 2026-09-22, 18 of 58 facts were season-long absences (60-day IL, out for the season): already priced, and no headline tonight can contradict them. [measured: fastlane.db briefs]
- In the same briefs only 4 of 47 scenarios were pregame or weather triggers; 7 were early leads of 5+ runs (some 14+) that almost never happen and 7 were one player's hits or home runs, which barely move a game line. [measured: fastlane.db briefs 2026-09-22]
- One brief named two different starters for the same team. The probable starter must be one named, dated fact. [measured: one brief, mlb-sd-lad 2026-09-22]
- A brief written with this playbook still wrote two 14-run-blowout scenarios, and priced one on the wrong side: Baltimore up 14 put Toronto -1.5 at 0.98. Check every scenario against the ladder: P(team -1.5) <= P(team wins) <= P(team +1.5). [measured: one A/B brief, Blue Jays-Orioles 2026-09-22]
- RSS headlines arrived a median 15.6 minutes after publication in the one game measured; a fact already in an RSS headline is usually priced. [measured: Giants-Rams 2026-09-21, NFL feeds]

## Brief
- facts: both probable starters by name with the date read; whether lineups are posted yet; unavailable relievers; wind and rain at an open park; clinched or eliminated status. Skip season-long injuries unless the player returns today. [general]
- scenarios: a starter scratched (each side); a named regular out of the lineup; a rain delay or postponement; a starter leaving early injured; the closer unavailable. Do not write triggers about one player's hits or improbable blowouts. [measured: stored-brief audit above]
- f5 lines depend on the starters only; leave them at the current price in bullpen scenarios. [general]
- Each fair_yes is for the YES side as listed in the market list, and a scenario that hurts the listed YES team lowers it. [measured: wrong-side orders]
