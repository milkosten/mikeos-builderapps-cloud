"""Per-project daily spend, and the HARD STOP that phase 33 made necessary.

Mike's number: **$10/day per project.** Not an alert — a stop. When a project has spent it,
assistant work PAUSES, the owner is told plainly in the thread they already read, and the
figure is visible in Usage.

Why this arrived with messaging and not before: until assistants could wake each other, the
only things that started a beat were a clock the owner set and a button the owner pressed —
both self-limiting. A DM is neither. Two assistants being conscientious at each other
("thanks, I'll retest" / "great, let me know") is a plausible, polite, *unbounded* loop, and
a Developer beat costs $0.20-1.59. Chain depth bounds ONE conversation; this bounds the day.

Three properties that matter more than the number:

* **It is computed from `llm_usage`, the same rows the Usage tab shows.** There is no second
  counter to drift: if the tab says $9.90 the stop is 10c away, and a beat whose cost was
  never recorded also never counted — which is the safe direction to be wrong in.
* **It gates BEATS, not tokens.** The check happens before a container is started, so a beat
  either runs with its whole budget or does not start. Killing a beat halfway leaves a
  half-applied change, which is worse than not starting and much worse than $1.59.
* **The stop is announced exactly once per project per day.** A hard stop that reposts
  itself into the thread every 60 seconds is indistinguishable from a broken app.
"""
import datetime as _dt
import logging
import os
from typing import Optional

from server.db import pool

logger = logging.getLogger(__name__)

# The ceiling, per project, per UTC day. Env-overridable so a test can set it to $0.001 and
# watch the stop bite without spending anything (that is exactly how it is verified).
DAILY_USD = float(os.environ.get("PROJECT_DAILY_USD", "10") or 10)

# Kinds of beat the stop applies to. A stop that let the schedule through would not be a stop.
# It applies to EVERY trigger, including the owner's own "Beat now": the money is the owner's
# either way, and a limit with a manual bypass is a limit that is bypassed.


async def spend_today(project_id: str) -> float:
    """USD this project has spent since midnight UTC, from `llm_usage`."""
    v = await pool().fetchval(
        "SELECT coalesce(sum(cost_usd),0)::float8 FROM builderapps.llm_usage "
        "WHERE project_id=$1 AND ts >= date_trunc('day', now() at time zone 'utc')",
        project_id)
    return round(float(v or 0.0), 6)


class Status:
    """The answer to 'may this project start a beat right now?', with the numbers attached.

    Carries the numbers rather than a bare bool because every caller has to SHOW them: the
    API returns them, the thread message quotes them, the UI banner renders them. A bool
    would mean three places re-deriving the same figure.
    """

    __slots__ = ("spent", "limit", "stopped")

    def __init__(self, spent: float, limit: float):
        self.spent = round(float(spent), 6)
        self.limit = round(float(limit), 6)
        self.stopped = self.limit > 0 and self.spent >= self.limit

    @property
    def remaining(self) -> float:
        return round(max(0.0, self.limit - self.spent), 6)

    def as_dict(self) -> dict:
        return {"spent_today_usd": self.spent, "daily_limit_usd": self.limit,
                "remaining_usd": self.remaining, "stopped": self.stopped}

    def message(self) -> str:
        return (
            f"**Assistant work is paused for today.** This project has spent "
            f"${self.spent:.2f} of its ${self.limit:.2f} daily budget, so no further "
            "assistant beats will start until midnight UTC. Nothing is broken and nothing "
            "is lost — scheduled beats, direct asks and messages between assistants are all "
            "held, not dropped. The breakdown is in the Usage tab.")


async def status(project_id: str) -> Status:
    return Status(await spend_today(project_id), DAILY_USD)


async def allows_beat(project_id: str) -> Status:
    """The gate. Call this immediately before claiming an assistant for a beat.

    Deliberately returns the Status rather than raising: the three callers (the scheduler,
    the owner's `POST /beat`, and a DM wake) each have a different way to tell somebody, and
    an exception would flatten them into one.
    """
    return await status(project_id)


# ---------------------------------------------------------------------------
# telling the owner — once
# ---------------------------------------------------------------------------
async def announce_once(project_id: str, st: Status) -> bool:
    """Put the stop in the project thread, at most once per project per UTC day.

    Returns True if it wrote one. The dedupe key is the day, read out of the thread itself,
    so it survives a control-plane restart without another table: the same durability rule as
    everywhere else here, achieved by looking at what is already written rather than by
    remembering something in RAM.
    """
    from server import store                       # local: avoid an import cycle at boot
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    marker = f"budget-stop:{day}"
    try:
        thread = await store.get_raw_thread(project_id)
    except Exception:  # noqa: BLE001 — never let the announcement break the stop itself
        thread = []
    for m in reversed(thread[-40:]):
        meta = (m or {}).get("meta") or {}
        if isinstance(meta, dict) and meta.get("kind") == marker:
            return False
    try:
        await store.append_message(project_id, "assistant", st.message(),
                                   meta={"kind": marker, "budget": st.as_dict()})
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not announce the budget stop for %s", project_id, exc_info=True)
        return False


async def stopped_projects(project_ids: list[str]) -> dict:
    """Which of these projects are halted, in ONE query.

    For the Apps list: N projects must not become N round trips (the rollup exists to make
    activity visible at a glance, and a rollup that takes a second to load is a rollup nobody
    waits for).
    """
    if not project_ids:
        return {}
    rows = await pool().fetch(
        "SELECT project_id, coalesce(sum(cost_usd),0)::float8 AS spent "
        "FROM builderapps.llm_usage "
        "WHERE project_id = ANY($1::text[]) "
        "  AND ts >= date_trunc('day', now() at time zone 'utc') "
        "GROUP BY project_id", list(project_ids))
    out = {}
    for r in rows:
        st = Status(float(r["spent"] or 0.0), DAILY_USD)
        out[str(r["project_id"])] = st.as_dict()
    return out
