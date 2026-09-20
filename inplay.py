"""
inplay.py -- live price feed for in-play markets. READ-ONLY. Places nothing.

    python inplay.py --game nfl-phi-ten-2026-09-20 --minutes 10
    python inplay.py --markets slug1,slug2 --minutes 5

This is the first half of the in-play pipeline: the feed. The pre-game
pipeline cannot be sped up into an in-play one -- Tier 2/3 take seconds
and ~$0.10 per market, and REST gives a ten-minute-old book across ~1,000
in-play markets. In-play is a different, cheaper shape:

    websocket ticks  ->  PriceTracker (real deltas, not REST snapshots)
    Jev ONCE per market, cached (structure does not change mid-game)
    arithmetic per tick, in code: live price vs live game state
    sizing unchanged: size_from_signal() under the portfolio lock

Verified live (2026-09-20, 1pm slate): the authenticated markets stream
delivers a `marketDataLite` snapshot per slug on subscribe, then a frame
on each change. The payload carries the same field set as the REST
`marketData` envelope -- bestBid/bestAsk/lastTradePx/sharesTraded/
openInterest/bidShares/askShares/state -- not the three fields the SDK
stub declares, so it parses through normalize_bbo({"marketData": ...})
unchanged. The stream returns HTTP 401 without credentials.

What this file does NOT yet do: read game state, ask Jev, or size. Those
come after the game-state feed is verified fresh enough to act on.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from config import load_dotenv_if_present

load_dotenv_if_present()

from polymarket_us import AsyncPolymarketUS  # noqa: E402
from polymarket_us.websocket.markets import MarketsWebSocket  # noqa: E402

from adapters import (  # noqa: E402
    PriceTracker,
    game_state,
    iter_event_markets,
    listed_mid,
    main_lines_first,
    normalize_bbo,
    normalize_market,
)
from questions import GateThresholds, StructuralLimits  # noqa: E402
from swarm import JevTriage  # noqa: E402

log = logging.getLogger("inplay")


def _mid(bbo: dict[str, Any]) -> float | None:
    b, a = bbo.get("bid"), bbo.get("ask")
    return None if b is None or a is None else round((b + a) / 2.0, 4)


#: A tick is only a price if the book is real. At kickoff the feed showed
#: "hsq-2q" with bid 0.01 / ask 0.97 -- a mid of 0.49 that means nothing.
#: Same ceiling the pre-game filter and the sizer use.
MAX_TICK_SPREAD = 0.04

#: A "slip": the mid moving at least this much within this many seconds
#: on a market Jev classified as tractable. Logged, not traded.
SLIP_MOVE = 0.05
SLIP_WINDOW_S = 30.0


class LiveFeed:
    """
    Subscribes a set of market slugs and keeps PriceTracker current.

    Frames arrive on the websocket's own task; `_on_lite` is synchronous
    and does no I/O, which is the only safe thing to do inside an event
    emitter. Jev is asked ONCE per market, from `classify()`, on the main
    task -- market structure does not change mid-game, so there is no
    reason to pay for it per tick. Everything per tick is arithmetic.
    """

    def __init__(self, key_id: str, secret_key: str,
                 max_spread: float = MAX_TICK_SPREAD) -> None:
        self._ws = MarketsWebSocket(key_id=key_id, secret_key=secret_key)
        self.tracker = PriceTracker(window_seconds=4 * 3600, max_points=2000)
        self.last: dict[str, dict[str, Any]] = {}
        self.verdicts: dict[str, Any] = {}      # slug -> TriageVerdict, once
        self.max_spread = max_spread
        self.frames = 0
        self.changes = 0
        self.wide = 0
        self.slips = 0
        self.errors = 0
        self._ws.on("market_data_lite", self._on_lite)
        self._ws.on("error", self._on_error)

    def _on_error(self, err: Any) -> None:
        self.errors += 1
        log.warning("ws error: %s", str(err)[:160])

    def _on_lite(self, msg: dict[str, Any]) -> None:
        payload = msg.get("marketDataLite") or {}
        slug = payload.get("marketSlug")
        if not slug:
            return
        # Same envelope shape as REST; same parser.
        bbo = normalize_bbo({"marketData": payload})
        self.frames += 1
        prev = self.last.get(slug)
        self.last[slug] = bbo
        b, a = bbo.get("bid"), bbo.get("ask")
        if b is None or a is None or (a - b) > self.max_spread:
            # Record nothing to the tracker: a delta computed against a
            # 0.96-wide book is noise, and PriceTracker would report it
            # as a move.
            self.wide += 1
            return
        self.tracker.observe_bbo(slug, {"marketData": payload})
        new_mid, old_mid = _mid(bbo), (_mid(prev) if prev else None)
        if old_mid is None or new_mid != old_mid:
            self.changes += 1
            v = self.verdicts.get(slug)
            tag = ("" if v is None else
                   f"  [{'GATE' if v.escalate else 'no'} {v.gate_score:.2f} "
                   f"{v.sports_market_type}]")
            log.info("%-46s %s -> %s  bid=%s ask=%s%s",
                     slug[:46], old_mid, new_mid, b, a, tag)
            # Slip detection: a real move, fast, on a market the gate
            # would have escalated pre-game. Arithmetic on the tracker.
            if v is not None and v.escalate:
                d = self.tracker.change(slug, SLIP_WINDOW_S)
                if d is not None and abs(d) >= SLIP_MOVE:
                    self.slips += 1
                    log.warning("SLIP %-42s %+.3f in %.0fs  now %.3f/%.3f",
                                slug[:42], d, SLIP_WINDOW_S, b, a)

    async def classify(self, jev: JevTriage, pairs: list[tuple[dict[str, Any], dict[str, Any]]],
                       limits: StructuralLimits) -> None:
        """
        Jev once per market. Uses the quote the feed already has (the
        subscribe snapshot), so this costs one Tier 1 call per market
        (~$0.00007) and no extra REST. Structural rejects are recorded
        too, so a wide book at classification time is visible later.
        """
        gate = GateThresholds()
        for m, ev in pairs:
            slug = m.get("slug")
            if not slug or slug in self.verdicts:
                continue
            bbo = self.last.get(slug)
            if not bbo:
                continue
            nm = normalize_market(m, ev)
            try:
                v = await jev.evaluate(nm, bbo, gate, self.tracker, limits)
            except Exception as exc:  # noqa: BLE001
                log.warning("classify failed %s: %s", slug, str(exc)[:100])
                continue
            self.verdicts[slug] = v
        n_gate = sum(1 for v in self.verdicts.values() if v.escalate)
        n_struct = sum(1 for v in self.verdicts.values() if v.structural_reject)
        log.info("classified %d markets once: %d pass the gate, %d structural, "
                 "%d vetoed", len(self.verdicts), n_gate, n_struct,
                 len(self.verdicts) - n_gate - n_struct)

    async def start(self, slugs: list[str]) -> None:
        await self._ws.connect()
        await self._ws.subscribe_market_data_lite("inplay-lite", slugs)
        log.info("subscribed %d markets", len(slugs))

    async def stop(self) -> None:
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def report(self) -> str:
        return (f"feed: {self.frames} frames, {self.changes} mid changes, "
                f"{self.wide} wide-book ticks ignored, {self.slips} slips, "
                f"{self.errors} errors, {len(self.last)} markets quoted, "
                f"{len(self.verdicts)} classified")


# --------------------------------------------------------------------------
# Game state: REST, polled. Verified live 2026-09-20: `live`, `period`,
# `score` and `elapsed` are on the event and move between polls ~20s
# apart (elapsed 15:00 -> 14:45 -> 14:42), each call 1.6-3.3s. Enough for
# a first version; no external feed needed.
# --------------------------------------------------------------------------

#: NFL regulation. Other sports get their own table when they get a
#: fair-value model; until then GameState reports seconds_left=None.
NFL_PERIODS = {"Q1": 0, "Q2": 1, "Q3": 2, "Q4": 3}
NFL_PERIOD_S = 15 * 60

#: Standard deviation of an NFL full-game total around its line, in
#: points. Widely cited at ~13-14; 13.5 is the working value. This one
#: number is what separates a fair value the book would laugh at from
#: one it would argue with -- see total_fair_value().
NFL_TOTAL_SD = 13.5


def _parse_score(s: str | None) -> tuple[int, int] | None:
    """'21-17' -> (21, 17). Order is the exchange's (typically away-home)."""
    if not s or "-" not in s:
        return None
    try:
        a, b = s.split("-", 1)
        return int(a), int(b)
    except ValueError:
        return None


