"""
fix_inverted_spreads.py -- one-off correction of fast-lane rows recorded
under an inverted spread label. Run once; safe to re-run (idempotent).

Before adapters.yes_side() (2026-09-23), a spread market's `outcome` was
its title, which on underdog ("pos") spreads names the OTHER team. Jev's
direction for such a row ("raises") was about the team in that wrong
label, i.e. the opposite of the YES side. This rewrites those rows: the
outcome becomes the YES side ("New York Giants +3.5"), the effect flips,
and `side_fix` records what was changed. Drift and P&L computed from the
row afterwards are then about the side YES actually pays on.

The YES team is read from the stored question ("Will the <team> cover
<line> ..."), which the exchange writes about the YES side. A row whose
question does not have that form is left alone.

    python tools/fix_inverted_spreads.py            # dry run: list changes
    python tools/fix_inverted_spreads.py --apply
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent.parent
_COVER = re.compile(r"^Will the (?P<team>.+?) cover (?P<line>[+-]?\d+(?:\.\d+)?)\b")
_FLIP = {"raises": "lowers", "lowers": "raises"}


def plan(conn: sqlite3.Connection) -> list[tuple[int, str, str, str, str]]:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
    if "side_fix" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN side_fix TEXT")
    out = []
    for sid, slug, q, outcome, effect, fixed in conn.execute(
            "SELECT id, market_slug, question, outcome, effect, side_fix FROM signals"):
        if fixed or "-pos-" not in (slug or ""):
            continue
        m = _COVER.match(q or "")
        if not m or not outcome or m["team"].lower() in outcome.lower():
            continue
        line = float(m["line"])
        label = f"{m['team']} +{abs(line):g}"
        out.append((sid, slug, outcome, label, _FLIP.get(effect, effect)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=str(ROOT / "fastlane.db"))
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    rows = plan(conn)
    for sid, slug, old, new, eff in rows:
        print(f"  signal {sid:<5} {slug[-28:]:<29} '{old[:40]}' -> '{new}'  effect -> {eff}")
    if args.apply and rows:
        for sid, _slug, old, new, eff in rows:
            conn.execute("UPDATE signals SET outcome=?, effect=?, side_fix=? WHERE id=?",
                         (new, eff, f"was '{old}'; effect flipped (inverted spread title)", sid))
        conn.commit()
    print(f"{len(rows)} rows {'corrected' if args.apply else 'would change (dry run)'}")


if __name__ == "__main__":
    main()
