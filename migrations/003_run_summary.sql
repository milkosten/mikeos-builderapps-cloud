-- 003 — an honest, human-readable outcome for every run (phase 28).
--
-- A FEATURE step is no longer fatal: it is retried once with the error fed back into the
-- agentic loop, and if it still fails it is marked `skipped` and the build continues. That
-- makes "the run finished" and "everything got built" two DIFFERENT facts, so the run has to
-- carry the difference — never claim complete success when features were skipped.
--
--   summary — e.g. "11 of 12 features built; 1 skipped: CSV export (deploy health gate failed)".
--             Written on EVERY terminal transition (done or failed) and shipped in the
--             terminal SSE `done` event so the UI shows it.
--
-- Idempotent (house rule).

ALTER TABLE builderapps.pipeline_runs
    ADD COLUMN IF NOT EXISTS summary text NOT NULL DEFAULT '';
