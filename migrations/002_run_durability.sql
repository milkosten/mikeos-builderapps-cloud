-- 002 — durable pipeline runs.
--
-- A run used to live and die with the HTTP request that started it: a control-plane redeploy
-- (or a client disconnect) left `pipeline_runs.status='running'` forever, the project stuck in
-- `creating`/`deploying`, and the UI spinning. These columns let a fresh process TELL that a
-- run is dead and take it over:
--   heartbeat_at — bumped every ~30s (and on every step transition) while a run is genuinely
--                  alive. Stale heartbeat => the process that owned it is gone.
--   owner        — which api process currently holds the run (host:pid:boot-uuid).
--   attempts     — how many times the run has been claimed; bounds resume loops.
--   error        — why a run ended up `failed`, so the UI can show a real reason.
-- Idempotent (house rule): every statement is IF NOT EXISTS / guarded.

ALTER TABLE builderapps.pipeline_runs
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;
ALTER TABLE builderapps.pipeline_runs
    ADD COLUMN IF NOT EXISTS owner text NOT NULL DEFAULT '';
ALTER TABLE builderapps.pipeline_runs
    ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE builderapps.pipeline_runs
    ADD COLUMN IF NOT EXISTS error text NOT NULL DEFAULT '';

-- The sweeper/janitor scans exactly this predicate.
CREATE INDEX IF NOT EXISTS pipeline_runs_status_heartbeat_idx
    ON builderapps.pipeline_runs (status, heartbeat_at);

-- Pre-existing orphans have no heartbeat at all; seed it from created_at so they read as
-- (very) stale rather than NULL-ambiguous, and the boot sweep picks them up immediately.
UPDATE builderapps.pipeline_runs
   SET heartbeat_at = created_at
 WHERE status = 'running' AND heartbeat_at IS NULL;
