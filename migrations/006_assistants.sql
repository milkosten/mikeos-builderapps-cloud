-- 006 — per-project AI assistants (phase 29).
--
-- An assistant is a closed-loop agent attached to ONE project: SOUL.md persona, a heartbeat
-- every `interval_minutes`, a granted capability set, and one `assistant_beats` row per beat.
--
-- DESIGN RULE THAT THE SCHEMA MUST NOT BREAK: **`role` is FREE TEXT, never an enum.**
-- "Product Owner" and "Expense management assistant" are the same kind of thing — a name, a
-- description, a SOUL and a set of capabilities. The six shipped templates are pre-fills in
-- `server/assistants.py`, not a constraint here. Nothing may add a CHECK on this column.
--
-- What the runtime actually enforces is `capabilities` (a jsonb array of capability ids),
-- NOT the role string. A "Security" assistant without `edit_code` cannot write, whatever its
-- SOUL says.
--
-- House rules honoured: idempotent (IF NOT EXISTS), timestamptz everywhere, no reserved
-- keyword as a column name (the beat's origin is `trigger_kind`, never `trigger`).

CREATE TABLE IF NOT EXISTS builderapps.assistants (
    id               bigserial PRIMARY KEY,
    project_id       text        NOT NULL,
    role             text        NOT NULL DEFAULT '',   -- FREE TEXT. never an enum.
    name             text        NOT NULL DEFAULT '',
    description      text        NOT NULL DEFAULT '',
    soul_md          text        NOT NULL DEFAULT '',
    capabilities     jsonb       NOT NULL DEFAULT '[]'::jsonb,
    interval_minutes integer     NOT NULL DEFAULT 60,
    status           text        NOT NULL DEFAULT 'paused',  -- active | paused
    -- Per-assistant control-plane credential. Scoped to exactly {project, assistant}; the
    -- beat container gets the plaintext for one beat and nothing else. sha is what the auth
    -- path looks up (O(1), no scan-and-decrypt); _enc is how we hand it back to a container.
    token_enc        text        NOT NULL DEFAULT '',
    token_sha        text        NOT NULL DEFAULT '',
    last_beat_at     timestamptz,
    next_beat_at     timestamptz,
    -- Scheduler ownership, same pattern as pipeline_runs: an atomic claim is the cross-process
    -- mutex, and a stale claim is how a redeploy's orphaned beat is detected.
    beat_owner       text        NOT NULL DEFAULT '',
    beat_claimed_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS assistants_project_idx ON builderapps.assistants (project_id, created_at);
-- exactly the scheduler's claim predicate
CREATE INDEX IF NOT EXISTS assistants_due_idx     ON builderapps.assistants (status, next_beat_at);
CREATE UNIQUE INDEX IF NOT EXISTS assistants_token_sha_idx
    ON builderapps.assistants (token_sha) WHERE token_sha <> '';

-- One row per beat: what it thought, what it did, what it cost. This is the assistant's
-- memory and its audit log at the same time — an idle assistant is visibly cheap and a
-- runaway one is visibly expensive.
CREATE TABLE IF NOT EXISTS builderapps.assistant_beats (
    id            bigserial PRIMARY KEY,
    assistant_id  bigint      NOT NULL REFERENCES builderapps.assistants(id) ON DELETE CASCADE,
    project_id    text        NOT NULL DEFAULT '',
    status        text        NOT NULL DEFAULT 'running',   -- running|done|skipped|failed
    trigger_kind  text        NOT NULL DEFAULT 'schedule',  -- schedule|manual  (`trigger` is reserved)
    thought       text        NOT NULL DEFAULT '',
    actions       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    log           text        NOT NULL DEFAULT '',
    tokens        integer     NOT NULL DEFAULT 0,
    cost_usd      numeric(12,6) NOT NULL DEFAULT 0,
    duration_ms   integer     NOT NULL DEFAULT 0,
    ts            timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

CREATE INDEX IF NOT EXISTS assistant_beats_assistant_idx
    ON builderapps.assistant_beats (assistant_id, ts DESC);
CREATE INDEX IF NOT EXISTS assistant_beats_project_idx
    ON builderapps.assistant_beats (project_id, ts DESC);
