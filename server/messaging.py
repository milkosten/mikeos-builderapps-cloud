"""Phase 33 — messages between assistants, and the wake that makes them mean something.

    Tester -> Developer:  "Found it. Filed #42 with the repro. Can you take it?"
    ...Developer's container starts, reads #42 in full, fixes it, ships it...
    Developer -> Tester:  "Fixed in a1b2c3d and deployed. Please retest."

**Delivery does not involve the browser.** An assistant container exists only for the length
of one beat, so there is no process to push a DM into; delivery is therefore a durable row
plus a wake flag, and the scheduler that already claims due assistants also claims woken
ones. Laptop shut, tab closed, nobody logged in — the exchange above still happens. The
WebSocket added alongside this only shows a watching browser what already occurred.

THE BOUNDS ARE THE HARD PART, not the delivery. Two assistants can be indefinitely polite at
each other and a Developer beat costs $0.20-1.59, so:

* **Chain depth.** A reply to a reply to a reply stops. Stored per message (`depth`), checked
  before the wake, and the stop is PERSISTED and RENDERED — the conversation visibly ends
  rather than quietly evaporating.
* **Coalesced wakes.** Setting the flag is `WHERE wake_pending_at IS NULL`, so three DMs
  arriving before the beat runs cost one beat and are all read by it.
* **The daily budget** (`server/budget.py`) is checked before a wake is granted, so messaging
  cannot spend past the project's $10/day stop.
* **"No reply needed" is a real answer.** It is written into the SOUL templates and into the
  tool's own help, because the default behaviour of a helpful model is to acknowledge, and an
  acknowledgement that costs a dollar is not helpfulness.
"""
import json
import logging
import os
from typing import Any, Optional

from server import assistants as A
from server import workspace_store as W
from server.db import pool

logger = logging.getLogger(__name__)

# How deep one conversation may go before it stops. 1 = the opening message, 2 = the reply,
# 3 = the reply to the reply. The default of 4 is chosen to allow the ONE exchange that has
# real value — "here is a bug" / "fixed, please retest" / "retested, it works" / "thanks,
# closing" — and nothing beyond it. Env-overridable; a test sets it to 2 to watch it bite.
MAX_CHAIN_DEPTH = max(1, int(os.environ.get("ASSISTANT_MAX_CHAIN_DEPTH", "4") or 4))

# How long a message stays "the thing you are replying to". Inside this window a message to a
# colleague continues the conversation and counts against the depth; outside it, the two start
# again from one. Both halves matter: without the window a runaway evades the bound by simply
# answering on a later beat, and without the reset two colleagues who talk regularly would
# eventually become permanently unable to reach each other — a bound that ends in deadlock is
# a bug wearing a bound's clothes.
CHAIN_WINDOW_MIN = max(5, int(os.environ.get("ASSISTANT_CHAIN_WINDOW_MIN", "120") or 120))

MAX_BODY = 8000
# How much of a referenced item travels inline in the wake task. `full_item` is deliberately
# fat and a bug with forty comments would otherwise crowd out the project context the
# recipient also needs, so it is rendered and clipped rather than dumped as raw JSON.
MAX_ITEM_CHARS = 6000

_COLS = ("id, project_id, from_assistant, from_name, to_assistant, to_name, body_md, "
         "thread_id, reply_to, depth, refs_item_id, beat_id, wake_beat_id, delivered_at, "
         "read_at, blocked, created_at")


def _row(r) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    for k in ("id", "from_assistant", "to_assistant", "thread_id", "reply_to",
              "refs_item_id", "beat_id", "wake_beat_id", "depth"):
        if d.get(k) is not None:
            d[k] = int(d[k])
    return d


