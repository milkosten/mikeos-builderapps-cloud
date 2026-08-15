-- 015 — the stepped questionnaire, and the user's own sentence back in the thread.
--
-- TWO FIXES, one file, because they are the same defect seen from two sides: the room was
-- not keeping an honest record of what was asked and what was answered.
--
-- 1. THE MISSING PROMPT. The sentence typed on the start page lived only in `seed`. The
--    thread therefore opened with the assistant's proposal — an answer to a question that
--    was nowhere on the screen — and a reload, or a shared /discuss/<id> link, showed no
--    record of what had actually been asked for. It is a message the user sent; it belongs
--    in `messages`. Every existing room is backfilled below from `seed`, which was kept
--    verbatim, so old rooms read correctly too.
--
-- 2. THE HALF-FILLED QUESTIONNAIRE. Answers used to be posted one chip at a time, which is
--    what made the model treat one answer as the whole set (it saw four questions asked and
--    one reply, and completed the pattern by inventing three decisions and marking them
--    agreed — and an agreed cell only changes by explicit revision, so the guesses stuck).
--    Answers are now collected in a stepper and submitted ONCE. `draft_answers` is where the
--    in-progress form lives so a reload mid-questionnaire does not lose it: a scratchpad,
--    never a turn, never seen by the model.
ALTER TABLE builderapps.discussions
    ADD COLUMN IF NOT EXISTS draft_answers jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Backfill the opening user message. Idempotent by construction: after this runs the first
-- message IS a user message, so the WHERE clause no longer matches. `created_at` is the
-- honest timestamp — that is when the sentence was typed.
UPDATE builderapps.discussions
   SET messages = jsonb_build_array(
           jsonb_build_object(
               'role', 'user',
               'text', seed,
               'ts',   (EXTRACT(EPOCH FROM created_at) * 1000)::bigint
           )
       ) || messages
 WHERE seed <> ''
   AND (messages -> 0 ->> 'role') IS DISTINCT FROM 'user';
