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

log = logging.getLogger(__name__)

# Warn when the token expires within this many days. Two weeks is enough notice
# to renew without it becoming background noise on every run.
TOKEN_EXPIRY_WARNING_DAYS = 14


def fetch_commits(config: Config, load: bool = False) -> int:
    """Page through commits, validate, and optionally load to Postgres."""
    # One RunStats for the whole run. When increment 9 adds pull requests, the
    # same object is passed to both — commits and PRs bill the same `core`
    # quota, so two separate views would each see half the spend and neither
    # would wait when it should.
    stats = RunStats()
    load_stats = LoadStats()

    # Accumulated rather than loaded page by page. A page-at-a-time load would
    # use less memory, and would also mean a run that fails on page 30 leaves
    # 29 pages committed — a partially loaded table that looks complete to
    # anything querying it. Loading once at the end makes the write all or
    # nothing. 4,173 records is a few megabytes; the trade only reverses at a
    # scale this tool does not claim to handle, and that is noted as a
    # limitation rather than pretended away.
    valid_records: list = []

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
            # Validate as each page arrives rather than after the whole pull. A
            # run that fetches 42 pages and then discovers every record is
            # malformed has spent 42 requests to learn what the first page would
            # have told it.
            rejected_on_page = 0
            for record in page.records:
                if validate_record(record, COMMIT_SCHEMA, stats=stats.validation):
                    rejected_on_page += 1
                else:
                    # Only validated records are kept. This is what makes the
                    # validation worth having rather than decorative.
                    valid_records.append(record)

            suffix = f"  {rejected_on_page} REJECTED" if rejected_on_page else ""
            print(
                f"  page {page.page_number:>3}  "
                f"{len(page.records):>3} records  "
                f"remaining {page.rate_limit_remaining}  "
                f"{page.request_id}{suffix}"
            )

    if load:
        print()
        print(f"  Loading {len(valid_records)} records to Postgres...")
        load_commits(config, valid_records, load_stats)

    print_summary(config, stats, load_stats if load else None)

    # Exit 1 when the pull was incomplete. A scheduler calling this needs to
    # know that a run which fetched 300 of 4,172 records did not do its job,
    # and exit 0 would tell it the opposite.
    return 0 if stats.complete else 1

def fetch_pull_requests(config: Config, load: bool = False) -> int:
    """Page through pull requests, validate, and optionally load.

    Two differences from the commits endpoint, neither of which required any
    change to the pagination, retry or rate limit code. That was the point of
    this increment: a second resource is a new schema, a new table and a new
    call, not a rewrite.

    state=all. The endpoint defaults to open pull requests ONLY. Omitting this
    parameter returns a filtered subset with no indication that filtering
    occurred — the Link header is consistent, the record count is plausible,
    and the answer is wrong. The same silent-wrong-answer shape as ignoring
    pagination, arriving by a different route.

    No `since` parameter. The commits endpoint accepts one; this one does not.
    Bounding by date therefore means sorting by `updated` descending and
    stopping client-side once records fall outside the window. Server-side
    filtering on one endpoint and client-side on its neighbour is a genuine
    inconsistency in the API, not a design choice here.
    """
    stats = RunStats()
    load_stats = LoadStats()
    valid_records: list = []

    # Client-side window bound, since the endpoint offers no `since`.
    # Compared as strings: both sides are ISO 8601 in UTC with identical
    # formatting, and ISO 8601 sorts lexicographically in the same order it
    # sorts chronologically. That holds ONLY because the formats match exactly.
    cutoff = config.since_iso

    # Sorted by `updated` descending, so once a full page falls outside the
    # window, every later page does too. Without this early exit, a one-year
    # window against dbt-labs/dbt-core fetched 67 pages to keep 15 — 52
    # requests and three minutes spent retrieving records that were discarded
    # on arrival.
    #
    # This is correct ONLY because of the sort order. Remove direction=desc and
    # the break becomes wrong rather than merely unhelpful.
    exhausted_window = False

    with requests.Session() as session:
        for page in iter_pages(
            session,
            build_pulls_url(config),
            resource="pull_requests",
            headers=build_headers(config),
            params={
                # Without this, only open PRs are returned.
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": config.per_page,
            },
            max_pages=config.max_pages,
            timeout=TIMEOUT,
            stats=stats,
        ):
            rejected_on_page = 0
            outside_window = 0

            for record in page.records:
                if validate_record(record, PULL_REQUEST_SCHEMA, stats=stats.validation):
                    rejected_on_page += 1
                    continue
                # Filtered after validation, not before. A record outside the
                # window is still a record the API returned, and its null rates
                # and field types are still worth counting — the validation
                # statistics describe what the API sent, not what this run
                # chose to keep.
                if record["updated_at"] < cutoff:
                    outside_window += 1
                    continue
                valid_records.append(record)

            parts = []
            if rejected_on_page:
                parts.append(f"{rejected_on_page} REJECTED")
            if outside_window:
                parts.append(f"{outside_window} outside window")
            suffix = f"  {', '.join(parts)}" if parts else ""

            print(
                f"  page {page.page_number:>3}  "
                f"{len(page.records):>3} records  "
                f"remaining {page.rate_limit_remaining}  "
                f"{page.request_id}{suffix}"
            )

            if page.records and outside_window == len(page.records):
                log.info(
                    "page %d was entirely outside the window; later pages are "
                    "older still, so stopping here",
                    page.page_number,
                )
                exhausted_window = True
                break

    if exhausted_window:
        # Not an incomplete pull. The window was exhausted, which is the
        # correct end condition for a date-bounded fetch against an endpoint
        # with no server-side `since`. Without this, the completeness check
        # would compare pages fetched against rel="last" and report a
        # deliberate, correct early exit as a failed run.
        counters = stats.resource("pull_requests")
        counters.pages_available = counters.pages_fetched

    if load:
        print()
        print(f"  Loading {len(valid_records)} pull requests to Postgres...")
        load_pull_requests(config, valid_records, load_stats)

    print_pr_summary(config, stats, load_stats if load else None)

    return 0 if stats.complete else 1


    return 0 if stats.complete else 1


