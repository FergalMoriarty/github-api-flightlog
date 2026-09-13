# github-api-flightlog

Ingests commit and pull request activity from the GitHub REST API into
PostgreSQL, and records what happened during the run: pages fetched against
pages available, records rejected and why, rate limit waits, retries by the
status code that caused each, and the null rate of every nullable field.

The reporting is the point. The common failure with API ingestion is not a
crash — it is a run that returns part of the data, exits zero, and reports
success. Ignoring the `Link` header on a repository with 4,000 commits returns
the first 100 of them without raising anything. This tool accounts for its own
run, so a partial sync is diagnosable rather than invisible.

Built against the GitHub REST API specifically. The pagination and rate limit
handling assume GitHub's header conventions — `Link` with `rel="next"`, and the
`X-RateLimit-*` family — and would need work against an API that paginates or
throttles differently.

[Sample run report](docs/sample-report.md) — real output, unedited.

## What a run looks like

```
**Complete.** Retrieved 4,184 commits, 1,600 pull_requests in 1m 38s.

## Worth reading

- pull_requests: merged_at was null in 48.4% of records (774 of 1,600).

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
```

The run exits 0 only when every resource was fetched to exhaustion. A capped or
truncated run exits 1, because a scheduler needs to distinguish "fetched
everything" from "fetched 7% of it".

## Requirements

Python 3.10 or later, Docker.

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements-dev.txt
    cp .env.example .env
    docker compose up -d

Then add a GitHub personal access token to `.env`. Public repository read
access is sufficient. Running without one works and is limited to 60 requests
per hour rather than 5,000 — which is how the rate limit handling gets
exercised deliberately.

`.env` is gitignored and holds both the token and the database password.

## Usage

    flightlog check                    # validate configuration, no network calls
    flightlog probe                    # one request, full response inspection
    flightlog ingest                   # fetch both resources, print a report
    flightlog ingest --load            # and load to PostgreSQL

Each is `python -m flightlog.cli <command>`.

`check` prints the resolved configuration — target repository, page size, pull
window, whether a token was loaded, database connection — without making a
request. It exits 2 on a configuration error, naming the setting and the rule
it broke.

`probe` makes one request and prints the status code, every response header,
and the first record. It exists because "what does the API actually return for
this repository, right now" is the first question when an ingestion run looks
wrong.

`ingest` fetches commits and pull requests, validates both, optionally loads
them, and writes a timestamped report to `reports/`.

## Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `GITHUB_TOKEN` | *(none)* | Blank runs unauthenticated at 60 requests/hour |
| `GITHUB_API_URL` | `https://api.github.com` | Overridable for GitHub Enterprise |
| `TARGET_REPO` | `dbt-labs/dbt-core` | `owner/name` |
| `PER_PAGE` | `100` | GitHub's maximum; its own default is 30 |
| `MAX_PAGES` | *(none)* | Development cap; blank fetches to exhaustion |
| `SINCE_MONTHS` | `12` | Pull window, approximated as 30-day months |
| `POSTGRES_*` | see `.env.example` | Host, port, database, user, password |
| `LOG_LEVEL` | `INFO` | |

Environment variables override `.env`, so a single run can be adjusted without
editing the file: `MAX_PAGES=1 flightlog ingest`.

## Design decisions

**Pagination terminates on the absence of `rel="next"`, not on a record count.**
The tempting alternatives all fail in ways that are hard to detect. Stopping
when a page returns fewer than `per_page` records fails when the total is an
exact multiple of the page size. Counting up to `rel="last"` breaks when the
result set changes size mid-pull, which on an active repository it does — the
observed page count for one repository moved from 41 to 42 between two runs
minutes apart.

**The next URL is followed verbatim.** GitHub rewrites `/repos/owner/name/` into
`/repositories/{id}/` and embeds the query parameters in the `Link` header.
Rebuilding the URL from a page counter would discard the server's own cursor,
and the numeric form survives a repository rename where the named form does
not.

**403 is handled differently from 401.** A 401 means the credentials were not
accepted: GitHub authenticates before it does anything else, so a 401 response
carries no rate limit headers and no permission information — the rest of the
request was never examined. A 403 means authentication succeeded and
authorisation did not, so the response carries both, including
`x-accepted-github-permissions` naming exactly what was required. The remedies
are unrelated: a 401 needs a different token, a 403 needs a different scope.

**403 is also how GitHub reports rate limiting**, in some circumstances, where
the HTTP specification has 429. The status code alone therefore cannot
distinguish quota exhaustion from insufficient permission, so classification
reads `X-RateLimit-Remaining` and the response body.

