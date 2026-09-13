"""Link header parsing and page iteration.

The single most consequential piece of code in this tool. A pagination bug does
not raise; it returns fewer records and reports success. Every other failure
mode in this project announces itself — a 404 raises, a bad token raises, a
malformed record is rejected and counted. Under-fetching is silent.

That shapes the design: the page iterator counts what it did and hands those
counts back, so that "we fetched 41 pages and 4,050 records" is a statement the
run report can make rather than something a human has to verify by hand.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

from .config import Config
from .errors import GitHubError, classify
from .retry import with_retry
from .stats import RunStats

log = logging.getLogger(__name__)

# RFC 8288 web linking. The header is a comma-separated list of links, each
# being a URI-reference in angle brackets followed by semicolon-separated
# parameters:
#
#   <https://api.github.com/...&page=2>; rel="next", <...&page=41>; rel="last"
#
# Parsed with a regex rather than by splitting on commas, because a URL may
# itself contain a comma in a query parameter value and splitting would break
# such a header in a way that is intermittent and hard to reproduce. The regex
# anchors on the angle brackets, which cannot appear unencoded inside a URL.
#
# The full RFC permits more than this — multiple rel values, additional
# parameters, quoted strings with escapes. This handles the subset GitHub
# actually emits, which is a deliberate limitation rather than an oversight,
# and it is documented as one.
_LINK_PATTERN = re.compile(r'<(?P<url>[^>]+)>;\s*rel="(?P<rel>[^"]+)"')


def parse_link_header(header: str | None) -> dict[str, str]:
    """Parse a Link header into {relation: url}.

    Returns an empty dict when the header is absent, which is the normal case
    for a result set that fits in one page. An empty dict is therefore not an
    error — it is one of the two ways a run ends.
    """
    if not header:
        return {}
    return {m.group("rel"): m.group("url") for m in _LINK_PATTERN.finditer(header)}


@dataclass
class PageResult:
    """One page of results, with the accounting that goes with it.

    Carries more than the records because the diagnostic report needs it. A
    function that returned only the parsed JSON would force every caller to
    re-derive the page number, the request ID and the rate limit state from a
    response object it no longer has.
    """

    records: list[dict[str, Any]]
    page_number: int
    url: str
    request_id: str | None
    rate_limit_remaining: int | None
    # The full parsed Link header. Kept whole rather than just `next` so the
    # report can state total pages from rel="last" on the first response —
    # which is what makes "fetched 41 of 41 pages" possible rather than merely
    # "fetched 41 pages".
    links: dict[str, str] = field(default_factory=dict)


def _header_int(response: requests.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _page_number_from_url(url: str) -> int | None:
    """Extract the page number from a URL, for logging only.

    Deliberately best-effort. The page number is not used to construct
    anything — that would be the mistake this module exists to avoid — so a
    failure to parse it costs nothing but a slightly less informative log line.
    """
    match = re.search(r"[?&]page=(\d+)", url)
    return int(match.group(1)) if match else None


def iter_pages(
    session: requests.Session,
    url: str,
    *,
    resource: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    max_pages: int | None = None,
    timeout: tuple[int, int] = (5, 30),
    stats: RunStats,
) -> Iterator[PageResult]:
    """Yield pages until the Link header stops offering a next one.

    A generator rather than a list, so a 42-page pull never holds all 4,172
    records in memory at once. Against a repository with 200,000 commits that
    stops being academic.

    `resource` names which counter set this pull contributes to — "commits",
    "pull_requests". Required rather than defaulted, because a mislabelled
    resource silently merges two pulls into one set of counts, which is the
    kind of wrong number that looks right.

    `stats` is required, not optional. An earlier version made the accounting
    optional and it was immediately clear that a fetch which might or might not
    have counted is worse than one that always does — the report cannot say
    "42 of 42 pages" if the caller forgot to pass a counter.

    max_pages caps the pull for development. It is NOT a safety net against a
    runaway loop: termination comes from the absence of rel="next", and if that
    logic were wrong a cap would only hide it at a different number.
    """
    counters = stats.resource(resource)
    page_number = 1
    # Parameters apply to the FIRST request only. Every subsequent URL comes
    # from the Link header with its parameters already embedded, and passing
    # them again would append duplicates to a URL that already has them.
    next_params: dict[str, Any] | None = params

    def fetch_one(request_url: str, request_params: dict[str, Any] | None):
        """One request, classified. The unit that gets retried.

        Defined inside iter_pages so it closes over session, headers and
        timeout — with_retry takes a zero-argument callable precisely so that
        the retry policy knows nothing about HTTP.

        The rate limit update happens before classify raises. A 403 for quota
        exhaustion still carries the rate limit headers, and they are exactly
        what is needed to know how long to wait, so they must be captured
        before the exception unwinds the stack.
        """
        response = session.get(
            request_url, headers=headers, params=request_params, timeout=timeout
        )
        stats.rate_limit.update(response)

        # Captured on every response, overwriting the previous value. GitHub
        # reports it on each authenticated call and it does not change within a
        # run; reading it here rather than in a special case keeps it simple.
        expiry = response.headers.get("github-authentication-token-expiration")
        if expiry:
            stats.token_expires_at = expiry

        try:
            classify(response)
        except GitHubError as exc:
            # Recorded before re-raising, so the report knows about failures
            # that a retry later fixed. A run that succeeded after three 502s
            # is healthy in its result and unhealthy in its behaviour — a
            # degrading integration looks fine right up until it stops working.
            #
            # The returned object is stashed on the exception so that the retry
            # wrapper can mark it recovered without the accounting having to
            # match failures to successes by URL.
            exc.failure_record = stats.record_failure(
                url=response.url,
                status_code=response.status_code,
                message=exc.message,
                request_id=exc.request_id,
            )
            raise

        return response

    def wait_for_reset(_exc) -> float:
        """Rate limit callback for with_retry.

        Delegates to RateLimitState, which knows the reset timestamp. Backoff
        would only be guessing at a number the server already told us.

        threshold=0 because we are here having already been refused: the
        question is no longer "are we close to the limit" but "wait until it
        resets".
        """
        return stats.rate_limit.wait_if_needed(threshold=0)

    while True:
        # Preemptive check, before the request rather than after a refusal.
        # No-op on the first iteration, when nothing is known yet.
        stats.rate_limit.wait_if_needed()

        log.debug("fetching %s page %d: %s", resource, page_number, url)

        # Bound at call time with default arguments rather than by closure.
        # A bare `lambda: fetch_one(url, next_params)` would capture the
        # variables, not their values, and both are reassigned at the bottom of
        # this loop — so a retry would re-fetch whatever page the loop had
        # moved on to. Exactly the class of bug that produces a plausible wrong
        # answer rather than an error.
        response = with_retry(
            lambda u=url, p=next_params: fetch_one(u, p),
            stats=stats.retries,
            on_rate_limit=wait_for_reset,
        )

        # Anything recorded as a failure on an earlier attempt of this page was
        # evidently transient, since this attempt returned. Marking them keeps
        # "3 failures, all recovered" distinct from "3 failures, run aborted".
        for failure in stats.failures:
            if failure.url == response.url and not failure.recovered:
                failure.recovered = True

        records = response.json()
        if not isinstance(records, list):
            # A 200 with a non-list body from a list endpoint should not
            # happen. Raising rather than coercing, because treating an
            # unexpected shape as "no records" is the silent under-fetch this
            # module exists to prevent.
            raise TypeError(
                f"expected a JSON array from {response.url}, got {type(records).__name__}"
            )

        links = parse_link_header(response.headers.get("Link"))

        # rel="last" appears on the first response and names the final page.
        # Captured here rather than by the caller so that every consumer of
        # iter_pages gets the completeness check for free.
        if counters.pages_available is None and "last" in links:
            tail = links["last"].split("page=")[-1].split("&")[0]
            if tail.isdigit():
                counters.pages_available = int(tail)

        counters.pages_fetched += 1
        counters.records_retrieved += len(records)

        yield PageResult(
            records=records,
            page_number=page_number,
            url=response.url,
            request_id=response.headers.get("X-GitHub-Request-Id"),
            rate_limit_remaining=_header_int(response, "X-RateLimit-Remaining"),
            links=links,
        )

        # Termination. The absence of rel="next" is the ONLY correct signal
        # that a result set is exhausted.
        #
        # The alternatives are all wrong in ways that are hard to detect.
        # Stopping when a page returns fewer than per_page records fails when
        # the total is an exact multiple of the page size. Stopping on an empty
        # page costs an extra request and still trusts the server to behave.
        # Counting up to rel="last" breaks when the result set changes size
        # mid-pull, which on an active repository it does — the observed count
        # moved from 41 pages to 42 between two runs minutes apart.
        next_url = links.get("next")
        if not next_url:
            log.debug("no rel=next on %s page %d; result set exhausted", resource, page_number)
            return

        if max_pages is not None and page_number >= max_pages:
            # Stopping early is a legitimate development mode, but it produces
            # an incomplete result set that looks exactly like a complete one.
            # Recorded on the counters as well as logged, so the report states
            # why the pull was short rather than only that it was.
            counters.stopped_early_reason = f"MAX_PAGES={max_pages}"
            log.warning(
                "stopping at %s page %d because MAX_PAGES=%d; "
                "a next page was available, so this result set is INCOMPLETE",
                resource,
                page_number,
                max_pages,
            )
            return

        url = next_url
        # Cleared after the first request. Every URL from here carries its own
        # parameters, embedded by GitHub. This one line is the difference
        # between following the server's cursor and reconstructing it.
        next_params = None
        page_number = _page_number_from_url(next_url) or page_number + 1