"""
by_category.py -- how does the pipeline treat each category?

Reads shadow.db only. For rows seen in the last N hours (default 3):
per category, how many markets were structurally rejected (and why), how
many reached Jev, how many the restricted veto stopped, how many
escalated -- then the escalated and the vetoed, by name, so a human can
check the judgment rather than the count.

    python tools/by_category.py [hours]
"""

import collections
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

# First match wins; order puts the specific before the general.
CATEGORIES = ("weather", "esports", "commodities", "crypto", "music",
              "entertainment", "tech", "science", "fed", "economics",
              "business", "awards", "sports", "culture")


def category(tags_json: str | None) -> str:
    try:
        tags = json.loads(tags_json or "[]")
    except ValueError:
        tags = []
    for c in CATEGORIES:
        if c in tags:
            return c
    return tags[0] if tags else "(untagged)"


def main() -> None:
    conn = sqlite3.connect(ROOT / "shadow.db")
    conn.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(hours=HOURS)).isoformat()
    rows = conn.execute(
        "SELECT * FROM verdicts WHERE seen_at >= ? ORDER BY seen_at", (since,)
    ).fetchall()
    print(f"{len(rows)} rows in the last {HOURS:g}h\n")

    by = collections.defaultdict(list)
    for r in rows:
        by[category(r["tags"])].append(r)

    print(f"{'category':<14}{'n':>4}{'struct':>7}{'jev':>5}{'veto':>5}"
          f"{'esc':>5}{'gate':>7}   top structural reasons")
    for cat, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        struct = [r for r in rs if r["structural_reject"]]
        jev = [r for r in rs if not r["structural_reject"]]
        veto = [r for r in jev if (r["veto_reason"] or "").startswith("restricted")]
        esc = [r for r in jev if r["escalate"]]
        gates = [r["gate_score"] for r in jev if r["gate_score"]]
        why = collections.Counter(
            (r["structural_reject"] or "").split("=")[0].split(" ")[0] for r in struct)
        print(f"{cat:<14}{len(rs):>4}{len(struct):>7}{len(jev):>5}{len(veto):>5}"
              f"{len(esc):>5}{(sum(gates) / len(gates) if gates else 0):>7.2f}   "
              + ", ".join(f"{k}:{v}" for k, v in why.most_common(3)))

    def show(title, pick, fmt):
        items = [r for r in rows if pick(r)]
        print(f"\n{title} ({len(items)})")
        for r in items[:40]:
            print("  " + fmt(r))

    name = lambda r: f"{(r['question'] or '')[:46]} / {(r['outcome'] or '')[:20]}"
    show("RESTRICTED VETOES",
         lambda r: (r["veto_reason"] or "").startswith("restricted"),
         lambda r: f"{category(r['tags']):<13} {r['veto_reason'][11:52]:<42} {name(r)}")
    show("ESCALATED",
         lambda r: r["escalate"],
         lambda r: f"{category(r['tags']):<13} gate {r['gate_score']:.2f} "
                   f"{r['bid']:.2f}/{r['ask']:.2f} {(r['outcome_type'] or r['sports_market_type'] or ''):<21} {name(r)}")
    show("REACHED JEV, NOT ESCALATED, NOT RESTRICTED",
         lambda r: not r["structural_reject"] and not r["escalate"]
         and not (r["veto_reason"] or "").startswith("restricted"),
         lambda r: f"{category(r['tags']):<13} {(r['veto_reason'] or '')[:38]:<39} {name(r)}")

    # The check that matters most: did anything political-looking get through?
    words = ("trump", "biden", "president", "election", "senate", "congress",
             "minister", "nobel peace", "governor", "tariff", "putin", "war ")
    leaks = [r for r in rows if r["escalate"] and any(
        w in f"{r['question']} {r['outcome']} {r['event_title']}".lower() for w in words)]
    print(f"\nESCALATED WITH POLITICAL-LOOKING TEXT: {len(leaks)}")
    for r in leaks:
        print(f"  pol={r['politics_or_government']}  {name(r)}")


main()
