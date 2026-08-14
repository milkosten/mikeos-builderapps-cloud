"""The per-project WORKSPACE — data access for the shared work-tracker (phase 32).

⚠️ NOT to be confused with `server/workspace.py`, which is the git CHECKOUT manager (the
directory a pipeline builds in). This module is the *work* tracker: items, comments, links,
events and the per-project `workspace-api-key`. Same word, two different things; the module
names are the only place they meet.

What it is, in one line: **one table of `items` that the build pipeline, every AI assistant
and the human all write to, so "what has actually been built / what is broken / what do we
know" is a queryable thing rather than a line in a log.**

Three rules this module exists to keep:

* **The taxonomy is not hardcoded.** `kind` and `status` are free text (see 012). Everything
  here passes them through — `DEFAULT_KINDS` / `DEFAULT_STATUSES` below are hints for the UI
  and the tool's `--help`, never validation. An assistant that invents `kind=risk` gets a
  risk item, not a 422.
* **Every mutation writes an event.** Create, status, assignee, kind, edit, comment, link.
  The audit trail is the product, not a debugging aid.
* **A project id is a tenant boundary.** Every query is `WHERE project_id=$1`. There is no
  read path in this file that can return another project's row.

Parameterized SQL only; writes verified (`RETURNING` + a None check) — never trust an
implicit success.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any, Optional

from server import crypto
from server.db import pool

logger = logging.getLogger(__name__)

# Conventions, NOT constraints. The UI groups by these and the `ws` tool lists them in its
# help; the API and the schema accept anything.
DEFAULT_KINDS = ["feature", "bug", "task", "testcase", "doc", "kb"]
DEFAULT_STATUSES = ["open", "in_progress", "blocked", "done", "rejected"]
# Which statuses mean "no longer being worked on" — used only to stamp `closed_at` and to
# grey a row in the UI. A status outside this list is simply open-ended work.
CLOSED_STATUSES = {"done", "rejected", "closed", "cancelled", "wontfix"}

MAX_TITLE = 300
MAX_BODY = 40000
MAX_COMMENT = 20000
MAX_LIST = 500

_COLS = ("id, project_id, kind, title, body_md, status, priority, assignee, "
         "created_by, created_by_kind, created_by_name, ext_key, "
         "created_at, updated_at, closed_at")


# ---------------------------------------------------------------------------
# the actor: who is doing this
# ---------------------------------------------------------------------------
class Actor:
    """Who performed an action, as the triple every table stores.

    `ident` is the stable machine id (`user:<uid>`, `assistant:<id>`, `pipeline`), `kind` is
    human | assistant | pipeline, `name` is what a person reads. Denormalised onto every row
    on purpose: an item outlives the assistant that filed it, and "who marked this done?"
    must still answer six months later.
    """

    __slots__ = ("ident", "kind", "name")

    def __init__(self, ident: str, kind: str = "human", name: str = ""):
        self.ident = (ident or "")[:120]
        self.kind = (kind or "human")[:20]
        self.name = (name or "")[:120]

    @classmethod
    def user(cls, user_id: str, name: str = "") -> "Actor":
        return cls(f"user:{user_id}", "human", name or "you")

    @classmethod
    def assistant(cls, assistant_id: Any, name: str = "") -> "Actor":
        return cls(f"assistant:{assistant_id}", "assistant", name or f"assistant {assistant_id}")

    @classmethod
    def pipeline(cls, name: str = "build pipeline") -> "Actor":
        return cls("pipeline", "pipeline", name)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"Actor({self.ident!r}, {self.kind!r}, {self.name!r})"


# ---------------------------------------------------------------------------
# the per-project key
# ---------------------------------------------------------------------------
def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_key() -> tuple[str, str, str]:
    """-> (plaintext, encrypted, sha256). Prefix `wsk_` so a leaked string is identifiable."""
    tok = "wsk_" + secrets.token_urlsafe(32)
    return tok, crypto.encrypt(tok), _sha(tok)


async def ensure_key(project_id: str) -> str:
    """The project's `workspace-api-key`, minted on first ask. Returns the PLAINTEXT.

    Called by the create pipeline (so a new project has one from birth) and lazily whenever
    a beat container is launched — which is what stops the projects that already existed
    before phase 32 from being stranded without a key.

    Concurrency: two schedulers can race here, so the INSERT is `ON CONFLICT DO NOTHING` and
    the winner is then read back. Whoever loses simply uses the key that is already there.
    """
    row = await pool().fetchrow(
        "SELECT key_enc FROM builderapps.workspace_keys WHERE project_id=$1", project_id)
    if row and row["key_enc"]:
        return crypto.decrypt(row["key_enc"])
    plain, enc, sha = mint_key()
    await pool().execute(
        "INSERT INTO builderapps.workspace_keys(project_id, key_enc, key_sha) "
        "VALUES ($1,$2,$3) ON CONFLICT (project_id) DO NOTHING",
        project_id, enc, sha)
    row = await pool().fetchrow(
        "SELECT key_enc FROM builderapps.workspace_keys WHERE project_id=$1", project_id)
    if not row or not row["key_enc"]:
        raise RuntimeError(f"could not provision a workspace key for {project_id}")
    return crypto.decrypt(row["key_enc"])


async def project_for_key(key: str) -> Optional[str]:
    """Resolve a `workspace-api-key` to the ONE project it is scoped to.

    Looked up by hash — the plaintext is never compared against a decrypted column, and an
    empty/short key can never match. A key that resolves to project A and is used against
    project B does not get a 403: the route answers 404, because another tenant's items must
    be indistinguishable from items that do not exist.
    """
    if not key or not key.startswith("wsk_"):
        return None
    sha = _sha(key)
    pid = await pool().fetchval(
        "SELECT project_id FROM builderapps.workspace_keys WHERE key_sha=$1", sha)
    if pid:
        # Best-effort freshness stamp — and DELIBERATELY THROTTLED. This function runs on
        # every single workspace request, and `workspace_keys` has exactly one row per
        # project, so an unconditional UPDATE turns the tenancy check (a read) into a write
        # on the same row from every beat and every tab poll: dead tuples on a one-row table,
        # lock contention between concurrent beats, and a check that can never be served
        # anywhere but the primary. Hourly resolution is all "when was this key last used?"
        # ever needed, so the predicate does the throttling in the database.
        try:
            await pool().execute(
                "UPDATE builderapps.workspace_keys SET last_used_at=now() "
                "WHERE key_sha=$1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 hour')",
                sha)
        except Exception:  # noqa: BLE001 — a stamp must never fail a request
            logger.debug("workspace key last_used_at stamp failed", exc_info=True)
    return str(pid) if pid else None


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------
def _row(r) -> dict:
    if r is None:
        return {}
    d = dict(r)
    for k in ("created_at", "updated_at", "closed_at", "ts"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()      # ISO-8601 out of the API, always (house rule)
    return d


def _clip(v: Any, n: int) -> str:
    return str(v or "")[:n]


# ---------------------------------------------------------------------------
# events — every mutation records one
# ---------------------------------------------------------------------------
async def add_event(item_id: int, project_id: str, actor: Actor, verb: str, *,
                    field: str = "", from_val: str = "", to_val: str = "",
                    note: str = "") -> None:
    await pool().execute(
        "INSERT INTO builderapps.workspace_events "
        "(item_id, project_id, actor, actor_kind, actor_name, verb, field, from_val, to_val, note) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
        item_id, project_id, actor.ident, actor.kind, actor.name,
        _clip(verb, 40), _clip(field, 40), _clip(from_val, 400), _clip(to_val, 400),
        _clip(note, 2000))


async def events_for(item_id: int, project_id: str, limit: int = 200) -> list[dict]:
    rows = await pool().fetch(
        "SELECT id, item_id, actor, actor_kind, actor_name, verb, field, from_val, to_val, "
        "       note, ts "
        "  FROM builderapps.workspace_events "
        " WHERE item_id=$1 AND project_id=$2 ORDER BY ts, id LIMIT $3",
        item_id, project_id, min(int(limit or 200), 500))
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------
async def create_item(project_id: str, actor: Actor, *, kind: str = "task", title: str = "",
                      body_md: str = "", status: str = "open", priority: str = "normal",
                      assignee: str = "", ext_key: Optional[str] = None) -> dict:
    """Create one item + its `created` event. `kind`/`status` are stored VERBATIM."""
    row = await pool().fetchrow(
        "INSERT INTO builderapps.workspace_items "
        "(project_id, kind, title, body_md, status, priority, assignee, "
        " created_by, created_by_kind, created_by_name, ext_key, closed_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, "
        "        CASE WHEN $5 = ANY($12::text[]) THEN now() ELSE NULL END) "
        f"RETURNING {_COLS}",
        project_id, _clip(kind, 40) or "task", _clip(title, MAX_TITLE),
        _clip(body_md, MAX_BODY), _clip(status, 40) or "open", _clip(priority, 20) or "normal",
        _clip(assignee, 120), actor.ident, actor.kind, actor.name,
        (ext_key or None), list(CLOSED_STATUSES))
    if not row:
        raise RuntimeError("create_item returned no row")   # never trust an implicit success
    item = _row(row)
    await add_event(int(item["id"]), project_id, actor, "created",
                    field="kind", to_val=item["kind"], note=item["title"][:200])
    return item


async def upsert_by_ext_key(project_id: str, ext_key: str, actor: Actor, *, kind: str,
                            title: str, body_md: str = "", status: str = "open") -> dict:
    """Create-or-fetch a MACHINE-OWNED item, keyed by `ext_key` (e.g. `build_03`).

    Idempotent by construction: a resumed pipeline run re-announces the same backlog and must
    not produce a second copy of every feature. If the row already exists the title/body are
    refreshed (the plan may have been re-parsed) but the STATUS IS LEFT ALONE — the pipeline
    owns transitions through `set_status`, and clobbering a `done` back to `open` on a resume
    would be a lie about what happened.
    """
    existing = await pool().fetchrow(
        f"SELECT {_COLS} FROM builderapps.workspace_items "
        "WHERE project_id=$1 AND ext_key=$2", project_id, ext_key)
    if existing:
        row = await pool().fetchrow(
            "UPDATE builderapps.workspace_items SET title=$3, body_md=$4, updated_at=now() "
            "WHERE project_id=$1 AND ext_key=$2 "
            f"RETURNING {_COLS}",
            project_id, ext_key, _clip(title, MAX_TITLE), _clip(body_md, MAX_BODY))
        return _row(row)
    return await create_item(project_id, actor, kind=kind, title=title, body_md=body_md,
                             status=status, ext_key=ext_key)


async def get_item(item_id: int, project_id: str) -> Optional[dict]:
    r = await pool().fetchrow(
        f"SELECT {_COLS} FROM builderapps.workspace_items WHERE id=$1 AND project_id=$2",
        item_id, project_id)
    return _row(r) if r else None


async def latest_item_by(project_id: str, created_by: str,
                         within_minutes: int = 15) -> Optional[dict]:
    """The newest item this actor filed, recently. Exists for ONE specific, guaranteed bug.

    An assistant reasons ONCE per beat and emits up to two actions in that single reply. The
    most valuable pair by far is "file the bug, then tell the Developer about it" — and it is
    impossible to write correctly, because the id of the item does not exist yet when the
    message naming it is composed. Observed on the very first real run: the Tester filed #21
    and messaged "see item #4".

    Telling the model to spend two beats instead is the wrong answer — a beat costs money and
    the hand-off is the entire point of the feature. So a DM whose `refs_item_id` is not in
    this project falls back to whatever this same assistant just filed, and SAYS SO in the
    action result. Never silent: a silent substitution would attach the wrong report to the
    wrong message and read as if it had worked.

    Bounded by time so a stale or genuinely wrong id can never attach a week-old item.
    """
    r = await pool().fetchrow(
        f"SELECT {_COLS} FROM builderapps.workspace_items "
        "WHERE project_id=$1 AND created_by=$2 "
        "  AND created_at > now() - make_interval(mins => $3::int) "
        "ORDER BY id DESC LIMIT 1",
        project_id, created_by, max(1, int(within_minutes)))
    return _row(r) if r else None


async def get_by_ext_key(project_id: str, ext_key: str) -> Optional[dict]:
    r = await pool().fetchrow(
        f"SELECT {_COLS} FROM builderapps.workspace_items "
        "WHERE project_id=$1 AND ext_key=$2", project_id, ext_key)
    return _row(r) if r else None


def _like(term: str) -> str:
    """Escape a user's search text so LIKE metacharacters are LITERAL.

    Without this, `ws search rate_limit` treats `_` as "any character" and quietly returns
    `rate-limit` and `rateXlimit` too — while making it impossible to search for a real
    identifier like `on_click` or `body_md`. A query of a bare `%` becomes "match every row",
    i.e. a search that silently turns into a full board dump. And a trailing backslash
    escapes the wildcard we append, so the pattern matches nothing. All three fail as WRONG
    RESULTS with HTTP 200 — never as an error — which is the worst way for a search to break.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_items(project_id: str, *, kind: str = "", status: str = "",
                     assignee: str = "", q: str = "", limit: int = 200,
                     offset: int = 0, open_only: bool = False,
                     newest_first: bool = False) -> list[dict]:
    """Filtered list. Every filter is optional and every one is a bound parameter.

    `open_only` excludes the closed statuses IN THE QUERY rather than in the caller. That
    distinction is not cosmetic: filtering after a `LIMIT` means the limit is spent on
    history, so once a project has more finished items than the cap, "the live board" comes
    back empty and whoever asked for it goes blind with no error to notice.
    """
    where = ["project_id = $1"]
    args: list[Any] = [project_id]
    if kind:
        args.append(kind[:40]); where.append(f"kind = ${len(args)}")
    if status:
        args.append(status[:40]); where.append(f"status = ${len(args)}")
    if open_only:
        args.append(list(CLOSED_STATUSES)); where.append(f"NOT (status = ANY(${len(args)}::text[]))")
    if assignee:
        args.append(assignee[:120]); where.append(f"assignee = ${len(args)}")
    if q:
        args.append(f"%{_like(q[:200])}%")
        where.append(f"(title ILIKE ${len(args)} ESCAPE '\\' "
                     f"OR body_md ILIKE ${len(args)} ESCAPE '\\')")
    args.append(min(int(limit or 200), MAX_LIST))
    args.append(max(int(offset or 0), 0))
    order = "updated_at DESC, id DESC" if newest_first else "id"
    rows = await pool().fetch(
        f"SELECT {_COLS} FROM builderapps.workspace_items WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT ${len(args) - 1} OFFSET ${len(args)}", *args)
    return [_row(r) for r in rows]


