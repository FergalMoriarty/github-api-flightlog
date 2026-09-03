# github-api-flightlog

Ingests commit and pull request activity from the GitHub REST API into
PostgreSQL, and records what happened during the run: pages fetched, records
retrieved and loaded, records rejected by validation and why, rate limit waits,
and retries by status code.

The reporting is the point. A partial sync that returns half the expected rows
and exits zero is the common failure with API ingestion, and it is invisible
unless the tool accounts for its own run. This one does.

Built against the GitHub REST API specifically. The pagination and rate limit
handling assume GitHub's header conventions — `Link` with `rel="next"`, and the
`X-RateLimit-*` family — and would need work against an API that paginates or
throttles differently.

**Status: under construction.** Configuration loading is in place; ingestion is
not. This README will be rewritten with design decisions and limitations once
the ingestion path is complete.

## Requirements

Python 3.10 or later. Docker, for the PostgreSQL target (not yet used).

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements-dev.txt
    cp .env.example .env

Then edit `.env`. A GitHub personal access token with public repository read
access raises the rate limit from 60 requests/hour to 5,000. Running without
one is supported and is how the rate limit handling gets exercised
deliberately.

`.env` is gitignored and must not be committed.

## Usage

    python -m flightlog.cli check

Prints the resolved configuration — target repository, page size, pull window,
and whether a token was loaded — without making any network calls. Exits 2 on a
configuration error, naming the setting and the rule it broke.

## Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `GITHUB_TOKEN` | *(none)* | Blank runs unauthenticated at 60 requests/hour |
| `GITHUB_API_URL` | `https://api.github.com` | Overridable for GitHub Enterprise |
| `TARGET_REPO` | `dbt-labs/dbt-core` | `owner/name` |
| `PER_PAGE` | `100` | GitHub's maximum; its own default is 30 |
| `MAX_PAGES` | *(none)* | Development cap; blank fetches to exhaustion |
| `SINCE_MONTHS` | `12` | Pull window, approximated as 30-day months |
| `LOG_LEVEL` | `INFO` | |

## Notes on AI assistance

`docs/working-with-ai.md` records where AI assistance during development was
wrong and how it was caught.
