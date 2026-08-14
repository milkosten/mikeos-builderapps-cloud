-- 007 — commit ⇒ auto-deploy, attributable and rollback-able (phase 30).
--
-- A Developer assistant may now edit code, commit and push. The moment it pushes, the control
-- plane must redeploy the app through the SAME normalizer + health gate the update pipeline
-- uses, and roll back to the last good commit if the gate does not go green.
--
-- Two facts the old schema could not answer, and both are needed for that:
--
--   1. **"which commit is currently on the air?"** — without it there is no such thing as a
--      "last good commit", so a rollback has nothing to roll back TO. `deployments.git_sha`
--      records the sha that was built, and the newest row with status='healthy' IS the last
--      good commit. (Deliberately derived from the deployments table rather than a
--      `projects.last_good_sha` column: one writer, no second source of truth to drift.)
--
--   2. **"who caused this deploy?"** — an unattended agent that can ship code must leave a
--      trail. `assistant_id`/`beat_id` on both the run and the deployment make every build
--      traceable to the exact beat that triggered it, and the beat record links back.
--
-- `pipeline_runs.kind` gains a third value, 'deploy': build + health-gate the commit that is
-- already in git, with NO code generation. That distinction matters for recovery — resuming a
-- 'deploy' run must not be mistaken for resuming a 'create' (see server/runner.py). kind has
-- no CHECK constraint and none is added here.
--
-- House rules: idempotent (IF NOT EXISTS), timestamptz, no reserved keyword as a column.

ALTER TABLE builderapps.deployments
    ADD COLUMN IF NOT EXISTS git_sha      text   NOT NULL DEFAULT '';
ALTER TABLE builderapps.deployments
    ADD COLUMN IF NOT EXISTS assistant_id bigint;
ALTER TABLE builderapps.deployments
    ADD COLUMN IF NOT EXISTS beat_id      bigint;

ALTER TABLE builderapps.pipeline_runs
    ADD COLUMN IF NOT EXISTS assistant_id bigint;
ALTER TABLE builderapps.pipeline_runs
    ADD COLUMN IF NOT EXISTS beat_id      bigint;

-- "the last commit that actually passed the health gate for this project" — the rollback
-- target. Partial index because that is the only status the lookup ever asks for.
CREATE INDEX IF NOT EXISTS deployments_last_good_idx
    ON builderapps.deployments (project_id, started_at DESC)
    WHERE status = 'healthy' AND git_sha <> '';

-- "show me every deploy this assistant caused"
CREATE INDEX IF NOT EXISTS deployments_assistant_idx
    ON builderapps.deployments (assistant_id, started_at DESC)
    WHERE assistant_id IS NOT NULL;
