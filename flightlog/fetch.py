"""Fetch all pages of a resource and report the accounting.

Not the final ingestion path — nothing is loaded anywhere yet. This exists to
prove the pagination works and to make the counts visible, since a silent
under-fetch is the failure this increment is guarding against.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import Config
from .pagination import iter_pages
from .probe import TIMEOUT, build_commits_url, build_headers
from .ratelimit import RateLimitState
from .retry import RetryStats

log = logging.getLogger(__name__)


def fetch_commits(config: Config) -> int:
    """Page through commits and print the run accounting."""
    started = time.monotonic()

    # One state object each for the whole run. When increment 9 adds pull
    # requests, the same objects are passed to both — commits and PRs bill the
    # same `core` quota, so two separate views would each see half the spend
    # and neither would wait when it should. The same argument applies to the
    # retry totals: the report wants one figure for the run, not one per
    # resource.
    rate_limit = RateLimitState()
    retry_stats = RetryStats()

    total_records = 0
    pages = 0
    expected_pages: int | None = None

    with requests.Session() as session:
        for page in iter_pages(
            session,
            build_commits_url(config),
            headers=build_headers(config),
            params={"since": config.since_iso, "per_page": config.per_page},
            max_pages=config.max_pages,
            timeout=TIMEOUT,
            rate_limit=rate_limit,
            retry_stats=retry_stats,
        ):
            pages += 1
            total_records += len(page.records)

            # rel="last" appears on the first response and names the final page
            # number, which is what turns "42 pages" into "42 of 42".
            if expected_pages is None and "last" in page.links:
                tail = page.links["last"].split("page=")[-1].split("&")[0]
                if tail.isdigit():
                    expected_pages = int(tail)

            print(
                f"  page {page.page_number:>3}  "
                f"{len(page.records):>3} records  "
                f"remaining {page.rate_limit_remaining}  "
                f"{page.request_id}"
            )

    elapsed = time.monotonic() - started

    print()
    print("RUN SUMMARY")
    print(f"  Repository       : {config.repo}")
    print(f"  Since            : {config.since_iso}")
    print(f"  Pages fetched    : {pages}")
    if expected_pages is not None:
        # The comparison that makes an under-fetch loud rather than silent.
        status = "complete" if pages >= expected_pages else "INCOMPLETE"
        print(f"  Pages available  : {expected_pages}  ({status})")
    print(f"  Records          : {total_records}")
    print(f"  Requests made    : {retry_stats.attempts}")
    if retry_stats.retries:
        # Per-cause rather than a bare total, because the causes call for
        # different responses: repeated 502s are GitHub's problem, repeated
        # timeouts are probably the network at this end.
        causes = ", ".join(
            f"{cause} x{count}" for cause, count in sorted(retry_stats.by_cause.items())
        )
        print(f"  Retries          : {retry_stats.retries} ({causes})")
        print(f"  Retry waiting    : {retry_stats.seconds_waited:.1f}s")
    else:
        print(f"  Retries          : 0")
    print(f"  Quota            : {rate_limit.remaining} of {rate_limit.limit} remaining")
    if rate_limit.reset_datetime:
        print(f"  Quota resets     : {rate_limit.reset_datetime.isoformat()}")
    # Printed even when zero. "Waited 0s" is information — it says the run was
    # not throttled, which is different from the report having nothing to say.
    print(f"  Rate limit waits : {rate_limit.waits} ({rate_limit.seconds_waited:.0f}s total)")
    print(f"  Elapsed          : {elapsed:.1f}s")

    if config.max_pages is not None and expected_pages and pages < expected_pages:
        print()
        print(f"  MAX_PAGES={config.max_pages} capped this run. Unset it in .env for a full pull.")

    return 0