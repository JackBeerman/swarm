"""
survey_tags.py -- what categories does the exchange actually carry?

Read-only, no auth, no model calls. For each candidate tagSlug: how many
open events, how many markets, the raw tag objects seen on them, how many
would be blocked by questions.political_tag(), and sample titles.

Paced at ~1 request / 1.3s: Cloudflare blocks bursts of ~25.
"""

import asyncio
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_us import AsyncPolymarketUS  # noqa: E402

from questions import political_tag  # noqa: E402

CANDIDATES = [
    "economics", "business", "crypto", "culture", "science", "sports",
    "tech", "technology", "ai", "entertainment", "movies", "music",
    "awards", "weather", "climate", "finance", "stocks", "earnings",
    "companies", "health", "space", "gaming", "esports", "pop-culture",
    "celebrities", "tv", "commodities", "markets", "world", "news",
    "mentions", "fed", "inflation", "bitcoin", "ethereum",
]


async def main() -> None:
    per_tag = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    seen_tags: collections.Counter = collections.Counter()
    tag_shape = None
    rows = []
    async with AsyncPolymarketUS() as pm:
        for tag in CANDIDATES:
            try:
                page = await pm.events.list(
                    {"limit": per_tag, "closed": False, "tagSlug": tag})
            except Exception as exc:  # noqa: BLE001
                rows.append((tag, -1, 0, 0, [f"ERROR {type(exc).__name__}"]))
                await asyncio.sleep(3)
                continue
            events = page.get("events", []) or []
            n_mkts = blocked = 0
            titles = []
            for ev in events:
                tags = ev.get("tags") or []
                if tags and tag_shape is None:
                    tag_shape = tags[0]
                for t in tags:
                    seen_tags[t.get("slug") if isinstance(t, dict) else str(t)] += 1
                n_mkts += len(ev.get("markets") or [])
                if political_tag(tags):
                    blocked += 1
                elif len(titles) < 4:
                    titles.append(str(ev.get("title") or ev.get("slug"))[:48])
            rows.append((tag, len(events), n_mkts, blocked, titles))
            await asyncio.sleep(1.3)

    print(f"{'tagSlug':<14}{'events':>7}{'mkts':>7}{'polit':>6}  sample (non-political)")
    for tag, n, m, b, titles in rows:
        if n:
            print(f"{tag:<14}{n:>7}{m:>7}{b:>6}  {' | '.join(titles)}")
    print("\nempty:", ", ".join(t for t, n, *_ in rows if n == 0))
    print("\nraw tag object shape:", json.dumps(tag_shape)[:300])
    print(f"\n{len(seen_tags)} distinct tag slugs on the wire; top 60:")
    print(", ".join(f"{k}({v})" for k, v in seen_tags.most_common(60)))
    flagged = sorted(k for k in seen_tags if political_tag([k]))
    print("\nslugs political_tag() blocks:", ", ".join(flagged) or "none")


asyncio.run(main())
