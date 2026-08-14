-- 014 — phase 34: "Discuss", the pre-build discussion room.
--
-- WHY THIS TABLE EXISTS. Until now one sentence went straight into a 23-step pipeline, and
-- the pipeline then INVENTED everything the sentence did not say — brand, audience, scope.
-- That is how a book-club tracker ended up named after its own buyer persona. A discussion
-- is the missing artifact: the shared understanding that exists BEFORE a container does.
--
-- IT IS NOT A PROJECT. A project owns a repo, a subnet, a subdomain, containers and a
-- deploy history; a discussion owns none of those and must never allocate them — it is text
-- only, which is also why it is cheap. Putting it in `projects` with a `status='draft'`
-- would have meant every query that means "a real app" growing a filter, and the first one
-- that forgot would have tried to deploy a conversation. Separate table, separate id space.
--
-- WHEN "BUILD IT" IS PRESSED the discussion is not migrated or moved: it is COMPOSED into a
-- brief, that brief becomes `projects.prompt`, and `project_id` here records which app came
-- out of which conversation. The discussion stays readable afterwards — it is the reasoning
-- behind the app, and losing it would lose the only record of why the scope is what it is.
CREATE TABLE IF NOT EXISTS builderapps.discussions (
    -- Its OWN id space. A discussion id is `d`+6, a project shortid is 6, so the two can
    -- never be confused in a URL, a log line or a WHERE clause — and a discussion id can
    -- never be mistaken for a subdomain, because it isn't one.
    id          text PRIMARY KEY,
    user_id     text        NOT NULL,

    -- The sentence the user typed on the start page, kept verbatim and never rewritten.
    -- It is what the opening turn reacts to, and the honest fallback for the brief if the
    -- conversation never got anywhere.
    seed        text        NOT NULL DEFAULT '',
    title       text        NOT NULL DEFAULT '',

    -- THE THREAD: [{role:'user'|'assistant', text, questions?, show?, ts}]. Questions ride
    -- with the assistant turn that asked them, so a reload re-renders the SAME chips
    -- alongside the SAME message instead of a bare paragraph with the answers missing.
    messages    jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- THE CANVAS — the living brief. Each field is {value, agreed, source}, not a bare
    -- string, because "agreed" is the whole point: a value the user actually decided may
    -- not be silently replaced by the next turn's enthusiasm. `changelog` records every
    -- change that was applied to an agreed field, so a revision is always VISIBLE.
    canvas      jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- 'open' while it is being discussed, 'built' once it produced a project.
    status      text        NOT NULL DEFAULT 'open',
    project_id  text,

    -- What the conversation cost. Text-only, no containers: this is cents, and it is
    -- carried into the project's usage as one `discuss` row when Build it is pressed, so
    -- the app's Usage tab accounts for the thinking as well as the building.
    cost_usd    numeric(12,6) NOT NULL DEFAULT 0,
    turns       integer     NOT NULL DEFAULT 0,

    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- The Apps list reads "my drafts, newest first" on every visit.
CREATE INDEX IF NOT EXISTS discussions_user_idx
    ON builderapps.discussions (user_id, updated_at DESC);
-- "which conversation produced this app" — the reverse link, used from a project.
CREATE INDEX IF NOT EXISTS discussions_project_idx
    ON builderapps.discussions (project_id)
    WHERE project_id IS NOT NULL;

-- The forward link. Nullable and un-constrained on purpose: a project built from a
-- one-liner has no discussion, and that is the normal case, not a missing row.
ALTER TABLE builderapps.projects
    ADD COLUMN IF NOT EXISTS discussion_id text;
