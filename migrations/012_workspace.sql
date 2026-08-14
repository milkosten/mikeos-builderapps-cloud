-- 012 — the per-project WORKSPACE (phase 32): one shared work-tracker for the pipeline,
-- the AI assistants and the human.
--
-- The ask, in Mike's words: "for each project have a system where we store a todo-list for
-- the AI assistants and the pipeline — things to build, features, test cases, backlogs,
-- bugs, documentation, knowledge base ... each of these should have comments and statuses,
-- and the assistants should be able to add / update / comment on each one."
--
-- ONE `items` TABLE WITH A `kind`, NOT SIX NEAR-IDENTICAL TABLES. A bug that turns out to be
-- a feature is then a field update, cross-kind search is one query, and the shared comment /
-- event / link machinery is written once.
--
-- ============================ THE DESIGN RULE ============================
-- **`kind` AND `status` ARE FREE TEXT. Nothing may ever put a CHECK constraint on them.**
-- `feature | bug | task | testcase | doc | kb` and `open | in_progress | blocked | done |
-- rejected` are CONVENTIONS the API defaults to and the UI groups by — they are not a
-- vocabulary the database enforces. A new kind ("risk", "decision", "incident") or a new
-- status must cost nothing: no migration, no deploy, no code change. This is exactly the
-- lesson `assistants.role` already carries in 006 — the taxonomy belongs to the people and
-- the agents using it, not to the schema.
-- ========================================================================
--
-- WHO DID IT is first-class. Every item, comment and event records the actor as a triple:
--   actor      — the stable machine id: `user:<user_id>` · `assistant:<id>` · `pipeline`
--   actor_kind — human | assistant | pipeline   (so the UI can tell a person from an agent)
--   actor_name — the display name at the time ("Ada", "Mike", "build pipeline")
-- A name alone would be ambiguous (two assistants can be called "Tester") and an id alone
-- would be unreadable a month later. Both, denormalised, is what makes the audit trail
-- answer "who changed this to blocked, a human or an agent?" without a join to a table the
-- row may outlive.
--
-- House rules honoured: idempotent (IF NOT EXISTS), timestamptz everywhere, parameterized
-- SQL only in the code above it, and NO reserved keyword as a column name — note `from_val`
-- / `to_val` rather than `from`/`to`, and `link_rel` rather than `rel`... `rel` is not
-- reserved, but `from_item`/`to_item` are spelled out for the same reason `left` became
-- `left_at` elsewhere in the estate.

-- ---------------------------------------------------------------------------
-- items — the single work-item table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS builderapps.workspace_items (
    id           bigserial PRIMARY KEY,
    project_id   text        NOT NULL,
    -- FREE TEXT. never an enum, never a CHECK. see the header.
    kind         text        NOT NULL DEFAULT 'task',
    title        text        NOT NULL DEFAULT '',
    body_md      text        NOT NULL DEFAULT '',
    -- FREE TEXT. never an enum, never a CHECK.
    status       text        NOT NULL DEFAULT 'open',
    priority     text        NOT NULL DEFAULT 'normal',
    assignee     text        NOT NULL DEFAULT '',
    -- the actor triple (see header)
    created_by      text     NOT NULL DEFAULT '',
    created_by_kind text     NOT NULL DEFAULT 'human',
    created_by_name text     NOT NULL DEFAULT '',
    -- Idempotency handle for machine-owned items. The build pipeline writes one item per
    -- backlog feature keyed `build_01`…`build_14`; a resumed run must UPDATE that row, not
    -- create a second one. NULL for anything a human or an assistant created.
    ext_key      text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    closed_at    timestamptz
);

