---
id: nfl_game_lines
title: NFL game lines
tags: nfl
kinds: aec, asc, tsc, cks
research: yes
priority: 10
---
Evidence markers: [general] domain knowledge, [measured: ...] observed in
this repo, [rule] operator rule, [unverified: ...] unchecked lead.

## Applies when
- NFL moneyline (aec), spread (asc) and total (tsc) markets, full game and periods. Period lines also load thin_props_periods. [rule]

## Search
- <team> final injury report week <n> game status
- <team> inactives <date>
- <team> starting quarterback <date>
- <stadium> weather <date> kickoff wind

## Trust
- The league's official injury report and team announcements for designations (out, doubtful, questionable). [general]
- Established beat reporters and national insiders at major outlets for inactives and late downgrades. [general]
- National Weather Service forecasts for wind and precipitation at open stadiums. [general]
- Never use picks, predictions or "best bets" articles: betting knowledge here comes only from this repo's scored history. [rule]

## Facts that matter
- The starting quarterback dominates: a starter ruled out moves a spread by several points, most other single absences by a point or less. [general]
- The final injury report comes out two days before a Sunday game; inactives are published about 90 minutes before kickoff. [general]
- "Questionable" is not "out": most questionable players play. [general]
- Sustained wind of roughly 15-20 mph or more lowers passing and totals; temperature alone matters little; domes remove weather. [general]
- 3 and 7 are the most common final margins (roughly 15% and 9% of games historically), so a scenario that moves the expected margin across 3 or 7 moves a spread's fair price more than the same shift elsewhere. [general]

## Pitfalls
- Read what YES pays from the market's YES side (the `outcome` leg, set by adapters.yes_side), never the title. On underdog "pos" spreads the title and settlement text name the favourite, but YES pays the underdog plus the line: verified on the Giants-Rams ladder (pos 0.5 settled NO, pos 33.5 YES on a 28-6 final). Three real orders were placed on the wrong side this way. [measured: README, 2026-09-23]
- Without a roster in the brief, "Puka Nacua ruled out" (no team named) was read as raising the Rams' chance to cover; with the roster it lowered the Rams' total. [measured: tools/probe_fastlane.py]
- RSS headlines arrived a median 15.6 minutes after publication (fastest 1.9 minutes) in one game; orders placed 17 minutes after the first signal all lost. [measured: Giants-Rams 2026-09-21]
- One injury produced eight headlines, each re-flagging the same markets. [measured: Giants-Rams 2026-09-21]
- The full-game spread repriced after the starting QB left (0.53 to 0.37); the period lines nearest 0.50 did not move in 30 minutes. [measured: one game]

## Brief
- teams: starting QB first, then skill players, pass rushers, kicker, and every player on the final injury report. [measured: roster probe above]
- facts: each injury designation with the report date; weather and wind at kickoff; any QB change this week. [general]
- scenarios: the starting QB ruled out or leaving injured (each side); a WR1 or RB1 inactive; wind above 20 mph at kickoff; a 14+ point lead at half. Avoid one player's yardage as a trigger. [general]
- Each fair_yes is for the YES side as listed in the market list; a scenario that hurts the listed YES team lowers it. [measured: wrong-side orders]
