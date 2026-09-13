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

from .config import Config
from .pagination import iter_pages
from .probe import TIMEOUT, build_commits_url, build_headers
from .stats import RunStats

log = logging.getLogger(__name__)

# Warn when the token expires within this many days. Two weeks is enough notice
# to renew without it becoming background noise on every run.
TOKEN_EXPIRY_WARNING_DAYS = 14


def fetch_commits(config: Config) -> int:
    """Page through commits and print the run accounting."""
    # One RunStats for the whole run. When increment 9 adds pull requests, the
    # same object is passed to both — commits and PRs bill the same `core`
    # quota, so two separate views would each see half the spend and neither
    # would wait when it should.
    stats = RunStats()

    with requests.Session() as session:
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
            print(
                f"  page {page.page_number:>3}  "
                f"{len(page.records):>3} records  "
                f"remaining {page.rate_limit_remaining}  "
                f"{page.request_id}"
            )

    print_summary(config, stats)

    # Exit 1 when the pull was incomplete. A scheduler calling this needs to
    # know that a run which fetched 300 of 4,172 records did not do its job,
    # and exit 0 would tell it the opposite. This is the single most important
    # line in the function.
    return 0 if stats.complete else 1


def print_summary(config: Config, stats: RunStats) -> None:
    """Print the run accounting.

    A placeholder for the diagnostic report at increment 10, which will have a
    proper format and a file output. The content is already right; only the
    presentation is provisional.
    """
    print()
    print("RUN SUMMARY")
    print(f"  Repository       : {config.repo}")
    print(f"  Started          : {stats.started_at.isoformat()}")
    print(f"  Since            : {config.since_iso}")
    print()

    for counters in stats.resources.values():
        available = (
            str(counters.pages_available)
            if counters.pages_available is not None
            else "unknown (single page)"
        )
        status = "complete" if counters.complete else "INCOMPLETE"
        print(f"  {counters.name}")
        print(f"    Pages fetched  : {counters.pages_fetched} of {available}  ({status})")
        print(f"    Records        : {counters.records_retrieved}")
        if counters.stopped_early_reason:
            print(f"    Stopped early  : {counters.stopped_early_reason}")

    print()
    # Requests made, not pages fetched. They are equal only when nothing was
    # retried; a page that failed twice and succeeded on the third attempt
    # billed three requests against the quota, and reporting the page count
    # would be quietly wrong in exactly the case the report exists to explain.
    print(f"  Requests made    : {stats.retries.attempts}")

    if stats.retries.retries:
        # Per-cause rather than a bare total: repeated 502s are GitHub's
        # problem, repeated timeouts are probably the network at this end.
        causes = ", ".join(
            f"{cause} x{count}" for cause, count in sorted(stats.retries.by_cause.items())
        )
        print(f"  Retries          : {stats.retries.retries} ({causes})")
        print(f"  Retry waiting    : {stats.retries.seconds_waited:.1f}s")
    else:
        print("  Retries          : 0")

    print(f"  Quota            : {stats.rate_limit.remaining} of {stats.rate_limit.limit} remaining")
    if stats.rate_limit.reset_datetime:
        print(f"  Quota resets     : {stats.rate_limit.reset_datetime.isoformat()}")
    # Printed even when zero. "Waited 0s" is information — it says the run was
    # not throttled, which differs from the report having nothing to say.
    print(
        f"  Rate limit waits : {stats.rate_limit.waits} "
        f"({stats.rate_limit.seconds_waited:.0f}s total)"
    )
    print(f"  Elapsed          : {stats.elapsed_seconds:.1f}s")

    # Failures that a retry fixed are still worth reporting. A run that
    # succeeded after three 502s is healthy in its result and unhealthy in its
    # behaviour, and a degrading integration looks fine until it stops working.
    if stats.failures:
        recovered = len(stats.failures) - len(stats.unrecovered_failures)
        print()
        print(f"  Failed requests  : {len(stats.failures)} ({recovered} recovered)")
        for failure in stats.failures[:5]:
            marker = "recovered" if failure.recovered else "UNRECOVERED"
            print(f"    {failure.status_code} {failure.message} [{marker}]")
            if failure.request_id:
                # The identifier GitHub support needs to find this call in
                # their own logs. Almost every client throws it away.
                print(f"      request id: {failure.request_id}")
        if len(stats.failures) > 5:
            print(f"    ... and {len(stats.failures) - 5} more")

    days = stats.token_days_remaining
    if days is not None and days <= TOKEN_EXPIRY_WARNING_DAYS:
        print()
        print(f"  WARNING: token expires in {days} days ({stats.token_expires_at})")

    if not stats.complete:
        print()
        print("  This run did NOT retrieve the full result set. Exit code 1.")
