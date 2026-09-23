---
id: thin_props_periods
title: Player props and period lines
tags: nfl, mlb, nba, nhl, cfb, soccer, mls, esports, cs2, lol, r6
kinds: astatc
period: yes
research: yes
priority: 20
---
Evidence markers: [general] domain knowledge, [measured: ...] observed in
this repo, [rule] operator rule, [unverified: ...] unchecked lead.

## Applies when
- Player and team stat props (slug prefix astatc) and any quarter, half, period or first-five line in a sports market. Game lines load their league skill too. [rule]

## Search
- <game> injury report and lineup <date> (research the game, never the prop)

## Facts that matter
- Props are most of the listing and most are empty books: on one MLB game 360 of 415 markets were player props, with listed 0.01/0.99 prices (a 0.98 spread). [measured: Padres-Dodgers 2026-09-22, fastlane.KIND_RANK]
- The period lines nearest 0.50 did not move a tick in 30 minutes after the starting QB left the game, while the full-game spread moved 0.53 to 0.37. Thin derivative books do not reprice on news. [measured: Giants-Rams 2026-09-21, one game]
- Game lines are where the book reprices; the watchlist already ranks them first. [measured: same game]

## Pitfalls
- Do not spend a search on a prop or a period line; one search on the game covers every market in it. [rule]
- A listed 0.50 over a 0.01/0.99 book is not a 0.50 market. [measured: fastlane.listed_width]
- How a prop settles when its player is inactive (void, NO, or pushed) has not been checked here. [unverified: no settled example recorded]

## Brief
- For props and period lines, repeat the current price as fair_yes in every scenario except one that names the prop's own player or decides the period. [general]
