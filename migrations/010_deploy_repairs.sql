-- 010 — a failed deploy tells the assistant what broke, and is BOUNDED (phase 31).
--
-- The loop this table exists to close: assistant ships -> deploy fails -> assistant is told
-- the real error -> it fixes forward -> reships. Without it an assistant is write-only and
-- every broken commit needs a human.
--
-- The loop this table exists to STOP: an agent that is handed its own failure and told to fix
-- it will try forever. So a failure opens a repair EPISODE, and the episode — not the commit —
-- is what carries the budget. Counting per commit would never bind: each repair pushes a new
-- sha, so "2 attempts per commit" would be "2 attempts per attempt", i.e. unbounded. An
-- episode opens on the first red deploy and closes when a deploy goes green.
--
-- `signature` is stage + the first real error line, and it is why an identical failure stops
-- the loop DEAD rather than after the full budget: re-running an unchanged error is
-- superstition, not debugging. Two different errors mean the assistant is at least moving.
--
-- No reserved keyword as a column (`stage`, `signature`, `attempts` are all fine; the beat
-- that was dispatched is `beat_id`). Idempotent. timestamptz throughout.

CREATE TABLE IF NOT EXISTS builderapps.deploy_repairs (
    id            bigserial PRIMARY KEY,
    project_id    text        NOT NULL,
    assistant_id  bigint,
    -- the commit whose deploy first went red in this episode; kept for the escalation message
    origin_sha    text        NOT NULL DEFAULT '',
    -- the most recent commit that failed (an episode walks forward as the agent pushes fixes)
    last_sha      text        NOT NULL DEFAULT '',
    stage         text        NOT NULL DEFAULT '',   -- build | up | health_gate | public_check
    signature     text        NOT NULL DEFAULT '',   -- stage + first error line, normalized
    -- how many REPAIR BEATS this episode has already dispatched. The cap is on this.
    attempts      integer     NOT NULL DEFAULT 0,
    status        text        NOT NULL DEFAULT 'open',   -- open | resolved | escalated
    detail        text        NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- "is there an episode running for this project right now?" — the only hot lookup, and the
-- partial index makes it a single-row probe however long the history gets.
CREATE UNIQUE INDEX IF NOT EXISTS deploy_repairs_open_idx
    ON builderapps.deploy_repairs (project_id) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS deploy_repairs_project_idx
    ON builderapps.deploy_repairs (project_id, created_at DESC);