# ---------------------------------------------------------------------------
# who you can talk to
# ---------------------------------------------------------------------------
async def roster(project_id: str, exclude_id: Optional[int] = None) -> list[dict]:
    """The assistants on this project — the address book.

    An assistant cannot message someone it does not know exists, so this rides along in
    `perceive()` on every beat. It is deliberately the whole roster including paused ones: a
    paused assistant still receives (pausing stops its *schedule*, it is not a prohibition on
    being addressed), exactly like the manual "Beat now" button.
    """
    rows = await pool().fetch(
        "SELECT id, name, role, description, status, capabilities "
        "FROM builderapps.assistants WHERE project_id=$1 ORDER BY id", project_id)
    out = []
    for r in rows:
        if exclude_id is not None and int(r["id"]) == int(exclude_id):
            continue
        caps = r["capabilities"]
        if isinstance(caps, str):
            try:
                caps = json.loads(caps)
            except Exception:  # noqa: BLE001
                caps = []
        out.append({"id": int(r["id"]), "name": r["name"] or "", "role": r["role"] or "",
                    "description": (r["description"] or "")[:200],
                    "status": r["status"] or "", "capabilities": caps or []})
    return out


async def resolve(project_id: str, ref: Any) -> Optional[dict]:
    """Turn whatever the model wrote into an assistant row: an id, a name, or a role.

    Tolerant on purpose. A model that has just been shown a roster saying `Developer` will
    write `"Developer"`, `"developer"`, `"the Developer"` or `4` depending on the day, and a
    DM tool that refuses three of those is a DM tool that gets abandoned after one failure.
    Matching is exact-id, then case-insensitive name, then role, then a contains-match — in
    that order, and a contains-match that is ambiguous is refused rather than guessed.
    """
    if ref is None:
        return None
    people = await roster(project_id)
    s = str(ref).strip()
    if not s:
        return None
    if s.isdigit():
        for p in people:
            if p["id"] == int(s):
                return p
        return None
    low = s.lower()
    for key in ("name", "role"):
        for p in people:
            if (p.get(key) or "").strip().lower() == low:
                return p
    hits = [p for p in people
            if low in (p.get("name") or "").lower() or low in (p.get("role") or "").lower()]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------
async def _auto_reply_to(project_id: str, sender_id: Optional[int], recipient_id: int,
                         beat_id: Optional[int]) -> Optional[dict]:
    """The message this send is answering, when the sender did not say.

    LOAD-BEARING FOR THE CHAIN BOUND. Depth only means anything if a reply is LINKED to what
    it replies to, and an LLM asked to remember `--reply 17` will not — so the link is
    inferred: a message to a colleague is a reply to the last thing that colleague said to
    you, if they said it recently.

    The first version only matched messages read by THIS beat, and the first real exchange
    walked straight through the hole. The Developer was woken by beat 26, but actually
    answered on beat 27 — so no message carried `wake_beat_id = 27`, the reply linked to
    nothing, and it was recorded as a brand-new conversation at depth 1. Every subsequent
    round would have done the same. The bound was in the code, was never going to fire, and
    nothing looked wrong: a runaway would have run for ever while the counter sat at 1.

    Hence the window rather than the beat. `CHAIN_WINDOW_MIN` is what separates "answering
    you" from "a new subject next week": within it, a message continues the conversation and
    counts against the depth; outside it, the pair start again from one. Without some such
    reset, two assistants that talk regularly would eventually be permanently unable to reach
    each other — a bound that ends in a deadlock is a bug, not a bound.
    """
    if sender_id is None:
        return None
    r = await pool().fetchrow(
        f"SELECT {_COLS} FROM builderapps.assistant_messages "
        "WHERE project_id=$1 AND to_assistant=$2 AND from_assistant=$3 AND blocked='' "
        "  AND created_at > now() - make_interval(mins => $4::int) "
        "ORDER BY created_at DESC LIMIT 1",
        project_id, int(sender_id), int(recipient_id), CHAIN_WINDOW_MIN)
    return _row(r)