**404 has three causes and the API refuses to distinguish them.** A repository
that does not exist, a repository the token cannot see, and a parameter that
does not resolve all return byte-identical responses. This is deliberate on
GitHub's part: returning 403 for a private repository and 404 for a nonexistent
one would let anyone enumerate private repositories by watching status codes.
The cost falls on whoever is debugging — a 404 cannot be resolved from the
response alone, and a user who insists the repository exists is often right.

**Rate limiting is handled both before and after the fact.** The preemptive
check reads the quota reported on the previous response and waits before the
limit is reached. It cannot see quota spent by anything else using the same
token, which is not hypothetical — the first run of this tool found 33 requests
already consumed. The reactive path recovers from a refusal that happened
anyway.

**Retry is decided by exception type, not by a status code list.** The
exception hierarchy splits 4xx from 5xx along the client/server line, so the
retry logic asks `isinstance(exc, ServerError)` rather than maintaining a
second list that could drift out of step. Retrying a 401 sends the same bad
token four times; retrying a 502 often succeeds.

**Backoff is exponential with jitter.** A fixed delay against an overloaded
server adds load at the worst moment. Jitter matters when several clients fail
simultaneously — without it they retry in synchronised waves, which is the load
pattern backoff exists to avoid.

**Schema validation comes from what the API declares, not from a sample.**
Checking 100 commit records for a null `author` found none, and the obvious
conclusion is wrong: GitHub's schema declares the field nullable, and a full
4,184-record pull found exactly one — a developer whose commit email is not
registered on GitHub. 0.02%, invisible in any reasonable sample, and a
validator built by sampling would have raised `KeyError` several thousand
records into a run, after a partial write.

**Null rates are reported whether or not anything failed.** A field normally
null 2% of the time and suddenly null 40% of the time indicates an upstream
change, and no individual record fails validation to reveal it.

**The load is idempotent on the source's own key**, commit SHA and PR id. Every
run is a full pull from the window, so consecutive runs overlap almost entirely
— two runs an hour apart share roughly 4,150 of 4,184 commits. `ON CONFLICT DO
UPDATE` rather than `DO NOTHING`, because the records do change: an open PR
gets merged, and a developer who registers a long-used email address gains a
`github_login` on every historical commit.

**Pull requests need `state=all`.** The endpoint returns only open pull
requests by default, with no indication that filtering occurred. The same
silent-wrong-answer shape as ignoring pagination, from a different direction.

**Pull requests have no `since` parameter**, unlike commits. Bounding by date
means sorting by `updated` descending and discarding older records client-side
— and stopping once a whole page falls outside the window, since the sort order
guarantees nothing later will qualify. Without that early exit, a one-year
window cost 67 pages to keep 16.

## Tests

    pytest

63 tests, no network calls. Responses were captured once from the live API by
`tests/capture_fixtures.py` — run by hand, and the only thing in the test
directory that makes a request — and are replayed from `tests/fixtures/`. The
suite needs no token, no database, and spends no quota.

Several tests encode bugs found during development rather than bugs imagined
after it: a completeness check that reported success for a run which fetched
nothing, and null rates divided by the wrong denominator when a second resource
was added.

## Limitations

**Tested against one API.** GitHub's pagination and rate limiting are
well-behaved and consistently documented. Many APIs are neither. The `Link`
parser handles the subset of RFC 8288 that GitHub emits, not the full
specification.

**No incremental sync.** Every run is a full pull from a starting point. That
is why the load is idempotent, and it means a run costs the same whether or not
anything changed. A record that has moved outside the window since a previous
run stays in the table and is not refreshed.

**Schema validation checks presence and type, not business correctness.** A
commit dated 1970 with an empty message and a fabricated SHA passes every
check, because it is structurally exactly what it claims to be.

**The pull window uses 30-day months.** Good enough for "roughly the last
year", wrong for anything needing calendar accuracy, and it drifts forward
daily so consecutive runs cover slightly different windows.

**The loader has no automated test.** It needs a database, which makes it an
integration test rather than a unit one. It is exercised by running the tool;
it is not covered by `pytest`.

**Resources are fetched sequentially, not concurrently.** Concurrency against a
rate-limited API needs a quota view that is safe across threads, and the
preemptive check is not. A full run of both resources takes about 90 seconds,
which did not justify the correctness risk.

**Everything is held in memory before loading.** A 4,184-record pull is a few
megabytes. A repository an order of magnitude larger would need the load
streaming page by page, which would in turn mean a failed run could leave a
partially loaded table.

## Notes on AI assistance

`docs/working-with-ai.md` records where AI assistance during development was
wrong and how it was caught — including a rate limit test that patched a module
constant and silently proved nothing, and a regex cleanup that deleted `main()`
along with the dead code it was aimed at.
