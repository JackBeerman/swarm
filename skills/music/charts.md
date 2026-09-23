---
id: charts
title: Music charts
tags: music, sptfy, bilbrd
kinds: ccrc, ccpc
research: yes
priority: 10
---
Evidence markers: [general] domain knowledge, [measured: ...] observed in
this repo, [rule] operator rule, [unverified: ...] unchecked lead.

## Applies when
- Chart and streaming-rank markets: Billboard #1 album (slug prefix ccpc, token bilbrd) and Spotify top artist (ccrc, token sptfy). Some rows carry no tags, so the slug tokens are matched too. [measured: shadow.db tags survey]

## Search
- <artist> new album release date announced
- Billboard 200 this week number one <date>
- <artist> first week album units projection
- Spotify most streamed artist <year> so far

## Trust
- Billboard's own chart pages and chart-news articles for positions and units. [general]
- The artist's or label's official announcement for release dates. [general]
- Spotify's own charts and announcements for streaming ranks. [general]

## Facts that matter
- The Billboard 200 ranks albums by equivalent album units (sales plus track and streaming equivalents) over a Friday-to-Thursday tracking week; the top of the chart is reported on Sunday. [general]
- New albums release on Fridays, so a #1 debut is decided by the first tracking week and first-week projections appear early in that week. [general]
- For "who will have a #1 album this year" markets the release calendar is the fact that matters: no release, no debut. [general]
- Spotify's year-end artist ranking comes from a tracking window that closes before year end. [unverified: the exact cutoff date has not been checked]

## Pitfalls
- A chart headline early in the week is usually a projection, not the chart. [general]
- A surprise release or deluxe edition can re-enter an album into contention. [general]
- Tour dates and award wins are not chart facts. [general]

## Brief
- facts: confirmed release dates for the named artists; the current #1 and the date of that chart; the leader's units or streams with date. [general]
- scenarios: a surprise album announced; a release date confirmed or delayed; a projection naming a #1 debut; the chart itself published. [general]
