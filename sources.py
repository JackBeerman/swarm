"""
sources.py -- faster, more authoritative inputs for the fast lane.

SHADOW ONLY. Reads public data; places no orders and imports nothing that can.

The fast lane's bottleneck is ingestion, not the model. Jev decides in
~200-400 ms; the RSS feeds it read measured a median 15-27 minutes behind
publication (Giants-Rams, 2026-09-21: the first QB-injury headline arrived
115 s after publication, the book moved over the following minutes). This
module adds sources that are either faster or closer to the fact:

  source                 what it is                                  ToS / etiquette
  ---------------------  ------------------------------------------  -----------------------------
  MLBLiveSource          statsapi.mlb.com live game feed: pitching    MLBAM copyright notice on
                         changes, starter removed, injuries,          every response: individual,
                         ejections, delays, scoring plays; plus       non-commercial, non-bulk use.
                         schedule status (postponed / delayed) and    Gameday itself asks clients to
                         probable-pitcher changes. DATA, not text.    wait 10 s; we poll 30 s.
  GoogleNewsSource       news.google.com/rss/search per event query   robots.txt DISALLOWS /rss/ for
                         (aggregates every outlet it indexes)         all agents. Opt-in only; the
                                                                      operator decides. Poll >= 120 s.
  BlueskyAuthorSource    public.api.bsky.app getAuthorFeed for beat   Public AppView, no auth, docs
                         reporters / fantasy news accounts            say 3000 req / 5 min / IP.
  NWSObservationSource   api.weather.gov latest station observation   Public domain, documented API;
                                                                      NWS asks for an identifying UA.
                                                                      (robots.txt Disallows / -- aimed
                                                                      at crawlers; weather.py already
                                                                      uses this API.)
  NWSAlertsSource        api.weather.gov active alerts by area        same
  GitHubReleasesSource   api.github.com releases for a repo           GitHub API terms; 60 req/h
                                                                      unauthenticated. (releases.atom
                                                                      is robots-Disallowed.)
  STATUS_FEEDS           status pages' /history.rss (via RSSSource)   allowed; their /api/ is not
  MLBTransactionsSource  statsapi transactions (IL moves, call-ups);  as MLBLiveSource
                         date-only, so lag cannot be measured
  RSSSource              any RSS/Atom feed (the existing FEEDS_BY_TAG) per the publisher

Probed 2026-09-22/23 and NOT usable: ESPN's site.api JSON (403 Access
Denied from this network, with either User-Agent), Reddit JSON (403;
the Data API now needs OAuth registration), Bluesky searchPosts on the
public AppView (403; search needs auth -- author feeds do not).

Every item is a dict fastlane's Recorder already understands (source,
url, title, summary, published_at) plus `fetched_at` and `kind`. MLB
items carry the event's own clock (`startTime` of the action, `endTime`
of a scoring play) as `published_at`, so lag = first seen - when it
happened on the field.

Politeness is enforced here, not left to callers: `Source.poll()` returns
[] if called sooner than `min_interval`, so a fast-lane loop polling
every 20 s cannot hit a third party faster than each source allows.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import httpx

# The one RSS/Atom parser in the repo. Imported at the top on purpose: a
# lazy import inside poll() would pull in the exchange SDK and litellm (which
# fetches a price table at import) in the middle of a timed poll. fastlane
# imports this module lazily, inside run(), so there is no cycle.
from fastlane import parse_feed

log = logging.getLogger("sources")

USER_AGENT = "swarm-research/0.1 (news latency study; shadow only)"
#: NWS asks for a UA that identifies the application.
NWS_USER_AGENT = "(swarm research; sources.py)"

Item = dict[str, Any]

ITEM_KEYS = ("source", "url", "title", "summary", "published_at", "fetched_at", "kind")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: str | datetime | None) -> str | None:
    """Normalise a timestamp to an aware ISO string, or None."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def make_item(source: str, url: str, title: str, *, summary: str = "",
              published_at: str | datetime | None = None, kind: str = "headline") -> Item:
    return {"source": source, "url": url, "title": title[:300], "summary": (summary or "")[:600],
            "published_at": _iso(published_at), "fetched_at": None, "kind": kind}


