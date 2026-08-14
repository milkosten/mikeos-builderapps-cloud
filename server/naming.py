"""Short project names.

A project's title used to be `prompt[:60]` — a truncated sentence ("A simple job board
where companies post roles and candidates"), which reads badly everywhere it appears: the
topbar dropdown, the Apps list, the builder header.

So at CREATE time we ask the model for a real product-style name, **at most 15 characters**
("LinkPulse", "StandupDaily", "JobBoard"). Two hard rules:

* **Creation must never fail because naming failed.** Every path here is best-effort and
  falls back to `slug_name()`, a deterministic name derived from the prompt itself. The LLM
  call is bounded by its own wall-clock budget so a slow provider delays a create by seconds,
  never by minutes.
* **The cap is enforced by us, not by the model.** Whatever comes back is stripped of quotes
  and punctuation, whitespace-collapsed and hard-truncated to 15 characters.
"""
import asyncio
import logging
import os
import re
from typing import Dict, Optional

from server import gpu

logger = logging.getLogger(__name__)

MAX_NAME = 15

# Wall-clock budget for the naming call. A create must not sit behind a slow provider.
NAME_BUDGET_SEC = float(os.environ.get("BUILDERAPPS_NAME_BUDGET_SEC", "12"))

# Words that carry no product meaning — dropped when deriving the deterministic fallback.
_STOP = {
    "a", "an", "the", "and", "or", "of", "for", "with", "without", "to", "in", "on", "at",
    "by", "from", "my", "our", "your", "their", "its", "that", "this", "these", "those",
    "where", "when", "which", "who", "what", "how", "each", "every", "some", "any", "all",
    "is", "are", "be", "can", "should", "would", "will", "i", "we", "you", "they", "it",
    "simple", "basic", "small", "little", "nice", "new", "app", "application", "site",
    "website", "web", "tool", "system", "platform", "service", "page", "build", "make",
    "create", "want", "need", "like", "please", "so", "then", "plus", "also", "own",
}

_SYSTEM = ("You name software products. You reply with ONLY the name — no quotes, no "
           "punctuation, no explanation, no markdown.")


def _ask(prompt: str) -> str:
    return (
        f"App idea:\n{prompt[:700]}\n\n"
        "Invent a short product name for it.\n"
        f"RULES: at most {MAX_NAME} characters TOTAL. One word, or two words joined in "
        "CamelCase. Letters and digits only. No quotes, no punctuation, no spaces, no "
        "explanation.\n"
        "Good examples: LinkPulse, StandupDaily, JobBoard, ExpenseTrail, ChangeLog.\n"
        "Reply with the name and nothing else."
    )


def _one_line(line: str) -> str:
    """A single candidate line -> a bare name (may be "")."""
    m = re.match(r"^\**\s*(?:product\s+|app\s+|brand\s+)?name\s*[:\-]\s*(.+)$", line, re.I)
    if m:
        line = m.group(1)
    line = line.strip().strip("`*\"'“”‘’ ")
    line = re.sub(r"[^0-9A-Za-z ]+", " ", line)      # a name is a brand, not a sentence
    line = re.sub(r"\s+", " ", line).strip()
    if not line:
        return ""
    # Always join to CamelCase: "Standup Daily" -> "StandupDaily".
    return "".join(w if w.isupper() else w[:1].upper() + w[1:] for w in line.split(" "))


def clean(raw: Optional[str]) -> str:
    """Normalise anything model-shaped into a <=15-char name. Returns "" if nothing survives."""
    text = str(raw or "").replace("```", "\n")
    best = ""
    for line in text.splitlines():
        line = line.strip()
        # Skip obvious preamble ("Here you go:", "Sure!") — a name never ends in a colon
        # and is never a sentence.
        if not line or line.endswith(":") or len(line.split()) > 3:
            continue
        cand = _one_line(line)
        if len(cand) < 2:
            continue
        if len(cand) <= MAX_NAME:
            return cand                      # a name that already fits wins outright
        best = best or cand
    if not best:
        # Nothing looked like a name — fall back to the very first non-empty line so a
        # one-line sentence still yields something rather than nothing.
        for line in text.splitlines():
            if line.strip():
                best = _one_line(line.strip())
                break
    return best[:MAX_NAME].strip()


def slug_name(prompt: str) -> str:
    """Deterministic fallback: a CamelCase name built from the prompt's own words.

    Tries three words, then two, then one, and keeps the longest that fits the cap — so
    "A simple job board where companies post roles" -> "SimpleJobBoard" and
    "A URL shortener with click analytics" -> "URLShortener".
    """
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]*", str(prompt or ""))
             if w.lower() not in _STOP]
    parts = [w if w.isupper() else w[:1].upper() + w[1:].lower() for w in words[:3]]
    for n in (3, 2, 1):
        cand = "".join(parts[:n])
        if cand and len(cand) <= MAX_NAME:
            return cand
    # Nothing usable (e.g. a prompt of pure punctuation or one very long word).
    fallback = re.sub(r"[^0-9A-Za-z]", "", str(prompt or ""))[:MAX_NAME]
    return fallback or "NewApp"


async def name_for(prompt: str, *, budget: float = NAME_BUDGET_SEC) -> str:
    """One cheap LLM call for a <=15-char product name. NEVER raises."""
    fallback = slug_name(prompt)
    try:
        raw = await asyncio.wait_for(
            gpu.chat(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": _ask(prompt)}],
                temperature=0.6, num_predict=24, timeout=max(6.0, budget), max_retries=1,
            ),
            timeout=budget,
        )
    except Exception as e:  # noqa: BLE001 — naming must never break a create
        logger.info("short-name call failed (%s); using slug %r", e, fallback)
        return fallback
    name = clean(raw)
    # Junk guard: a name that is a single character, or that the model padded with a
    # sentence, is worse than the deterministic slug.
    if len(name) < 2 or not re.search(r"[A-Za-z]", name):
        logger.info("short-name reply %r rejected; using slug %r", str(raw)[:80], fallback)
        return fallback
    return name


# ---- a better name from the strategy docs ---------------------------------
# The strategy pass may name the product explicitly ("Product name: LinkPulse"). If it does,
# that beats the name we invented from the raw prompt — still capped at 15 characters. No
# extra tokens are spent: this only reads what the docs already say.
#
# TWO GUARDS, both learned on the first live build. A bare `Name:` matched
# "**Name:** Dana Whitfield" in BUYER-PERSONA.md — that doc's spec literally asks the model
# for a persona's NAME — and the project got renamed after a fictional book-club member
# instead of the product. So: only docs that describe the PRODUCT are searched (a persona /
# ICP doc describes people), and the key must actually say product/app/brand.
_BRAND_DOCS = ("VISION.md", "MARKETING.md")
_BRAND_RE = re.compile(
    r"^\s*[*#\-\s]*(?:product(?:\s+name)?|app\s+name|brand(?:\s+name)?)\**\s*[:\-]\s*(.+)$",
    re.I | re.M)


def brand_from_docs(docs: Dict[str, str]) -> Optional[str]:
    """Return a <=15-char brand name if the PRODUCT docs state one, else None."""
    for path, body in (docs or {}).items():
        if not isinstance(body, str):
            continue
        if not any(str(path).endswith(d) for d in _BRAND_DOCS):
            continue
        m = _BRAND_RE.search(body[:4000])
        if not m:
            continue
        name = clean(m.group(1))
        if len(name) >= 2 and re.search(r"[A-Za-z]", name):
            logger.info("brand name %r taken from %s", name, path)
            return name
    return None
