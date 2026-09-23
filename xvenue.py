"""
xvenue.py -- cross-venue arbitrage, Polymarket US vs Kalshi. Shadow only.

SHADOW ONLY. Places no orders on either venue and imports no order path.
It records what a two-leg hedge would have cost and paid, after both
venues' fees, at the prices and depth actually on the books.

If proposition X is listed on both venues,

    buy YES(X) on one venue + buy NO(X) on the other   pays exactly $1

whatever happens, so the pair profits when the two prices plus both fees
come to less than $1. When a Kalshi contract is the COMPLEMENT of the
Polymarket one (Polymarket "White Sox +1.5" vs Kalshi "Royals win by over
1.5"), YES+YES is the hedge instead.

THE WHOLE RISK IS "THE SAME PROPOSITION". Two venues can list the same
game and still settle differently (postponement windows, who decides a
"fair price", which publisher's number counts). A pair is recorded only
when equivalence is proved from STRUCTURED fields:

  sports   same sport, same two teams (both venues' abbreviations checked
           against reference.py's tables), same originally-scheduled date,
           start times within 30 min when Kalshi states one, same market
           kind, same full-game period and the same half-point line; the
           Polymarket YES side comes from marketSides (reference.parse_market
           / adapters.yes_side), never the title; the Kalshi team comes from
           the ticker and is cross-checked against the team id on the game
           market.
  weather  same station (Polymarket "(KNYC)" <-> Kalshi "(CLINYC)"), same
           date, identical integer band.

Everything else is skipped with a reason and counted. Residual settlement
differences that structure cannot remove are written on every row
(`risk`) and set out per category in docs/VENUES.md.

Politics is excluded three ways: only allow-listed Kalshi series are read,
every series is checked against a category deny-list and
questions.political_tag(), and Polymarket events are checked with
political_tag() too.

    python xvenue.py                 # one scan (hard wall-clock cap), record, report
    python xvenue.py --report        # what has been recorded
    python xvenue.py --max-seconds 240 --max-books 6
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import kalshi
from arb import band_of
from fees import FEE_RATE, fee_per_share, fee_usd

log = logging.getLogger("xvenue")

DB_PATH = os.getenv("XVENUE_DB", "xvenue.db")

# --------------------------------------------------------------------------
# what is read -- an allow-list, never a crawl
# --------------------------------------------------------------------------

SPORT_SERIES: dict[str, dict[str, str]] = {
    "mlb": {"KXMLBGAME": "h2h", "KXMLBSPREAD": "spreads", "KXMLBTOTAL": "totals"},
    "nfl": {"KXNFLGAME": "h2h", "KXNFLSPREAD": "spreads", "KXNFLTOTAL": "totals"},
}
WEATHER_SERIES = ("KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHMIA", "KXHIGHTSFO")
ALLOWED_SERIES = {s for m in SPORT_SERIES.values() for s in m} | set(WEATHER_SERIES)

#: Kalshi team abbreviation -> (Polymarket abbreviation, Kalshi's
#: yes_sub_title on the game market). Every row was read off live open
#: markets 2026-09-23. The Polymarket abbreviation then goes through
#: reference.TEAMS, so a team is only ever named one way.
KALSHI_TEAMS: dict[str, dict[str, tuple[str, str]]] = {
    "mlb": {
        "ATH": ("ath", "A's"), "ATL": ("atl", "Atlanta"), "AZ": ("az", "Arizona"),
        "BAL": ("bal", "Baltimore"), "BOS": ("bos", "Boston"), "CHC": ("chc", "Chicago C"),
        "CIN": ("cin", "Cincinnati"), "CLE": ("cle", "Cleveland"), "COL": ("col", "Colorado"),
        "CWS": ("cws", "Chicago WS"), "DET": ("det", "Detroit"), "HOU": ("hou", "Houston"),
        "KC": ("kc", "Kansas City"), "LAA": ("laa", "Los Angeles A"),
        "LAD": ("lad", "Los Angeles D"), "MIA": ("mia", "Miami"), "MIL": ("mil", "Milwaukee"),
        "MIN": ("min", "Minnesota"), "NYM": ("nym", "New York M"), "NYY": ("nyy", "New York Y"),
        "PHI": ("phi", "Philadelphia"), "PIT": ("pit", "Pittsburgh"), "SD": ("sd", "San Diego"),
        "SEA": ("sea", "Seattle"), "SF": ("sf", "San Francisco"), "STL": ("stl", "St. Louis"),
        "TB": ("tb", "Tampa Bay"), "TEX": ("tex", "Texas"), "TOR": ("tor", "Toronto"),
        "WSH": ("wsh", "Washington"),
    },
    "nfl": {
        "ARI": ("ari", "Arizona"), "ATL": ("atl", "Atlanta"), "BAL": ("bal", "Baltimore"),
        "BUF": ("buf", "Buffalo"), "CAR": ("car", "Carolina"), "CHI": ("chi", "Chicago"),
        "CIN": ("cin", "Cincinnati"), "CLE": ("cle", "Cleveland"), "DAL": ("dal", "Dallas"),
        "DEN": ("den", "Denver"), "DET": ("det", "Detroit"), "GB": ("gb", "Green Bay"),
        "HOU": ("hou", "Houston"), "IND": ("ind", "Indianapolis"),
        # Kalshi JAC = Polymarket jax.
        "JAC": ("jax", "Jacksonville"), "KC": ("kc", "Kansas City"),
        "LAC": ("lac", "Los Angeles C"), "LAR": ("lar", "Los Angeles R"),
        "LV": ("lv", "Las Vegas"), "MIA": ("mia", "Miami"), "MIN": ("min", "Minnesota"),
        "NE": ("ne", "New England"), "NO": ("no", "New Orleans"), "NYG": ("nyg", "New York G"),
        "NYJ": ("nyj", "New York J"), "PHI": ("phi", "Philadelphia"),
        "PIT": ("pit", "Pittsburgh"), "SEA": ("sea", "Seattle"), "SF": ("sf", "San Francisco"),
        "TB": ("tb", "Tampa Bay"), "TEN": ("ten", "Tennessee"), "WAS": ("was", "Washington"),
    },
}

#: Settlement differences that structured matching cannot remove. Read
#: off both venues' rule text 2026-09-23; see docs/VENUES.md for sources.
RISK = {
    "mlb": ("postponed/suspended: Kalshi waits 48h then settles at ITS last fair price; "
            "Polymarket waits two weeks then settles at ITS last fair price"),
    "nfl": ("postponed/suspended: Kalshi 48h; Polymarket two days (moneyline, total) or two "
            "weeks (full-game spread); each venue sets its own fair price. Ties: both 0.50"),
    "weather": ("Kalshi's rules settle on The Weather Company's report of the CLI value; "
                "Polymarket on the NWS Daily Climatological Report. Same station, different "
                "publisher"),
}

MAX_START_GAP = timedelta(minutes=30)
_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_K_EVENT = re.compile(r"^(?P<series>KX[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
                      r"(?P<hhmm>\d{4})?(?P<teams>[A-Z]*)$")
_PERIOD_WORDS = re.compile(r"\b(first|second|1st|2nd|half|quarter|inning|innings|period)\b", re.I)
_K_H2H_RULE = re.compile(r"^If (?P<team>.+?) wins the .+? game originally scheduled for ")
_K_SPREAD_RULE = re.compile(r"^If (?P<team>.+?) wins by more than (?P<x>\d+(?:\.\d+)?) "
                            r"(?:runs|points) in the .+? game originally scheduled for ")
_K_TOTAL_RULE = re.compile(r"collectively score more (?:than )?(?P<x>\d+(?:\.\d+)?) "
                           r"(?:runs|points) in the .+? game originally scheduled for ")
_K_WX_RULE = re.compile(r"maximum temperature recorded at .+? \(CLI(?P<st>[A-Z]{3})\) for "
                        r"(?P<mon>[A-Z][a-z]{2}) (?P<dd>\d{1,2}), (?P<yyyy>\d{4})")
_PM_EVENT_DATE = re.compile(r"-(?P<date>\d{4}-\d{2}-\d{2})(?P<dh>-dh\d)?$")
_PM_WX_EVENT = re.compile(r"^temp-(?P<city>[a-z]+)-(?P<date>\d{4}-\d{2}-\d{2})$")
_PM_STATION = re.compile(r"highest temperature recorded at .+?\((?P<st>K[A-Z]{3})\).+?for "
                         r"(?P<date>\d{4}-\d{2}-\d{2}).+?National Weather Service", re.I | re.S)


class Skip(Exception):
    """Not provably the same proposition; the message is the counted reason."""


def _count(d: dict[str, int], reason: str) -> None:
    d[reason] = d.get(reason, 0) + 1


def _half_point(x: float | None) -> bool:
    return x is not None and abs((x - math.floor(x)) - 0.5) < 1e-9


def et_to_utc(local: datetime) -> datetime:
    """
    US Eastern wall-clock -> UTC. Kalshi tickers and rules are in ET. No
    tz database on this machine (zoneinfo has no tzdata on Windows), so
    the US rule is written out: DST from 02:00 on the second Sunday of
    March to 02:00 on the first Sunday of November.
    """
    y = local.year
    mar1 = datetime(y, 3, 1)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7, hours=2)
    nov1 = datetime(y, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=2)
    offset = 4 if dst_start <= local < dst_end else 5
    return (local + timedelta(hours=offset)).replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Kalshi side
# --------------------------------------------------------------------------

@dataclass
class KClaim:
    """What one Kalshi market's YES pays on, in the same terms as reference.Claim."""
    ticker: str
    game: str                      # event ticker suffix shared by a game's series
    category: str                  # mlb | nfl | weather
    kind: str                      # h2h | spreads | totals | band
    team: str | None               # canonical team (h2h / spreads)
    line: float | None             # team handicap in Polymarket terms (-S), or total
    teams: frozenset[str]
    date: str                      # originally scheduled date, YYYY-MM-DD (ET)
    start_utc: datetime | None
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float | None
    yes_ask_size: float | None
    fee_type: str
    fee_mult: float
    station: str | None = None
    band: tuple[float | None, float | None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def parse_event_ticker(ticker: str) -> dict[str, Any]:
    m = _K_EVENT.match(ticker or "")
    if not m or m["mon"] not in _MONTHS:
        raise Skip("kalshi event ticker not recognised")
    date = f"20{m['yy']}-{_MONTHS[m['mon']]:02d}-{int(m['dd']):02d}"
    start = None
    if m["hhmm"]:
        local = datetime.fromisoformat(f"{date}T{m['hhmm'][:2]}:{m['hhmm'][2:]}")
        start = et_to_utc(local)
    game = ticker.split("-", 1)[1]
    return {"series": m["series"], "date": date, "start_utc": start, "teams": m["teams"],
            "game": game}


def split_teams(s: str, sport: str) -> tuple[str, str]:
    """'NYJDET' -> ('NYJ', 'DET'); must split exactly one way into known teams."""
    table = KALSHI_TEAMS[sport]
    splits = [(s[:i], s[i:]) for i in range(1, len(s)) if s[:i] in table and s[i:] in table]
    if len(splits) != 1:
        raise Skip("kalshi teams do not split uniquely")
    return splits[0]


def _canon(sport: str, kabbr: str) -> str:
    from reference import TEAMS

    pm_abbr, _ = KALSHI_TEAMS[sport][kabbr]
    return TEAMS[sport][pm_abbr]


def _team_uuid(m: dict[str, Any]) -> str | None:
    cs = m.get("custom_strike") or {}
    vals = [v for k, v in cs.items() if k.endswith("_team")]
    return vals[0] if len(vals) == 1 else None


def kalshi_sport_claims(
    sport: str, by_series: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> tuple[list[KClaim], dict[str, int]]:
    """
    by_series: {series_ticker: (series, events)}. Game markets are parsed
    first so spread markets can be checked against the team id they carry.
    """
    claims: list[KClaim] = []
    skips: dict[str, int] = {}
    uuids: dict[tuple[str, str], str] = {}          # (game, abbr) -> team uuid
    order = sorted(by_series, key=lambda s: SPORT_SERIES[sport].get(s) != "h2h")
    for st in order:
        series, events = by_series[st]
        kind = SPORT_SERIES[sport].get(st)
        if kind is None:
            _count(skips, "series not allow-listed")
            continue
        if (why := kalshi.series_blocked(series)):
            _count(skips, f"series blocked: {why}")
            continue
        fee_type = str(series.get("fee_type") or "")
        mult = float(series.get("fee_multiplier") or 1.0)
        for ev in events:
            try:
                info = parse_event_ticker(ev.get("event_ticker") or "")
                a, b = split_teams(info["teams"], sport)
                teams = frozenset((_canon(sport, a), _canon(sport, b)))
            except Skip as s:
                _count(skips, str(s))
                continue
            for m in ev.get("markets") or []:
                try:
                    claims.append(_kalshi_sport_market(sport, kind, m, info, (a, b), teams,
                                                       uuids, fee_type, mult))
                except Skip as s:
                    _count(skips, str(s))
    return claims, skips


def _kalshi_sport_market(sport: str, kind: str, m: dict[str, Any], info: dict[str, Any],
                         abbrs: tuple[str, str], teams: frozenset[str],
                         uuids: dict[tuple[str, str], str], fee_type: str,
                         mult: float) -> KClaim:
    if m.get("status") != "active":
        raise Skip("kalshi market not active")
    if m.get("market_type", "binary") != "binary":
        raise Skip("kalshi market not binary")
    ticker = m.get("ticker") or ""
    suffix = ticker.rsplit("-", 1)[-1]
    rules = m.get("rules_primary") or ""
    if _PERIOD_WORDS.search(m.get("title") or "") or _PERIOD_WORDS.search(rules):
        raise Skip("kalshi market is not full game")
    team = None
    line = None
    if kind == "h2h":
        if suffix not in abbrs:
            raise Skip("kalshi moneyline suffix is not an event team")
        if m.get("yes_sub_title") != KALSHI_TEAMS[sport][suffix][1]:
            raise Skip("kalshi yes_sub_title disagrees with team table")
        if not _K_H2H_RULE.match(rules):
            raise Skip("kalshi moneyline rule text not recognised")
        uuid = _team_uuid(m)
        if not uuid:
            raise Skip("kalshi moneyline carries no team id")
        uuids[(info["game"], suffix)] = uuid
        team = _canon(sport, suffix)
    elif kind == "spreads":
        sm = re.fullmatch(r"(?P<abbr>[A-Z]+)(?P<n>\d+)", suffix)
        if not sm or sm["abbr"] not in abbrs:
            raise Skip("kalshi spread suffix not recognised")
        floor = kalshi.dollars(m.get("floor_strike"))
        rm = _K_SPREAD_RULE.match(rules)
        if m.get("strike_type") != "greater" or not _half_point(floor):
            raise Skip("kalshi spread is not 'wins by more than' a half point")
        if not rm or float(rm["x"]) != floor or int(sm["n"]) != math.ceil(floor):
            raise Skip("kalshi spread ticker, strike and rule text disagree")
        known = uuids.get((info["game"], sm["abbr"]))
        if known is None:
            raise Skip("kalshi spread: no game market to confirm the team")
        if _team_uuid(m) != known:
            raise Skip("kalshi spread team id differs from the game market")
        team = _canon(sport, sm["abbr"])
        line = -floor                               # "wins by more than S" == team -S
    else:  # totals
        floor = kalshi.dollars(m.get("floor_strike"))
        rm = _K_TOTAL_RULE.search(rules)
        if m.get("strike_type") != "greater" or not _half_point(floor):
            raise Skip("kalshi total is not 'over' a half point")
        if not rm or float(rm["x"]) != floor:
            raise Skip("kalshi total strike and rule text disagree")
        line = floor
    q = kalshi.market_quote(m)
    return KClaim(ticker=ticker, game=info["game"], category=sport, kind=kind, team=team,
                  line=line, teams=teams, date=info["date"], start_utc=info["start_utc"],
                  yes_bid=q["yes_bid"], yes_ask=q["yes_ask"], yes_bid_size=q["yes_bid_size"],
                  yes_ask_size=q["yes_ask_size"], fee_type=fee_type, fee_mult=mult)


def kalshi_band(m: dict[str, Any]) -> tuple[float | None, float | None]:
    """
    Integer band a Kalshi temperature market covers, cross-checked against
    the label it shows. less 65 -> (None, 64) "64° or below"; between 65 66
    -> (65, 66) "65° to 66°"; greater 72 -> (73, None) "73° or above".
    """
    st = m.get("strike_type")
    lo, hi = kalshi.dollars(m.get("floor_strike")), kalshi.dollars(m.get("cap_strike"))
    sub = (m.get("yes_sub_title") or "").replace("°", "").strip()
    if st == "less" and hi is not None and hi == int(hi):
        band, shown = (None, hi - 1), re.fullmatch(r"(\d+) or below", sub)
        ok = shown and float(shown[1]) == hi - 1
    elif st == "between" and lo is not None and hi is not None and lo == int(lo) and hi == int(hi):
        band, shown = (lo, hi), re.fullmatch(r"(\d+) to (\d+)", sub)
        ok = shown and (float(shown[1]), float(shown[2])) == (lo, hi)
    elif st == "greater" and lo is not None and lo == int(lo):
        band, shown = (lo + 1, None), re.fullmatch(r"(\d+) or above", sub)
        ok = shown and float(shown[1]) == lo + 1
    else:
        raise Skip("kalshi temperature strike not an integer band")
    if not ok:
        raise Skip("kalshi temperature label disagrees with strikes")
    return band


def kalshi_weather_claims(
    by_series: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> tuple[list[KClaim], dict[str, int]]:
    claims: list[KClaim] = []
    skips: dict[str, int] = {}
    for st, (series, events) in by_series.items():
        if st not in WEATHER_SERIES:
            _count(skips, "series not allow-listed")
            continue
        if (why := kalshi.series_blocked(series)):
            _count(skips, f"series blocked: {why}")
            continue
        fee_type = str(series.get("fee_type") or "")
        mult = float(series.get("fee_multiplier") or 1.0)
        for ev in events:
            try:
                info = parse_event_ticker(ev.get("event_ticker") or "")
                if info["teams"] or info["start_utc"]:
                    raise Skip("kalshi weather ticker not a plain date")
            except Skip as s:
                _count(skips, str(s))
                continue
            for m in ev.get("markets") or []:
                try:
                    if m.get("status") != "active":
                        raise Skip("kalshi market not active")
                    rm = _K_WX_RULE.search(m.get("rules_primary") or "")
                    if not rm:
                        raise Skip("kalshi weather rule text not recognised")
                    rdate = (f"{rm['yyyy']}-{_MONTHS[rm['mon'].upper()]:02d}-"
                             f"{int(rm['dd']):02d}")
                    if rdate != info["date"]:
                        raise Skip("kalshi weather rule date differs from ticker")
                    band = kalshi_band(m)
                    q = kalshi.market_quote(m)
                    claims.append(KClaim(
                        ticker=m.get("ticker") or "", game=info["game"], category="weather",
                        kind="band", team=None, line=None, teams=frozenset(), date=info["date"],
                        start_utc=None, yes_bid=q["yes_bid"], yes_ask=q["yes_ask"],
                        yes_bid_size=q["yes_bid_size"], yes_ask_size=q["yes_ask_size"],
                        fee_type=fee_type, fee_mult=mult, station="K" + rm["st"], band=band))
                except Skip as s:
                    _count(skips, str(s))
    return claims, skips


# --------------------------------------------------------------------------
# Polymarket side
# --------------------------------------------------------------------------

@dataclass
class PClaim:
    slug: str
    event_slug: str
    category: str
    kind: str
    team: str | None
    line: float | None
    teams: frozenset[str]
    date: str
    start_utc: datetime | None
    bid: float | None
    ask: float | None
    fee_rate: float
    label: str
    station: str | None = None
    band: tuple[float | None, float | None] | None = None


def pm_sport_claims(events: list[dict[str, Any]], sport: str, now: datetime,
                    trust_sides: bool = False) -> tuple[list[PClaim], dict[str, int]]:
    """Full-game lines via reference.parse_market: YES from marketSides, not the title."""
    from questions import political_tag
    from reference import claims_from_events

    skips: dict[str, int] = {}
    clean = []
    for ev in events:
        if political_tag(ev.get("tags")):
            _count(skips, "polymarket event has a political tag")
            continue
        m = _PM_EVENT_DATE.search(ev.get("slug") or "")
        if not m:
            _count(skips, "polymarket event slug has no date")
            continue
        if m["dh"]:
            _count(skips, "polymarket doubleheader game (Kalshi ticker has no game number)")
            continue
        clean.append(ev)
    claims, ref_skips, _ = claims_from_events(clean, sport, now, trust_sides=trust_sides,
                                              include_f5=False)
    for k, v in ref_skips.items():
        skips[k] = skips.get(k, 0) + v
    out = []
    for c in claims:
        if c.period != "full":
            _count(skips, "polymarket line is not full game")
            continue
        out.append(PClaim(slug=c.market_slug, event_slug=c.event_slug, category=sport,
                          kind=c.kind, team=c.team, line=c.line, teams=c.teams,
                          date=_PM_EVENT_DATE.search(c.event_slug)["date"],
                          start_utc=c.starts_at, bid=c.bid, ask=c.ask, fee_rate=c.fee_rate,
                          label=c.label + ("" if c.text_check != "conflict" else " [text_conflict]")))
    return out, skips


def pm_weather_claims(events: list[dict[str, Any]]) -> tuple[list[PClaim], dict[str, int]]:
    from adapters import amount
    from questions import political_tag

    out: list[PClaim] = []
    skips: dict[str, int] = {}
    for ev in events:
        em = _PM_WX_EVENT.match(ev.get("slug") or "")
        if not em:
            _count(skips, "polymarket weather event is not a daily high")
            continue
        if political_tag(ev.get("tags")):
            _count(skips, "polymarket event has a political tag")
            continue
        for m in ev.get("markets") or []:
            if m.get("closed") or m.get("status") == "MARKET_STATUS_RESOLVED":
                continue
            slug = m.get("slug") or ""
            sm = _PM_STATION.search(m.get("description") or "")
            band = band_of(slug)
            if not slug.startswith(f"tc-{ev['slug']}-") or band is None:
                _count(skips, "polymarket weather band not recognised")
                continue
            if not sm:
                _count(skips, "polymarket weather station not stated")
                continue
            if sm["date"] != em["date"]:
                _count(skips, "polymarket weather description date differs from slug")
                continue
            lo, hi = band
            label = (f"{sm['st']} {em['date']} "
                     + (f"<= {hi:g}" if lo is None else f">= {lo:g}" if hi is None
                        else f"{lo:g}-{hi:g}"))
            out.append(PClaim(slug=slug, event_slug=ev["slug"], category="weather", kind="band",
                              team=None, line=None, teams=frozenset(), date=em["date"],
                              start_utc=None, bid=amount(m.get("bestBidQuote")),
                              ask=amount(m.get("bestAskQuote")),
                              fee_rate=float(m.get("feeCoefficient") or FEE_RATE), label=label,
                              station=sm["st"], band=band))
    return out, skips


# --------------------------------------------------------------------------
# matching -- equivalence or complement, proved; everything else counted
# --------------------------------------------------------------------------

@dataclass
class Pair:
    pm: PClaim
    k: KClaim
    relation: str          # same | complement


def _relation(p: PClaim, k: KClaim) -> str | None:
    if p.kind != k.kind:
        return None
    if p.kind == "h2h":
        return "same" if p.team == k.team else "complement"
    if p.kind == "spreads":
        if p.line is None or k.line is None or not _half_point(abs(p.line)):
            return None
        if p.team == k.team and p.line == k.line:
            return "same"
        if p.team != k.team and p.line == -k.line:
            return "complement"          # T +S  ==  NOT(other wins by more than S)
        return None
    if p.kind == "totals":
        return "same" if p.line is not None and p.line == k.line and _half_point(p.line) else None
    if p.kind == "band":
        return "same" if (p.station, p.date, p.band) == (k.station, k.date, k.band) else None
    return None


def match_sports(pm: list[PClaim], ks: list[KClaim]) -> tuple[list[Pair], dict[str, int]]:
    near: dict[str, int] = {}
    pairs: list[Pair] = []
    games: dict[tuple[str, frozenset[str], str], dict[str, list[KClaim]]] = {}
    for k in ks:
        games.setdefault((k.category, k.teams, k.date), {}).setdefault(k.game, []).append(k)
    for p in pm:
        cands = games.get((p.category, p.teams, p.date))
        if not cands:
            _count(near, "no Kalshi game with these teams on this date")
            continue
        if len(cands) > 1:
            _count(near, "ambiguous: several Kalshi games (doubleheader?)")
            continue
        (kgame,) = cands.values()
        start = next((k.start_utc for k in kgame if k.start_utc), None)
        if start and p.start_utc and abs(start - p.start_utc) > MAX_START_GAP:
            _count(near, "start times differ >30 min (rescheduled?)")
            continue
        found = [Pair(p, k, rel) for k in kgame if (rel := _relation(p, k))]
        if not found:
            _count(near, f"no Kalshi {p.kind} line at this number")
            continue
        pairs.extend(found)
    return pairs, near


def match_weather(pm: list[PClaim], ks: list[KClaim]) -> tuple[list[Pair], dict[str, int]]:
    near: dict[str, int] = {}
    pairs: list[Pair] = []
    by_day: dict[tuple[str | None, str], list[KClaim]] = {}
    for k in ks:
        by_day.setdefault((k.station, k.date), []).append(k)
    for p in pm:
        day = by_day.get((p.station, p.date))
        if not day:
            _count(near, "no Kalshi market for this station and date")
            continue
        found = [Pair(p, k, "same") for k in day if _relation(p, k) == "same"]
        if not found:
            _count(near, "Kalshi bands differ from this band")
            continue
        pairs.extend(found)
    return pairs, near


# --------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------

def legs(pair: Pair) -> dict[str, dict[str, Any]]:
    """
    The two hedges, as (Polymarket leg, Kalshi leg) with the price of each
    at the touch. Each pays exactly $1 in every outcome where the two
    propositions settle as their rules say.
    """
    p, k = pair.pm, pair.k
    k_no = None if k.yes_bid is None else round(1.0 - k.yes_bid, 4)
    pm_no = None if p.bid is None else round(1.0 - p.bid, 4)
    if pair.relation == "same":
        return {"pm_yes": {"pm_side": "YES", "pm_px": p.ask, "k_side": "NO", "k_px": k_no,
                           "k_size": k.yes_bid_size},
                "pm_no": {"pm_side": "NO", "pm_px": pm_no, "k_side": "YES", "k_px": k.yes_ask,
                          "k_size": k.yes_ask_size}}
    return {"pm_yes": {"pm_side": "YES", "pm_px": p.ask, "k_side": "YES", "k_px": k.yes_ask,
                       "k_size": k.yes_ask_size},
            "pm_no": {"pm_side": "NO", "pm_px": pm_no, "k_side": "NO", "k_px": k_no,
                      "k_size": k.yes_bid_size}}


def edge(pm_px: float | None, k_px: float | None, pm_rate: float, k_mult: float) -> float | None:
    """Profit per $1 set after both venues' marginal fees (unrounded), at the touch."""
    if pm_px is None or k_px is None:
        return None
    return round(1.0 - pm_px - k_px - fee_per_share(pm_px, pm_rate)
                 - kalshi.taker_fee_per_contract(k_px, k_mult), 5)


def price_pair(pair: Pair) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m1 = max(1.0, pair.k.fee_mult)
    for name, leg in legs(pair).items():
        out[f"edge_{name}"] = edge(leg["pm_px"], leg["k_px"], pair.pm.fee_rate, pair.k.fee_mult)
        out[f"edge_{name}_m1"] = edge(leg["pm_px"], leg["k_px"], pair.pm.fee_rate, m1)
    return out


def walk(pm_asks: list[tuple[float, float]], k_asks: list[tuple[float, float]],
         pm_rate: float, k_mult: float, max_sets: float | None = None) -> dict[str, Any]:
    """
    Buy both legs level by level while the next set still profits after
    marginal fees; then charge fees the way each venue rounds them (per
    fill, up to the cent -- the conservative reading for both). Returns
    whole sets only: Polymarket US quantities are integers.
    """
    i = j = 0
    pm_left = pm_asks[0][1] if pm_asks else 0.0
    k_left = k_asks[0][1] if k_asks else 0.0
    sets = 0.0
    pm_fills: list[tuple[float, float]] = []
    k_fills: list[tuple[float, float]] = []
    while i < len(pm_asks) and j < len(k_asks):
        a, b = pm_asks[i][0], k_asks[j][0]
        if (edge(a, b, pm_rate, k_mult) or 0.0) <= 0:
            break
        q = min(pm_left, k_left)
        if max_sets is not None:
            q = min(q, max_sets - sets)
        if q <= 0:
            break
        pm_fills.append((a, q))
        k_fills.append((b, q))
        sets += q
        pm_left -= q
        k_left -= q
        if pm_left <= 1e-9:
            i += 1
            pm_left = pm_asks[i][1] if i < len(pm_asks) else 0.0
        if k_left <= 1e-9:
            j += 1
            k_left = k_asks[j][1] if j < len(k_asks) else 0.0
    whole = math.floor(sets + 1e-9)
    if max_sets is None and whole != sets:
        return walk(pm_asks, k_asks, pm_rate, k_mult, max_sets=whole)
    cost = sum(p * q for p, q in pm_fills) + sum(p * q for p, q in k_fills)
    fees = (sum(fee_usd(p, q, pm_rate) for p, q in pm_fills)
            + sum(kalshi.taker_fee(p, q, k_mult) for p, q in k_fills))
    return {"sets": whole, "cost": round(cost, 4), "fees": round(fees, 4),
            "profit": round(whole - cost - fees, 4),
            "pm_fills": pm_fills, "k_fills": k_fills}


def pm_ladders(book: dict[str, Any]) -> dict[str, list[tuple[float, float]]]:
    """Polymarket book -> buy ladders. Buying NO costs 1 - (a YES bid)."""
    from adapters import amount

    md = book.get("marketData") if isinstance(book.get("marketData"), dict) else book

    def lv(rows: list[dict[str, Any]], flip: bool) -> list[tuple[float, float]]:
        out = []
        for r in rows or []:
            px, q = amount(r.get("px")), kalshi.dollars(r.get("qty"))
            if px is not None and q and 0.0 < px < 1.0:
                out.append((round(1.0 - px, 4) if flip else px, q))
        return sorted(out)

    return {"yes_asks": lv(md.get("offers"), False), "no_asks": lv(md.get("bids"), True)}


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, pm_calls INTEGER, kalshi_calls INTEGER,
    pm_claims INTEGER, kalshi_claims INTEGER, pairs INTEGER, positive INTEGER,
    positive_m1 INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS pairs (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, category TEXT NOT NULL,
    pm_slug TEXT NOT NULL, kalshi_ticker TEXT NOT NULL, relation TEXT NOT NULL, label TEXT,
    pm_bid REAL, pm_ask REAL, pm_fee_rate REAL,
    k_bid REAL, k_ask REAL, k_bid_size REAL, k_ask_size REAL, k_fee_type TEXT, k_fee_mult REAL,
    edge_pm_yes REAL, edge_pm_no REAL,        -- per $1 set, after marginal fees, at the touch
    edge_pm_yes_m1 REAL, edge_pm_no_m1 REAL,  -- same with Kalshi multiplier forced to >= 1
    depth_dir TEXT, depth_sets REAL, depth_profit REAL, depth_json TEXT,
    risk TEXT
);
CREATE INDEX IF NOT EXISTS idx_pairs_at ON pairs(at);
CREATE TABLE IF NOT EXISTS skips (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, side TEXT NOT NULL, category TEXT,
    reason TEXT NOT NULL, n INTEGER NOT NULL
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def store(conn: sqlite3.Connection, at: str, rows: list[dict[str, Any]],
          skips: list[tuple[str, str, str, int]], scan: dict[str, Any]) -> None:
    cols = ("category", "pm_slug", "kalshi_ticker", "relation", "label", "pm_bid", "pm_ask",
            "pm_fee_rate", "k_bid", "k_ask", "k_bid_size", "k_ask_size", "k_fee_type",
            "k_fee_mult", "edge_pm_yes", "edge_pm_no", "edge_pm_yes_m1", "edge_pm_no_m1",
            "depth_dir", "depth_sets", "depth_profit", "depth_json", "risk")
    conn.executemany(
        f"INSERT INTO pairs (at, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
        [(at, *[r.get(c) for c in cols]) for r in rows])
    conn.executemany("INSERT INTO skips (at, side, category, reason, n) VALUES (?,?,?,?,?)",
                     [(at, *s) for s in skips])
    conn.execute("INSERT INTO scans (at, pm_calls, kalshi_calls, pm_claims, kalshi_claims, pairs,"
                 " positive, positive_m1, notes) VALUES (?,?,?,?,?,?,?,?,?)",
                 (at, scan["pm_calls"], scan["kalshi_calls"], scan["pm_claims"],
                  scan["kalshi_claims"], len(rows), scan["positive"], scan["positive_m1"],
                  scan.get("notes")))
    conn.commit()


def pair_row(pair: Pair) -> dict[str, Any]:
    p, k = pair.pm, pair.k
    return {"category": p.category, "pm_slug": p.slug, "kalshi_ticker": k.ticker,
            "relation": pair.relation, "label": p.label, "pm_bid": p.bid, "pm_ask": p.ask,
            "pm_fee_rate": p.fee_rate, "k_bid": k.yes_bid, "k_ask": k.yes_ask,
            "k_bid_size": k.yes_bid_size, "k_ask_size": k.yes_ask_size,
            "k_fee_type": k.fee_type, "k_fee_mult": k.fee_mult, **price_pair(pair),
            "risk": RISK.get(p.category)}


def best_edge(row: dict[str, Any], conservative: bool = False) -> tuple[float, str]:
    sfx = "_m1" if conservative else ""
    opts = [(row.get(f"edge_pm_yes{sfx}"), "pm_yes"), (row.get(f"edge_pm_no{sfx}"), "pm_no")]
    opts = [(e, d) for e, d in opts if e is not None]
    return max(opts) if opts else (-1.0, "")


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

async def _pm_fetch(max_calls: int, pace: float) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Three events.list calls: MLB (next 40 h), NFL (next 6 days), weather."""
    import asyncio

    from polymarket_us import AsyncPolymarketUS

    now = datetime.now(timezone.utc)

    def f(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")

    out: dict[str, list[dict[str, Any]]] = {}
    calls = 0
    async with AsyncPolymarketUS() as pm:
        for tag, hours in (("mlb", 40), ("nfl", 144), ("weather", None)):
            if calls >= max_calls:
                break
            params: dict[str, Any] = {"limit": 40, "closed": False, "tagSlug": tag}
            if hours:
                params.update(startTimeMin=f(now), startTimeMax=f(now + timedelta(hours=hours)))
            try:
                page = await pm.events.list(params)
                out[tag] = page.get("events") or []
            except Exception as exc:  # noqa: BLE001 -- one tag down must not stop the rest
                log.warning("polymarket %s failed: %s", tag, exc)
                out[tag] = []
            calls += 1
            await asyncio.sleep(pace)
    return out, calls


async def _pm_books(slugs: list[str], pace: float) -> tuple[dict[str, dict[str, Any]], int]:
    import asyncio

    from polymarket_us import AsyncPolymarketUS

    books: dict[str, dict[str, Any]] = {}
    calls = 0
    async with AsyncPolymarketUS() as pm:
        for s in slugs:
            try:
                books[s] = await pm.markets.book(s)
            except Exception as exc:  # noqa: BLE001
                log.warning("polymarket book %s failed: %s", s, exc)
            calls += 1
            await asyncio.sleep(pace)
    return books, calls


def build_pairs(pm_events: dict[str, list[dict[str, Any]]],
                k_series: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
                now: datetime, trust_sides: bool = False,
                ) -> tuple[list[Pair], list[tuple[str, str, str, int]], int, int]:
    """Pure: raw responses -> proven pairs, skip counts, claim counts."""
    pairs: list[Pair] = []
    skips: list[tuple[str, str, str, int]] = []
    n_pm = n_k = 0
    for sport in ("mlb", "nfl"):
        pc, ps = pm_sport_claims(pm_events.get(sport, []), sport, now, trust_sides)
        kc, ks = kalshi_sport_claims(sport, {s: v for s, v in k_series.items()
                                             if s in SPORT_SERIES[sport]})
        got, near = match_sports(pc, kc)
        pairs += got
        n_pm += len(pc)
        n_k += len(kc)
        skips += [("polymarket", sport, r, n) for r, n in ps.items()]
        skips += [("kalshi", sport, r, n) for r, n in ks.items()]
        skips += [("near-miss", sport, r, n) for r, n in near.items()]
    pc, ps = pm_weather_claims(pm_events.get("weather", []))
    kc, ks = kalshi_weather_claims({s: v for s, v in k_series.items() if s in WEATHER_SERIES})
    got, near = match_weather(pc, kc)
    pairs += got
    n_pm += len(pc)
    n_k += len(kc)
    skips += [("polymarket", "weather", r, n) for r, n in ps.items()]
    skips += [("kalshi", "weather", r, n) for r, n in ks.items()]
    skips += [("near-miss", "weather", r, n) for r, n in near.items()]
    return pairs, skips, n_pm, n_k


def scan(db: str = DB_PATH, max_seconds: float = 240.0, max_books: int = 6,
         trust_sides: bool = False) -> list[dict[str, Any]]:
    import asyncio

    t0 = time.monotonic()
    deadline = t0 + max_seconds
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pm_events, pm_calls = asyncio.run(asyncio.wait_for(_pm_fetch(3, 2.5), timeout=90))
    k_series: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    with kalshi.Kalshi(deadline=deadline) as kc:
        for st in sorted(ALLOWED_SERIES):
            try:
                ser = kc.series(st)
                if (why := kalshi.series_blocked(ser)):
                    log.warning("skip series %s: %s", st, why)
                    continue
                k_series[st] = (ser, kc.events(st))
            except kalshi.KalshiError as exc:
                log.warning("kalshi %s: %s", st, exc)
                if "budget" in str(exc):
                    break
        pairs, skips, n_pm, n_k = build_pairs(pm_events, k_series,
                                              datetime.now(timezone.utc), trust_sides)
        rows = [pair_row(p) for p in pairs]
        # Depth only where the touch already clears both fees: books cost calls.
        cands = sorted(((best_edge(r, True)[0], i) for i, r in enumerate(rows)
                        if best_edge(r, True)[0] > 0), reverse=True)[:max_books]
        books, book_calls = ({}, 0)
        if cands:
            slugs = list(dict.fromkeys(rows[i]["pm_slug"] for _, i in cands))
            books, book_calls = asyncio.run(asyncio.wait_for(_pm_books(slugs, 2.5), 120))
        for _, i in cands:
            r = rows[i]
            _, d = best_edge(r, True)
            try:
                kob = kalshi.ladders(kc.orderbook(r["kalshi_ticker"]))
            except kalshi.KalshiError as exc:
                log.warning("kalshi book %s: %s", r["kalshi_ticker"], exc)
                continue
            if r["pm_slug"] not in books:
                continue
            pl = pm_ladders(books[r["pm_slug"]])
            leg = legs(pairs[i])[d]
            res = walk(pl["yes_asks" if leg["pm_side"] == "YES" else "no_asks"],
                       kob["yes_asks" if leg["k_side"] == "YES" else "no_asks"],
                       r["pm_fee_rate"], max(1.0, r["k_fee_mult"]))
            r.update(depth_dir=d, depth_sets=res["sets"], depth_profit=res["profit"],
                     depth_json=json.dumps(res))
        k_calls = kc.calls
    positive = sum(1 for r in rows if best_edge(r)[0] > 0)
    positive_m1 = sum(1 for r in rows if best_edge(r, True)[0] > 0)
    with closing(connect(db)) as conn:
        store(conn, at, rows, skips, {"pm_calls": pm_calls + book_calls, "kalshi_calls": k_calls,
                                      "pm_claims": n_pm, "kalshi_claims": n_k,
                                      "positive": positive, "positive_m1": positive_m1,
                                      "notes": f"{time.monotonic() - t0:.0f}s"})
    log.info("scan: %d pairs, %d positive (%d at M>=1); polymarket %d calls, kalshi %d calls",
             len(rows), positive, positive_m1, pm_calls + book_calls, k_calls)
    return rows


def report(db: str = DB_PATH, at: str | None = None) -> None:
    with closing(connect(db)) as conn:
        at = at or (conn.execute("SELECT max(at) FROM scans").fetchone()[0])
        if not at:
            print("no scans recorded")
            return
        sc = conn.execute("SELECT * FROM scans WHERE at = ?", (at,)).fetchone()
        rows = [dict(r) for r in conn.execute("SELECT * FROM pairs WHERE at = ?", (at,))]
        skips = conn.execute("SELECT side, category, reason, n FROM skips WHERE at = ? "
                             "ORDER BY side, category, n DESC", (at,)).fetchall()
        n_scans = conn.execute("SELECT count(*) FROM scans").fetchone()[0]
        ever = conn.execute("SELECT count(*) FROM pairs WHERE max(coalesce(edge_pm_yes_m1,-1),"
                            " coalesce(edge_pm_no_m1,-1)) > 0").fetchone()[0]
    print(f"scan {at}: polymarket {sc['pm_calls']} calls, kalshi {sc['kalshi_calls']} calls, "
          f"{sc['notes']}")
    print(f"  claims: polymarket {sc['pm_claims']}, kalshi {sc['kalshi_claims']}")
    by_cat: dict[str, int] = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    print(f"  matched pairs: {len(rows)}  " + ", ".join(f"{c} {n}" for c, n in sorted(by_cat.items())))
    print(f"  positive after fees at the touch: {sc['positive']} "
          f"({sc['positive_m1']} with Kalshi multiplier >= 1)")
    print(f"  all scans: {n_scans}; pair-rows ever positive at M>=1: {ever}")
    print("  skipped / near-miss reasons:")
    for s in skips:
        print(f"    {s['side']:<10} {s['category'] or '':<8} {s['n']:>5}  {s['reason']}")
    edges = sorted(rows, key=lambda r: -best_edge(r)[0])[:12]
    print("  best edges (per $1 set, after fees; negative = no arb):")
    for r in edges:
        e, d = best_edge(r)
        e1, _ = best_edge(r, True)
        depth = (f" depth {r['depth_sets']:.0f} sets -> ${r['depth_profit']:+.2f}"
                 if r.get("depth_sets") is not None else "")
        print(f"    {e:+.4f} (M>=1 {e1:+.4f}) {d:<6} {r['relation']:<10} "
              f"{(r['label'] or '')[:34]:<35} {r['kalshi_ticker']}{depth}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Polymarket-Kalshi cross-venue arbitrage. Shadow only.")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=240.0, help="hard wall-clock cap")
    ap.add_argument("--max-books", type=int, default=6, help="Polymarket book calls at most")
    ap.add_argument("--trust-sides", action="store_true",
                    help="match NFL 'pos' spreads whose exchange text names the other side")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not args.report:
        scan(max_seconds=args.max_seconds, max_books=min(args.max_books, 8),
             trust_sides=args.trust_sides)
    report()


if __name__ == "__main__":
    main()