async def counts(project_id: str) -> dict:
    """`{by_kind:{feature:12,…}, by_status:{done:9,…}, total:N}` — what the tab header shows
    without pulling every row."""
    rows = await pool().fetch(
        "SELECT kind, status, count(*) AS n FROM builderapps.workspace_items "
        "WHERE project_id=$1 GROUP BY kind, status", project_id)
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total = 0
    for r in rows:
        n = int(r["n"])
        total += n
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + n
        by_status[r["status"]] = by_status.get(r["status"], 0) + n
    return {"by_kind": by_kind, "by_status": by_status, "total": total}


async def update_item(item_id: int, project_id: str, actor: Actor, **fields) -> Optional[dict]:
    """PATCH the editable columns, writing ONE event per field that genuinely changed.

    A PATCH is partial: `None` means "not supplied". A field set to the value it already has
    is not an event — a trail full of `status: done -> done` teaches people to stop reading
    it. `closed_at` follows `status` automatically.
    """
    before = await get_item(item_id, project_id)
    if not before:
        return None

    allowed = {"kind": 40, "title": MAX_TITLE, "body_md": MAX_BODY, "status": 40,
               "priority": 20, "assignee": 120}
    sets, args, changed = [], [], []
    for col, cap in allowed.items():
        if fields.get(col) is None:
            continue
        new = _clip(fields[col], cap)
        if new == (before.get(col) or ""):
            continue
        args.append(new)
        sets.append(f"{col}=${len(args) + 2}")
        changed.append((col, str(before.get(col) or ""), new))
    if not sets:
        return before

    if any(c[0] == "status" for c in changed):
        new_status = next(c[2] for c in changed if c[0] == "status")
        sets.append("closed_at=" + ("now()" if new_status in CLOSED_STATUSES else "NULL"))

    row = await pool().fetchrow(
        f"UPDATE builderapps.workspace_items SET {', '.join(sets)}, updated_at=now() "
        f"WHERE id=$1 AND project_id=$2 RETURNING {_COLS}",
        item_id, project_id, *args)
    if not row:
        return None
    for col, old, new in changed:
        # `edited` for prose (a full title/body diff in the trail would be unreadable);
        # the field name itself as the verb for the small, meaningful ones.
        verb = col if col in ("status", "assignee", "kind", "priority") else "edited"
        await add_event(item_id, project_id, actor, verb, field=col,
                        from_val=old if verb != "edited" else "",
                        to_val=new if verb != "edited" else "")
    return _row(row)


