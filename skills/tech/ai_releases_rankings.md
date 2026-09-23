---
id: ai_releases_rankings
title: AI model releases and rankings
tags:
kinds: aimc, aimrc
research: yes
priority: 10
---
Evidence markers: [general] domain knowledge, [measured: ...] observed in
this repo, [rule] operator rule, [unverified: ...] unchecked lead.

## Applies when
- "Is <model> released by <date>" (slug prefix aimc) and "which company or model is #1" (aimrc) markets. IPO, CEO and net-worth markets are not covered. [rule]

## Search
- <product> release official announcement <company> blog
- <product> general availability API docs changelog
- <leaderboard named in the rules> leaderboard current top models <month year>
- <company> event date <month year> keynote

## Trust
- The company's own newsroom, blog, developer documentation, changelog and model card: these decide "released". [general]
- For rankings, the leaderboard named in the market rules, read directly, with the date read. [general]
- Dated reporting from established tech outlets (The Verge, Ars Technica, TechCrunch) for timing statements. [general]
- Leakers, "spotted in code" posts and rumour accounts are not facts. [general]

## Facts that matter
- What counts as released is written in the market's rules: public availability, a preview, an API model id, or only an announcement. Read `description` before anything else. [general]
- Launches are staged: announcement, waitlist or preview, then general availability, and app and API can ship on different days. [general]
- Arena-style leaderboards rank with confidence intervals, so several models can share rank 1, and a new model needs days of votes before it is listed. [general]
- Launches cluster around company events and competitors' launches. [general]

## Pitfalls
- IPO markets resolve on a regulatory filing and were vetoed at 0.83 on federal_policy_outcome; they are not this skill. [measured: fastlane.drop_restricted, 2026-09-22]
- No scenario may depend on a government, regulator, court or political figure; those headlines are never acted on. [rule]
- Tech feeds differ in freshness: The Verge within minutes, TechCrunch and the Google blog up to half a day behind. [measured: feed probe 2026-09-22, fastlane.FEEDS_BY_TAG]
- A headline about testing, a leak or a benchmark is not a release. [general]

## Brief
- facts: the newest shipped version with its date; the company's latest dated statement on timing; for rankings, the current top three with scores and the date read. [general]
- scenarios: official general availability; a preview or waitlist only; an explicit delay or denial; a rival model taking #1 on the named leaderboard. [general]
