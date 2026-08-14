-- 013 — phase 33: messaging between assistants.
--
-- THE DESIGN POINT, and the reason this is a table and not a socket: assistant containers
-- are EPHEMERAL. They exist only for the duration of a beat, so at the moment one assistant
-- messages another there is normally no process on the other end to push anything into.
--
-- So delivery is a DURABLE QUEUE PLUS A WAKE:
--
--     send -> row here -> recipient marked wake-pending -> the scheduler starts ONE beat
--             -> the recipient reads its inbox, acts, may reply
--
-- All of that is server-side. **The browser is not in the delivery path at all.** A DM sent
-- while nobody is looking at /builder is delivered, worked and answered exactly the same;
-- the WebSocket added in this phase only pushes what already happened to a browser that
-- happens to be watching. Designing it the other way — the socket as the transport BETWEEN
-- assistants — would silently drop a message whenever nobody was looking, which is exactly
-- when an autonomous assistant is most likely to be sending one.
--
-- NAMING: the table is `assistant_messages`, not `messages`. `builderapps.messages` already
-- exists and is the HUMAN thread (one jsonb row per project, the /builder transcript). Two
-- different things called the same thing is how a migration ends up dropping the wrong one.

-- ---------------------------------------------------------------------------
-- the messages themselves
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS builderapps.assistant_messages (
    id             bigserial PRIMARY KEY,
    project_id     text        NOT NULL,
    -- Sender. NULL means the control plane / the human sent it, so a system notice ("your
    -- project hit its daily budget") can land in the same thread as everything else.
    from_assistant bigint,
    from_name      text        NOT NULL DEFAULT '',
    to_assistant   bigint      NOT NULL,
    to_name        text        NOT NULL DEFAULT '',
    body_md        text        NOT NULL DEFAULT '',

    -- THE CONVERSATION. `thread_id` is the id of the first message in the chain (a message
    -- that starts one points at itself); `reply_to` is the single message being answered and
    -- `depth` is how far down the chain this is. `depth` is stored rather than computed
    -- because it is the bound that stops two assistants being polite at each other until the
    -- project's budget is gone, and a bound you have to recompute by walking a chain is a
    -- bound that gets skipped on the one code path that matters.
    thread_id      bigint      NOT NULL DEFAULT 0,
    reply_to       bigint,
    depth          integer     NOT NULL DEFAULT 1,

    -- THE POINT OF THE WHOLE FEATURE. "I found a bug, it is #42, go and fix it" is only
    -- useful if #42 travels with it: the recipient wakes in a fresh container holding nothing
    -- but this row, and `workspace_store.full_item` turns this id into the whole report —
    -- body, comments, history and links — with no extra round trip.
    refs_item_id   bigint,

    -- Provenance both ways: the beat that SENT it, and the beat that was woken to read it.
    beat_id        bigint,
    wake_beat_id   bigint,

    delivered_at   timestamptz,   -- a beat picked it up
    read_at        timestamptz,   -- that beat actually reasoned over it

    -- Why this message was NOT delivered, when it was not. Empty = delivered normally.
    -- 'chain_depth' | 'budget' | 'no_recipient'. The message is still stored and still
    -- rendered — a bound that silently eats the message looks like a lost message, and the
    -- user needs to SEE that the conversation was stopped and why.
    blocked        text        NOT NULL DEFAULT '',

    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS assistant_messages_project_idx
    ON builderapps.assistant_messages (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS assistant_messages_thread_idx
    ON builderapps.assistant_messages (thread_id, created_at);
-- The inbox query: undelivered mail for one assistant. Partial, because the interesting rows
-- are always the tiny minority.
CREATE INDEX IF NOT EXISTS assistant_messages_inbox_idx
    ON builderapps.assistant_messages (to_assistant, created_at)
    WHERE read_at IS NULL AND blocked = '';

-- ---------------------------------------------------------------------------
-- the wake flag
-- ---------------------------------------------------------------------------
-- COALESCING lives here. A second DM to an assistant that is already wake-pending must not
-- queue a second beat — `wake_pending_at` is set with a `WHERE wake_pending_at IS NULL`, so
-- the second send is a no-op against a flag that is already raised, and both messages are
-- read by the one beat that eventually runs. Three DMs in a row cost one beat, not three.
--
-- It is a COLUMN, not an in-memory set, for the same reason every other claim in this
-- codebase is a column: a control-plane redeploy between "message stored" and "beat started"
-- would otherwise strand the wake forever, with the message sitting in a table nobody reads.
ALTER TABLE builderapps.assistants
    ADD COLUMN IF NOT EXISTS wake_pending_at timestamptz;

CREATE INDEX IF NOT EXISTS assistants_wake_idx
    ON builderapps.assistants (wake_pending_at)
    WHERE wake_pending_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- "what happened while I was away"
-- ---------------------------------------------------------------------------
-- Assistants work whether or not anyone is watching, which is the whole point — and until
-- now nothing said so. This is the high-water mark of what the owner has actually seen, per
-- project, so /builder can say "3 new since you were last here" and the Apps list can show
-- which projects moved without opening each one.
CREATE TABLE IF NOT EXISTS builderapps.project_seen (
    project_id text        NOT NULL,
    user_id    text        NOT NULL,
    seen_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_id)
);
