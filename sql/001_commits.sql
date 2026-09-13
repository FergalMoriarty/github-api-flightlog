-- Commits table.
--
-- Runs once, automatically, when the container initialises an empty data
-- directory. Changing this file does NOT alter an existing database: the
-- volume has to be recreated with `docker compose down -v` first.

CREATE TABLE IF NOT EXISTS commits (
    -- The commit SHA. Natural primary key: it is content-addressed, globally
    -- unique, and supplied by the source, so there is no reason to invent a
    -- surrogate. This is also the conflict target that makes re-running the
    -- load idempotent.
    sha                 TEXT PRIMARY KEY,

    -- Which repository this came from. Not in the API record — every record in
    -- a response is implicitly from the repository that was requested — so it
    -- is supplied by the loader. Without it the table can only ever hold one
    -- repository's history.
    repo                TEXT        NOT NULL,

    message             TEXT        NOT NULL,

    -- Author as git recorded it locally: self-reported, unverified, present on
    -- essentially every commit because git requires it to make one at all.
    author_name         TEXT        NOT NULL,
    author_email        TEXT        NOT NULL,
    authored_at         TIMESTAMPTZ NOT NULL,

    -- Committer, which differs from author whenever a commit is rebased,
    -- cherry-picked, or merged through the GitHub web interface — in which
    -- case committer_name is literally "GitHub". Not an anomaly.
    committer_name      TEXT        NOT NULL,
    committed_at        TIMESTAMPTZ NOT NULL,

    -- GitHub's match of the commit email to a user account. A DIFFERENT thing
    -- from author_name above, and nullable for a reason: the match fails for
    -- unregistered work emails, bots, and imported history. Measured at 0% null
    -- on dbt-labs/dbt-core, which is a fact about corporate repositories rather
    -- than about the API.
    --
    -- Separate columns rather than one author identity, because collapsing them
    -- would destroy exactly the distinction the null-rate reporting exists to
    -- surface.
    github_login        TEXT,
    github_user_id      BIGINT,

    verified            BOOLEAN,
    html_url            TEXT        NOT NULL,

    -- When this tool wrote the row, not when the commit happened. Distinguishes
    -- "the data is old" from "the sync has not run", which look identical
    -- otherwise.
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Commit volume over time, and windowed queries generally, both scan on
-- authored_at. Indexed because the table grows to tens of thousands of rows.
CREATE INDEX IF NOT EXISTS commits_authored_at_idx ON commits (authored_at DESC);

-- Contributor counts group on this.
CREATE INDEX IF NOT EXISTS commits_author_email_idx ON commits (author_email);

-- Every query that is not cross-repository filters on this first.
CREATE INDEX IF NOT EXISTS commits_repo_idx ON commits (repo);