async def set_status(project_id: str, ext_key: str, actor: Actor, status: str,
                     note: str = "") -> Optional[dict]:
    """Move a machine-owned item (`ext_key`) to a status, recording the reason.

    This is the pipeline's whole interface to the tracker: `build_03` goes `open ->
    in_progress -> done`, or `-> blocked` carrying the skip reason. `note` is what makes a
    blocked feature actionable — "failed twice (…); reverted to last good commit" is the
    difference between a visible open item and a shrug.
    """
    item = await get_by_ext_key(project_id, ext_key)
    if not item:
        return None

    # The reason belongs in the BODY, not only in the trail — an item you have to open the
    # history to understand is an item nobody understands. Composed here and passed to the
    # single `update_item` call rather than written by a second UPDATE afterwards: the
    # pipeline calls this three or four times per feature and ~50 times per build, and the
    # old shape cost a re-SELECT and a second write every time.
    fields: dict[str, Any] = {"status": status}
    if note:
        body = item.get("body_md") or ""
        line = ("\n\n**Blocked:** " + note[:1500]) if status == "blocked" \
            else ("\n\n_" + note[:1500] + "_")
        # Idempotent on a resumed run: compare the EXACT line we are about to append, not a
        # 60-character prefix of the note. The prefix test matched whenever two notes shared
        # an opening — e.g. a retry note and the final `blocked` note — and silently dropped
        # the one that actually explained the outcome.
        if line not in body:
            fields["body_md"] = (body + line)[:MAX_BODY]

    updated = await update_item(int(item["id"]), project_id, actor, **fields)
    if note:
        await add_event(int(item["id"]), project_id, actor, "note", note=note)
    return updated


