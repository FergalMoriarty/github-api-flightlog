"""Fetch all pages of a resource and report the accounting.

Not the final ingestion path — nothing is loaded anywhere yet. This exists to
prove the pagination works and to make the counts visible, since a silent
under-fetch is the failure this increment is guarding against.

The summary printed here is a placeholder for the diagnostic report at
increment 10. It reads from RunStats rather than from local counters, so that
the report can be generated from the same object without this function having
to hand anything over.
"""

from __future__ import annotations

import logging

import requests
from .load import (
    LoadStats,
    load_commits,
    load_pull_requests,
    pr_table_counts,
    table_counts,
)
from .probe import TIMEOUT, build_commits_url, build_headers, build_pulls_url
from .schema import COMMIT_SCHEMA, PULL_REQUEST_SCHEMA, validate_record
from .config import Config
from .pagination import iter_pages
from .stats import RunStats
from .report import build_report, write_report

log = logging.getLogger(__name__)

# Warn when the token expires within this many days. Two weeks is enough notice
# to renew without it becoming background noise on every run.
TOKEN_EXPIRY_WARNING_DAYS = 14


def ingest(config: Config, load: bool = False, write_file: bool = True) -> int:
    """Fetch commits and pull requests in one run, and report on it.

    One RunStats across both resources, which is the point. They bill the same
    `core` quota, so a shared rate limit view means the second resource knows
    what the first spent — two separate views would each see half the spend and
    neither would wait when it should. The retry totals and the failure list
    are shared for the same reason: the report wants one figure for the run.

    Resources are fetched in sequence, not concurrently. Concurrency against a
    rate-limited API needs a shared quota view that is safe across threads, and
    the preemptive check here is not. A sequential run of 58 requests takes
    about a minute and a half, which is not worth the correctness risk.
    """
    stats = RunStats()
    load_stats: dict[str, LoadStats] = {}
    counts: dict[str, dict] = {}

    with requests.Session() as session:
        # --- Commits -----------------------------------------------------
        print("Commits")
        commit_records: list = []
        for page in iter_pages(
            session,
            build_commits_url(config),
            resource="commits",
            headers=build_headers(config),
            params={"since": config.since_iso, "per_page": config.per_page},
            max_pages=config.max_pages,
            timeout=TIMEOUT,
            stats=stats,
        ):
            rejected = 0
            for record in page.records:
                if validate_record(
                    record, COMMIT_SCHEMA, stats=stats.resource("commits").validation
                ):
                    rejected += 1
                else:
                    commit_records.append(record)
            _print_page(page, rejected=rejected)

        # --- Pull requests -----------------------------------------------
        print()
        print("Pull requests")
        pr_records: list = []
        cutoff = config.since_iso
        exhausted_window = False

        for page in iter_pages(
            session,
            build_pulls_url(config),
            resource="pull_requests",
            headers=build_headers(config),
            params={
                # Without this the endpoint returns only open PRs, with no
                # indication that filtering occurred.
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": config.per_page,
            },
            max_pages=config.max_pages,
            timeout=TIMEOUT,
            stats=stats,
        ):
            rejected = 0
            outside = 0
            for record in page.records:
                if validate_record(
                    record,
                    PULL_REQUEST_SCHEMA,
                    stats=stats.resource("pull_requests").validation,
                ):
                    rejected += 1
                    continue
                if record["updated_at"] < cutoff:
                    outside += 1
                    continue
                pr_records.append(record)

            _print_page(page, rejected=rejected, outside=outside)

            # Descending sort means a page entirely outside the window
            # guarantees every later page is too. Without this, a one-year
            # window cost 67 pages to keep 16.
            if page.records and outside == len(page.records):
                log.info(
                    "page %d was entirely outside the window; stopping here",
                    page.page_number,
                )
                exhausted_window = True
                break

    if exhausted_window:
        # The window was exhausted, which is the correct end condition here —
        # not an incomplete pull. Without this the completeness check compares
        # pages fetched against rel="last" and reports a deliberate, correct
        # early exit as a failure.
        pr_counters = stats.resource("pull_requests")
        pr_counters.pages_available = pr_counters.pages_fetched

    # --- Load ------------------------------------------------------------
    if load:
        print()
        print(f"Loading {len(commit_records):,} commits and {len(pr_records):,} pull requests...")
        load_stats["commits"] = load_commits(config, commit_records)
        load_stats["pull_requests"] = load_pull_requests(config, pr_records)
        counts["commits"] = table_counts(config)
        counts["pull_requests"] = pr_table_counts(config)

    # --- Report ----------------------------------------------------------
    report = build_report(
        config,
        stats,
        load_stats=load_stats or None,
        table_counts=counts or None,
    )

    print()
    print(report)

    if write_file:
        path = write_report(report)
        print()
        print(f"Report written to {path}")

    return 0 if stats.complete else 1


def _print_page(page, rejected: int = 0, outside: int = 0) -> None:
    """One progress line per page.

    Progress output, not the report. It exists so a four-minute run is visibly
    doing something; the report is what gets read afterwards.
    """
    parts = []
    if rejected:
        parts.append(f"{rejected} REJECTED")
    if outside:
        parts.append(f"{outside} outside window")
    suffix = f"  {', '.join(parts)}" if parts else ""
    print(
        f"  page {page.page_number:>3}  "
        f"{len(page.records):>3} records  "
        f"remaining {page.rate_limit_remaining}  "
        f"{page.request_id}{suffix}"
    )