class Source:
    """
    Something that can be polled for items. Subclasses implement `_fetch`.

    `poll()` never raises: a dead source must not stop the others (same
    rule as fastlane.poll_feed). It returns [] when called before
    `min_interval` has passed since the last attempt.
    """

    def __init__(self, name: str, min_interval: float = 60.0):
        self.name = name
        self.min_interval = float(min_interval)
        self._last = -math.inf
        self.polls = 0          # attempts that reached the network
        self.ok_polls = 0
        self.errors = 0
        self.last_ok = False
        self.last_ms: float | None = None
        self.last_bytes = 0

    def due(self) -> bool:
        return time.monotonic() - self._last >= self.min_interval

    async def poll(self, client: httpx.AsyncClient) -> list[Item]:
        if not self.due():
            return []
        self._last = time.monotonic()
        self.polls += 1
        self.last_bytes = 0
        self.last_ok = False
        t0 = time.perf_counter()
        try:
            items = await self._fetch(client)
        except Exception as exc:  # noqa: BLE001 -- one dead source must not stop the rest
            self.errors += 1
            log.debug("source %s failed: %s", self.name, exc)
            return []
        finally:
            self.last_ms = (time.perf_counter() - t0) * 1000
        self.ok_polls += 1
        self.last_ok = True
        fetched = _now().isoformat()
        for it in items:
            it["fetched_at"] = fetched
        return items

    async def _get(self, client: httpx.AsyncClient, url: str, *, params: dict | None = None,
                   ua: str = USER_AGENT, accept: str | None = None) -> httpx.Response:
        headers = {"User-Agent": ua}
        if accept:
            headers["Accept"] = accept
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        self.last_bytes += len(r.content)
        return r

    async def _fetch(self, client: httpx.AsyncClient) -> list[Item]:
        raise NotImplementedError


# --------------------------------------------------------------------------
# text news
# --------------------------------------------------------------------------

def _parse_feed(xml_text: str, source: str) -> list[dict[str, Any]]:
    return parse_feed(xml_text, source)


class RSSSource(Source):
    """The existing feeds, as Sources. Behaviour identical to fastlane.poll_feed."""

    def __init__(self, name: str, url: str, min_interval: float = 60.0, kind: str = "headline"):
        super().__init__(name, min_interval)
        self.url, self.kind = url, kind

    async def _fetch(self, client: httpx.AsyncClient) -> list[Item]:
        r = await self._get(client, self.url)
        out = []
        for h in _parse_feed(r.text, self.name):
            out.append(make_item(h["source"], h["url"], h["title"], summary=h["summary"],
                                 published_at=h["published_at"], kind=self.kind))
        return out


def google_news_url(query: str, window: str = "1h") -> str:
    q = f"{query} when:{window}" if window else query
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"


def event_query(title: str) -> str:
    """'NY Giants vs LA Rams' -> 'NY Giants LA Rams'. Google ANDs the words."""
    for sep in (" vs. ", " vs ", " at ", " @ ", " v "):
        title = title.replace(sep, " ")
    return " ".join(title.split())


class GoogleNewsSource(Source):
    """
    Google News search RSS for one query. Aggregates every outlet Google
    indexes, so a local beat writer's story can surface before the national
    feeds carry it. `window` ("1h", "1d") is Google's `when:` operator: with
    1h the result set stays small and relevance-ranking churn cannot bring
    back old articles, at the cost of never seeing items older than an hour
    (lag measured on this source is censored at 60 min).

    Titles arrive as "Headline - Publisher"; kept as is, the publisher is
    information. Links are news.google.com redirects.
    """

    def __init__(self, query: str, label: str | None = None, window: str = "1h",
                 min_interval: float = 120.0):
        label = label or query
        super().__init__(f"gnews:{label}", min_interval)
        self.query, self.window = query, window

    async def _fetch(self, client: httpx.AsyncClient) -> list[Item]:
        r = await self._get(client, google_news_url(self.query, self.window))
        return [make_item(self.name, h["url"], h["title"], summary=h["summary"],
                          published_at=h["published_at"], kind="news")
                for h in _parse_feed(r.text, self.name)]


BSKY_API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"


def parse_bsky_feed(body: dict[str, Any], handle: str) -> list[Item]:
    """Own posts only. A repost carries the ORIGINAL createdAt, which would read as huge lag."""
    out = []
    for it in body.get("feed") or []:
        if it.get("reason"):                       # repost
            continue
        post = it.get("post") or {}
        rec = post.get("record") or {}
        text = (rec.get("text") or "").strip()
        uri = post.get("uri") or ""
        if not text or not uri:
            continue
        author = (post.get("author") or {}).get("handle") or handle
        if author != handle:                       # quote/thread of someone else
            continue
        rkey = uri.rsplit("/", 1)[-1]
        first_line = text.splitlines()[0]
        out.append(make_item(f"bsky:{handle}", f"https://bsky.app/profile/{handle}/post/{rkey}",
                             first_line, summary=text, published_at=rec.get("createdAt"),
                             kind="social"))
    return out