def _parse_clock(s: str | None) -> int | None:
    """'14:45' -> 885 seconds remaining in the period."""
    if not s or ":" not in s:
        return None
    try:
        m, sec = s.split(":", 1)
        return int(m) * 60 + int(sec)
    except ValueError:
        return None


class GameState:
    """A snapshot of one game, with the arithmetic the tick loop needs."""

    def __init__(self, ev: dict[str, Any]) -> None:
        self.slug = ev.get("slug")
        self.period = ev.get("period")
        self.score = _parse_score(ev.get("score"))
        self.clock_s = _parse_clock(ev.get("elapsed"))
        self.live = bool(ev.get("live"))
        self.state = game_state({"period": self.period})
        self.at = time.time()

    @property
    def points(self) -> int | None:
        return None if self.score is None else self.score[0] + self.score[1]

    @property
    def seconds_left(self) -> int | None:
        """
        Regulation seconds remaining. None in overtime or if unparsed.

        Breaks are real periods on the wire -- "End Q1" was the first
        value the live loop saw -- and the clock is meaningless during
        them, so they map to the START of the next quarter rather than
        returning None and going quiet for the whole break.
        """
        if self.state == "finished":
            return 0
        p = (self.period or "").strip()
        breaks = {"End Q1": 3, "End Q2": 2, "Halftime": 2, "Half": 2,
                  "HT": 2, "End Q3": 1}
        if p in breaks:
            return breaks[p] * NFL_PERIOD_S
        idx = NFL_PERIODS.get(p)
        if idx is None or self.clock_s is None:
            return None          # OT, or an unrecognised period string
        return (3 - idx) * NFL_PERIOD_S + self.clock_s

    @property
    def fraction_played(self) -> float | None:
        s = self.seconds_left
        return None if s is None else 1.0 - s / (4 * NFL_PERIOD_S)

    def __str__(self) -> str:
        return (f"{self.period} {self.clock_s if self.clock_s is not None else '?'}s "
                f"score={self.score} pts={self.points} left={self.seconds_left}s")