# ---------------------------------------------------------------------------
# comments
# ---------------------------------------------------------------------------
async def add_comment(item_id: int, project_id: str, actor: Actor, body_md: str) -> Optional[dict]:
    if not await get_item(item_id, project_id):
        return None
    row = await pool().fetchrow(
        "INSERT INTO builderapps.workspace_comments "
        "(item_id, project_id, author, author_kind, author_name, body_md) "
        "VALUES ($1,$2,$3,$4,$5,$6) "
        "RETURNING id, item_id, author, author_kind, author_name, body_md, created_at",
        item_id, project_id, actor.ident, actor.kind, actor.name, _clip(body_md, MAX_COMMENT))
    if not row:
        raise RuntimeError("add_comment returned no row")
    await pool().execute(
        "UPDATE builderapps.workspace_items SET updated_at=now() WHERE id=$1", item_id)
    await add_event(item_id, project_id, actor, "commented", note=_clip(body_md, 300))
    return _row(row)


async def comments_for(item_id: int, project_id: str, limit: int = 200) -> list[dict]:
    rows = await pool().fetch(
        "SELECT id, item_id, author, author_kind, author_name, body_md, created_at "
        "  FROM builderapps.workspace_comments WHERE item_id=$1 AND project_id=$2 "
        " ORDER BY created_at, id LIMIT $3",
        item_id, project_id, min(int(limit or 200), 500))
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------
async def add_link(project_id: str, from_item: int, to_item: int, link_rel: str,
                   actor: Actor) -> Optional[dict]:
    """Link two items. BOTH ends are checked against this project — a link is the one place
    an id from elsewhere could otherwise sneak in."""
    if from_item == to_item:
        return None
    if not await get_item(from_item, project_id) or not await get_item(to_item, project_id):
        return None
    row = await pool().fetchrow(
        "INSERT INTO builderapps.workspace_links(project_id, from_item, to_item, link_rel, created_by) "
        "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (from_item, to_item, link_rel) DO NOTHING "
        "RETURNING id, project_id, from_item, to_item, link_rel, created_at",
        project_id, from_item, to_item, _clip(link_rel, 40) or "relates", actor.ident)
    if row:
        await add_event(from_item, project_id, actor, "linked",
                        field=_clip(link_rel, 40) or "relates", to_val=str(to_item))
    return _row(row) if row else {"from_item": from_item, "to_item": to_item,
                                  "link_rel": link_rel, "duplicate": True}