class BlueskyAuthorSource(Source):
    """
    Latest posts of named accounts. Each handle is its own endpoint and is
    hit once per `min_interval`. Measured index lag (indexedAt - createdAt)
    was 0.4-1.7 s; the limit is how often these accounts post here -- most
    insiders post first on X, which has no free API.
    """

    def __init__(self, handles: list[str], name: str = "bluesky", min_interval: float = 60.0):
        super().__init__(name, min_interval)
        self.handles = list(handles)

    async def _fetch(self, client: httpx.AsyncClient) -> list[Item]:
        out: list[Item] = []
        failures = 0
        for h in self.handles:
            try:
                r = await self._get(client, BSKY_API,
                                    params={"actor": h, "limit": 10, "filter": "posts_no_replies"})
                out.extend(parse_bsky_feed(r.json(), h))
            except Exception as exc:  # noqa: BLE001 -- one missing handle must not hide the rest
                failures += 1
                log.debug("bsky %s failed: %s", h, exc)
        if self.handles and failures == len(self.handles):
            raise RuntimeError("every bluesky handle failed")
        return out


# --------------------------------------------------------------------------
# MLB: structured game data, faster than any headline
# --------------------------------------------------------------------------

MLB_API = "https://statsapi.mlb.com/api"

#: Trimmed live feed: 700 KB -> ~70 KB per poll (measured 2026-09-23).
MLB_LIVE_FIELDS = (
    "gamePk,gameData,status,abstractGameState,detailedState,teams,away,home,name,teamName,"
    "liveData,plays,allPlays,about,atBatIndex,inning,halfInning,startTime,endTime,isScoringPlay,"
    "result,eventType,description,awayScore,homeScore,playEvents,type,details,isSubstitution,"
    "player,id,position,abbreviation,replacedPlayer"
)

#: playEvents[].details.eventType -> item kind. Everything else is ignored.
MLB_ACTION_KINDS = {
    "pitching_substitution": "pitching_change",
    "injury": "injury",
    "ejection": "ejection",
    "offensive_substitution": "substitution",
    "defensive_substitution": "substitution",
}
_ADVISORY_WORDS = ("delay", "injury", "suspend", "postpone", "rain", "weather")
_BAD_STATES = ("postponed", "suspended", "delayed", "cancelled", "canceled")


def parse_live_feed(feed: dict[str, Any], source: str = "mlb") -> list[Item]:
    """
    One statsapi live feed -> fast-lane items. Pure, deterministic and
    idempotent: the whole feed is re-parsed each poll and identity is the
    url (mlb://game/<pk>/<atBat>/<event>), so a repeat poll yields the same
    items and the caller's dedup drops them.

    The scenario the brief most often writes for baseball, "starting
    pitcher removed", is a DATA event here: the first pitching_substitution
    for a fielding team is emitted as kind="starter_removed", with the
    team named, so Jev's scenario match is recognition of plain text.
    """
    gd = feed.get("gameData") or {}
    pk = feed.get("gamePk")
    teams = gd.get("teams") or {}
    away = (teams.get("away") or {}).get("name") or "away"
    home = (teams.get("home") or {}).get("name") or "home"
    title_game = f"{away} at {home}"
    plays = ((feed.get("liveData") or {}).get("plays") or {}).get("allPlays") or []
    out: list[Item] = []
    starters_out: set[str] = set()
    last_score = (0, 0)
    for play in plays:
        about = play.get("about") or {}
        res = play.get("result") or {}
        half = about.get("halfInning") or ""
        inning = about.get("inning")
        fielding = home if half == "top" else away
        batting = away if half == "top" else home
        where = f"{half} {inning}" if inning else ""
        ab = about.get("atBatIndex")
        for i, ev in enumerate(play.get("playEvents") or []):
            d = ev.get("details") or {}
            et = d.get("eventType")
            desc = (d.get("description") or "").strip()
            if not desc:
                continue
            kind = MLB_ACTION_KINDS.get(et or "")
            if et == "game_advisory" and any(w in desc.lower() for w in _ADVISORY_WORDS):
                kind = "advisory"
            if not kind:
                continue
            team = fielding if et in ("pitching_substitution", "defensive_substitution") else (
                batting if et == "offensive_substitution" else None)
            if kind == "pitching_change" and fielding not in starters_out:
                starters_out.add(fielding)
                kind = "starter_removed"
                title = f"{fielding} starting pitcher removed. {desc}"
            else:
                # An advisory ("Injury Delay.") names nobody; the game and
                # inning are what let Jev route it to an event.
                title = f"{team}: {desc}" if team else f"{title_game}, {where}: {desc}"
            out.append(make_item(
                source, f"mlb://game/{pk}/{ab}/{i}", title,
                summary=f"{title_game}, {where}, score {away} {last_score[0]} - {home} "
                        f"{last_score[1]}. {desc}",
                published_at=ev.get("startTime"), kind=kind))
        if res.get("awayScore") is not None and res.get("homeScore") is not None:
            score = (res["awayScore"], res["homeScore"])
        else:
            score = last_score
        if about.get("isScoringPlay") and res.get("description"):
            out.append(make_item(
                source, f"mlb://game/{pk}/{ab}/score",
                f"{batting} score, {away} {score[0]} - {home} {score[1]} ({where}). "
                f"{res['description']}",
                summary=f"{title_game}: {res['description']}",
                published_at=about.get("endTime"), kind="score"))
        last_score = score
    state = (gd.get("status") or {}).get("abstractGameState")
    if state == "Final" and plays:
        out.append(make_item(source, f"mlb://game/{pk}/final",
                             f"Final: {away} {last_score[0]}, {home} {last_score[1]}",
                             summary=title_game, published_at=None, kind="final"))
    return out


