"""
skills.py -- per-family playbooks for the LLM roles, selected in code.

A skill is a short markdown file under skills/<family>/<id>.md: when it
applies, what to search for, which sources to trust, which facts matter,
the pitfalls this repo has already paid for, and what a brief should hold.
Every bullet carries its evidence ([general], [measured: ...], [rule],
[unverified: ...]) so a reader can tell domain knowledge from what was
observed here. The markers are for humans and are stripped before a skill
reaches a model.

Selection is arithmetic on the market's tags and slug, so it is code, not a
question (CLAUDE.md, design rule 1): no LLM and no Jev call picks a skill.

    select(market)            -> [Skill, ...]  (at most MAX_SKILLS)
    gatherer_block(state)     -> text appended to a gatherer's system prompt
    brief_block(markets)      -> text sent as the brief writer's `system`
    research_allowed(market)  -> False for families priced in code (weather)

Two guarantees, both tested:
  * a market no skill matches gets "" from both hooks, so its prompts are
    byte-identical to what they were before this module existed;
  * a restricted or political market selects nothing, whatever its tags.

Skills are never edited by code. The lesson_drafter role proposes edits in
docs/PROPOSALS.md and a human applies them (docs/AGENTS.md).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from questions import political_tag

log = logging.getLogger("skills")

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

#: A market loads at most this many skills; the prompt stays small.
MAX_SKILLS = 2

#: Sections a gatherer reads, and sections a brief writer reads.
GATHER_SECTIONS = ("Search", "Trust", "Facts that matter", "Pitfalls")
BRIEF_SECTIONS = ("Facts that matter", "Pitfalls", "Brief")
ALL_SECTIONS = ("Applies when", "Search", "Trust", "Facts that matter", "Pitfalls", "Brief")

#: Sections whose bullets are instructions (query templates), not claims.
UNMARKED_SECTIONS = ("Search",)

MARKER = re.compile(r"\s*\[(general|rule|measured:[^\]]+|unverified:[^\]]+)\]\s*$")

#: Same tokens as fastlane._PERIOD_TOKENS (a test holds them equal). Copied
#: rather than imported: fastlane pulls in the exchange SDK and imports us.
PERIOD_TOKENS = frozenset({"1h", "2h", "1q", "2q", "3q", "4q", "1p", "2p", "3p", "h1", "h2",
                           "q1", "q2", "q3", "q4", "1st", "2nd", "3rd", "4th"})

#: Whole words in a market's text that mark it restricted even when its
#: tags do not. A false positive costs a market its playbook, nothing more;
#: the real veto is political_tag() plus the four restricted Nouls.
RESTRICTED_WORDS = frozenset({
    "election", "elections", "midterm", "midterms", "governor", "gubernatorial",
    "mayor", "senator", "congressman", "tariff", "tariffs", "fed", "fomc",
    "cftc", "regulator", "regulators", "regulation", "legislation", "law",
    "impeachment", "cabinet", "minister", "sanctions", "nominee", "ceasefire",
})

_GATHER_PREAMBLE = (
    "Playbook notes for this market family, kept by this system's operators "
    "from its own measured history. Use what fits the market in front of you "
    "and ignore anything that does not. The notes are not a source: every "
    "fact you report must come from your own search, dated. They do not "
    "change the required output."
)
_BRIEF_PREAMBLE = (
    "Playbook notes for this market family, kept by this system's operators "
    "from its own measured history. Use what fits the event in the user "
    "message and ignore anything that does not. The notes are not a source: "
    "every fact in the brief must come from your search, dated. They do not "
    "change the required JSON shape."
)
_NO_RESEARCH = (
    "Do not search for this market. It is priced in code. Return "
    "data_confidence 0.0 and say so in identified_catalyst."
)


@dataclass(frozen=True)
class Skill:
    id: str
    title: str
    family: str
    tags: frozenset[str]
    kinds: frozenset[str]
    period: bool                      # also matches period lines in its tags
    research: bool
    priority: int                     # lower loads first
    sections: tuple[tuple[str, tuple[str, ...]], ...]   # (name, bullets with markers)
    path: str

    def bullets(self, section: str, *, markers: bool = False) -> list[str]:
        for name, items in self.sections:
            if name == section:
                return list(items) if markers else [MARKER.sub("", b) for b in items]
        return []


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _csv(value: str) -> frozenset[str]:
    return frozenset(v.strip().lower() for v in value.split(",") if v.strip())


def _yes(value: str, name: str, path: Path) -> bool:
    v = value.strip().lower()
    if v not in ("yes", "no"):
        raise ValueError(f"{path}: {name} must be yes or no, got {value!r}")
    return v == "yes"


def parse_skill(text: str, path: Path) -> Skill:
    """Strict: a malformed skill raises, so a typo is a failing test, not a silent no-op."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text.replace("\r\n", "\n"), re.S)
    if not m:
        raise ValueError(f"{path}: missing front matter")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"{path}: bad front matter line {line!r}")
        meta[key.strip()] = value.strip()
    for key in ("id", "title", "kinds", "research", "priority"):
        if key not in meta:
            raise ValueError(f"{path}: front matter lacks {key}")
    if meta["id"] != path.stem:
        # One id, one file: a proposed edit names the id, and must land in one place.
        raise ValueError(f"{path}: id {meta['id']!r} must equal the file name")

    sections: list[tuple[str, list[str]]] = []
    for line in m.group(2).splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            if name not in ALL_SECTIONS:
                raise ValueError(f"{path}: unknown section {name!r}")
            sections.append((name, []))
        elif line.startswith("- "):
            if not sections:
                raise ValueError(f"{path}: bullet before any section")
            sections[-1][1].append(line[2:].strip())
        elif line.strip() and sections and sections[-1][1] and line.startswith("  "):
            sections[-1][1][-1] += " " + line.strip()      # wrapped bullet
    names = [n for n, _ in sections]
    for required in ("Applies when", "Pitfalls", "Brief"):
        if required not in names:
            raise ValueError(f"{path}: missing section {required!r}")
    for name, items in sections:
        if name in UNMARKED_SECTIONS:
            continue
        for b in items:
            if not MARKER.search(b):
                raise ValueError(f"{path}: unmarked claim in {name!r}: {b[:60]!r}")
    kinds = _csv(meta["kinds"])
    tags = _csv(meta.get("tags", ""))
    if not kinds and not tags:
        raise ValueError(f"{path}: a skill with no kinds and no tags would match everything")
    return Skill(
        id=meta["id"], title=meta["title"], family=path.parent.name,
        tags=tags, kinds=kinds,
        period=_yes(meta.get("period", "no"), "period", path),
        research=_yes(meta["research"], "research", path),
        priority=int(meta["priority"]),
        sections=tuple((n, tuple(i)) for n, i in sections),
        path=str(path),
    )


