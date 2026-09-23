"""
reference.py -- sharp sportsbook prices as a fair-value reference. Code only.

SHADOW ONLY. Places no orders and never imports the order path.

For a game line, the consensus of sharp books (Pinnacle above all) with
the bookmaker's margin removed is the best public estimate of the true
probability. If the exchange prices Padres +1.5 at 0.63 and Pinnacle's
de-vigged price is 0.67, that gap needs no news and no model to explain
it. This records the gap per market, then scores after settlement which
of the two was closer.

    python reference.py                     # MLB: fetch odds, match, record
    python reference.py --sport nfl
    python reference.py --coverage          # exchange side only; no odds key needed
    python reference.py --odds-json F       # offline: a saved odds payload, records nothing
    python reference.py --backfill          # fill outcomes from the exchange
    python reference.py --report            # gaps now; Brier by event after settlement
    python reference.py --check-teams       # 1 credit/sport: team names vs the table

INERT WITHOUT A KEY. With no ODDS_API_KEY in the environment or .env,
a record run prints how to enable it and exits 0 without touching either
API.

PROVIDER: The Odds API (https://the-odds-api.com), v4. Shapes below are
taken from its published documentation, not from a live call -- no key
existed when this was written. Verified against the docs (2026-09-22):
  * GET /v4/sports/{sport}/odds; cost = markets x regions, and "every
    group of 10 bookmakers is the equivalent of 1 region". So
    h2h,spreads,totals over <= 10 named books costs 3 credits for the
    whole slate.
  * GET /v4/sports/{sport}/events/{id}/odds serves the period markets
    (totals_1st_5_innings, ...); cost = markets returned x regions, per
    game. That is why first-five lines are opt-in (--f5).
  * Headers x-requests-remaining / x-requests-used / x-requests-last.
  * Pinnacle is bookmaker key `pinnacle`, region eu, flagged "odds are
    from public website which may incur a delay".
  * `regions` is listed as required, and "if both bookmakers and regions
    are specified, bookmakers takes priority", so both are sent.
  * /participants returns [{"full_name", "id"}], 1 credit (--check-teams).
NOT verified: whether the free plan ("most bookmakers") includes
Pinnacle -- a competitor's blog says it does not. If it does not, the
fair price falls back to the median of the other books and every row
says which it used (fair_source = pinnacle | consensus).

WHICH SIDE IS YES -- read before changing the matcher. Verified live
2026-09-22 on both MLB and NFL game lines:
  * The YES instrument is the `marketSides` entry with long=True. Its
    teamId, its description ("+1.50") and the market's `line` agree,
    and agree with the slug's pos/neg.
  * `title` is NOT the YES side on underdog spreads:
    asc-mlb-sd-lad-2026-09-22-pos-1pt5 is YES = Padres +1.5 at 0.63,
    titled "Los Angeles Dodgers wins by over 1.5 runs". normalize_market
    copies title into `outcome`, so anything reading `outcome` on these
    markets reads the opposite side.
  * On NFL "pos" spreads the settlement DESCRIPTION also names the
    opposite side ("settle to Yes if Green Bay Packers wins by more than
    3.5" on a market whose long side is Falcons +3.5 at 0.435, with the
    Falcons 0.28 to win outright -- only the long reading fits the
    price). A settled NFL pos market in shadow.db (Panthers +14.5, bid
    0.91, resolved YES) also fits only the long reading. Still, two
    exchange fields disagree, so by default such markets are SKIPPED
    (reason text_conflict); --trust-sides records them, flagged.

DE-VIG. Three methods are computed for every book and stored, so the
choice can be re-scored on outcomes rather than argued:
  multiplicative  p_i = q_i / sum(q)           margin spread pro rata
  power           p_i = q_i ** k, sum = 1      more margin off the longshot
  shin            Shin (1993) insider model     for two outcomes this equals
                                                subtracting the margin equally
Default is power. Books load more margin onto longshots (the
favourite-longshot bias), which multiplicative ignores; power corrects
for it, is defined for any number of outcomes, has no degenerate cases,
and Clarke, Kovalchik & Ingram (2017) found it at least as accurate as
multiplicative and Shin (recalled, not re-checked). On Pinnacle's ~2-3%
two-way margins the methods agree within 0.1pp near 0.50 and differ by
1-3pp at 0.85; --report prints the Brier of each once outcomes arrive.

The unit of evidence is the EVENT, not the market: every line of one
game resolves together.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import statistics
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from adapters import amount
from fees import FEE_RATE, fee_usd

log = logging.getLogger("reference")

DB_PATH = os.getenv("REFERENCE_DB", "reference.db")
ENV_KEY = "ODDS_API_KEY"
API_BASE = "https://api.the-odds-api.com/v4"
SIGNUP_URL = "https://the-odds-api.com/"

SPORT_KEYS = {"mlb": "baseball_mlb", "nfl": "americanfootball_nfl"}
FEATURED = ("h2h", "spreads", "totals")
F5_MARKETS = ("h2h_1st_5_innings", "spreads_1st_5_innings", "totals_1st_5_innings")

#: Ten books cost the same as one region. Pinnacle first; the rest are the
#: consensus fallback. Keys from the provider's bookmaker list.
DEFAULT_BOOKS = ("pinnacle", "lowvig", "betonlineag", "draftkings", "fanduel",
                 "betmgm", "williamhill_us", "betrivers", "bovada", "betus")
SHARP_BOOK = "pinnacle"
MIN_CONSENSUS_BOOKS = 3

#: Free plan is 500 credits/month. Stay under it with room to spare, and
#: never spend below a floor of what the provider says is left.
MONTHLY_BUDGET = int(os.getenv("ODDS_API_MONTHLY_BUDGET", "450"))
DAILY_BUDGET = int(os.getenv("ODDS_API_DAILY_BUDGET", str(MONTHLY_BUDGET // 30)))
RESERVE_CREDITS = 25

#: Two games of the same pair within this window cannot be told apart.
MATCH_WINDOW = timedelta(hours=3)
#: Book lines on started games are live prices; do not compare them.
MIN_LEAD = timedelta(minutes=5)
#: Fees round up to the cent per fill; edge is quoted at this fill size.
NOMINAL_SHARES = 10
DEFAULT_METHOD = "power"

# --------------------------------------------------------------------------
# teams -- explicit, tested. Exchange abbreviation -> sportsbook name.
# Every abbreviation below was seen on the exchange 2026-09-20/22, and the
# exchange's own team name equals the sportsbook name except where an
# alias is listed.
# --------------------------------------------------------------------------

MLB_TEAMS: dict[str, str] = {
    "az": "Arizona Diamondbacks", "ath": "Athletics", "atl": "Atlanta Braves",
    "bal": "Baltimore Orioles", "bos": "Boston Red Sox", "chc": "Chicago Cubs",
    "cws": "Chicago White Sox", "cin": "Cincinnati Reds", "cle": "Cleveland Guardians",
    "col": "Colorado Rockies", "det": "Detroit Tigers", "hou": "Houston Astros",
    "kc": "Kansas City Royals", "laa": "Los Angeles Angels", "lad": "Los Angeles Dodgers",
    "mia": "Miami Marlins", "mil": "Milwaukee Brewers", "min": "Minnesota Twins",
    "nym": "New York Mets", "nyy": "New York Yankees", "phi": "Philadelphia Phillies",
    "pit": "Pittsburgh Pirates", "sd": "San Diego Padres", "sf": "San Francisco Giants",
    "sea": "Seattle Mariners", "stl": "St. Louis Cardinals", "tb": "Tampa Bay Rays",
    "tex": "Texas Rangers", "tor": "Toronto Blue Jays", "wsh": "Washington Nationals",
}

NFL_TEAMS: dict[str, str] = {
    "ari": "Arizona Cardinals", "atl": "Atlanta Falcons", "bal": "Baltimore Ravens",
    "buf": "Buffalo Bills", "car": "Carolina Panthers", "chi": "Chicago Bears",
    "cin": "Cincinnati Bengals", "cle": "Cleveland Browns", "dal": "Dallas Cowboys",
    "den": "Denver Broncos", "det": "Detroit Lions", "gb": "Green Bay Packers",
    "hou": "Houston Texans", "ind": "Indianapolis Colts", "jax": "Jacksonville Jaguars",
    "kc": "Kansas City Chiefs", "lac": "Los Angeles Chargers", "lar": "Los Angeles Rams",
    "lv": "Las Vegas Raiders", "mia": "Miami Dolphins", "min": "Minnesota Vikings",
    "ne": "New England Patriots", "no": "New Orleans Saints", "nyg": "New York Giants",
    "nyj": "New York Jets", "phi": "Philadelphia Eagles", "pit": "Pittsburgh Steelers",
    "sea": "Seattle Seahawks", "sf": "San Francisco 49ers", "tb": "Tampa Bay Buccaneers",
    "ten": "Tennessee Titans", "was": "Washington Commanders",
}

TEAMS = {"mlb": MLB_TEAMS, "nfl": NFL_TEAMS}

#: Spellings a sportsbook feed may use for the same team. Unverified which
#: one the provider sends for the relocated Athletics, so all are accepted.
ALIASES: dict[str, tuple[str, ...]] = {
    "Athletics": ("Oakland Athletics", "Sacramento Athletics", "Las Vegas Athletics"),
    "St. Louis Cardinals": ("St Louis Cardinals",),
}


def norm_name(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _reverse(sport: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for canon in TEAMS[sport].values():
        out[norm_name(canon)] = canon
        for alt in ALIASES.get(canon, ()):
            out[norm_name(alt)] = canon
    return out


def book_team(sport: str, name: str | None) -> str | None:
    """Sportsbook team name -> canonical name, or None if not in the table."""
    return _reverse(sport).get(norm_name(name))


# --------------------------------------------------------------------------
# de-vig
# --------------------------------------------------------------------------

def to_decimal(price: float, odds_format: str) -> float:
    if odds_format == "decimal":
        return float(price)
    p = float(price)
    if p >= 100:
        return 1.0 + p / 100.0
    if p <= -100:
        return 1.0 + 100.0 / -p
    raise ValueError(f"not an American price: {price}")


def _bisect(f, lo: float, hi: float, iters: int = 200) -> float:
    """Root of a decreasing function on [lo, hi]."""
    for _ in range(iters):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def devig_multiplicative(q: list[float]) -> list[float]:
    s = sum(q)
    return [x / s for x in q]


def devig_power(q: list[float]) -> list[float]:
    """p_i = q_i ** k with k chosen so the p_i sum to one."""
    k = _bisect(lambda k: sum(x ** k for x in q) - 1.0, 1e-3, 100.0)
    return [x ** k for x in q]


def devig_shin(q: list[float]) -> list[float]:
    """Shin's insider-trading model; z is the implied insider share."""
    s = sum(q)
    if s <= 1.0:
        return devig_multiplicative(q)

    def probs(z: float) -> list[float]:
        return [(math.sqrt(z * z + 4 * (1 - z) * x * x / s) - z) / (2 * (1 - z)) for x in q]

    z = _bisect(lambda z: sum(probs(z)) - 1.0, 0.0, 0.999)
    return probs(z)


