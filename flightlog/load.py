"""Load validated records into PostgreSQL.

Only records that passed validation reach this module. That is what makes the
validation worth having: a malformed record is a counted, explained rejection
in the report rather than a KeyError several hundred rows into a write that has
already half-committed.

The load is idempotent on commit SHA. Every run is a full pull from the `since`
window, so consecutive runs overlap almost completely — two runs an hour apart
against dbt-labs/dbt-core share roughly 4,150 of their 4,173 records. Without
ON CONFLICT that is either a duplicate key error or four thousand duplicate
rows, depending on whether there is a primary key. With it, a re-run is simply
a no-op for unchanged rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import execute_values

from .config import Config

log = logging.getLogger(__name__)

# Rows per INSERT statement.
#
# One statement per row means one network round trip per row: 4,173 round trips
# for a full pull, which is minutes rather than seconds. Batching sends them as
# a single multi-row INSERT. 500 is a reasonable middle — large enough that the
# round trip cost disappears, small enough that a failing batch is a
# manageable unit to investigate and that the statement stays under any
# practical size limit.
BATCH_SIZE = 500

# ON CONFLICT DO UPDATE rather than DO NOTHING.
#
# DO NOTHING would be simpler and is wrong here. A commit's SHA is
# content-addressed and its content cannot change — but the GitHub user matched
# to it CAN: someone registers the email address they have been committing from
# for years, and every one of their historical commits gains a github_login
# that was previously null. DO NOTHING would keep the stale nulls forever.
#
# EXCLUDED is the row that would have been inserted. Updating from it means the
# newest fetch wins.
_UPSERT_SQL = """
INSERT INTO commits (
    sha, repo, message,
    author_name, author_email, authored_at,
    committer_name, committed_at,
    github_login, github_user_id,
    verified, html_url
) VALUES %s
ON CONFLICT (sha) DO UPDATE SET
    repo           = EXCLUDED.repo,
    message        = EXCLUDED.message,
    author_name    = EXCLUDED.author_name,
    author_email   = EXCLUDED.author_email,
    authored_at    = EXCLUDED.authored_at,
    committer_name = EXCLUDED.committer_name,
    committed_at   = EXCLUDED.committed_at,
    github_login   = EXCLUDED.github_login,
    github_user_id = EXCLUDED.github_user_id,
    verified       = EXCLUDED.verified,
    html_url       = EXCLUDED.html_url,
    ingested_at    = now()
"""
# Keyed on id, not number. `number` is unique per repository only, so a second
# repository would collide on it — and because the composite unique constraint
# on (repo, number) exists, that collision would be an error rather than silent
# corruption. Keying on the globally unique id avoids the question entirely.
#
# DO UPDATE rather than DO NOTHING for a stronger reason than with commits: a
# PR record genuinely changes. An open PR gets merged, retitled, taken out of
# draft. DO NOTHING would freeze every PR in the state it held when first seen,
# which for an actively developed repository means the table is wrong within
# hours.
_PR_UPSERT_SQL = """
INSERT INTO pull_requests (
    id, number, repo,
    title, state, draft,
    user_login, user_id,
    created_at, updated_at, closed_at, merged_at,
    base_ref, head_ref, html_url
) VALUES %s
ON CONFLICT (id) DO UPDATE SET
    number      = EXCLUDED.number,
    repo        = EXCLUDED.repo,
    title       = EXCLUDED.title,
    state       = EXCLUDED.state,
    draft       = EXCLUDED.draft,
    user_login  = EXCLUDED.user_login,
    user_id     = EXCLUDED.user_id,
    created_at  = EXCLUDED.created_at,
    updated_at  = EXCLUDED.updated_at,
    closed_at   = EXCLUDED.closed_at,
    merged_at   = EXCLUDED.merged_at,
    base_ref    = EXCLUDED.base_ref,
    head_ref    = EXCLUDED.head_ref,
    html_url    = EXCLUDED.html_url,
    ingested_at = now()
