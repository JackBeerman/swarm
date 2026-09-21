"""
probe_fastlane.py -- does Jev get the DIRECTION of a headline right?

Labelled headlines against a fixed event, run with and without the roster
brief. Live Jev, ~$0.002. Re-run after any edit to the fast-lane
questions in questions.py.

    python tools/probe_fastlane.py
"""

import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import fastlane as fl  # noqa: E402  (loads .env before swarm)
from swarm import JevTriage  # noqa: E402

TEAMS = {
    "New York Giants": ["Jaxson Dart (QB)", "Cam Skattebo (RB)", "Malik Nabers (WR)",
                        "Wan'Dale Robinson (WR)", "Brian Burns (LB)", "Dexter Lawrence (DT)",
                        "Abdul Carter (LB)", "Andrew Thomas (OT)"],
    "Los Angeles Rams": ["Matthew Stafford (QB)", "Kyren Williams (RB)", "Puka Nacua (WR)",
                         "Davante Adams (WR)", "Jared Verse (LB)", "Braden Fiske (DT)",
                         "Tyler Higbee (TE)"],
}
MARKETS = [
    {"question": "Will the New York Giants cover 3.5 vs the Los Angeles Rams?",
     "outcome": "Los Angeles Rams wins by over 3.5 points"},
    {"question": "Will the total in New York Giants vs. Los Angeles Rams be more than 27.5 for Los Angeles Rams?",
     "outcome": "Los Angeles Rams over 27.5 total points"},
    {"question": "Will the total in NY Giants vs. LA Rams be more than 22.5 for New York Giants?",
     "outcome": "New York Giants over 22.5 total points"},
]
# (headline, expected effect per market: Rams cover / Rams team total / Giants team total)
CASES = [
    # No team named: only the brief can say whose player this is.
    ("Giants vs. Rams live updates, inactives: Puka Nacua ruled out", ("lowers", "lowers", None)),
    ("Giants vs. Rams inactives: Malik Nabers will not play", ("raises", None, "lowers")),
    ("Rams WR Puka Nacua ruled out for Monday night with ankle injury", ("lowers", "lowers", None)),
    ("Matthew Stafford (back) will not play tonight; backup to start for Rams", ("lowers", "lowers", None)),
    ("Giants QB Jaxson Dart ruled out with concussion, veteran backup to start", ("raises", None, "lowers")),
    ("Giants DT Dexter Lawrence inactive tonight against Rams", ("raises", "raises", None)),
    ("Rams take 14-0 lead on second Stafford touchdown pass", ("raises", "raises", None)),
    ("Giants vs. Rams odds, predictions, best bets for Monday Night Football", (None, None, None)),
    ("Cardinals CB Will Johnson has a neck injury that could end his season", (None, None, None)),
]


async def main() -> None:
    jev = JevTriage()
    for label, teams in (("WITHOUT brief", {}), ("WITH brief", TEAMS)):
        right = wrong = 0
        print(f"\n=== {label}")
        for title, expected in CASES:
            h = {"title": title, "summary": "", "source": "probe"}
            ev = {"title": "NY Giants vs LA Rams", "markets": MARKETS, "teams": teams}
            body = await jev._post(fl.build_request(h, ev, jev._model))
            head, per = fl.read_answers(body, len(MARKETS))
            acted = [fl.would_act({**head, **p}, fl.FastLaneThresholds()) for p in per]
            marks = []
            for p, a, exp in zip(per, acted, expected, strict=True):
                got = p["effect"] if a else None
                if exp is None and got is None:
                    marks.append("  .   ")
                elif got == exp:
                    marks.append(f"{got:<6}")
                    right += 1
                elif exp is None or got is None:
                    # expected nothing but acted, or expected a call and stayed out
                    marks.append(f"{(got or 'none'):<5}?")
                    wrong += exp is None        # acting when it should not is the costly one
                else:
                    marks.append(f"{got:<5}X")
                    wrong += 1
            print(f"  fact={head['reports_new_fact']:.2f} ev={head['concerns_event']:.2f}  "
                  f"{' | '.join(marks)}   {title[:62]}")
        print(f"  correct directional calls: {right}   wrong or unwarranted: {wrong}")
    await jev.aclose()
    print("\n  columns: Rams cover 3.5 | Rams team total | Giants team total")
    print("  X = wrong direction, ? = acted when it should not / stayed out, . = correctly quiet")


asyncio.run(main())