async def poll_game(pm: Any, game_slug: str) -> GameState | None:
    """One REST read of the event. The slug filter is honoured."""
    page = await pm.events.list({"limit": 5, "closed": False, "slug": [game_slug]})
    for ev in page.get("events", []) or []:
        if ev.get("slug") == game_slug:
            return GameState(ev)
    return None


def total_fair_value(line: float, gs: GameState, pregame_total: float | None) -> float | None:
    """
    P(final total > line), from points so far and time left. Arithmetic.

    Expected remaining points = (pregame total or league-average pace)
    scaled by the fraction of the game left; remaining points are treated
    as Poisson-ish with that mean, which is crude but honest about being
    crude. This is a comparison the model must not be asked to make: it
    is a calculation, and calculations live in code (CLAUDE.md).

    Returns None when the game clock is unavailable.

    Dispersion matters more than the mean here. The first version used a
    Poisson on remaining points and, live at Q2 with 7 scored, put the
    40.5 total at 0.271 while the book sat at 0.50/0.51. The book is the
    sharp reference; a 24-point disagreement is the model's fault. NFL
    scoring is bursty (a touchdown is 7 at once), so a full-game total
    has SD ~13.5 -- roughly 2.5x what a Poisson at that mean allows --
    and every Poisson fair value was too extreme whenever the chase was
    off pace. Normal, SD scaled by sqrt(time left). Still crude; no
    longer systematically confident.
    """
    import math
    if gs.points is None or gs.seconds_left is None:
        return None
    frac_left = gs.seconds_left / (4 * NFL_PERIOD_S)
    base = pregame_total if pregame_total else 44.0     # NFL-ish average
    if line - gs.points < 0:
        return 1.0                                        # already over
    if frac_left <= 0:
        return 0.0                                        # over never came
    mean = gs.points + base * frac_left
    sd = max(NFL_TOTAL_SD * math.sqrt(frac_left), 0.5)
    z = (line - mean) / sd
    p_over = 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))
    return max(0.0, min(1.0, p_over))


