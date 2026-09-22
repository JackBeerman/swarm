"""
probe_concerns.py -- does `concerns_event` fire for non-sports subjects?

Live Jev, ~$0.001. Re-run after any edit to the fast-lane headline
questions. The Binance case is expected to miss on the event and hit on
the political question; either way it is not acted on.
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

CASES = [
    ("How high will Bitcoin get this year?", {"Bitcoin": ["BTC spot price", "spot Bitcoin ETFs"]},
     "Bitcoin falls 6% as spot ETF outflows hit a record", True),
    ("How high will Bitcoin get this year?", {"Bitcoin": ["BTC spot price"]},
     "Binance probed by U.S. federal prosecutors for sanctions violations", True),   # political too; that is the other question's job
    ("How high will Bitcoin get this year?", {"Bitcoin": ["BTC spot price"]},
     "Solana validator outage resolved after four hours", False),
    ("Gemini 3.5 Pro Released by September 30?", {"Google": ["Gemini 3.5 Pro", "DeepMind"]},
     "Google says Gemini 3.5 enters public preview for developers", True),
    ("Gemini 3.5 Pro Released by September 30?", {"Google": ["Gemini 3.5 Pro"]},
     "AstroForge is putting AI in command of its next spacecraft", False),
    ("Spotify Top Global Artist 2026", {"Bad Bunny": [], "Taylor Swift": [], "Drake": []},
     "Bad Bunny announces surprise album for October", True),
    ("NY Giants vs LA Rams", {"New York Giants": ["Jaxson Dart (QB)"]},
     "Cardinals CB Will Johnson has a neck injury", False),
]


async def main():
    jev = JevTriage()
    wrong = 0
    for event, teams, title, expected in CASES:
        ev = {"title": event, "teams": teams, "markets": [{"question": event, "outcome": "Yes"}]}
        body = await jev._post(fl.build_request({"title": title, "summary": "", "source": "p"}, ev, jev._model))
        head, _ = fl.read_answers(body, 1)
        got = head["concerns_event"] >= 0.6
        wrong += got != expected
        print(f"  ev={head['concerns_event']:.2f} pol={head['political']:.2f} {'ok ' if got == expected else 'XX '} {event[:34]:<35} <- {title[:52]}")
    await jev.aclose()
    print(f"  wrong: {wrong}/{len(CASES)}")

asyncio.run(main())
