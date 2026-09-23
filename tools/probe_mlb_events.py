"""
probe_mlb_events.py -- does Jev read MLB live-feed events correctly?

sources.MLBLiveSource turns game events into headline text ("<team>
starting pitcher removed. Pitching Change: ..."). This checks, against a
fixed brief with pre-priced scenarios, that Jev (a) matches the right
scenario, (b) gives the right direction per market, and (c) stays quiet
on routine events. Live Jev, ~$0.002. Re-run after editing the fast-lane
questions or the event wording in sources.py.
"""

import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import fastlane as fl  # noqa: E402
from swarm import JevTriage  # noqa: E402

BRIEF = {
    "written_at": "2026-09-23T22:00:00Z",
    "teams": {"Kansas City Royals": ["Daniel Lynch IV (SP)", "Bobby Witt Jr. (SS)", "Salvador Perez (C)"],
              "Chicago White Sox": ["Davis Martin (SP)", "Luis Robert Jr. (CF)", "Andrew Vaughn (1B)"]},
    "facts": ["Daniel Lynch IV starts for Kansas City, 9/23", "Davis Martin starts for Chicago, 9/23"],
    "scenarios": [
        {"id": "kc_starter_early_exit", "trigger": "Kansas City's starting pitcher leaves before the 5th inning",
         "affects": {"m0": 0.40, "m1": 0.62, "m2": 0.60}},
        {"id": "cws_starter_early_exit", "trigger": "Chicago's starting pitcher leaves before the 5th inning",
         "affects": {"m0": 0.64, "m1": 0.38, "m2": 0.60}},
        {"id": "witt_injured", "trigger": "Bobby Witt Jr. leaves the game injured",
         "affects": {"m0": 0.47, "m1": 0.55, "m2": 0.50}},
    ],
}
MARKETS = [
    {"slug": "m0", "question": "Who will win Chicago White Sox vs Kansas City Royals?", "outcome": "Kansas City Royals wins"},
    {"slug": "m1", "question": "Will the Chicago White Sox cover 1.5 vs the Kansas City Royals?", "outcome": "Chicago White Sox +1.5"},
    {"slug": "m2", "question": "Will the total be more than 8.5?", "outcome": "Over 8.5 total runs"},
]
# (headline as MLBLiveSource writes it, expected scenario, expected effect per market or None=quiet)
CASES = [
    ("Kansas City Royals starting pitcher removed in the 2nd inning. Pitching Change: Tony Gonsolin replaces Daniel Lynch IV.",
     "kc_starter_early_exit", ("lowers", "raises", "raises")),
    ("Chicago White Sox starting pitcher removed in the 3rd inning. Pitching Change: Jordan Leasure replaces Davis Martin.",
     "cws_starter_early_exit", ("raises", "lowers", "raises")),
    ("Injury delay: Bobby Witt Jr. leaves the game after being hit by a pitch.",
     "witt_injured", ("lowers", None, None)),
    ("Kansas City Royals starting pitcher removed in the 8th inning. Pitching Change: Lucas Erceg replaces Daniel Lynch IV.",
     "none_of_these", (None, None, None)),
    ("Mound visit: Kansas City Royals.", "none_of_these", (None, None, None)),
]


async def main() -> None:
    jev = JevTriage()
    ev = {"title": "Chicago White Sox vs Kansas City Royals", "markets": MARKETS, "brief": BRIEF,
          "teams": BRIEF["teams"]}
    ok_sc = ok_dir = total_dir = 0
    for title, exp_sc, exp_eff in CASES:
        body = await jev._post(fl.build_request({"title": title, "summary": "", "source": "mlb_live"},
                                                ev, jev._model))
        head, per = fl.read_answers(body, len(MARKETS))
        got_sc = head.get("scenario")
        ok_sc += got_sc == exp_sc
        acted = [fl.would_act({**head, **p}, fl.FastLaneThresholds()) for p in per]
        marks = []
        for p, a, e in zip(per, acted, exp_eff, strict=True):
            got = p["effect"] if a else None
            total_dir += 1
            ok_dir += got == e
            marks.append(f"{(got or '.'):<7}{'' if got == e else 'X'}")
        print(f"  sc={str(got_sc)[:22]:<23}{'' if got_sc == exp_sc else '(expected ' + exp_sc + ')':<34}"
              f"{' | '.join(marks)}   {title[:58]}")
    await jev.aclose()
    print(f"\n  scenario matches: {ok_sc}/{len(CASES)}   market directions: {ok_dir}/{total_dir}")
    print("  columns: KC moneyline | CWS +1.5 | Over 8.5.  '.' = stayed quiet")


asyncio.run(main())