@lru_cache(maxsize=4)
def load(root: str | None = None) -> tuple[Skill, ...]:
    """Every skill under `root`, sorted by (priority, id). Cached; files are read-only here."""
    base = Path(root) if root else SKILLS_DIR
    out = [parse_skill(p.read_text(encoding="utf-8"), p) for p in sorted(base.glob("*/*.md"))]
    ids = [s.id for s in out]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate skill ids under {base}")
    return tuple(sorted(out, key=lambda s: (s.priority, s.id)))


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def market_kind(slug: str | None) -> str:
    """Slug prefix: `aec`, `asc`, `astatc`, ... and `tc-temp` for weather bands."""
    parts = str(slug or "").lower().split("-")
    if parts[0] == "tc" and len(parts) > 1:
        return f"tc-{parts[1]}"
    return parts[0]


def _tag_set(market: dict[str, Any]) -> set[str]:
    """Tags plus the slug's second and third tokens (league, coin, chart)."""
    out: set[str] = set()
    for t in market.get("tags") or []:
        s = str(t.get("slug") or t.get("label") or "") if isinstance(t, dict) else str(t)
        out.add(s.strip().lower())
    parts = str(market.get("slug") or "").lower().split("-")
    out.update(parts[1:3])
    out.discard("")
    return out


