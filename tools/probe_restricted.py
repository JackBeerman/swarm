import asyncio, os, sys
import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
from config import load_dotenv_if_present
load_dotenv_if_present()
import swarm as sw
from questions import RESTRICTED_QUESTIONS

CASES = [
    # (expect_veto, question, outcome, event, tags)
    (1, "Will the UK call a snap election before July?", "Yes", "UK Politics", ["world"]),
    (1, "French Presidential Election Winner", "Marine Le Pen", "France 2027", ["world"]),
    (1, "Who leaves first?", "Secretary of Defense", "Cabinet departures", ["culture"]),
    (1, "TIME Person of the Year 2026", "Donald Trump", "TIME Person of the Year", ["culture"]),
    (1, "Will the Senate pass the crypto market structure bill?", "Yes", "Crypto regulation", ["crypto"]),
    (1, "Will the US impose new tariffs on China by December?", "Yes", "Trade", ["economics"]),
    (1, "What will the President say during the State of the Union?", "Says 'inflation'", "SOTU", ["culture"]),
    (1, "Will Canada's Prime Minister resign by March?", "Yes", "Canada", ["world"]),
    (1, "Will the Fed cut rates in October?", "Yes", "FOMC October Decision", ["economics"]),
    (0, "Total Points: Over 47.5", "Over", "NYG vs LAR", ["nfl", "sports"]),
    (0, "Team Total Sacks: Over 2.5", "Over", "Washington Commanders vs New England Patriots", ["nfl", "sports"]),
    (0, "NFL Defensive Player of the Year", "Myles Garrett", "NFL Awards", ["nfl", "sports"]),
    (0, "Will Bitcoin close above $120k on Dec 31?", "Yes", "Bitcoin price", ["crypto"]),
    (0, "Best Picture winner", "Film X", "Academy Awards", ["culture"]),
    (0, "Will Tesla deliver 500k vehicles this quarter?", "Yes", "Tesla Q3", ["business"]),
    (0, "Spotify Top Global Artist 2026", "Taylor Swift", "Spotify Wrapped", ["culture"]),
    (0, "Will a hurricane make landfall in Florida in October?", "Yes", "Hurricane season", ["science"]),
]


async def main():
    jev = sw.JevTriage()
    bad = 0
    print(f"{'exp':>3} {'fed':>5} {'def':>5} {'usel':>5} {'POL':>5} {'max':>5}  verdict  market")
    for exp, q, outcome, event, tags in CASES:
        body = await jev._post({
            "model": jev._model,
            "state": {"question": q, "outcome": outcome, "event": event, "tags": tags},
            "questions": RESTRICTED_QUESTIONS,
        })
        a = body["answers"]
        vals = [a[k]["noul"] for k in ("federal_policy_outcome", "defense_or_military",
                                       "us_election_or_appointment", "politics_or_government")]
        veto = max(vals) > 0.12
        ok = veto == bool(exp)
        bad += not ok
        print(f"{exp:>3} " + " ".join(f"{v:5.2f}" for v in vals) + f" {max(vals):5.2f}  "
              f"{'VETO' if veto else 'pass':<5}{'' if ok else ' <-- WRONG'}  {q[:44]} / {outcome[:22]}")
    await jev.aclose()
    print(f"\nmodel={body.get('model')}  wrong={bad}/{len(CASES)}")

asyncio.run(main())
