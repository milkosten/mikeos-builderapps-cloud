-- builderapps control-plane schema (idempotent). See ARCHITECTURE §5 / phase-07.
-- Rules: parameterized SQL only (enforced in code); NO reserved-keyword columns;
-- all timestamps timestamptz; secret/token columns stored ENCRYPTED (value_enc/token_enc).

CREATE SCHEMA IF NOT EXISTS builderapps;

-- MikeOS user_id -> background-provisioned Gitea account. token_enc is Fernet-encrypted.
CREATE TABLE IF NOT EXISTS builderapps.gitea_accounts (
    user_id         text PRIMARY KEY,
    gitea_username  text NOT NULL,
    token_enc       text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- One row per project. id = 6-char lowercase-alnum shortid (matches the Caddy wildcard regex).
-- status ∈ {creating, building, deploying, live, failed, stopped}
CREATE TABLE IF NOT EXISTS builderapps.projects (
    id           text PRIMARY KEY,
    user_id      text NOT NULL,
    gitea_owner  text NOT NULL,
    gitea_repo   text NOT NULL,
    subdomain    text NOT NULL,
    title        text NOT NULL DEFAULT '',
    prompt       text NOT NULL DEFAULT '',
    status       text NOT NULL DEFAULT 'creating',
    pipeline     text NOT NULL DEFAULT 'create',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS projects_user_idx ON builderapps.projects (user_id, created_at DESC);

-- Per-project secrets (pg password, app secret, deploy key). value_enc is Fernet-encrypted.
CREATE TABLE IF NOT EXISTS builderapps.project_secrets (
    project_id  text NOT NULL,
    secret_key  text NOT NULL,          -- 'db_password' | 'app_secret' | 'deploy_key' ...  (NOT reserved)
    value_enc   text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, secret_key)
);

-- Deploy history for a project (image tag, compose hash, health, timings).
CREATE TABLE IF NOT EXISTS builderapps.deployments (
    id          bigserial PRIMARY KEY,
    project_id  text NOT NULL,
    image_tag   text NOT NULL DEFAULT '',
    compose_hash text NOT NULL DEFAULT '',
    status      text NOT NULL DEFAULT 'pending',   -- pending | deploying | healthy | failed
    health      text NOT NULL DEFAULT '',          -- last /health JSON body (truncated)
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS deployments_project_idx ON builderapps.deployments (project_id, started_at DESC);

-- Pipeline runs (create | update).  current_step/total_steps mirror current_work.json.
CREATE TABLE IF NOT EXISTS builderapps.pipeline_runs (
    id           bigserial PRIMARY KEY,
    project_id   text NOT NULL,
    kind         text NOT NULL DEFAULT 'create',   -- create | update
    status       text NOT NULL DEFAULT 'running',  -- running | done | failed
    current_step integer NOT NULL DEFAULT 0,
    total_steps  integer NOT NULL DEFAULT 0,
    request      text NOT NULL DEFAULT '',         -- the natural-language prompt/change
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);
CREATE INDEX IF NOT EXISTS pipeline_runs_project_idx ON builderapps.pipeline_runs (project_id, created_at DESC);

-- Individual steps of a run (durable mirror of the runtime step engine).
-- status ∈ {pending, running, done, failed, skipped}
CREATE TABLE IF NOT EXISTS builderapps.pipeline_steps (
    id       bigserial PRIMARY KEY,
    run_id   bigint NOT NULL,
    idx      integer NOT NULL,
    name     text NOT NULL,
    status   text NOT NULL DEFAULT 'pending',
    log      text NOT NULL DEFAULT '',
    ts       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, idx)
);
CREATE INDEX IF NOT EXISTS pipeline_steps_run_idx ON builderapps.pipeline_steps (run_id, idx);

-- Per-project conversation memory (designer pattern).
CREATE TABLE IF NOT EXISTS builderapps.messages (
    project_id text PRIMARY KEY,
    thread     jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