METHODS = {"multiplicative": devig_multiplicative, "power": devig_power, "shin": devig_shin}


def devig(decimal_odds: list[float]) -> dict[str, list[float]]:
    """All three methods for one book's outcomes, in the given order."""
    if any(o <= 1.0 for o in decimal_odds):
        raise ValueError(f"decimal odds must exceed 1: {decimal_odds}")
    q = [1.0 / o for o in decimal_odds]
    return {name: fn(q) for name, fn in METHODS.items()}


# --------------------------------------------------------------------------
# the exchange side: what exactly does YES pay on?
# --------------------------------------------------------------------------

#: sportsMarketType -> (kind, period). Only types seen on the wire.
SPORTS_MARKET_TYPES = {
    "baseball_team_full_game_winner": ("h2h", "full"),
    "baseball_team_full_game_spread": ("spreads", "full"),
    "baseball_team_full_game_total": ("totals", "full"),
    "baseball_team_first_five_spread": ("spreads", "f5"),
    "baseball_team_first_five_total": ("totals", "f5"),
    "football_team_full_game_winner": ("h2h", "full"),
    "football_team_full_game_spread": ("spreads", "full"),
    "football_team_full_game_total": ("totals", "full"),
}

_NUM = r"(\d+)pt(\d+)"
#: Slug suffix after "<prefix>-<event slug>" per (sport, kind, period).
#: cks-: seen once (cks-nfl-nyg-lar-2026-09-21-nyg-1pt5), never with its
#: marketSides, so it is accepted only when every structured field agrees.
_SUFFIX = {
    ("mlb", "h2h", "full"): r"",
    ("mlb", "spreads", "full"): rf"-(?P<sign>pos|neg)-{_NUM}",
    ("mlb", "totals", "full"): rf"-{_NUM}",
    ("mlb", "spreads", "f5"): rf"-f5-(?P<sign>pos|neg)-{_NUM}",
    ("mlb", "totals", "f5"): rf"-f5-{_NUM}",
    ("nfl", "h2h", "full"): r"",
    ("nfl", "spreads", "full"): rf"-(?P<sign>pos|neg)-{_NUM}",
    ("nfl", "totals", "full"): rf"-total-{_NUM}",
}
_PREFIX = {"h2h": ("aec",), "spreads": ("asc", "cks"), "totals": ("tsc",)}
_EVENT_SLUG = re.compile(r"^(?P<sport>mlb|nfl)-(?P<a>[a-z]+)-(?P<b>[a-z]+)-\d{4}-\d{2}-\d{2}(-dh\d)?$")