async def send(project_id: str, *, sender: Optional[dict], to: dict, body_md: str,
               refs_item_id: Optional[int] = None, reply_to: Optional[int] = None,
               beat_id: Optional[int] = None, wake: bool = True) -> dict:
    """Persist a DM and (unless a bound says otherwise) wake the recipient.

    Order matters and is the durability contract: the row is written FIRST and the wake flag
    second. A crash between them leaves a stored, visible, replayable message and an
    unwoken recipient — recoverable, and obvious in the UI. The reverse order would leave a
    beat starting to read a message that does not exist.
    """
    from server import budget                      # local import: avoids a boot-time cycle

    body = (body_md or "").strip()
    if not body:
        return {"ok": False, "detail": "a message needs a body"}
    sender_id = int(sender["id"]) if sender else None
    to_id = int(to["id"])
    if sender_id is not None and sender_id == to_id:
        return {"ok": False, "detail": "an assistant cannot message itself"}

    parent = None
    if reply_to:
        parent = _row(await pool().fetchrow(
            f"SELECT {_COLS} FROM builderapps.assistant_messages WHERE id=$1 AND project_id=$2",
            int(reply_to), project_id))
        if not parent:
            return {"ok": False, "detail": f"no message #{reply_to} in this project"}
    else:
        parent = await _auto_reply_to(project_id, sender_id, to_id, beat_id)

    depth = (int(parent["depth"]) + 1) if parent else 1

    # A referenced item must exist and be in THIS project. Checked at send time so the sender
    # is told immediately — a recipient woken to read "see item #999" that does not exist
    # burns a whole beat discovering the id was wrong.
    if refs_item_id:
        it = await W.get_item(int(refs_item_id), project_id)
        if not it:
            return {"ok": False, "detail": f"no workspace item #{refs_item_id} in this project"}

    # ---- the bounds, in the order they should be reported ------------------
    blocked = ""
    if depth > MAX_CHAIN_DEPTH:
        blocked = "chain_depth"
    else:
        st = await budget.allows_beat(project_id)
        if st.stopped:
            blocked = "budget"

    sender_name = ""
    if sender:
        sender_name = sender.get("name") or sender.get("role") or f"assistant {sender_id}"
    row = _row(await pool().fetchrow(
        "INSERT INTO builderapps.assistant_messages"
        "(project_id, from_assistant, from_name, to_assistant, to_name, body_md, thread_id, "
        " reply_to, depth, refs_item_id, beat_id, blocked) "
        "VALUES ($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,$11) "
        f"RETURNING {_COLS}",
        project_id, sender_id, sender_name[:120], to_id,
        (to.get("name") or to.get("role") or f"assistant {to_id}")[:120],
        body[:MAX_BODY], (int(parent["id"]) if parent else None), depth,
        (int(refs_item_id) if refs_item_id else None),
        (int(beat_id) if beat_id else None), blocked))
    if not row:
        return {"ok": False, "detail": "the message could not be stored"}

    # A message that starts a thread IS the thread. Set in a second statement rather than a
    # CTE because the id only exists after the insert, and correctness here beats a round trip.
    thread_id = int(parent["thread_id"]) if parent and parent.get("thread_id") else None
    if not thread_id:
        thread_id = int(parent["id"]) if parent else int(row["id"])
    await pool().execute(
        "UPDATE builderapps.assistant_messages SET thread_id=$2 WHERE id=$1",
        int(row["id"]), thread_id)
    row["thread_id"] = thread_id

    result: dict = {"ok": True, "message": row, "blocked": blocked, "woke": False,
                    "depth": depth, "max_depth": MAX_CHAIN_DEPTH}

    if blocked == "chain_depth":
        result["detail"] = (
            f"stored, but NOT delivered: this conversation has reached its maximum chain "
            f"depth of {MAX_CHAIN_DEPTH}. It is visible in the thread and the other "
            "assistant can read it on its next ordinary beat, but no beat was started for "
            "it. If this still needs doing, put it on the workspace board instead — a board "
            "item is picked up without anyone having to be woken.")
    elif blocked == "budget":
        st = await budget.status(project_id)
        result["detail"] = (
            f"stored, but NOT delivered: this project has spent ${st.spent:.2f} of its "
            f"${st.limit:.2f} daily budget and assistant work is paused until midnight UTC.")
        result["budget"] = st.as_dict()
    elif wake:
        result["woke"] = await mark_wake_pending(to_id)
        result["detail"] = (
            f"delivered to {row['to_name']}" +
            (" — a beat is starting for it" if result["woke"]
             else " — it is already awake and will read this in the beat it is running"))
    else:
        result["detail"] = f"stored for {row['to_name']}"

    await publish_feed(project_id)
    return result