def parse_schedule(body: dict[str, Any]) -> list[dict[str, Any]]:
    """statsapi schedule (hydrate=team,probablePitcher) -> flat game dicts."""
    games = []
    for d in body.get("dates") or []:
        for g in d.get("games") or []:
            st = g.get("status") or {}
            t = g.get("teams") or {}

            def side(s: str, t: dict = t) -> dict[str, Any]:
                x = t.get(s) or {}
                team = x.get("team") or {}
                pp = x.get("probablePitcher") or {}
                return {"name": team.get("name"), "short": team.get("teamName") or team.get("name"),
                        "probable": pp.get("fullName")}

            games.append({"pk": g.get("gamePk"), "start": g.get("gameDate"),
                          "state": st.get("abstractGameState"), "detail": st.get("detailedState"),
                          "away": side("away"), "home": side("home")})
    return games


def schedule_items(games: list[dict[str, Any]], prev_probables: dict[tuple, str | None],
                   source: str = "mlb") -> list[Item]:
    """
    Schedule-level facts: a postponement or delay, and a probable pitcher
    that changed since we last looked (a late scratch). `prev_probables`
    is updated in place. These carry no timestamp from MLB, so lag is not
    measurable; published_at is None.
    """
    out = []
    for g in games:
        game = f"{g['away']['name']} at {g['home']['name']}"
        detail = g.get("detail") or ""
        if any(w in detail.lower() for w in _BAD_STATES):
            out.append(make_item(source, f"mlb://game/{g['pk']}/status/{detail}",
                                 f"{game}: {detail}", summary=f"scheduled {g['start']}",
                                 kind="game_status"))
        for s in ("away", "home"):
            key = (g["pk"], s)
            now_p = g[s]["probable"]
            if key in prev_probables and now_p and prev_probables[key] and now_p != prev_probables[key]:
                out.append(make_item(
                    source, f"mlb://game/{g['pk']}/probable/{s}/{now_p}",
                    f"{g[s]['name']} change starting pitcher: {now_p} replaces "
                    f"{prev_probables[key]}", summary=game, kind="probable_change"))
            if now_p:
                prev_probables[key] = now_p
    return out


