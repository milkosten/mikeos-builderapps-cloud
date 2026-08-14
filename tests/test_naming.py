"""Offline tests for short project names (server.naming).

A project's title used to be `prompt[:60]` — a truncated sentence that read badly in the
topbar dropdown and the Apps list. Now it is a real product-style name, and these tests pin
the two properties that matter and that nothing downstream can compensate for:

* **the 15-character cap is OURS, not the model's** — whatever comes back is cleaned and
  hard-truncated, so no reply can widen the UI;
* **naming can never fail a create** — every junk/garbage reply falls back to a deterministic
  slug of the prompt itself, which is always non-empty.

    python3 -m tests.test_naming
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import naming  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed.append(name)
        print(f"  FAIL {name}  {detail}")


PROMPTS = [
    "A simple job board where companies post roles and candidates apply",
    "A URL shortener with click analytics and a dashboard of my links",
    "A team standup tool: each member posts yesterday/today/blockers, with a daily digest",
    "A personal expense tracker with categories, a monthly chart, and CSV export",
    "A public changelog / release-notes site with an admin editor and RSS feed",
    "A recipe box where I save recipes with ingredients and steps",
]


def main() -> int:
    print("[the cap is enforced by us]")
    junk = [
        '"JobBoard"', "Name: LinkPulse", "**Standup Daily**",
        "Here you go:\nJobBoard", "Sure! The name is:\n\nExpenseTrail\n",
        "A very long product name indeed that rambles on and on",
        "LinkPulseAnalyticsSuiteEnterprise", "   ", "", "```\nJobBoard\n```",
        "\n\n\n", "x", "1", "😀😀😀",
    ]
    check("no cleaned reply is ever longer than the cap",
          all(len(naming.clean(j)) <= naming.MAX_NAME for j in junk),
          [(j, naming.clean(j)) for j in junk if len(naming.clean(j)) > naming.MAX_NAME])
    check("quotes/markdown/preamble are stripped",
          naming.clean('"JobBoard"') == "JobBoard"
          and naming.clean("**Standup Daily**") == "StandupDaily"
          and naming.clean("Here you go:\nJobBoard") == "JobBoard"
          and naming.clean("Name: LinkPulse") == "LinkPulse")
    check("multi-word names are joined, never left with spaces",
          " " not in naming.clean("  Job Board Pro  "),
          naming.clean("  Job Board Pro  "))

    print("\n[the deterministic fallback is always usable]")
    for p in PROMPTS:
        s = naming.slug_name(p)
        check(f"slug fits and is non-empty: {s!r}",
              0 < len(s) <= naming.MAX_NAME and s.isalnum(), (p, s))
    check("a prompt with no usable words still yields a name",
          naming.slug_name("!!! ??? ...") == "NewApp", naming.slug_name("!!! ??? ..."))
    check("one absurdly long word is truncated, not dropped",
          0 < len(naming.slug_name("supercalifragilisticexpialidocious")) <= naming.MAX_NAME)
    check("an empty prompt still yields a name", bool(naming.slug_name("")))

    print("\n[a brand name from the strategy docs]")
    check("an explicit product name in the docs is picked up",
          naming.brand_from_docs({"docs/VISION.md": "# Vision\n\nProduct name: Link Pulse\n"})
          == "LinkPulse")
    check("docs without a name yield None",
          naming.brand_from_docs({"docs/VISION.md": "# Vision\n\nnothing to see\n"}) is None)
    long_brand = naming.brand_from_docs(
        {"docs/MARKETING.md": "Brand: Something Very Long Indeed"})
    check("a brand name is found AND capped",
          long_brand and len(long_brand) <= naming.MAX_NAME, long_brand)
    check("a non-string doc body cannot crash the parse",
          naming.brand_from_docs({"docs/VISION.md": None, "docs/MARKETING.md": 42}) is None)

    # THE bug the first live build produced: BUYER-PERSONA.md is *asked* for a persona's
    # name, so a bare `Name:` there renamed a book-club app "DanaWhitfield".
    persona = ("# Ideal Buyer Persona\n\n**Name:** Dana Whitfield\n\n"
               "**Role:** Book club organiser\n")
    check("a persona's name is NOT the product's name",
          naming.brand_from_docs({"docs/BUYER-PERSONA.md": persona}) is None)
    check("an ICP doc is not searched either",
          naming.brand_from_docs({"docs/ICP.md": "Name: Someone Else"}) is None)
    check("a persona alongside a real product name still yields the product name",
          naming.brand_from_docs({"docs/BUYER-PERSONA.md": persona,
                                  "docs/VISION.md": "Product name: BookVote\n"}) == "BookVote")
    check("a bare 'Name:' in a product doc is not enough",
          naming.brand_from_docs({"docs/VISION.md": "Name: Dana Whitfield\n"}) is None)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
