-- Phase 35 — the prior-art scout and the "adopt & extend" pipeline path.
--
-- NOTE ON THE NUMBER: this is 016 because 015 is the highest number ACTUALLY APPLIED in
-- production, not because 015 is the highest file in the directory. This repo has a
-- historical 010/011 collision (010_browser_capability.sql and 010_deploy_repairs.sql both
-- shipped as 010; the latter was renamed to 011 and, being idempotent, applied twice under
-- both names). Always read `SELECT filename FROM _migrations` before picking the next one.

-- WHAT THE SCOUT FOUND, on the discussion it was scouting for.
--
-- One jsonb column rather than a table of candidates: this is the record of one decision made
-- once, at the top of one conversation, and it is read only by that conversation and by the
-- build it produces. A candidates table would let us query "how often is amilich/isometric-city
-- proposed" — a question nobody has asked and which would cost a join on every poll.
--
-- Shape: {status, category, reason, queries[], candidates[], pick, candidate{}, proposal{},
--         cost_usd, seconds, decision, decided_at, error}
-- `status` is the state machine the SPA polls:
--     classifying -> scouting -> proposed -> accepted | declined
--                 -> skipped   (the classifier said the pipeline can build this well)
--                 -> none      (we looked and nothing was worth proposing)
--                 -> failed    (and we say so rather than spinning forever)
ALTER TABLE builderapps.discussions
  ADD COLUMN IF NOT EXISTS prior_art jsonb NOT NULL DEFAULT '{}'::jsonb;

-- PROVENANCE, on the project itself and permanently.
--
-- {repo, url, licence, licence_source, upstream_commit, upstream_branch, stars, loc,
--  adapter_mode, imported_at, discussion_id}
--
-- This is deliberately on `projects` and not only in a doc in the repo: it must be obvious
-- from the platform's own records, forever, that this app is DERIVED WORK. A NOTICE file can
-- be deleted by the next agent that tidies the repo; a column cannot.
ALTER TABLE builderapps.projects
  ADD COLUMN IF NOT EXISTS adopted jsonb;

CREATE INDEX IF NOT EXISTS projects_adopted_idx
  ON builderapps.projects ((adopted->>'repo'))
  WHERE adopted IS NOT NULL;