_DESC_COVER = re.compile(r"settle to Yes if (?:the )?(?P<team>.+?) cover a "
                         r"(?P<h>[+-]?\d+(?:\.\d+)?) (?:run|point)", re.I)
_DESC_WINS_BY = re.compile(r"settle to Yes if (?:the )?(?P<team>.+?) wins? by more than "
                           r"(?P<h>\d+(?:\.\d+)?)", re.I)
_DESC_OVER = re.compile(r"combine for over (?P<t>\d+(?:\.\d+)?)", re.I)


@dataclass
class Claim:
    """What one exchange market's YES pays on, in sportsbook terms."""
    market_slug: str
    event_slug: str
    sport: str
    kind: str                     # h2h | spreads | totals
    period: str                   # full | f5
    team: str | None              # canonical team for h2h/spreads
    line: float | None            # team handicap (spreads) or total
    teams: frozenset[str]
    starts_at: datetime
    bid: float | None
    ask: float | None
    fee_rate: float
    text_check: str               # agree | conflict | unparsed

    @property
    def label(self) -> str:
        per = " F5" if self.period == "f5" else ""
        if self.kind == "h2h":
            return f"{self.team} win{per}"
        if self.kind == "spreads":
            return f"{self.team} {self.line:+g}{per}"
        return f"Over {self.line:g}{per}"


class Skip(Exception):
    """Market not comparable; the message is the reason, recorded by count."""


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_teams(event: dict[str, Any], sport: str) -> dict[Any, tuple[str, str]]:
    """{team id: (abbreviation, canonical name)} -- both must agree with the table."""
    table = TEAMS[sport]
    m = _EVENT_SLUG.match(event.get("slug") or "")
    if not m or m["sport"] != sport:
        raise Skip("event slug not a game")
    out: dict[Any, tuple[str, str]] = {}
    for t in event.get("teams") or []:
        abbr = (t.get("abbreviation") or "").lower()
        if abbr not in table:
            raise Skip(f"unknown team abbreviation {abbr!r}")
        canon = table[abbr]
        if book_team(sport, t.get("name")) != canon:
            raise Skip(f"team name {t.get('name')!r} disagrees with table for {abbr}")
        out[t.get("id")] = (abbr, canon)
    if len(out) != 2 or {a for a, _ in out.values()} != {m["a"], m["b"]}:
        raise Skip("event teams do not match its slug")
    return out


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_market(market: dict[str, Any], event: dict[str, Any], sport: str,
                 trust_sides: bool = False) -> Claim:
    """Raw exchange market + parent event -> Claim, or raise Skip(reason)."""
    slug = market.get("slug") or ""
    smt = market.get("sportsMarketType")
    if smt not in SPORTS_MARKET_TYPES:
        raise Skip(f"market type not covered ({smt or slug.split('-')[0]})")
    kind, period = SPORTS_MARKET_TYPES[smt]
    teams = event_teams(event, sport)
    ev_slug = event["slug"]
    prefix = slug.split("-")[0]
    if prefix not in _PREFIX[kind] or not slug.startswith(f"{prefix}-{ev_slug}"):
        raise Skip("slug disagrees with sportsMarketType")
    suffix = _SUFFIX.get((sport, kind, period))
    rest = slug[len(prefix) + 1 + len(ev_slug):]
    if prefix == "cks":
        suffix = rf"-(?P<abbr>[a-z]+)-{_NUM}"
    sm = re.fullmatch(suffix, rest) if suffix is not None else None
    if sm is None:
        raise Skip("slug suffix not recognised")

    sides = market.get("marketSides") or []
    longs = [s for s in sides if s.get("long") is True]
    shorts = [s for s in sides if s.get("long") is False]
    if len(sides) != 2 or len(longs) != 1 or len(shorts) != 1:
        raise Skip("marketSides not one long + one short")
    long_, short = longs[0], shorts[0]
    line = _float(market.get("line"))
    desc = market.get("description") or ""
    text = "unparsed"
    team: str | None = None

    if kind in ("h2h", "spreads"):
        if long_.get("teamId") not in teams or short.get("teamId") not in teams \
                or long_.get("teamId") == short.get("teamId"):
            raise Skip("sides do not name both event teams")
        long_abbr, team = teams[long_["teamId"]]
        side_abbr = ((long_.get("team") or {}).get("abbreviation") or long_abbr).lower()
        if side_abbr != long_abbr:
            raise Skip("long side team id and abbreviation disagree")

    if kind == "h2h":
        if line is not None:
            raise Skip("moneyline carries a line")
        names = {norm_name(t.get("name")) for t in event.get("teams") or []
                 if t.get("id") == long_["teamId"]}
        names |= {norm_name(t.get(k)) for t in event.get("teams") or []
                  if t.get("id") == long_["teamId"] for k in ("alias", "safeName")}
        if norm_name(long_.get("description")) not in names:
            raise Skip("long side description is not its team")
        text = "agree"
    elif kind == "spreads":
        if line is None or line == 0:
            raise Skip("spread without a line")
        if _float(long_.get("description")) != line or _float(short.get("description")) != -line:
            raise Skip("side descriptions disagree with line")
        num = float(f"{sm.group(len(sm.groups()) - 1)}.{sm.group(len(sm.groups()))}")
        if num != abs(line):
            raise Skip("slug number disagrees with line")
        if prefix == "cks":
            if sm["abbr"] != long_abbr:
                raise Skip("cks slug team is not the long side")
        elif (sm["sign"] == "pos") != (line > 0):
            raise Skip("slug pos/neg disagrees with line sign")
        claimed = None
        if (c := _DESC_COVER.search(desc)):
            claimed = (book_team(sport, c["team"]), float(c["h"]))
        elif (c := _DESC_WINS_BY.search(desc)):
            claimed = (book_team(sport, c["team"]), -float(c["h"]))
        if claimed is not None:
            text = "agree" if claimed == (team, line) else "conflict"
    else:  # totals
        if line is None or line <= 0:
            raise Skip("total without a line")
        if norm_name(long_.get("description")) != "over" \
                or norm_name(short.get("description")) != "under":
            raise Skip("totals long side is not Over")
        if float(f"{sm.group(1)}.{sm.group(2)}") != line:
            raise Skip("slug number disagrees with line")
        if (c := _DESC_OVER.search(desc)):
            text = "agree" if float(c["t"]) == line else "conflict"

    if text == "conflict" and not trust_sides:
        raise Skip("text_conflict: description names the other side")
    starts = _ts(event.get("startTime")) or _ts(market.get("gameStartTime"))
    if starts is None:
        raise Skip("no start time")
    fee_rate = _float(market.get("feeCoefficient")) or FEE_RATE
    return Claim(
        market_slug=slug, event_slug=ev_slug, sport=sport, kind=kind, period=period,
        team=team, line=None if kind == "h2h" else line,
        teams=frozenset(c for _, c in teams.values()), starts_at=starts,
        bid=amount(market.get("bestBidQuote")), ask=amount(market.get("bestAskQuote")),
        fee_rate=fee_rate, text_check=text)


