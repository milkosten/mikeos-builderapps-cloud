"""Phase 34 — the two pieces of Discuss that are pure logic and must not regress.

1. `merge_canvas`: an AGREED decision is never silently overwritten. Everything else in the
   room is a model output and can be re-generated; this is the one invariant that makes the
   canvas worth reading, and it is exactly the kind of rule a later "simplification" removes.
2. `webread.check_url`: the SSRF guard. Discuss browses the OPEN internet on the user's
   behalf (deliberately unlike the assistants, which are pinned to their own app), so the
   guard is the only thing between "read this article" and "read the cloud metadata service".

Run: python3 -m pytest tests/test_discuss.py -q     (no DB, no network)
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import discuss, webread  # noqa: E402


# ---------------------------------------------------------------------------
# the canvas
# ---------------------------------------------------------------------------
def test_empty_canvas_is_filled_from_the_draft():
    c = discuss.merge_canvas({}, {"audience": "solo founders", "features": ["a", "b"]}, [], [])
    assert discuss.field(c, "audience") == "solo founders"
    assert discuss.field(c, "features") == ["a", "b"]
    assert c["audience"]["agreed"] is False        # a draft is not a decision


def test_a_draft_may_be_replaced_but_a_decision_may_not():
    c = discuss.merge_canvas({}, {"audience": "anyone"}, [], [])
    c = discuss.merge_canvas(c, {"audience": "book clubs"}, ["audience"], [])
    assert discuss.field(c, "audience") == "book clubs"
    assert c["audience"]["agreed"] is True
    # THE RULE: the next turn's enthusiasm does not get to undo what the user settled.
    c = discuss.merge_canvas(c, {"audience": "enterprise teams"}, [], [])
    assert discuss.field(c, "audience") == "book clubs"


def test_an_agreed_value_changes_only_by_an_EXPLICIT_revision_and_it_is_logged():
    c = discuss.merge_canvas({}, {"audience": "book clubs"}, ["audience"], [])
    c = discuss.merge_canvas(
        c, {"audience": "ignored"}, [],
        [{"field": "audience", "to": "school reading groups",
          "because": "you said it is for teachers"}])
    assert discuss.field(c, "audience") == "school reading groups"
    log = c["changelog"]
    assert len(log) == 1
    assert log[0]["from"] == "book clubs" and log[0]["to"] == "school reading groups"
    assert "teachers" in log[0]["because"]          # a revision is never silent


def test_lists_accumulate_and_never_lose_an_agreed_item():
    c = discuss.merge_canvas({}, {"features": ["shelves", "ratings", "notes"]}, ["features"], [])
    c = discuss.merge_canvas(c, {"features": ["ratings", "export"]}, [], [])
    assert discuss.field(c, "features") == ["shelves", "ratings", "notes", "export"]


def test_a_revision_may_replace_a_whole_list():
    c = discuss.merge_canvas({}, {"features": ["a", "b"]}, ["features"], [])
    c = discuss.merge_canvas(c, {}, [], [{"field": "features", "to": ["c"], "because": "scope cut"}])
    assert discuss.field(c, "features") == ["c"]


def test_unknown_fields_are_ignored():
    c = discuss.merge_canvas({}, {"budget": "£5", "vision": "v"}, ["budget"], [])
    assert "budget" not in c and discuss.field(c, "vision") == "v"


# ---------------------------------------------------------------------------
# the brief — what actually reaches the pipeline
# ---------------------------------------------------------------------------
def test_the_brief_carries_the_decisions_not_just_the_sentence():
    canvas = discuss.merge_canvas(
        {}, {"name": "Shelfie", "vision": "Track what a book club is reading.",
             "audience": "small in-person book clubs", "features": ["a shared shelf", "votes"],
             "stack": "accounts per club, data owned by the club",
             "out_of_scope": ["ebook reading"]},
        ["name", "audience", "features", "out_of_scope"], [])
    brief = discuss.compose_brief({"seed": "a book club tracker", "canvas": canvas,
                                  "messages": [{"role": "user", "text": "for my book club"}]})
    assert "Shelfie" in brief
    assert "small in-person book clubs" in brief
    assert "a shared shelf" in brief
    assert "OUT OF SCOPE" in brief and "ebook reading" in brief
    assert "do not invent another" in brief          # the naming guard rail
    assert "a book club tracker" in brief            # the original sentence survives


def test_the_brief_degrades_to_the_seed_when_nothing_was_settled():
    assert discuss.compose_brief({"seed": "a todo app", "canvas": {}, "messages": []}) \
        == "a todo app"


# ---------------------------------------------------------------------------
# "show me the vision" is a promise, not a hint to the model
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("show me the vision", "vision"),
    ("Show me the vision please", "vision"),
    ("can I see the vision?", "vision"),
    ("show me the whole brief", "canvas"),
    ("what's the plan so far", "canvas"),
    ("I want a vision statement for my users", ""),   # not an ask to be SHOWN
    ("build it", ""),
])
def test_show_detection(text, expected):
    assert discuss.wants_shown(text) == expected


# ---------------------------------------------------------------------------
# the answer set — one submit, every question accounted for
#
# THE BUG THIS GUARDS. Clicking one chip used to post that option as a whole user turn. The
# model saw four questions asked and one line back, completed the pattern by inventing the
# other three answers, and listed their fields in `decided` — which under merge_canvas means
# AGREED, i.e. changeable only by an explicit revision. One click froze three decisions the
# user never made. The stepper submits the set at once, and the set names its own gaps.
# ---------------------------------------------------------------------------
def test_an_empty_answer_is_a_skip_not_an_answer():
    a = discuss.clean_answers([{"q": "Who is it for?", "answer": "   "}])
    assert a[0]["skipped"] is True
    # ... and an explicit skip stays a skip.
    b = discuss.clean_answers([{"q": "Who?", "answer": "a team", "skipped": True}])
    assert b[0]["skipped"] is True


def test_a_question_with_no_text_is_dropped_and_the_set_is_bounded():
    a = discuss.clean_answers([{"q": "", "answer": "x"}, {"q": "real?", "answer": "yes"}])
    assert [x["q"] for x in a] == ["real?"]
    assert len(discuss.clean_answers([{"q": f"q{i}", "answer": "a"} for i in range(20)])) <= 8


def test_the_transcript_shows_the_skip_rather_than_hiding_it():
    text = discuss.answers_text(discuss.clean_answers([
        {"q": "Who is it for?", "answer": "a team"},
        {"q": "Does it need logins?", "answer": "", "skipped": True},
    ]))
    assert "Who is it for?" in text and "a team" in text
    # the thread is a RECORD: an unanswered question is not silently absent from it
    assert "Does it need logins?" in text and "skipped" in text


def test_the_model_is_told_which_questions_have_no_answer():
    p = discuss._answers_prompt(discuss.clean_answers([
        {"q": "Who is it for?", "answer": "a team"},
        {"q": "Does it need logins?", "answer": ""},
        {"q": "What would make it a failure?", "answer": "", "skipped": True},
    ]))
    assert "COMPLETE ANSWER SET" in p
    assert p.count("NOT ANSWERED") == 3          # two markers + the closing instruction
    assert "Do NOT invent" in p and "`decided`" in p
    assert "ANSWER: a team" in p


def test_a_full_set_is_not_hedged_with_a_skip_warning():
    p = discuss._answers_prompt(discuss.clean_answers([
        {"q": "Who is it for?", "answer": "a team"},
        {"q": "Logins?", "answer": "yes"},
    ]))
    assert "NOT ANSWERED" not in p
    assert "Every question was answered" in p


def test_questions_are_single_select_unless_the_model_says_multi():
    qs = discuss._clean_questions([
        {"q": "Who is it for?", "options": ["me", "a team"]},
        {"q": "Which of these matter?", "options": ["a", "b"], "multi": True},
    ])
    assert qs[0]["multi"] is False and qs[1]["multi"] is True


def test_the_system_prompt_forbids_inventing_a_skipped_answer():
    """The rule lives in the prompt, so the prompt is where it can be deleted by accident."""
    s = discuss._SYSTEM
    assert "COMPLETE SET" in s
    assert "SKIPPED" in s and "invent" in s


# ---------------------------------------------------------------------------
# the SSRF guard
# ---------------------------------------------------------------------------
def _refuses(url) -> str:
    with pytest.raises(webread.Refused) as e:
        asyncio.run(webread.check_url(url))
    return str(e.value)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/",
    "http://localhost:8000/api/health",
    "http://169.254.169.254/latest/meta-data/",     # cloud metadata — THE target
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://172.16.0.9/",
    "http://[::1]/",
    "file:///etc/passwd",
    "data:text/html,<h1>hi",
    "javascript:alert(1)",
    "gopher://example.com/",
    "http://mikeos-builderapps:8000/api/projects",  # a docker service name (no dot)
    "http://gitea/",
    "https://builderapps-api.osmike.com/api/health",
    "https://abc123.builderapps.osmike.com/",       # another tenant's app
    "https://chrome-pool.osmike.com/session",
])
def test_blocked_targets(url):
    assert _refuses(url)


@pytest.mark.parametrize("url", [
    "https://example.com/",
    "https://en.wikipedia.org/wiki/Book_club",
    "example.com",                                   # scheme-less is normalised, not refused
])
def test_public_targets_are_allowed(url):
    got = asyncio.run(webread.check_url(url))
    assert got.startswith("http")


def test_refusal_wording_does_not_repeat_the_address():
    """A literal IP names itself once; a NAME must show the indirection, because "evil.com
    resolves to 127.0.0.1" is the whole reason the resolved-IP check exists."""
    msg = _refuses("http://169.254.169.254/latest/meta-data/")
    assert msg.count("169.254.169.254") == 1
    assert "link-local" in msg and "points at" not in msg