def total_line_from_slug(slug: str) -> float | None:
    """'tsc-nfl-phi-ten-2026-09-20-total-39pt5' -> 39.5. Only game totals."""
    if "-total-" not in slug or "-1h-" in slug or "-2h-" in slug \
            or "-1q-" in slug or "-2q-" in slug or "-3q-" in slug or "-4q-" in slug:
        return None
    tail = slug.rsplit("-total-", 1)[-1]
    try:
        return float(tail.replace("pt", "."))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Calibration log. Every fair value is written next to the book it
# disagreed with, so the model can be scored -- against the market now,
# against settlement later -- before anyone trusts an "edge" from it.
# Same discipline as shadow.db: a number you never scored is a guess.
# --------------------------------------------------------------------------

INPLAY_DB = os.getenv("INPLAY_DB", "inplay.db")

_FV_SCHEMA = """
CREATE TABLE IF NOT EXISTS fair_values (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    game          TEXT NOT NULL,
    market_slug   TEXT NOT NULL,
    line          REAL NOT NULL,
    period        TEXT,
    seconds_left  INTEGER,
    points        INTEGER,
    anchor        REAL,
    fv            REAL NOT NULL,
    bid           REAL,
    ask           REAL,
    gate_score    REAL,
    model         TEXT NOT NULL,
    -- filled by a backfill once the market settles, like shadow.db
    resolved_outcome TEXT
);
CREATE INDEX IF NOT EXISTS idx_fv_slug ON fair_values(market_slug);
"""

FV_MODEL = "normal-sd13.5-sqrt"


def connect_db(path: str = INPLAY_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_FV_SCHEMA)
    return conn