def print_summary(
    config: Config, stats: RunStats, load_stats: LoadStats | None = None
) -> None:
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
    # Validation counts, printed even when nothing failed. "300 checked, 300
    # accepted" confirms validation ran; silence would be indistinguishable
    # from validation not running at all.
    v = stats.validation
    print(f"  Records checked  : {v.records_checked} ({v.records_accepted} accepted)")

    if v.records_rejected:
        print(f"  Records rejected : {v.records_rejected}")
        # Grouped by field and reason rather than listed per record. Four
        # thousand records failing the same check is one problem, and printing
        # it four thousand times buries everything else.
        grouped: dict[tuple[str, str], list] = {}
        for r in v.rejections:
            grouped.setdefault((r.field_path, r.reason), []).append(r)
        for (path, reason), group in sorted(grouped.items()):
            print(f"    {path}: {reason}  [{len(group)} records]")
            # One worked example per group. "expected str, got dict" is less
            # use than seeing the dict — the brief's own example is a nullable
            # field arriving as an object, which is only diagnosable if the
            # object is visible.
            sample = group[0]
            print(f"      e.g. {sample.identity}: {sample.value_excerpt}")

    # Null rates for every nullable field, reported whether or not anything
    # failed. The rate is the signal: a field normally null 2% of the time and
    # suddenly null 40% of the time indicates an upstream change, and no
    # individual record failed validation to reveal it.
    if v.null_counts:
        print("  Null fields      :")
        for path, count in sorted(v.null_counts.items()):
            print(f"    {path}: {count} of {v.records_checked} ({v.null_rate(path):.1%})")

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

    if load_stats is not None:
        counts = table_counts(config)
        print()
        print("  Postgres")
        print(f"    Rows submitted : {load_stats.rows_submitted} in {load_stats.batches} batches")
        print(f"    Rows affected  : {load_stats.rows_affected}")
        # The resulting table state, not just what this run did. "Loaded 4,173,
        # table holds 4,173" and "loaded 4,173, table holds 12,006" describe
        # different situations, and only the second tells you the load was
        # additive.
        print(f"    Table total    : {counts['total']} for this repo")
        print(f"    Distinct authors: {counts['distinct_authors']}")
        print(f"    Unmatched users : {counts['unmatched_authors']}")
        if counts["earliest"]:
            print(f"    Date range     : {counts['earliest']:%Y-%m-%d} to {counts['latest']:%Y-%m-%d}")

    if not stats.complete:
        print()
        print("  This run did NOT retrieve the full result set. Exit code 1.")

def print_pr_summary(
    config: Config, stats: RunStats, load_stats: LoadStats | None = None
) -> None:
    """Print the pull request run accounting.

    Duplicates most of print_summary. Both are placeholders for the diagnostic
    report at increment 10, which replaces them with one implementation reading
    from RunStats — worth doing once rather than twice, and worth doing there
    rather than here.
    """
    v = stats.validation
    print()
    print("RUN SUMMARY")
    print(f"  Repository       : {config.repo}")
    print(f"  Window           : updated since {config.since_iso}")
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
    print(f"  Requests made    : {stats.retries.attempts}")
    print(f"  Records checked  : {v.records_checked} ({v.records_accepted} accepted)")

    if v.records_rejected:
        print(f"  Records rejected : {v.records_rejected}")
        grouped: dict[tuple[str, str], list] = {}
        for r in v.rejections:
            grouped.setdefault((r.field_path, r.reason), []).append(r)
        for (path, reason), group in sorted(grouped.items()):
            print(f"    {path}: {reason}  [{len(group)} records]")
            print(f"      e.g. {group[0].identity}: {group[0].value_excerpt}")

    if v.null_counts:
        print("  Null fields      :")
        for path, count in sorted(v.null_counts.items()):
            print(f"    {path}: {count} of {v.records_checked} ({v.null_rate(path):.1%})")

    print(f"  Quota            : {stats.rate_limit.remaining} of {stats.rate_limit.limit} remaining")
    print(
        f"  Rate limit waits : {stats.rate_limit.waits} "
        f"({stats.rate_limit.seconds_waited:.0f}s total)"
    )
    print(f"  Elapsed          : {stats.elapsed_seconds:.1f}s")

    if load_stats is not None:
        counts = pr_table_counts(config)
        print()
        print("  Postgres")
        print(f"    Rows submitted : {load_stats.rows_submitted} in {load_stats.batches} batches")
        print(f"    Table total    : {counts['total']} for this repo")
        print(f"    Open           : {counts['open']}")
        print(f"    Merged         : {counts['merged']}")
        # Closed without merging. The state column alone cannot express this —
        # a merged PR and an abandoned one are both 'closed'.
        print(f"    Closed unmerged: {counts['closed_unmerged']}")
        if counts["median_merge_hours"] is not None:
            print(f"    Median to merge: {counts['median_merge_hours']:.1f} hours")

    if not stats.complete:
        print()
        print("  This run did NOT retrieve the full result set. Exit code 1.")