class MLBLiveSource(Source):
    """
    Schedule every `schedule_interval` s; each live game's trimmed feed at
    most every `min_interval` s. `teams` (names or short names) limits it to
    those teams' games; `match_titles` limits it to games whose team name or
    short name ("Yankees") appears in one of the titles (the fast lane's
    watched events). Neither: every game.
    """

    def __init__(self, teams: set[str] | None = None, min_interval: float = 30.0,
                 schedule_interval: float = 120.0, name: str = "mlb_live",
                 match_titles: list[str] | None = None):
        super().__init__(name, min_interval)
        self.teams = {t.lower() for t in teams} if teams else None
        self.titles = [t.lower() for t in match_titles] if match_titles else None
        self.schedule_interval = schedule_interval
        self._sched_at = -math.inf
        self.games: list[dict[str, Any]] = []
        self._probables: dict[tuple, str | None] = {}

    def _wanted(self, g: dict[str, Any]) -> bool:
        names = {str(g[s].get(k) or "").lower() for s in ("away", "home") for k in ("name", "short")}
        names.discard("")
        if self.teams is not None and not names & self.teams:
            return False
        if self.titles is not None and not any(n in t for n in names for t in self.titles):
            return False
        return True

    async def _fetch(self, client: httpx.AsyncClient) -> list[Item]:
        out: list[Item] = []
        if time.monotonic() - self._sched_at >= self.schedule_interval:
            r = await self._get(client, f"{MLB_API}/v1/schedule",
                                params={"sportId": 1, "hydrate": "team,probablePitcher"})
            self._sched_at = time.monotonic()
            self.games = [g for g in parse_schedule(r.json()) if self._wanted(g)]
            out.extend(schedule_items(self.games, self._probables, self.name))
        for g in self.games:
            if g["state"] != "Live":
                continue
            try:
                r = await self._get(client, f"{MLB_API}/v1.1/game/{g['pk']}/feed/live",
                                    params={"fields": MLB_LIVE_FIELDS})
                out.extend(parse_live_feed(r.json(), self.name))
            except Exception as exc:  # noqa: BLE001 -- one game must not hide the others
                log.debug("mlb game %s failed: %s", g["pk"], exc)
        return out


class MLBTransactionsSource(Source):
    """IL placements, activations, call-ups. Dated to the day only: no measurable lag."""

    def __init__(self, min_interval: float = 300.0, name: str = "mlb_transactions"):
        super().__init__(name, min_interval)

    async def _fetch(self, client: httpx.AsyncClient) -> list[Item]:
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        r = await self._get(client, f"{MLB_API}/v1/transactions",
                            params={"sportId": 1, "startDate": today, "endDate": today})
        return parse_transactions(r.json(), self.name)


def parse_transactions(body: dict[str, Any], source: str = "mlb_transactions") -> list[Item]:
    out = []
    for t in body.get("transactions") or []:
        desc = (t.get("description") or "").strip()
        if not desc or t.get("id") is None:
            continue
        out.append(make_item(source, f"mlb://transaction/{t['id']}", desc,
                             summary=f"{t.get('typeDesc') or ''} {t.get('date') or ''}".strip(),
                             kind="transaction"))
    return out


# --------------------------------------------------------------------------
# weather
# --------------------------------------------------------------------------

NWS_API = "https://api.weather.gov"


def parse_nws_observation(body: dict[str, Any], station: str,
                          source: str = "nws_obs") -> list[Item]:
    p = body.get("properties") or {}
    ts = p.get("timestamp")
    t = (p.get("temperature") or {}).get("value")
    if not ts or t is None:
        return []
    f = t * 9 / 5 + 32
    desc = p.get("textDescription") or ""
    return [make_item(f"{source}:{station}", p.get("@id") or f"nws://{station}/{ts}",
                      f"{station} observed {f:.0f}F ({t:.1f}C){', ' + desc if desc else ''}",
                      summary=f"observation at {ts}", published_at=ts, kind="observation")]


class NWSObservationSource(Source):
    """
    Latest observation per station. For same-day "highest temperature"
    markets the observed temperature so far is the settling agency's own
    number. Observations are hourly (METAR ~:51) plus specials, so polling
    faster than every couple of minutes buys nothing.
    """

    def __init__(self, stations: list[str], min_interval: float = 120.0, name: str = "nws_obs"):
        super().__init__(name, min_interval)
        self.stations = list(stations)

    async def _fetch(self, client: httpx.AsyncClient) -> list[Item]:
        out: list[Item] = []
        for st in self.stations:
            r = await self._get(client, f"{NWS_API}/stations/{st}/observations/latest",
                                ua=NWS_USER_AGENT, accept="application/geo+json")
            out.extend(parse_nws_observation(r.json(), st, self.name))
        return out


def parse_nws_alerts(body: dict[str, Any], source: str = "nws_alerts") -> list[Item]:
    out = []
    for f in body.get("features") or []:
        p = f.get("properties") or {}
        head = p.get("headline") or p.get("event")
        if not head:
            continue
        out.append(make_item(source, p.get("@id") or f.get("id") or head, head,
                             summary=p.get("areaDesc") or "", published_at=p.get("sent"),
                             kind="alert"))
    return out


