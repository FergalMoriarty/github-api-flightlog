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

log = logging.getLogger(__name__)


def fetch_commits(config: Config) -> int:
    """Page through commits and print the run accounting."""
    started = time.monotonic()

    total_records = 0
    pages = 0
    expected_pages: int | None = None
    last_remaining: int | None = None

    with requests.Session() as session:
        for page in iter_pages(
            session,
            build_commits_url(config),
            headers=build_headers(config),
            params={"since": config.since_iso, "per_page": config.per_page},
            max_pages=config.max_pages,
            timeout=TIMEOUT,
        ):
            pages += 1
            total_records += len(page.records)
            last_remaining = page.rate_limit_remaining

            # rel="last" appears on the first response and names the final page
            # number. Capturing it lets the summary state pages fetched against
            # pages available, which is what turns "41 pages" into "41 of 41".
            if expected_pages is None and "last" in page.links:
                match = page.links["last"].split("page=")[-1].split("&")[0]
                if match.isdigit():
                    expected_pages = int(match)

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
    print(f"  Requests used    : {pages}")
    print(f"  Quota remaining  : {last_remaining}")
    print(f"  Elapsed          : {elapsed:.1f}s")

    if config.max_pages is not None and expected_pages and pages < expected_pages:
        print()
        print(f"  MAX_PAGES={config.max_pages} capped this run. Unset it in .env for a full pull.")

    return 0