GAME_LINE_PREFIXES = ("aec", "asc", "tsc", "cks")


def claims_from_events(events: list[dict[str, Any]], sport: str, now: datetime,
                       trust_sides: bool = False, include_f5: bool = True,
                       ) -> tuple[list[Claim], dict[str, int], int]:
    """(claims, skip reason -> count, markets considered). Game lines only."""
    claims: list[Claim] = []
    skips: dict[str, int] = {}
    seen = 0
    for ev in events:
        for m in ev.get("markets") or []:
            if (m.get("slug") or "").split("-")[0] not in GAME_LINE_PREFIXES:
                continue
            if m.get("closed") or m.get("status") == "MARKET_STATUS_RESOLVED":
                continue
            seen += 1
            try:
                c = parse_market(m, ev, sport, trust_sides=trust_sides)
                if c.starts_at - now < MIN_LEAD or ev.get("live"):
                    raise Skip("game started")
                if c.period == "f5" and not include_f5:
                    raise Skip("first-five (needs --f5)")
            except Skip as s:
                skips[str(s)] = skips.get(str(s), 0) + 1
                continue
            claims.append(c)
    return claims, skips, seen


def merge_counts(*dicts: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in dicts:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


# --------------------------------------------------------------------------
# the sportsbook side
# --------------------------------------------------------------------------

@dataclass
class BookGame:
    id: str
    commence: datetime
    teams: frozenset[str]
    #: book key -> market key -> list of (name, decimal price, point)
    books: dict[str, dict[str, list[tuple[str, float, float | None]]]] = field(default_factory=dict)
    updated: dict[str, datetime] = field(default_factory=dict)


def parse_odds(payload: list[dict[str, Any]], sport: str,
               odds_format: str = "decimal") -> tuple[list[BookGame], dict[str, int]]:
    """The provider's odds array -> games with canonical team names."""
    games: list[BookGame] = []
    skips: dict[str, int] = {}
    for g in payload or []:
        home, away = book_team(sport, g.get("home_team")), book_team(sport, g.get("away_team"))
        start = _ts(g.get("commence_time"))
        if not home or not away or home == away or start is None:
            skips["book game with unknown team or time"] = skips.get(
                "book game with unknown team or time", 0) + 1
            continue
        bg = BookGame(id=str(g.get("id")), commence=start, teams=frozenset((home, away)))
        for b in g.get("bookmakers") or []:
            key = b.get("key")
            for mk in b.get("markets") or []:
                rows = []
                for o in mk.get("outcomes") or []:
                    name = o.get("name")
                    if name not in ("Over", "Under"):
                        name = book_team(sport, name)
                    try:
                        price = to_decimal(o["price"], odds_format)
                    except (KeyError, TypeError, ValueError):
                        name = None
                    if name is None:
                        rows = []
                        break
                    rows.append((name, price, _float(o.get("point"))))
                if rows:
                    bg.books.setdefault(key, {})[mk.get("key")] = rows
            if (u := _ts(b.get("last_update"))) is not None:
                bg.updated[key] = u
        games.append(bg)
    return games, skips


def match_game(claim: Claim, games: list[BookGame]) -> BookGame:
    cands = [g for g in games if g.teams == claim.teams
             and abs(g.commence - claim.starts_at) <= MATCH_WINDOW]
    if not cands:
        raise Skip("no book game for this pair and time")
    if len(cands) > 1:
        raise Skip("two book games in the window (doubleheader?)")
    return cands[0]


def _book_pair(claim: Claim, rows: list[tuple[str, float, float | None]]
               ) -> tuple[float, float] | None:
    """(decimal odds for YES, decimal odds for NO) from one book, exact line only."""
    if claim.kind == "h2h":
        if len(rows) != 2:
            return None                      # a draw leg makes it three-way
        yes = [p for n, p, _ in rows if n == claim.team]
        no = [p for n, p, _ in rows if n != claim.team and n in claim.teams]
    elif claim.kind == "spreads":
        yes = [p for n, p, pt in rows if n == claim.team and pt == claim.line]
        no = [p for n, p, pt in rows if n != claim.team and n in claim.teams
              and pt == -claim.line]
    else:
        yes = [p for n, p, pt in rows if n == "Over" and pt == claim.line]
        no = [p for n, p, pt in rows if n == "Under" and pt == claim.line]
    if len(yes) != 1 or len(no) != 1:
        return None
    return yes[0], no[0]


@dataclass
class Fair:
    prob: float
    source: str                        # pinnacle | consensus
    method: str
    by_method: dict[str, float]        # of the chosen source
    consensus: float | None
    books: list[str]
    age_s: float | None


def fair_value(claim: Claim, game: BookGame, now: datetime,
               method: str = DEFAULT_METHOD) -> Fair:
    mkey = claim.kind if claim.period == "full" else f"{claim.kind}_1st_5_innings"
    per_book: dict[str, dict[str, float]] = {}
    for book, markets in game.books.items():
        pair = _book_pair(claim, markets.get(mkey) or [])
        if pair is None:
            continue
        per_book[book] = {m: v[0] for m, v in devig(list(pair)).items()}
    if not per_book:
        raise Skip("no book quotes this exact line")
    consensus = statistics.median(v[method] for v in per_book.values())
    if SHARP_BOOK in per_book:
        chosen, source = per_book[SHARP_BOOK], "pinnacle"
        upd = game.updated.get(SHARP_BOOK)
    elif len(per_book) >= MIN_CONSENSUS_BOOKS:
        chosen = {m: statistics.median(v[m] for v in per_book.values()) for m in METHODS}
        source = "consensus"
        upd = min(game.updated.values()) if game.updated else None
    else:
        raise Skip(f"no Pinnacle and fewer than {MIN_CONSENSUS_BOOKS} books at this line")
    return Fair(prob=chosen[method], source=source, method=method, by_method=chosen,
                consensus=consensus, books=sorted(per_book),
                age_s=(now - upd).total_seconds() if upd else None)


def edges(fair: float, bid: float | None, ask: float | None,
          rate: float) -> tuple[float | None, float | None]:
    """Expected value per share after the fee: buy YES at the ask, or sell at the bid."""
    n = NOMINAL_SHARES
    yes = None if ask is None else fair - ask - fee_usd(ask, n, rate) / n
    no = None if bid is None else bid - fair - fee_usd(bid, n, rate) / n
    return yes, no


# --------------------------------------------------------------------------
# storage and the call budget
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS comparisons (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    sport         TEXT NOT NULL,
    event_slug    TEXT NOT NULL,
    market_slug   TEXT NOT NULL,
    kind          TEXT NOT NULL,        -- h2h | spreads | totals
    period        TEXT NOT NULL,        -- full | f5
    yes_side      TEXT NOT NULL,        -- what YES pays on, e.g. "San Diego Padres +1.5"
    line          REAL,
    starts_at     TEXT,
    poly_bid      REAL,
    poly_ask      REAL,
    fee_rate      REAL,
    fair_prob     REAL NOT NULL,
    fair_source   TEXT NOT NULL,        -- pinnacle | consensus
    method        TEXT NOT NULL,
    fair_multiplicative REAL,
    fair_power    REAL,
    fair_shin     REAL,
    fair_consensus REAL,                -- median over all books, chosen method
    n_books       INTEGER NOT NULL,
    books_used    TEXT NOT NULL,        -- JSON list of book keys at this exact line
    book_age_s    REAL,                 -- seconds since the source book updated
    odds_event_id TEXT,
    edge_yes      REAL,                 -- per share after fees, buying YES at the ask
    edge_no       REAL,                 -- per share after fees, selling YES at the bid
    text_check    TEXT,                 -- agree | unparsed | conflict (only with --trust-sides)
    resolved_outcome TEXT,              -- "1", "0" or "0.5" (tie/push)
    resolved_at   TEXT,
    UNIQUE(market_slug, at)
);
CREATE INDEX IF NOT EXISTS idx_cmp_market ON comparisons(market_slug);
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    sport         TEXT NOT NULL,
    n_markets     INTEGER NOT NULL,     -- game-line markets considered
    n_claims      INTEGER NOT NULL,     -- parsed to an unambiguous YES side
    n_matched     INTEGER NOT NULL,     -- with a fair price recorded
    skips_json    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS odds_calls (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    sport         TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    est_cost      INTEGER NOT NULL,
    credits_last  INTEGER,
    credits_used  INTEGER,
    credits_remaining INTEGER,
    status        INTEGER
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def estimate_cost(n_markets: int, n_books: int) -> int:
    """markets x region-equivalents, where 10 books = 1 region (provider docs)."""
    return n_markets * max(1, math.ceil(n_books / 10))


class Budget:
    """Refuses a call that would cross the monthly or daily cap or the reserve."""

    def __init__(self, conn: sqlite3.Connection, monthly: int = MONTHLY_BUDGET,
                 daily: int = DAILY_BUDGET, reserve: int = RESERVE_CREDITS):
        self.conn, self.monthly, self.daily, self.reserve = conn, monthly, daily, reserve

    def spent_since(self, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(COALESCE(credits_last, est_cost)), 0) FROM odds_calls"
            " WHERE at >= ?", (since.isoformat(),)).fetchone()
        return int(row[0])

    def refusal(self, cost: int, now: datetime) -> str | None:
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.spent_since(month) + cost > self.monthly:
            return f"monthly budget {self.monthly} would be exceeded"
        if self.spent_since(day) + cost > self.daily:
            return f"daily budget {self.daily} would be exceeded"
        row = self.conn.execute(
            "SELECT credits_remaining FROM odds_calls WHERE credits_remaining IS NOT NULL"
            " ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None and row[0] - cost < self.reserve:
            return f"provider reports {row[0]} credits left; reserve is {self.reserve}"
        return None

    def record(self, sport: str, endpoint: str, est: int, resp: httpx.Response | None,
               now: datetime) -> None:
        h = resp.headers if resp is not None else {}

        def hdr(name: str) -> int | None:
            v = _float(h.get(name))
            return None if v is None else int(v)
        self.conn.execute(
            "INSERT INTO odds_calls (at, sport, endpoint, est_cost, credits_last, credits_used,"
            " credits_remaining, status) VALUES (?,?,?,?,?,?,?,?)",
            (now.isoformat(), sport, endpoint, est, hdr("x-requests-last"),
             hdr("x-requests-used"), hdr("x-requests-remaining"),
             resp.status_code if resp is not None else None))
        self.conn.commit()


class OddsError(RuntimeError):
    pass


async def odds_get(client: httpx.AsyncClient, key: str, budget: Budget, sport: str,
                   path: str, params: dict[str, Any], est: int,
                   now: datetime) -> Any:
    """One budgeted GET. The key goes in params and is never logged."""
    if (why := budget.refusal(est, now)) is not None:
        raise OddsError(f"budget: {why}; not calling")
    resp = None
    try:
        resp = await client.get(f"{API_BASE}{path}", params={"apiKey": key, **params})
    finally:
        budget.record(sport, path, est, resp, now)
    if resp.status_code == 401:
        raise OddsError("odds API rejected the key (401): check ODDS_API_KEY")
    if resp.status_code == 429:
        raise OddsError("odds API says slow down or quota exhausted (429)")
    if resp.status_code != 200:
        raise OddsError(f"odds API returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def api_key() -> str | None:
    from config import load_dotenv_if_present
    load_dotenv_if_present()
    return os.getenv(ENV_KEY) or None


INERT_MESSAGE = f"""\
reference.py is INERT: no {ENV_KEY} is set, so no sportsbook odds were fetched
and nothing was recorded. To switch it on:
  1. Get a free key at {SIGNUP_URL} (Starter plan: 500 credits/month).
  2. Add one line to .env:   {ENV_KEY}=<your key>
  3. Run `python reference.py --check-teams` once (2 credits), then
     `python reference.py` (3 credits per MLB slate snapshot).
Budget: at most {MONTHLY_BUDGET} credits/month and {DAILY_BUDGET}/day, enforced
from reference.db before every call. `--coverage` works without a key."""


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

async def fetch_events(sport: str, hours: float) -> list[dict[str, Any]]:
    """One events.list call to the exchange: the whole slate, listed prices included."""
    from polymarket_us import AsyncPolymarketUS

    now = datetime.now(timezone.utc)
    async with AsyncPolymarketUS() as pm:
        page = await pm.events.list({
            "limit": 40, "closed": False, "tagSlug": sport,
            "startTimeMin": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "startTimeMax": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return page.get("events", []) or []


def compare(claims: list[Claim], games: list[BookGame], now: datetime,
            method: str = DEFAULT_METHOD) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    skips: dict[str, int] = {}
    for c in claims:
        try:
            g = match_game(c, games)
            f = fair_value(c, g, now, method)
        except Skip as s:
            skips[str(s)] = skips.get(str(s), 0) + 1
            continue
        ey, en = edges(f.prob, c.bid, c.ask, c.fee_rate)
        rows.append({
            "at": now.isoformat(), "sport": c.sport, "event_slug": c.event_slug,
            "market_slug": c.market_slug, "kind": c.kind, "period": c.period,
            "yes_side": c.label, "line": c.line, "starts_at": c.starts_at.isoformat(),
            "poly_bid": c.bid, "poly_ask": c.ask, "fee_rate": c.fee_rate,
            "fair_prob": round(f.prob, 5), "fair_source": f.source, "method": f.method,
            "fair_multiplicative": round(f.by_method["multiplicative"], 5),
            "fair_power": round(f.by_method["power"], 5),
            "fair_shin": round(f.by_method["shin"], 5),
            "fair_consensus": None if f.consensus is None else round(f.consensus, 5),
            "n_books": len(f.books), "books_used": json.dumps(f.books),
            "book_age_s": f.age_s, "odds_event_id": g.id,
            "edge_yes": None if ey is None else round(ey, 5),
            "edge_no": None if en is None else round(en, 5),
            "text_check": c.text_check,
        })
    return rows, skips


def store(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    conn.executemany(
        f"INSERT OR IGNORE INTO comparisons ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})", [tuple(r[c] for c in cols) for r in rows])
    conn.commit()


def print_rows(rows: list[dict[str, Any]], limit: int = 40) -> None:
    def best(r: dict[str, Any]) -> float:
        return max(x for x in (r["edge_yes"], r["edge_no"], -9) if x is not None)
    print(f"\n  {'market':<44}{'YES pays on':<30}{'bid':>6}{'ask':>6}{'fair':>7}"
          f"{'src':>10}{'books':>6}{'edge':>7}")
    for r in sorted(rows, key=best, reverse=True)[:limit]:
        e = best(r)
        flag = "  <--" if e > 0 else ""
        print(f"  {r['market_slug'][:43]:<44}{r['yes_side'][:29]:<30}"
              f"{r['poly_bid'] if r['poly_bid'] is not None else float('nan'):>6.3f}"
              f"{r['poly_ask'] if r['poly_ask'] is not None else float('nan'):>6.3f}"
              f"{r['fair_prob']:>7.3f}{r['fair_source']:>10}{r['n_books']:>6}{e:>+7.3f}{flag}")


def print_skips(title: str, skips: dict[str, int]) -> None:
    if skips:
        print(f"\n  {title}:")
        for k, v in sorted(skips.items(), key=lambda kv: -kv[1]):
            print(f"    {v:>5}  {k}")


async def record(sport: str, db: str = DB_PATH, hours: float = 30.0,
                 books: tuple[str, ...] = DEFAULT_BOOKS, f5: bool = False,
                 trust_sides: bool = False, method: str = DEFAULT_METHOD,
                 odds_json: str | None = None, events_json: str | None = None) -> int:
    key = api_key()
    if key is None and odds_json is None:
        print(INERT_MESSAGE)
        return 0
    now = datetime.now(timezone.utc)
    events = (json.load(open(events_json, encoding="utf-8"))["events"] if events_json
              else await fetch_events(sport, hours))
    claims, skips, seen = claims_from_events(events, sport, now, trust_sides, include_f5=f5)
    if not claims:
        print(f"{seen} game-line markets, none comparable")
        print_skips("not compared", skips)
        return 0

    if odds_json:
        payload = json.load(open(odds_json, encoding="utf-8"))
        games, gskips = parse_odds(payload, sport)
        rows, mskips = compare(claims, games, now, method)
        print(f"OFFLINE (--odds-json): {len(rows)} of {seen} markets compared; nothing recorded")
        print_rows(rows)
        print_skips("not compared", merge_counts(skips, gskips, mskips))
        return 0

    assert key is not None
    with closing(connect(db)) as conn:
        budget = Budget(conn)
        async with httpx.AsyncClient(timeout=30) as client:
            games: list[BookGame] = []
            gskips: dict[str, int] = {}
            try:
                payload = await odds_get(
                    client, key, budget, sport, f"/sports/{SPORT_KEYS[sport]}/odds",
                    {"regions": "us", "bookmakers": ",".join(books), "markets": ",".join(FEATURED),
                     "oddsFormat": "decimal", "dateFormat": "iso",
                     "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "commenceTimeTo": (now + timedelta(hours=hours)).strftime(
                         "%Y-%m-%dT%H:%M:%SZ")},
                    estimate_cost(len(FEATURED), len(books)), now)
            except OddsError as exc:
                print(f"no odds fetched: {exc}")
                return 1
            games, gskips = parse_odds(payload, sport)
            if f5 and sport == "mlb":
                games = await _add_f5(client, key, budget, sport, games, claims, books, now)
        rows, mskips = compare(claims, games, now, method)
        store(conn, rows)
        allskips = merge_counts(skips, gskips, mskips)
        conn.execute("INSERT INTO runs (at, sport, n_markets, n_claims, n_matched, skips_json)"
                     " VALUES (?,?,?,?,?,?)",
                     (now.isoformat(), sport, seen, len(claims), len(rows), json.dumps(allskips)))
        conn.commit()
        left = conn.execute("SELECT credits_remaining FROM odds_calls ORDER BY id DESC LIMIT 1"
                            ).fetchone()
    print(f"{sport}: {seen} game-line markets, {len(claims)} with an unambiguous YES side, "
          f"{len(rows)} compared and recorded. Credits left: {left[0] if left else '?'}")
    print_rows(rows)
    print_skips("not compared", allskips)
    return 0


async def _add_f5(client: httpx.AsyncClient, key: str, budget: Budget, sport: str,
                  games: list[BookGame], claims: list[Claim], books: tuple[str, ...],
                  now: datetime) -> list[BookGame]:
    """First-five markets live on the per-event endpoint: 3 credits per game."""
    wanted = {g.id for c in claims if c.period == "f5" for g in games
              if g.teams == c.teams and abs(g.commence - c.starts_at) <= MATCH_WINDOW}
    for g in games:
        if g.id not in wanted:
            continue
        try:
            one = await odds_get(
                client, key, budget, sport, f"/sports/{SPORT_KEYS[sport]}/events/{g.id}/odds",
                {"regions": "us", "bookmakers": ",".join(books), "markets": ",".join(F5_MARKETS),
                 "oddsFormat": "decimal", "dateFormat": "iso"},
                estimate_cost(len(F5_MARKETS), len(books)), now)
        except OddsError as exc:
            print(f"first-five stopped: {exc}")
            break
        extra, _ = parse_odds([one], sport)
        for e in extra:
            for book, mk in e.books.items():
                g.books.setdefault(book, {}).update(mk)
    return games


async def check_teams(db: str = DB_PATH) -> int:
    """1 credit per sport: every provider team name must map, and every table entry exist."""
    key = api_key()
    if key is None:
        print(INERT_MESSAGE)
        return 0
    now = datetime.now(timezone.utc)
    bad = 0
    with closing(connect(db)) as conn:
        budget = Budget(conn)
        async with httpx.AsyncClient(timeout=30) as client:
            for sport, skey in SPORT_KEYS.items():
                try:
                    parts = await odds_get(client, key, budget, sport,
                                           f"/sports/{skey}/participants", {}, 1, now)
                except OddsError as exc:
                    print(f"{sport}: {exc}")
                    return 1
                names = {p.get("full_name") for p in parts}
                unmapped = sorted(n for n in names if n and book_team(sport, n) is None)
                mapped = {book_team(sport, n) for n in names}
                missing = sorted(set(TEAMS[sport].values()) - mapped)
                bad += len(unmapped) + len(missing)
                print(f"{sport}: {len(names)} provider teams; unmapped {unmapped or 'none'}; "
                      f"table teams not offered {missing or 'none'}")
    return 1 if bad else 0


def coverage(sport: str, events: list[dict[str, Any]], trust_sides: bool = False) -> None:
    """Exchange side only: which markets have an unambiguous sportsbook equivalent."""
    now = datetime.now(timezone.utc)
    claims, skips, seen = claims_from_events(events, sport, now, trust_sides)
    by: dict[str, int] = {}
    for c in claims:
        k = f"{c.kind}/{c.period}"
        by[k] = by.get(k, 0) + 1
    games = len({c.event_slug for c in claims})
    print(f"{sport}: {len(events)} events, {seen} game-line markets; {len(claims)} parse to an "
          f"unambiguous YES side across {games} not-started games")
    for k, v in sorted(by.items()):
        print(f"    {v:>5}  {k}")
    print_skips("not comparable", skips)
    print("\n  A claim becomes a comparison only when a book quotes the SAME line; the"
          "\n  featured endpoint carries one spread and one total per book per game.")


async def backfill(db: str = DB_PATH, pause: float = 2.0, limit: int = 200) -> None:
    """Settlement from the exchange, paced; same shape shadow.py verified."""
    from polymarket_us import AsyncPolymarketUS

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    with closing(connect(db)) as conn:
        slugs = [r[0] for r in conn.execute(
            "SELECT DISTINCT market_slug FROM comparisons WHERE resolved_outcome IS NULL"
            " AND starts_at < ? LIMIT ?", (cutoff, limit))]
        filled = 0
        async with AsyncPolymarketUS() as pm:
            for slug in slugs:
                try:
                    res = await pm.markets.settlement(slug)
                except Exception:  # noqa: BLE001 -- NotFoundError means not settled yet
                    await asyncio.sleep(pause)
                    continue
                v = _float(res.get("settlement"))
                if v in (0.0, 0.5, 1.0):
                    conn.execute("UPDATE comparisons SET resolved_outcome=?, resolved_at=?"
                                 " WHERE market_slug=?",
                                 ("0.5" if v == 0.5 else str(int(v)),
                                  datetime.now(timezone.utc).isoformat(), slug))
                    conn.commit()
                    filled += 1
                await asyncio.sleep(pause)
    print(f"filled {filled} of {len(slugs)}")


def score_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Brier of the reference vs the exchange mid, per event then across events."""
    by_event: dict[str, list[tuple[float, float, dict[str, float]]]] = {}
    for r in rows:
        if r["poly_bid"] is None or r["poly_ask"] is None:
            continue
        y = float(r["resolved_outcome"])
        mid = (r["poly_bid"] + r["poly_ask"]) / 2
        meth = {m: (r[f"fair_{m}"] - y) ** 2 for m in METHODS if r[f"fair_{m}"] is not None}
        by_event.setdefault(r["event_slug"], []).append(
            ((r["fair_prob"] - y) ** 2, (mid - y) ** 2, meth))
    ev_ref, ev_poly, wins = [], [], {"reference": 0, "exchange": 0, "tie": 0}
    ev_meth: dict[str, list[float]] = {m: [] for m in METHODS}
    for items in by_event.values():
        br = statistics.mean(i[0] for i in items)
        bp = statistics.mean(i[1] for i in items)
        ev_ref.append(br)
        ev_poly.append(bp)
        wins["reference" if br < bp - 1e-9 else "exchange" if bp < br - 1e-9 else "tie"] += 1
        for m in METHODS:
            vals = [i[2][m] for i in items if m in i[2]]
            if vals:
                ev_meth[m].append(statistics.mean(vals))
    return {
        "events": len(by_event), "markets": sum(len(v) for v in by_event.values()),
        "brier_reference": statistics.mean(ev_ref) if ev_ref else None,
        "brier_exchange": statistics.mean(ev_poly) if ev_poly else None,
        "closer_by_event": wins,
        "brier_by_method": {m: statistics.mean(v) for m, v in ev_meth.items() if v},
    }


def report(db: str = DB_PATH, limit: int = 25) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect(db)) as conn:
        runs = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 4").fetchall()
        spent = conn.execute("SELECT COALESCE(SUM(COALESCE(credits_last, est_cost)),0),"
                             " MIN(credits_remaining) FROM odds_calls").fetchone()
        latest = conn.execute(
            "SELECT c.* FROM comparisons c JOIN (SELECT market_slug, MAX(at) at FROM comparisons"
            " GROUP BY market_slug) l ON c.market_slug=l.market_slug AND c.at=l.at"
            " WHERE c.starts_at > ?", (now,)).fetchall()
        # Last snapshot before the start: what the books said when the game began.
        settled = conn.execute(
            "SELECT c.* FROM comparisons c JOIN (SELECT market_slug, MAX(at) at FROM comparisons"
            " WHERE at < starts_at GROUP BY market_slug) l"
            " ON c.market_slug=l.market_slug AND c.at=l.at"
            " WHERE c.resolved_outcome IS NOT NULL").fetchall()
    print("=" * 72)
    print(f"  reference.db: {len(runs)} recent runs; credits spent {spent[0]}, "
          f"provider-reported low {spent[1]}")
    print("=" * 72)
    for r in runs:
        print(f"  {r['at'][:16]} {r['sport']}: {r['n_markets']} markets, {r['n_claims']} "
              f"claims, {r['n_matched']} compared")
    if latest:
        print(f"\n  Open gaps (latest snapshot, {len(latest)} markets not yet started):")
        print_rows([dict(r) for r in latest], limit)
    if not settled:
        print("\n  Nothing settled yet. Run --backfill after the games.")
        return
    s = score_rows(settled)
    print(f"\n  Settled: {s['markets']} markets across {s['events']} events "
          "(the event is the unit; its lines resolve together)")
    print(f"    Brier, mean of event means   reference {s['brier_reference']:.4f}   "
          f"exchange mid {s['brier_exchange']:.4f}")
    w = s["closer_by_event"]
    print(f"    closer, by event             reference {w['reference']}   exchange "
          f"{w['exchange']}   tie {w['tie']}")
    print("    de-vig method, Brier         " + "   ".join(
        f"{m} {v:.4f}" for m, v in s["brier_by_method"].items()))
    if s["events"] < 30:
        print("    Fewer than 30 events: a direction, not a finding.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sportsbook fair value vs exchange prices. Shadow.")
    ap.add_argument("--sport", choices=sorted(SPORT_KEYS), default="mlb")
    ap.add_argument("--hours", type=float, default=30.0, help="games starting within")
    ap.add_argument("--method", choices=sorted(METHODS), default=DEFAULT_METHOD)
    ap.add_argument("--f5", action="store_true", help="MLB first-five lines: 3 credits per game")
    ap.add_argument("--trust-sides", action="store_true",
                    help="also record markets whose description names the other side")
    ap.add_argument("--odds-json", help="offline: a saved odds payload; records nothing")
    ap.add_argument("--events-json", help="use a saved exchange events payload")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--check-teams", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    # httpx logs full request URLs at INFO, and the odds key is a query parameter.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.report:
        report()
    elif args.backfill:
        asyncio.run(backfill())
    elif args.check_teams:
        raise SystemExit(asyncio.run(check_teams()))
    elif args.coverage:
        events = (json.load(open(args.events_json, encoding="utf-8"))["events"]
                  if args.events_json else asyncio.run(fetch_events(args.sport, args.hours)))
        coverage(args.sport, events, args.trust_sides)
    else:
        raise SystemExit(asyncio.run(record(
            args.sport, hours=args.hours, f5=args.f5, trust_sides=args.trust_sides,
            method=args.method, odds_json=args.odds_json, events_json=args.events_json)))


if __name__ == "__main__":
    main()