def record_fv(conn: sqlite3.Connection, game: str, slug: str, line: float,
              gs: GameState, anchor: float | None, fv: float,
              bbo: dict[str, Any], gate_score: float | None) -> None:
    conn.execute(
        """INSERT INTO fair_values (at, game, market_slug, line, period,
               seconds_left, points, anchor, fv, bid, ask, gate_score, model)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(), game, slug, line, gs.period,
         gs.seconds_left, gs.points, anchor, fv, bbo.get("bid"), bbo.get("ask"),
         gate_score, FV_MODEL),
    )
    conn.commit()


#: Game-total markets always get these many slots on the watchlist. They
#: are the only market type the fair-value model scores, and on three of
#: four live games the 40-nearest-0.50 set contained none of them.
TOTAL_SLOTS = 4


def select_watchlist(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    max_markets: int,
    total_slots: int = TOTAL_SLOTS,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """
    The game totals nearest 0.50 first, then everything else nearest 0.50.
    Pure, so it is testable without a socket.
    """
    ordered = main_lines_first(pairs)
    totals = [pe for pe in ordered if total_line_from_slug(pe[0].get("slug") or "")]
    picked = totals[:total_slots]
    seen = {pe[0].get("slug") for pe in picked}
    for pe in ordered:
        if len(picked) >= max_markets:
            break
        if pe[0].get("slug") not in seen:
            picked.append(pe)
            seen.add(pe[0].get("slug"))
    return picked[:max_markets]


async def markets_for_game(pm: Any, game_slug: str, max_markets: int) -> list[str]:
    """
    The game's open markets, via the events endpoint the rest of the
    codebase uses. Skips finished games; warns if the game has not
    started, since a pre-game feed is just a quiet feed.
    """
    now = datetime.now(timezone.utc)
    page = await pm.events.list({
        "limit": 60, "closed": False, "slug": [game_slug],
        "startTimeMin": (now - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "startTimeMax": (now + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    evs = [e for e in page.get("events", []) if e.get("slug") == game_slug]
    if not evs:
        # slug filter may not be honoured; fall back to scanning the window
        evs = [e for e in page.get("events", []) if e.get("slug") == game_slug]
    if not evs:
        raise SystemExit(f"game {game_slug} not found in the +/-8h window")
    ev = evs[0]
    pairs = iter_event_markets({"events": [ev]})
    nm0 = normalize_market(pairs[0][0], ev) if pairs else {}
    gs = game_state(nm0)
    log.info("game %s: period=%s score=%s state=%s markets=%d",
             game_slug, ev.get("period"), ev.get("score"), gs, len(pairs))
    if gs == "finished":
        raise SystemExit("game is finished; nothing to watch")
    if gs == "not_started":
        log.warning("game has not started; feed will be quiet until kickoff")
    # Totals first, then main lines and contested props nearest 0.50. The
    # first live feed took the head of the list and got thirty 0.985
    # alt-lines that never ticked; the next one got forty markets nearest
    # 0.50 and, on three of four games, not one game total among them.
    chosen = select_watchlist(pairs, max_markets)
    n_tot = sum(1 for m, _ in chosen if total_line_from_slug(m.get("slug") or ""))
    log.info("prescreen kept %d of %d; subscribing %d (%d game totals)",
             len(main_lines_first(pairs)), len(pairs), len(chosen), n_tot)
    return [m["slug"] for m, _ in chosen], chosen


async def backfill_fv(pause: float = 2.0, limit: int = 500) -> None:
    """
    Fill resolved_outcome on fair_values once markets settle. Mirrors
    shadow.backfill: NotFoundError is "not yet" and final for the pass;
    RateLimitError is Cloudflare and retried with backoff; a non-binary
    settlement (a push) is skipped rather than mislabelled.
    """
    conn = connect_db()
    slugs = [r["market_slug"] for r in conn.execute(
        """SELECT DISTINCT market_slug FROM fair_values
           WHERE resolved_outcome IS NULL ORDER BY id DESC LIMIT ?""", (limit,))]
    log.info("%d in-play markets awaiting resolution", len(slugs))
    filled = pending = failed = 0
    async with AsyncPolymarketUS() as pm:
        for slug in slugs:
            res, terminal, delay = None, False, pause
            for attempt in range(4):
                try:
                    res = await pm.markets.settlement(slug)
                    break
                except Exception as exc:  # noqa: BLE001
                    if type(exc).__name__ == "NotFoundError":
                        terminal = True
                        break
                    if attempt == 3:
                        failed += 1
                        break
                    await asyncio.sleep(delay)
                    delay *= 2
            if terminal or res is None:
                pending += 1
                await asyncio.sleep(pause)
                continue
            try:
                sval = float(res.get("settlement"))
            except (TypeError, ValueError):
                sval = None
            if sval not in (0.0, 1.0):
                pending += 1
                log.warning("non-binary settlement %r for %s; skipped",
                            res.get("settlement"), slug)
            else:
                conn.execute(
                    """UPDATE fair_values SET resolved_outcome = ?
                       WHERE market_slug = ? AND resolved_outcome IS NULL""",
                    (str(int(sval)), slug))
                conn.commit()
                filled += 1
            await asyncio.sleep(pause)
    conn.close()
    log.info("backfill: %d resolved, %d still open, %d errors",
             filled, pending, failed)


def score_fv(db: str = INPLAY_DB) -> None:
    """
    Was the model ever right when the book was wrong?

    Two Brier scores side by side -- model fair value vs outcome, and the
    book's own mid vs outcome -- over the same resolved rows. If the model
    does not beat the book, it has no edge, whatever it reports live. The
    third number is the one that would justify a trade: rows where the
    model sat on the correct side of 0.50 and the book did not.
    """
    import statistics
    with connect_db(db) as conn:
        rows = conn.execute(
            """SELECT fv, bid, ask, resolved_outcome, seconds_left
               FROM fair_values
               WHERE resolved_outcome IS NOT NULL
                 AND bid IS NOT NULL AND ask IS NOT NULL"""
        ).fetchall()
    print("=" * 62)
    print(f"  {len(rows)} resolved fair-value rows")
    print("=" * 62)
    if not rows:
        print("\n  nothing resolved yet. Run --backfill after markets settle.")
        return
    bm, bb, model_right_book_wrong, book_right_model_wrong = [], [], 0, 0
    for r in rows:
        y = float(r["resolved_outcome"])
        mid = (r["bid"] + r["ask"]) / 2.0
        bm.append((r["fv"] - y) ** 2)
        bb.append((mid - y) ** 2)
        m_side, b_side = r["fv"] >= 0.5, mid >= 0.5
        truth = y >= 0.5
        if m_side == truth and b_side != truth:
            model_right_book_wrong += 1
        if b_side == truth and m_side != truth:
            book_right_model_wrong += 1
    print(f"\n  brier  model {statistics.mean(bm):.4f}   book {statistics.mean(bb):.4f}"
          f"   ({'model better' if statistics.mean(bm) < statistics.mean(bb) else 'book better'})")
    print(f"  model right / book wrong : {model_right_book_wrong}")
    print(f"  book right / model wrong : {book_right_model_wrong}")

    # The headline flatters whoever guessed the direction: late-game rows
    # are near-certain for both sides, and a biased model looks brilliant
    # on the afternoons its bias points the right way. Score the hard rows
    # separately -- first half, outcome genuinely open.
    early = [r for r in rows if (r["seconds_left"] or 0) >= 1800]
    if early:
        em = statistics.mean((r["fv"] - float(r["resolved_outcome"])) ** 2 for r in early)
        eb = statistics.mean(((r["bid"] + r["ask"]) / 2.0 - float(r["resolved_outcome"])) ** 2
                             for r in early)
        print(f"\n  first-half rows only ({len(early)}): "
              f"model {em:.4f}   book {eb:.4f}   "
              f"({'model better' if em < eb else 'book better'})")
    print("\n  rows are per poll, not per market -- the same market appears at\n"
          "  many clocks. That is intended: it scores the model at every\n"
          "  point it would have been asked to act. But the effective sample\n"
          "  is the number of MARKETS, and a handful of games is a coin flip.")


async def run(args: argparse.Namespace) -> int:
    key_id = os.getenv("POLYMARKET_KEY_ID", "")
    secret = os.getenv("POLYMARKET_SECRET_KEY", "")
    if not key_id or not secret:
        log.error("the markets websocket needs POLYMARKET_KEY_ID / "
                  "POLYMARKET_SECRET_KEY (it returns 401 unauthenticated)")
        return 3

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pm = AsyncPolymarketUS()           # kept open for game-state polling
    await pm.__aenter__()
    if args.markets:
        slugs = [s.strip() for s in args.markets.split(",") if s.strip()]
        if not args.no_jev:
            log.warning("--markets gives no event context; skipping Jev "
                        "classification (use --game for that)")
    else:
        slugs, pairs = await markets_for_game(pm, args.game, args.max_markets)

    # The pre-game main total: the game-total line listed nearest 0.50.
    # It anchors the pace estimate in total_fair_value().
    pregame_total: float | None = None
    best = 1.0
    for m, _ in pairs:
        line = total_line_from_slug(m.get("slug") or "")
        mid = listed_mid(m)
        if line is not None and mid is not None and abs(mid - 0.5) < best:
            best, pregame_total = abs(mid - 0.5), line
    if pregame_total is not None:
        log.info("pre-game main total ~ %.1f", pregame_total)

    feed = LiveFeed(key_id, secret)
    db = connect_db()
    await feed.start(slugs)
    jev = None if (args.no_jev or not pairs) else JevTriage()
    t_end = time.time() + args.minutes * 60
    try:
        if jev is not None:
            # The subscribe snapshot lands within ~100ms; give it a moment
            # so every market has a quote before it is classified.
            await asyncio.sleep(2.0)
            # allow_in_play: the pre-game filter rejects a live game by
            # design. In-play classification is the point here. The
            # research floor falls back to the settlement clock (event is
            # in the past), which is weeks out and passes.
            await feed.classify(jev, pairs, StructuralLimits(allow_in_play=True))
        # Game-state poll joined to the live ticks. ~15s is well inside
        # the ~20s at which REST elapsed was seen to move, and one call.
        last_poll = 0.0
        gs: GameState | None = None
        while time.time() < t_end:
            if args.game and time.time() - last_poll >= args.poll:
                last_poll = time.time()
                try:
                    gs = await poll_game(pm, args.game)
                except Exception as exc:  # noqa: BLE001
                    log.warning("game poll failed: %s", str(exc)[:100])
                if gs is not None:
                    log.info("GAME %s", gs)
                    if gs.state == "finished":
                        log.info("game over; stopping")
                        break
                    # Fair value vs market, for the one market type we
                    # can compute in code today: the game total.
                    for slug, bbo in feed.last.items():
                        line = total_line_from_slug(slug)
                        if line is None:
                            continue
                        fv = total_fair_value(line, gs, pregame_total)
                        mid = _mid(bbo)
                        b, a = bbo.get("bid"), bbo.get("ask")
                        if fv is None or mid is None or b is None or a is None \
                                or (a - b) > feed.max_spread:
                            continue
                        v = feed.verdicts.get(slug)
                        edge = fv - a          # buy-YES edge at the ask
                        flag = "  <-- EDGE" if edge >= 0.10 else ""
                        log.info("  total %5.1f  fv=%.3f  mkt=%.3f/%.3f  "
                                 "edge(yes@ask)=%+.3f%s%s",
                                 line, fv, b, a, edge,
                                 f"  [gate {v.gate_score:.2f}]" if v else "",
                                 flag)
                        record_fv(db, args.game, slug, line, gs, pregame_total,
                                  fv, bbo, v.gate_score if v else None)
            await asyncio.sleep(1.0)
    finally:
        await feed.stop()
        if jev is not None:
            await jev.aclose()
        await pm.__aexit__(None, None, None)
        db.close()
        log.info("%s", feed.report())
        # what the tracker now knows: real deltas per market
        for slug in sorted(feed.last):
            d = feed.tracker.change(slug, 3600)
            cov = feed.tracker.coverage(slug)
            if d is not None:
                log.info("  %-46s delta=%+.4f over %.0fs", slug[:46], d, cov)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", default=None, help="event slug, e.g. nfl-phi-ten-2026-09-20")
    ap.add_argument("--markets", default=None, help="comma-separated market slugs (overrides --game)")
    ap.add_argument("--max-markets", type=int, default=40,
                    help="cap per game; a game carries ~800 markets")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--no-jev", action="store_true",
                    help="feed only; skip the once-per-market classification")
    ap.add_argument("--poll", type=float, default=15.0,
                    help="seconds between game-state polls (REST elapsed was "
                         "seen to move at ~20s)")
    ap.add_argument("--backfill", action="store_true",
                    help="fill resolved_outcome on fair_values from settlement")
    ap.add_argument("--score-fv", action="store_true",
                    help="model vs book Brier over resolved fair-value rows")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    if args.backfill:
        asyncio.run(backfill_fv())
        return 0
    if args.score_fv:
        score_fv()
        return 0
    if not args.game and not args.markets:
        ap.error("--game or --markets is required")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