"""

@dataclass
class LoadStats:
    """What the load did, for the run report.

    rows_affected rather than separate insert and update counts: ON CONFLICT
    DO UPDATE reports both identically, and Postgres offers no straightforward
    way to distinguish them without an extra column or a RETURNING clause that
    would cost more than the distinction is worth here. Stated plainly rather
    than guessed at.
    """

    rows_submitted: int = 0
    rows_affected: int = 0
    batches: int = 0


def _commit_row(record: dict[str, Any], repo: str) -> tuple:
    """Flatten one validated commit record into a row tuple.

    Column order must match the INSERT statement above. A mismatch here would
    load every value into the wrong column — and because most of them are TEXT,
    Postgres would accept it silently. That is why this function and the SQL sit
    next to each other in one file rather than being separated for tidiness.

    Every path read here was checked by the validator, with one exception: the
    nested reads under `author` and `committer` are guarded, because the
    validator confirms those objects are dict-or-null and does not check what is
    inside them. `record["author"]["login"]` on a null author is exactly the
    crash this whole module is arranged to avoid.
    """
    commit = record["commit"]
    author = record.get("author") or {}
    committer = record.get("committer") or {}
    verification = commit.get("verification") or {}

    return (
        record["sha"],
        repo,
        commit["message"],
        commit["author"]["name"],
        commit["author"]["email"],
        # Passed through as the ISO 8601 string GitHub sent. Postgres parses it
        # into timestamptz on insert, and its parser handles the format
        # correctly including the trailing Z. Parsing in Python first would add
        # a step that can fail, to produce a value Postgres would immediately
        # re-encode.
        commit["author"]["date"],
        commit["committer"]["name"],
        commit["committer"]["date"],
        # None where GitHub could not match the email to an account. The
        # nullable columns exist for exactly this.
        author.get("login"),
        author.get("id"),
        verification.get("verified"),
        record["html_url"],
    )

def _pr_row(record: dict[str, Any], repo: str) -> tuple:
    """Flatten one validated pull request record into a row tuple.

    Column order must match the INSERT above. Most of these columns are TEXT,
    so a mismatch would load values into the wrong columns and Postgres would
    accept it without complaint — which is why the SQL and this function sit
    together rather than being separated for tidiness.
    """
    # `or {}` rather than .get with a default, because the key is present with
    # a value of None when the account has been deleted. A plain
    # record.get("user", {}) returns None in that case, not the default, and
    # the next line raises AttributeError. The same guard as the commit author.
    user = record.get("user") or {}
    base = record.get("base") or {}
    head = record.get("head") or {}

    return (
        record["id"],
        record["number"],
        repo,
        record["title"],
        record["state"],
        record.get("draft"),
        user.get("login"),
        user.get("id"),
        # ISO 8601 strings passed through for Postgres to parse into
        # timestamptz. The three nullable ones are None for an open PR, and
        # for one closed without merging.
        record["created_at"],
        record["updated_at"],
        record.get("closed_at"),
        record.get("merged_at"),
        base["ref"],
        head.get("ref"),
        record["html_url"],
    )

def load_commits(
    config: Config,
    records: Iterable[dict[str, Any]],
    stats: LoadStats | None = None,
) -> LoadStats:
    """Upsert validated commit records.

    Opens its own connection and commits once at the end, so a failed load
    leaves the table exactly as it was rather than partially written. For a
    4,000-row load that is the right trade: the whole thing fits comfortably in
    one transaction, and "the run failed and changed nothing" is far easier to
    reason about than "the run failed somewhere around row 2,600".
    """
    stats = stats if stats is not None else LoadStats()
    rows = [_commit_row(r, config.repo) for r in records]

    if not rows:
        log.info("no records to load")
        return stats

    # Context managers on both. For psycopg2 the connection context manager
    # commits on clean exit and rolls back on exception — it does NOT close the
    # connection, which is a well-known wrinkle, hence the outer `with
    # psycopg2.connect(...)` still needing the close in a finally. Using
    # closing() from contextlib would be the tidier fix; kept explicit here
    # because the behaviour surprises people.
    connection = psycopg2.connect(config.pg_dsn)
    try:
        with connection:
            with connection.cursor() as cursor:
                for start in range(0, len(rows), BATCH_SIZE):
                    batch = rows[start : start + BATCH_SIZE]
                    # execute_values builds one multi-row INSERT from the list,
                    # substituting for the %s placeholder in the template. It
                    # parameterises every value, so a commit message containing
                    # a quote is data rather than syntax.
                    execute_values(cursor, _UPSERT_SQL, batch, page_size=BATCH_SIZE)
                    stats.batches += 1
                    stats.rows_affected += cursor.rowcount
                    log.debug(
                        "loaded batch %d (%d rows)", stats.batches, len(batch)
                    )
        stats.rows_submitted += len(rows)
    finally:
        connection.close()

    return stats

def load_pull_requests(
    config: Config,
    records: Iterable[dict[str, Any]],
    stats: LoadStats | None = None,
) -> LoadStats:
    """Upsert validated pull request records.

    Structurally identical to load_commits, deliberately not abstracted into a
    shared function. The two differ in their SQL, their row builder and their
    conflict target, which is most of what either function does — a common
    implementation would be a thin wrapper around three parameters that
    together constitute the whole body. Two readable functions beat one
    parameterised one at this size.
    """
    stats = stats if stats is not None else LoadStats()
    rows = [_pr_row(r, config.repo) for r in records]

    if not rows:
        log.info("no pull requests to load")
        return stats

    connection = psycopg2.connect(config.pg_dsn)
    try:
        with connection:
            with connection.cursor() as cursor:
                for start in range(0, len(rows), BATCH_SIZE):
                    batch = rows[start : start + BATCH_SIZE]
                    execute_values(cursor, _PR_UPSERT_SQL, batch, page_size=BATCH_SIZE)
                    stats.batches += 1
                    stats.rows_affected += cursor.rowcount
                    log.debug("loaded PR batch %d (%d rows)", stats.batches, len(batch))
        stats.rows_submitted += len(rows)
    finally:
        connection.close()

    return stats


def pr_table_counts(config: Config) -> dict[str, Any]:
    """Summary of the pull_requests table, for the report."""
    connection = psycopg2.connect(config.pg_dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    count(*),
                    count(*) FILTER (WHERE state = 'open'),
                    count(*) FILTER (WHERE merged_at IS NOT NULL),
                    -- Closed without merging. The state column alone cannot
                    -- express this: a merged PR and an abandoned one are both
                    -- 'closed', and only merged_at tells them apart.
                    count(*) FILTER (WHERE state = 'closed' AND merged_at IS NULL),
                    -- Median time to merge, in hours. Median rather than mean
                    -- because one PR left open for eight months would drag an
                    -- average somewhere useless.
                    percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (merged_at - created_at)) / 3600
                    ) FILTER (WHERE merged_at IS NOT NULL)
                FROM pull_requests
                WHERE repo = %s
                """,
                (config.repo,),
            )
            total, open_count, merged, closed_unmerged, median_hours = cursor.fetchone()
    finally:
        connection.close()

    return {
        "total": total,
        "open": open_count,
        "merged": merged,
        "closed_unmerged": closed_unmerged,
        "median_merge_hours": median_hours,
    }
    
def table_counts(config: Config) -> dict[str, Any]:
    """Summary of what is in the table, for the report.

    Read after the load so the report can state the resulting state rather than
    only what this run did. "Loaded 4,173, table now holds 4,173" and "loaded
    4,173, table now holds 12,006" describe different situations.
    """
    connection = psycopg2.connect(config.pg_dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    count(*),
                    count(DISTINCT author_email),
                    count(*) FILTER (WHERE github_login IS NULL),
                    min(authored_at),
                    max(authored_at)
                FROM commits
                WHERE repo = %s
                """,
                (config.repo,),
            )
            total, authors, unmatched, earliest, latest = cursor.fetchone()
    finally:
        connection.close()

    return {
        "total": total,
        "distinct_authors": authors,
        "unmatched_authors": unmatched,
        "earliest": earliest,
        "latest": latest,
    }
