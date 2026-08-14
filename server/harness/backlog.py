"""Parse TECHNICAL-PLAN.md's `## Build Backlog` into an ordered list of feature strings.

This is what makes phase 15's step count scale to the app: each backlog item becomes one
narrow build step. Deterministic parsing (no LLM) so the step list is stable + resumable.
"""
import re
from typing import List

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")
_NUM_ITEM_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


def parse_backlog(tech_plan_md: str, *, cap: int = 16) -> List[str]:
    """Return the ordered build-backlog items from the `## Build Backlog` section.

    Accepts numbered (`1. ...`) or bulleted (`- ...`) items. Falls back to scanning the whole
    doc for a numbered list if the heading is missing. Each item is trimmed of markdown bold.
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
    return uniq[:cap]