async def links_for(item_id: int, project_id: str) -> list[dict]:
    """Both directions, with the other end's title resolved — a link that shows only an id
    forces a second call to be readable, and this payload is what a DM recipient acts on."""
    rows = await pool().fetch(
        "SELECT l.id, l.from_item, l.to_item, l.link_rel, l.created_at, "
        "       CASE WHEN l.from_item=$1 THEN 'out' ELSE 'in' END AS direction, "
        "       i.id AS other_id, i.title AS other_title, i.kind AS other_kind, "
        "       i.status AS other_status "
        "  FROM builderapps.workspace_links l "
        "  JOIN builderapps.workspace_items i "
        "    ON i.id = CASE WHEN l.from_item=$1 THEN l.to_item ELSE l.from_item END "
        " WHERE l.project_id=$2 AND ($1 IN (l.from_item, l.to_item)) "
        " ORDER BY l.created_at LIMIT 200",
        item_id, project_id)
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# the full item (what GET /items/{id} answers, and what a DM can carry)
# ---------------------------------------------------------------------------
async def full_item(item_id: int, project_id: str) -> Optional[dict]:
    """Item + comments + events + links in ONE payload.

    Deliberately fat. Phase 33 lets one assistant send another a DM carrying `refs_item_id`;
    the recipient wakes up in a fresh container with nothing but that id, and it must be able
    to ACT on the item without three more round trips. So the single GET is the whole story:
    what it is, what was said about it, what has happened to it, and what it is connected to.
    """
    item = await get_item(item_id, project_id)
    if not item:
        return None
    item["comments"] = await comments_for(item_id, project_id)
    item["events"] = await events_for(item_id, project_id)
    item["links"] = await links_for(item_id, project_id)
    return item
