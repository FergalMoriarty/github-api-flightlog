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
from .errors import classify

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
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    max_pages: int | None = None,
    timeout: tuple[int, int] = (5, 30),
) -> Iterator[PageResult]:
    """Yield pages until the Link header stops offering a next one.

    A generator rather than a function returning a list, for two reasons. The
    caller can begin processing page one while later pages are still being
    fetched, and — more importantly here — a 41-page pull never has to hold all
    4,050 records in memory at once. Against a repository with 200,000 commits
    that distinction stops being academic.

    max_pages caps the pull for development. It is NOT a safety net against a
    runaway loop: termination comes from the absence of rel="next", and if that
    logic were wrong a cap would merely hide it at a different number.
    """
    # A Session rather than bare requests.get, for connection reuse. Without
    # one, each of the 41 requests opens a new TCP connection and repeats the
    # TLS handshake — measurably slower over a long pull, and needless load on
    # the other end.
    page_number = 1
    # Parameters apply to the FIRST request only. Every subsequent URL comes
    # from the Link header with its parameters already embedded, and passing
    # them again would append duplicates to a URL that already has them.
    next_params: dict[str, Any] | None = params

    while True:
        log.debug("fetching page %d: %s", page_number, url)
        response = session.get(url, headers=headers, params=next_params, timeout=timeout)

        # Classify before anything else. An error body is a JSON object where
        # success is an array, so parsing first would mean handling the shape
        # difference in two places. Exceptions propagate — unlike in probe,
        # where they were caught for display. A failure mid-pull must stop the
        # run rather than be swallowed into a partial result that looks
        # complete.
        classify(response)

        records = response.json()
        if not isinstance(records, list):
            # A 200 with a non-list body from a list endpoint should not
            # happen. Raising rather than coercing, because silently treating
            # an unexpected shape as "no records" is precisely the silent
            # under-fetch this module exists to prevent.
            raise TypeError(
                f"expected a JSON array from {response.url}, got {type(records).__name__}"
            )

        links = parse_link_header(response.headers.get("Link"))

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
        # The tempting alternatives are all wrong in ways that are hard to
        # detect. Stopping when a page returns fewer than per_page records
        # fails when the total is an exact multiple of the page size. Stopping
        # when a page is empty costs an extra request and still relies on the
        # server behaving as expected. Counting up to rel="last" breaks when
        # the result set changes size mid-pull, which on an active repository
        # it does.
        next_url = links.get("next")
        if not next_url:
            log.debug("no rel=next on page %d; result set exhausted", page_number)
            return

        if max_pages is not None and page_number >= max_pages:
            # Stopping early is a legitimate development mode, but it produces
            # an incomplete result set that looks exactly like a complete one.
            # A warning, not a debug line, because the whole point of this tool
            # is that a partial pull is never silent.
            log.warning(
                "stopping at page %d because MAX_PAGES=%d; "
                "a next page was available, so this result set is INCOMPLETE",
                page_number,
                max_pages,
            )
            return

        url = next_url
        # Cleared after the first request. Every URL from here carries its own
        # parameters, embedded by GitHub. This one line is the difference
        # between following the server's cursor and reconstructing it.
        next_params = None
        page_number = (_page_number_from_url(next_url) or page_number + 1)
