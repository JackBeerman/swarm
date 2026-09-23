---
id: temperature_bands
title: Daily high temperature bands
tags:
kinds: tc-temp
research: no
priority: 0
---
Evidence markers: [general] domain knowledge, [measured: ...] observed in
this repo, [rule] operator rule, [unverified: ...] unchecked lead.

## Applies when
- "Highest temperature in <city> on <date>" band markets (slug prefix tc-temp). [measured: shadow.db tags survey]

## Search
- None. Do not search.

## Facts that matter
- These markets are priced in code: weather.py from the NWS point forecast, weather_ensemble.py from ensemble members. No LLM and no research are part of that path. [rule]
- The market settles on the NWS Daily Climate Report, whose day runs midnight to midnight local standard time all year. [general]

## Pitfalls
- The first weather paper run spent $0.20 on three gatherers and Tier 3, and returned the market's own price on every market: the gatherers searched for a temperature that did not exist yet and fell back to climate normals. [measured: weather.py docstring, 2026-09-22]

## Brief
- Do not research. Report that this market is priced by weather.py and weather_ensemble.py, with data_confidence 0.0. [rule]
