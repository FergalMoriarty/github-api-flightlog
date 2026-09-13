-- Pull requests table.
--
-- Runs once when the container initialises an empty data directory, in
-- filename order after 001_commits.sql. Changing this file does NOT alter an
-- existing database — the volume must be recreated with `docker compose down -v`.

CREATE TABLE IF NOT EXISTS pull_requests (
    -- GitHub's internal PR id: globally unique across all repositories.
    -- NOT the PR number, which is per-repository and would collide the moment
    -- a second repo is ingested. Both are stored; this one is the key.
    id                  BIGINT PRIMARY KEY,

    -- The number people actually use — "#16245". Unique within a repository
    -- only, hence the composite unique constraint below rather than a
    -- primary key.
    number              INTEGER     NOT NULL,
    repo                TEXT        NOT NULL,

    title               TEXT        NOT NULL,
    -- "open" or "closed". Note there is no "merged" state: a merged PR is
    -- closed with merged_at set. Deriving "was it merged" from state alone
    -- would count every abandoned PR as merged.
    state               TEXT        NOT NULL,
    draft               BOOLEAN,

    -- The PR author. Nullable for the same reason commits.github_login is:
    -- GitHub returns null when the account has been deleted. Rarer than the
    -- commit case — a PR requires an account to open — but not impossible.
    user_login          TEXT,
    user_id             BIGINT,

    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    -- Null while the PR is open. Both of these are null for an open PR, and
    -- closed_at is set with merged_at null for one that was closed unmerged.
    closed_at           TIMESTAMPTZ,
    merged_at           TIMESTAMPTZ,

    base_ref            TEXT        NOT NULL,
    head_ref            TEXT,

    html_url            TEXT        NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Number is unique per repository, not globally. This makes "PR #16245 in
    -- dbt-core" an enforced fact rather than an assumption.
    CONSTRAINT pull_requests_repo_number_key UNIQUE (repo, number)
);

-- "How long do PRs stay open" scans on these two together.
CREATE INDEX IF NOT EXISTS pull_requests_created_at_idx ON pull_requests (created_at DESC);
CREATE INDEX IF NOT EXISTS pull_requests_merged_at_idx ON pull_requests (merged_at);

CREATE INDEX IF NOT EXISTS pull_requests_repo_state_idx ON pull_requests (repo, state);