-- the list view: "this project's items, newest first" and "…of this kind"
CREATE INDEX IF NOT EXISTS workspace_items_project_idx
    ON builderapps.workspace_items (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS workspace_items_kind_idx
    ON builderapps.workspace_items (project_id, kind, status);
-- the pipeline's upsert predicate. Partial, so the millions of NULL ext_keys cost nothing.
CREATE UNIQUE INDEX IF NOT EXISTS workspace_items_extkey_idx
    ON builderapps.workspace_items (project_id, ext_key) WHERE ext_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- comments — the conversation on an item (human OR assistant)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS builderapps.workspace_comments (
    id          bigserial PRIMARY KEY,
    item_id     bigint      NOT NULL REFERENCES builderapps.workspace_items(id) ON DELETE CASCADE,
    -- denormalised so every read path can scope by project without a join — the tenant check
    -- is the thing that must never be forgotten, so it is on the row itself.
    project_id  text        NOT NULL DEFAULT '',
    author      text        NOT NULL DEFAULT '',
    author_kind text        NOT NULL DEFAULT 'human',
    author_name text        NOT NULL DEFAULT '',
    body_md     text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workspace_comments_item_idx
    ON builderapps.workspace_comments (item_id, created_at);

-- ---------------------------------------------------------------------------
-- links — item ↔ item ("this test case covers that feature", "this bug blocks it")
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS builderapps.workspace_links (
    id          bigserial PRIMARY KEY,
    project_id  text        NOT NULL DEFAULT '',
    from_item   bigint      NOT NULL REFERENCES builderapps.workspace_items(id) ON DELETE CASCADE,
    to_item     bigint      NOT NULL REFERENCES builderapps.workspace_items(id) ON DELETE CASCADE,
    -- FREE TEXT as well: covers | blocks | duplicates | relates are conventions.
    link_rel    text        NOT NULL DEFAULT 'relates',
    created_by  text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS workspace_links_uniq_idx
    ON builderapps.workspace_links (from_item, to_item, link_rel);
CREATE INDEX IF NOT EXISTS workspace_links_to_idx ON builderapps.workspace_links (to_item);

-- ---------------------------------------------------------------------------
-- events — the audit trail. EVERY change lands here.
-- ---------------------------------------------------------------------------
-- This is the column of the whole feature that the user actually asked for: "the assistants
-- should be able to add / update / comment" implies the obvious next question — *which one
-- did, and when?* A status that moved from `open` to `done` with no record of who moved it
-- is worth very little; with `actor_kind=assistant, actor_name=Ada` next to it, the tracker
-- becomes a log of what the agents genuinely did.
CREATE TABLE IF NOT EXISTS builderapps.workspace_events (
    id          bigserial PRIMARY KEY,
    item_id     bigint      NOT NULL REFERENCES builderapps.workspace_items(id) ON DELETE CASCADE,
    project_id  text        NOT NULL DEFAULT '',
    actor       text        NOT NULL DEFAULT '',
    actor_kind  text        NOT NULL DEFAULT 'human',
    actor_name  text        NOT NULL DEFAULT '',
    verb        text        NOT NULL DEFAULT '',   -- created|status|assignee|kind|edited|commented|linked
    field       text        NOT NULL DEFAULT '',
    from_val    text        NOT NULL DEFAULT '',   -- `from`/`to` are reserved words
    to_val      text        NOT NULL DEFAULT '',
    note        text        NOT NULL DEFAULT '',
    ts          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workspace_events_item_idx
    ON builderapps.workspace_events (item_id, ts);
CREATE INDEX IF NOT EXISTS workspace_events_project_idx
    ON builderapps.workspace_events (project_id, ts DESC);

-- ---------------------------------------------------------------------------
-- keys — ONE `workspace-api-key` per project, shared by that project's assistants
-- ---------------------------------------------------------------------------
-- The tenancy model is the same as everything else here: a key is scoped to exactly one
-- project, and an item belonging to any other project is not "forbidden", it is NOT FOUND.
-- Stored the way the per-assistant tokens are (006): `key_sha` is what the auth path looks
-- up (O(1), no scan-and-decrypt), `key_enc` is how we hand the plaintext back to a beat
-- container. Nothing stores the plaintext.
CREATE TABLE IF NOT EXISTS builderapps.workspace_keys (
    project_id   text PRIMARY KEY,
    key_enc      text        NOT NULL DEFAULT '',
    key_sha      text        NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS workspace_keys_sha_idx
    ON builderapps.workspace_keys (key_sha) WHERE key_sha <> '';
