<!--
Real output from `flightlog ingest --load` against dbt-labs/dbt-core on
2026-09-13. Copied unedited from reports/, which is gitignored because a
report per run is output rather than source. Committed so the report — the
thing that makes this more than a fetch script — is visible without cloning
and running the tool.
-->

# Ingestion run — dbt-labs/dbt-core

**Started** 2026-09-13 19:50:07 UTC  
**Duration** 1m 38s  
**Window** since 2025-09-18T19:51:44Z

**Complete.** Retrieved 4,184 commits, 1,600 pull_requests in 1m 38s.

## Worth reading

- `pull_requests`: `merged_at` was null in 48.4% of records (774 of 1,600). Expected for a genuinely optional field; worth checking against previous runs if not.

## What was fetched

| Resource | Pages | Records | Complete |
| --- | --- | --- | --- |
| commits | 42 of 42 | 4,184 | yes |
| pull_requests | 16 of 16 | 1,600 | yes |

## Requests

- 58 requests made
- No retries
- Rate limit not reached
- Quota: 4,925 of 5,000 remaining (75 used this window, by all clients on this token)
- Quota resets 20:44:43 UTC

## Validation

### commits

- 4,184 records checked, 4,184 accepted
- No records rejected

| Field | Null | Rate | |
| --- | --- | --- | --- |
| `author` | 1 of 4,184 | 0.0% |  |

### pull_requests

- 1,600 records checked, 1,600 accepted
- No records rejected

| Field | Null | Rate | |
| --- | --- | --- | --- |
| `closed_at` | 265 of 1,600 | 16.6% |  |
| `merged_at` | 774 of 1,600 | 48.4% | flagged |

Null and absence rates are over every record the API returned for that resource, including any this run discarded before loading.

## Loaded to PostgreSQL

- `commits`: 4,184 rows submitted in 9 batches, 4,184 affected
- `pull_requests`: 1,443 rows submitted in 3 batches, 1,443 affected

### Table state after the run

**commits**

- total: 4,184
- distinct authors: 143
- unmatched authors: 1
- earliest: 2025-05-30
- latest: 2026-09-12

**pull_requests**

- total: 1,444
- open: 265
- merged: 699
- closed unmerged: 480
- median merge hours: 4.9