def _text(market: dict[str, Any]) -> str:
    return " ".join(str(market.get(k) or "") for k in
                    ("question", "outcome", "event", "event_title", "slug"))


def is_restricted(market: dict[str, Any]) -> bool:
    """Political by tag, or by a restricted word anywhere in its text."""
    if political_tag(market.get("tags")):
        return True
    words = re.findall(r"[a-z0-9]+", _text(market).lower())
    return bool(RESTRICTED_WORDS.intersection(words)) or political_tag(words) is not None


def _is_period(market: dict[str, Any]) -> bool:
    """Same rule as fastlane.is_period_market. First-five-innings (f5) lines
    are NOT periods: their books were tight (0.01) on 2026-09-22."""
    tokens = set(str(market.get("slug") or "").lower().split("-"))
    if tokens & PERIOD_TOKENS:
        return True
    text = f"{market.get('question') or ''} {market.get('outcome') or ''}".lower()
    return any(w in text for w in ("half", "quarter", "1st period", "2nd period", "3rd period"))


def _matches(skill: Skill, kind: str, tags: set[str], market: dict[str, Any]) -> bool:
    if skill.tags and not (skill.tags & tags):
        return False
    if not skill.kinds:
        return True
    return kind in skill.kinds or (skill.period and _is_period(market))


def select(market: dict[str, Any], root: str | None = None) -> list[Skill]:
    """
    Skills for one market (a normalized market or a Jev state): a pure
    function of its tags, slug and text. Restricted markets get none.
    """
    if not market or is_restricted(market):
        return []
    kind, tags = market_kind(market.get("slug")), _tag_set(market)
    return [s for s in load(root) if _matches(s, kind, tags, market)][:MAX_SKILLS]


def select_for_event(markets: list[dict[str, Any]], root: str | None = None) -> list[Skill]:
    """Union over an event's markets, in load order. Any restricted market empties it."""
    if any(is_restricted(m) for m in markets):
        return []
    chosen = {s.id for m in markets for s in select(m, root)}
    return [s for s in load(root) if s.id in chosen][:MAX_SKILLS]


def research_allowed(market: dict[str, Any]) -> bool:
    """False when a matched skill says the family is priced in code. Not wired yet."""
    return all(s.research for s in select(market))


# --------------------------------------------------------------------------
# rendering -- the two hooks
# --------------------------------------------------------------------------

def render(skills: list[Skill], sections: tuple[str, ...], preamble: str) -> str:
    if not skills:
        return ""
    lines = [f'<playbook skills="{",".join(s.id for s in skills)}">', preamble]
    if any(not s.research for s in skills):
        lines.append(_NO_RESEARCH)
    for s in skills:
        lines.append(f"\n### {s.title}")
        for name in sections:
            items = s.bullets(name)
            if items:
                lines.append(f"{name}:")
                lines.extend(f"- {b}" for b in items)
    lines.append("</playbook>")
    return "\n".join(lines)


def gatherer_block(state: dict[str, Any]) -> str:
    """
    Appended to the END of a gatherer's system prompt, so the role text
    stays a fixed prefix. "" when nothing matches. A broken skill file
    never stops research: it logs and returns "".
    """
    try:
        text = render(select(state), GATHER_SECTIONS, _GATHER_PREAMBLE)
    except Exception as exc:  # noqa: BLE001 -- advisory text must not break Tier 2
        log.warning("skills: gatherer block skipped: %s", exc)
        return ""
    return "\n\n" + text if text else ""


def brief_block(markets: list[dict[str, Any]]) -> str:
    """
    The brief writer's `system` text, "" when nothing matches. Sent as the
    request's system prompt, ahead of the per-event user message, so it is
    identical for every event of a family.
    """
    try:
        return render(select_for_event(markets), BRIEF_SECTIONS, _BRIEF_PREAMBLE)
    except Exception as exc:  # noqa: BLE001 -- a brief without a playbook is today's brief
        log.warning("skills: brief block skipped: %s", exc)
        return ""
