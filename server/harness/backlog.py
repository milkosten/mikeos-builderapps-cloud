"""Parse TECHNICAL-PLAN.md's `## Build Backlog` into an ordered list of feature strings.

This is what makes phase 15's step count scale to the app: each backlog item becomes one
narrow build step. Deterministic parsing (no LLM) so the step list is stable + resumable.
"""
import re
from typing import List

# The ONE place the backlog size is decided. The pipeline caps the step list here and the
# TECHNICAL-PLAN prompt quotes this same number, so the two can never disagree again — they
# did (prompt said "6-14 tasks", the cap was 12) and the campaign's changelog build lost
# backlog items 13-14 in silence. Item 14 was *"Frontend /admin editor page … then GET
# /rss.xml feed"* — the admin editor and the RSS feed, i.e. two of the three things the user
# had actually asked for. The build then reported "12 of 12 features built".
MAX_FEATURES = 14

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")
_NUM_ITEM_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


def parse_backlog(tech_plan_md: str, *, cap: int = MAX_FEATURES) -> List[str]:
    """Return the ordered build-backlog items from the `## Build Backlog` section.

    Accepts numbered (`1. ...`) or bulleted (`- ...`) items. Falls back to scanning the whole
    doc for a numbered list if the heading is missing. Each item is trimmed of markdown bold.

    Over-long backlogs are **folded, never truncated**: everything past the cap is merged into
    the last step so the step count stays bounded while no promised work is silently dropped.
    Truncation cost the campaign a whole build — the plan's last item was the admin editor
    page and the RSS feed, and both simply never got built while the run reported success.
    `cap=0` means "no cap", i.e. return exactly what the plan promised.
    """
    lines = tech_plan_md.splitlines()
    # locate the Build Backlog heading
    start = None
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if m and "backlog" in m.group(1).lower():
            start = i + 1
            break

    section = lines[start:] if start is not None else lines
    items: List[str] = []
    for ln in section:
        # stop at the next heading (end of the backlog section)
        if start is not None and _HEADING_RE.match(ln):
            break
        m = _NUM_ITEM_RE.match(ln) or _BULLET_RE.match(ln)
        if m:
            txt = m.group(m.lastindex).strip()
            txt = re.sub(r"^\*\*(.+?)\*\*", r"\1", txt)  # strip a leading **bold**
            txt = txt.strip("` ").strip()
            if txt and len(txt) > 3:
                items.append(txt)

    # de-dup while preserving order
    seen = set()
    uniq: List[str] = []
    for it in items:
        key = it.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(it)

    if cap >= 1 and len(uniq) > cap:
        head, overflow = uniq[:cap - 1], uniq[cap - 1:]
        merged = overflow[0]
        for extra in overflow[1:]:
            merged += f" THEN, in this same step, also build: {extra}"
        return head + [merged]
    return uniq