# ---------------------------------------------------------------------------
# ONE payload, two transports
# ---------------------------------------------------------------------------
async def live_feed(project_id: str, limit: int = 6) -> dict:
    """Everything the /builder left pane draws, in one dict.

    THE POINT: the poll and the WebSocket return the SAME body, built here. A socket that
    pushed a different, smaller, faster shape would be a second source of truth, and the one
    that drifts is always the one nobody reloads to check — you would see one thing live and
    another after refresh, with no way to know which was right. So the socket pushes exactly
    what the poll would have answered, and the client feeds both into the same merge.
    """
    from server import budget, store                # local: keeps the import graph acyclic
    feed = await A.recent_activity(project_id, limit)
    try:
        messages = (await store.get_messages(project_id))[-12:]
    except Exception:  # noqa: BLE001 — the feed must never fail on the extra
        messages = []
    try:
        dms = await project_feed(project_id, 40)
    except Exception:  # noqa: BLE001
        dms = []
    try:
        bud = (await budget.status(project_id)).as_dict()
    except Exception:  # noqa: BLE001
        bud = {}
    return {"beats": feed, "beating": any(b.get("status") == "running" for b in feed),
            "messages": messages, "dms": dms, "budget": bud,
            "max_chain_depth": MAX_CHAIN_DEPTH}


async def publish_feed(project_id: str, limit: int = 6) -> None:
    """Push the current feed to any browser watching this project. Best-effort, never fatal.

    Short-circuits when nobody is connected, which is the normal case: an assistant working
    at 3am must cost nothing extra just because a socket endpoint exists.
    """
    try:
        from server import ws_hub
        if not ws_hub.watchers(project_id):
            return
        ws_hub.publish(project_id, {"type": "feed", **(await live_feed(project_id, limit))})
    except Exception:  # noqa: BLE001
        logger.debug("ws publish failed for %s", project_id, exc_info=True)


# ---------------------------------------------------------------------------
# the wake flag
# ---------------------------------------------------------------------------
async def mark_wake_pending(assistant_id: int) -> bool:
    """Raise the wake flag. Returns True only if THIS call raised it.

    `WHERE wake_pending_at IS NULL` is the coalescing: a second DM to an assistant that is
    already wake-pending updates nothing and starts nothing, and both messages are read by
    the single beat that follows. Three colleagues messaging the Developer in the same minute
    cost one beat, not three.
    """
    res = await pool().execute(
        "UPDATE builderapps.assistants SET wake_pending_at=now() "
        "WHERE id=$1 AND wake_pending_at IS NULL", assistant_id)
    return res.endswith(" 1")


async def clear_wake(assistant_id: int) -> None:
    await pool().execute(
        "UPDATE builderapps.assistants SET wake_pending_at=NULL WHERE id=$1", assistant_id)


async def resweep_unread() -> int:
    """BOOT SWEEP for mail. Re-raise the wake flag for anyone holding unread messages.

    The window this closes: `claim_for_beat` stamps a message `delivered` and the beat marks
    it `read` only after it has actually reasoned over it. A control plane that died in
    between left the flag cleared, the message unread, and the sender waiting forever. Same
    bug class as a stranded run — and the same fix, which is why it lives next to the others
    and runs from the same `sweep_on_boot`.
    """
    res = await pool().execute(
        "UPDATE builderapps.assistants SET wake_pending_at=now() "
        "WHERE wake_pending_at IS NULL AND id IN ("
        "  SELECT DISTINCT to_assistant FROM builderapps.assistant_messages "
        "  WHERE read_at IS NULL AND blocked='')")
    try:
        return int(str(res).rsplit(" ", 1)[-1])
    except Exception:  # noqa: BLE001
        return 0


