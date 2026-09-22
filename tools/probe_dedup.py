"""
probe_dedup.py -- does Jev recognise a repeat of a fact already acted on?

Last night's real headline sequence (2026-09-21, Giants-Rams). Live Jev,
~$0.001. Re-run after any edit to `repeats_acted_fact` in questions.py.
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

FIRST = "Jaxson Dart goes down holding his knee in pain, walks to locker room"
# (headline, expected repeat?)
SEQUENCE = [
    ("Giants vs. Rams score, live updates: Jaxson Dart suffers apparent knee injury", True),
    ("Jaxson Dart injures leg on Giants' first possession", True),
    ("Jaxson Dart injury: NY Giants QB appears to suffer left knee injury vs. Rams", True),
    ("Giants QB Jaxson Dart questionable to return vs. Rams with knee injury", True),
    ("Jaxson Dart injury update as Giants QB leaves game vs Rams", True),
    ("Jaxson Dart returns to the game in the third quarter", False),
    ("Malik Nabers injury update as Giants WR injures shoulder in MNF game", False),
    ("Rams honor helicopter crash victims before MNF game vs. Giants", False),
    ("Jaxson Dart ruled OUT for the rest of the game", False),   # material change: judgment call
]
MARKETS = [{"question": "Will the Giants cover 1.5 vs the Rams?", "outcome": "Giants cover"}]


async def main() -> None:
    jev = JevTriage()
    ev = {"title": "NY Giants vs LA Rams", "markets": MARKETS, "teams": {},
          "already_acted": [FIRST]}
    print(f"already_acted: {FIRST!r}\n")
    wrong = 0
    for title, expected in SEQUENCE:
        body = await jev._post(fl.build_request(
            {"title": title, "summary": "", "source": "probe"}, ev, jev._model))
        head, _ = fl.read_answers(body, 1)
        is_rep = head["repeat"] > fl.FastLaneThresholds().max_repeat
        ok = is_rep == expected
        wrong += not ok
        print(f"  rep={head['repeat']:.2f} {'REPEAT' if is_rep else 'new   '}"
              f"{'' if ok else '  <-- expected ' + ('repeat' if expected else 'new')}  {title[:66]}")
    await jev.aclose()
    print(f"\n  disagreements: {wrong}/{len(SEQUENCE)} (the 'ruled OUT' case is a judgment call)")


asyncio.run(main())
