-- 009 — a beat can carry a direct instruction from a human (phase 30).
--
-- One character in the composer decides which half of the product runs:
--
--     "add a search box"              -> the BUILD PIPELINE. Strict, ordered, deterministic.
--     "@Developer add a search box"   -> straight into that ASSISTANT's next beat.
--
-- That is the user-visible form of the separation the whole phase rests on: the pipeline
-- produces v1, assistants evolve the product with free judgment, and they are not the same
-- machine. Making the routing a visible `@` rather than hidden state means the user always
-- knows which one they are talking to.
--
-- The ask is stored ON THE BEAT rather than passed only in memory, for three reasons:
--   * the beat container is launched asynchronously, and a control-plane restart between
--     "kick" and "launch" would otherwise silently drop what the user actually asked for;
--   * `/api/assistant/reason` can then read the ask from the DATABASE instead of trusting
--     whatever the container claims it was told;
--   * the left pane restores after a reload from the same row as everything else.
--
-- `user_ask`, not `request`/`ask`: unambiguous, and not a reserved word anywhere.
--
-- House rules: idempotent, no reserved keyword as a column name.

ALTER TABLE builderapps.assistant_beats
    ADD COLUMN IF NOT EXISTS user_ask text NOT NULL DEFAULT '';