async def wake_due(limit: int = 20) -> list[dict]:
    """Assistants that have been messaged and are not currently beating.

    Same shape and the same guard as `assistants.due_now`, so the scheduler treats a woken
    assistant exactly like a due one — including the atomic `claim`. Note what is NOT here:
    `status='active'`. A paused assistant that is sent a direct message is woken, for the
    same reason the manual beat button ignores `paused`: pausing stops the clock, it does not
    make a colleague unreachable.
    """
    rows = await pool().fetch(
        "SELECT id, project_id, wake_pending_at FROM builderapps.assistants "
        "WHERE wake_pending_at IS NOT NULL AND (beat_owner='' OR beat_owner IS NULL) "
        "ORDER BY wake_pending_at LIMIT $1", max(1, min(int(limit), 100)))
    return [{"id": int(r["id"]), "project_id": str(r["project_id"]),
             "wake_pending_at": r["wake_pending_at"]} for r in rows]


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
async def inbox(assistant_id: int, *, unread_only: bool = True, limit: int = 25) -> list[dict]:
    where = "to_assistant=$1 AND blocked=''"
    if unread_only:
        where += " AND read_at IS NULL"
    rows = await pool().fetch(
        f"SELECT {_COLS} FROM builderapps.assistant_messages WHERE {where} "
        "ORDER BY created_at DESC LIMIT $2", int(assistant_id), max(1, min(int(limit), 100)))
    return [_row(r) for r in reversed(rows)]


async def thread(thread_id: int, project_id: str, limit: int = 100) -> list[dict]:
    rows = await pool().fetch(
        f"SELECT {_COLS} FROM builderapps.assistant_messages "
        "WHERE thread_id=$1 AND project_id=$2 ORDER BY created_at LIMIT $3",
        int(thread_id), project_id, max(1, min(int(limit), 200)))
    return [_row(r) for r in rows]


async def project_feed(project_id: str, limit: int = 40) -> list[dict]:
    """Every DM on this project, oldest-first — the /builder left pane's restore path.

    The same rows the live socket pushes, from the same table, so what you saw live and what
    you see after a hard refresh cannot disagree. That equivalence is the reason the socket
    is a view and not a channel.
    """
    rows = await pool().fetch(
        f"SELECT {_COLS} FROM builderapps.assistant_messages WHERE project_id=$1 "
        "ORDER BY created_at DESC LIMIT $2", project_id, max(1, min(int(limit), 200)))
    return [_row(r) for r in reversed(rows)]


async def claim_for_beat(assistant_id: int, beat_id: int) -> list[dict]:
    """Hand this beat everything unread in its inbox, and mark it delivered.

    One statement, so a beat cannot read a message that a concurrent beat also reads: the
    UPDATE ... RETURNING is the claim. The rows come back stamped with this beat, which is
    what `_auto_reply_to` later uses to know that a message sent during this beat is a REPLY
    (and therefore one step deeper in the chain).
    """
    rows = await pool().fetch(
        "UPDATE builderapps.assistant_messages SET delivered_at=now(), wake_beat_id=$2 "
        "WHERE id IN (SELECT id FROM builderapps.assistant_messages "
        "             WHERE to_assistant=$1 AND read_at IS NULL AND blocked='' "
        "             ORDER BY created_at LIMIT 25) "
        f"RETURNING {_COLS}", int(assistant_id), int(beat_id))
    return [_row(r) for r in sorted(rows, key=lambda r: r["created_at"])]


async def mark_read(ids: list[int], beat_id: Optional[int] = None) -> int:
    if not ids:
        return 0
    res = await pool().execute(
        "UPDATE builderapps.assistant_messages SET read_at=now() "
        "WHERE id = ANY($1::bigint[]) AND read_at IS NULL", [int(i) for i in ids])
    try:
        return int(str(res).rsplit(" ", 1)[-1])
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# turning a DM into a beat's task
# ---------------------------------------------------------------------------
def _render_item(item: dict) -> str:
    """The referenced workspace item, as text a model can act on.

    Rendered, not dumped: `full_item` returns fields + every comment + the whole event trail
    + links, and pasting that JSON into a prompt spends most of the window on `created_by`
    strings. What a recipient needs is the report — what it is, what was said, what state it
    is in.
    """
    out = [f"#{item.get('id')} [{item.get('kind')}] {item.get('title')}",
           f"status: {item.get('status')}"
           + (f"   assignee: {item['assignee']}" if item.get("assignee") else "")
           + f"   filed by: {item.get('created_by_name') or item.get('created_by')}"]
    body = (item.get("body_md") or "").strip()
    if body:
        out += ["", body]
    comments = item.get("comments") or []
    if comments:
        out += ["", f"--- comments ({len(comments)}) ---"]
        for c in comments[-12:]:
            out.append(f"  {c.get('author_name')}: {(c.get('body_md') or '').strip()}")
    links = item.get("links") or []
    if links:
        out += ["", "--- linked ---"]
        for l in links[:10]:
            out.append(f"  {l.get('link_rel')} #{l.get('other_id')} "
                       f"[{l.get('other_kind')}/{l.get('other_status')}] {l.get('other_title')}")
    events = item.get("events") or []
    if events:
        out += ["", "--- history ---"]
        for e in events[-8:]:
            bit = ""
            if e.get("from_val") or e.get("to_val"):
                bit = f" {e.get('from_val') or '-'} -> {e.get('to_val') or '-'}"
            out.append(f"  {e.get('actor_name')} {e.get('verb')}{bit}")
    return "\n".join(out)[:MAX_ITEM_CHARS]


