-- Token + cost accounting per project (the builder's "Usage" tab).
-- One row per LLM call, stamped with the project/run/step that caused it.
CREATE TABLE IF NOT EXISTS builderapps.llm_usage (
    id                bigserial PRIMARY KEY,
    project_id        text        NOT NULL,
    run_id            bigint,
    step              text,
    model             text        NOT NULL DEFAULT '',
    prompt_tokens     integer     NOT NULL DEFAULT 0,
    completion_tokens integer     NOT NULL DEFAULT 0,
    cached_tokens     integer     NOT NULL DEFAULT 0,
    cost_usd          numeric(12,6) NOT NULL DEFAULT 0,
    cost_estimated    boolean     NOT NULL DEFAULT false,
    ts                timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_usage_project_idx ON builderapps.llm_usage (project_id, ts);
CREATE INDEX IF NOT EXISTS llm_usage_run_idx     ON builderapps.llm_usage (run_id);