class NWSAlertsSource(Source):
    def __init__(self, area: str, min_interval: float = 120.0):
        super().__init__(f"nws_alerts:{area}", min_interval)
        self.area = area

    async def _fetch(self, client: httpx.AsyncClient) -> list[Item]:
        r = await self._get(client, f"{NWS_API}/alerts/active", params={"area": self.area},
                            ua=NWS_USER_AGENT, accept="application/geo+json")
        return parse_nws_alerts(r.json(), self.name)


# --------------------------------------------------------------------------
# tech
# --------------------------------------------------------------------------

#: Statuspage incident history as RSS. The JSON API (/api/v2/) is
#: Disallowed by status.claude.com's robots.txt; /history.rss is not. One
#: item per incident (its title does not change as updates are posted).
STATUS_FEEDS = {
    "openai": "https://status.openai.com/history.rss",
    "anthropic": "https://status.claude.com/history.rss",
}

GITHUB_API = "https://api.github.com"


def parse_github_releases(body: list[dict[str, Any]], repo: str,
                          source: str = "github") -> list[Item]:
    out = []
    for r in body or []:
        if r.get("draft") or not r.get("html_url"):
            continue
        name = r.get("name") or r.get("tag_name") or ""
        out.append(make_item(f"{source}:{repo}", r["html_url"], f"{repo} released {name}",
                             summary=(r.get("body") or "")[:600],
                             published_at=r.get("published_at"), kind="release"))
    return out


class GitHubReleasesSource(Source):
    """
    Releases of one repo via the REST API. Not releases.atom: github.com's
    robots.txt Disallows /*.atom. Unauthenticated limit is 60 requests an
    hour per IP, so keep min_interval >= 120 s and the repo count small.
    """

    def __init__(self, repo: str, min_interval: float = 300.0):
        super().__init__(f"github:{repo}", min_interval)
        self.repo = repo

    async def _fetch(self, client: httpx.AsyncClient) -> list[Item]:
        r = await self._get(client, f"{GITHUB_API}/repos/{self.repo}/releases",
                            params={"per_page": 10}, accept="application/vnd.github+json")
        return parse_github_releases(r.json(), self.repo)


# --------------------------------------------------------------------------
# what the fast lane uses
# --------------------------------------------------------------------------

#: Public Bluesky accounts, checked to exist 2026-09-23. Most insiders post
#: first on X; these are the ones that also post here.
BSKY_HANDLES = {
    "mlb": ["rotowiremlb.bsky.social", "underdogmlb.bsky.social", "mlbtraderumors.bsky.social",
            "jonheyman.bsky.social", "bnightengale.bsky.social", "ken-rosenthal.bsky.social"],
    "nfl": ["rapsheet.bsky.social", "adamschefter.bsky.social", "tompelissero.bsky.social",
            "rotowirenfl.bsky.social"],
}

#: Exchange city slug -> NWS station id (see weather.STATIONS).
NWS_STATIONS = ["KNYC", "KLAX", "KSFO", "KMIA", "KMDW"]


def sources_for(tags: tuple[str, ...], event_titles: list[str] | None = None,
                max_queries: int = 8, google_news: bool = False) -> list[Source]:
    """
    Extra sources for a fast-lane run, by exchange tag.

    Google News is OFF unless asked for: news.google.com/robots.txt
    Disallows /rss/ for every user agent (checked 2026-09-23). It is the
    only per-event aggregator that worked, so whether a feed reader polling
    a handful of queries is "crawling" is the operator's call, not code's.
    When on, one query per watched event, capped, each every 2 min.
    """
    out: list[Source] = []
    if google_news:
        for title in list(dict.fromkeys(event_titles or []))[:max_queries]:
            out.append(GoogleNewsSource(event_query(title), label=title))
    tagset = set(tags)
    if tagset & {"mlb", "sports"}:
        # Only the watched games: every item is one Jev call per watched event.
        out.append(MLBLiveSource(match_titles=event_titles or None))
    handles = [h for t in ("mlb", "nfl") if tagset & {t, "sports"} for h in BSKY_HANDLES[t]]
    if handles:
        out.append(BlueskyAuthorSource(handles))
    if "weather" in tagset:
        out.append(NWSObservationSource(NWS_STATIONS))
    if tagset & {"tech", "economics", "business"}:
        out.extend(RSSSource(f"status:{n}", u, min_interval=120, kind="status")
                   for n, u in STATUS_FEEDS.items())
    return out