async def wake_task(project_id: str, messages: list[dict]) -> str:
    """Render the inbox as the beat's task.

    THE REFERENCED ITEM TRAVELS INLINE. "I found a bug, id #42, check it out" is only a real
    hand-off if #42 arrives with it; a recipient that has to go and fetch the item spends a
    reasoning round discovering what it was asked about, and a recipient whose model forgets
    to fetch it acts on a one-line summary of a bug report. So the whole item is here, in the
    task, before the first thought.
    """
    parts = []
    for m in messages:
        head = f"{m.get('from_name') or 'an assistant'} -> {m.get('to_name') or 'you'}"
        parts.append(f"--- message #{m['id']} from {head} ---\n{m.get('body_md') or ''}")
        if m.get("refs_item_id"):
            try:
                item = await W.full_item(int(m["refs_item_id"]), project_id)
            except Exception:  # noqa: BLE001 — a tracker hiccup must not lose the message
                item = None
            if item:
                parts.append("\nThe workspace item it refers to, in full:\n\n"
                             + _render_item(item))
            else:
                parts.append(f"\n(it references workspace item #{m['refs_item_id']}, which "
                             "no longer exists)")
        parts.append("")
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# "what happened while I was away"
# ---------------------------------------------------------------------------
async def seen(project_id: str, user_id: str) -> None:
    await pool().execute(
        "INSERT INTO builderapps.project_seen(project_id, user_id, seen_at) "
        "VALUES ($1,$2,now()) ON CONFLICT (project_id, user_id) "
        "DO UPDATE SET seen_at=now()", project_id, user_id)


async def unread_rollup(user_id: str, project_ids: list[str]) -> dict:
    """Per project: how much has happened since the owner last looked.

    One query for the whole Apps list. Counts DMs and finished beats, because those are the
    two things that mean "an assistant did something" — an activity line is a step inside a
    beat, and counting steps would make one beat read as fourteen events.
    """
    if not project_ids:
        return {}
    ids = list(project_ids)
    rows = await pool().fetch(
        "WITH seen AS ("
        "  SELECT p.pid AS project_id,"
        "         coalesce(s.seen_at, to_timestamp(0)) AS seen_at"
        "  FROM unnest($2::text[]) AS p(pid)"
        "  LEFT JOIN builderapps.project_seen s ON s.project_id = p.pid AND s.user_id = $1)"
        " SELECT seen.project_id, seen.seen_at,"
        "        (SELECT count(*) FROM builderapps.assistant_messages m"
        "          WHERE m.project_id = seen.project_id AND m.created_at > seen.seen_at)"
        "          ::int AS dms,"
        "        (SELECT count(*) FROM builderapps.assistant_beats b"
        "          WHERE b.project_id = seen.project_id AND b.status <> 'running'"
        "            AND coalesce(b.finished_at, b.ts) > seen.seen_at)::int AS beats"
        " FROM seen", user_id, ids)
    out = {}
    for r in rows:
        dms, beats = int(r["dms"] or 0), int(r["beats"] or 0)
        out[str(r["project_id"])] = {"dms": dms, "beats": beats, "total": dms + beats,
                                     "seen_at": r["seen_at"]}
    return out
