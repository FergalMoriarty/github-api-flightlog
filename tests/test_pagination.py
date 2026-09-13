"""Tests for Link header parsing and page iteration.

The most consequential module in the tool. A pagination bug does not raise —
it returns fewer records and reports success. Every other failure here
announces itself; under-fetching does not.
"""

from __future__ import annotations

import pytest

from flightlog.errors import NotFoundError
from flightlog.pagination import iter_pages, parse_link_header
from flightlog.stats import RunStats

from .conftest import FakeSession, make_response, response_from_fixture


# --- Link header parsing --------------------------------------------------


def test_parses_next_and_last():
    header = (
        '<https://api.github.com/repositories/1/commits?page=2>; rel="next", '
        '<https://api.github.com/repositories/1/commits?page=42>; rel="last"'
    )
    links = parse_link_header(header)
    assert links["next"] == "https://api.github.com/repositories/1/commits?page=2"
    assert links["last"] == "https://api.github.com/repositories/1/commits?page=42"


def test_absent_header_is_not_an_error():
    """No Link header means a single-page result set, not a failure.

    GitHub omits the header entirely when everything fits in one page.
    Treating that as an error would break every small repository.
    """
    assert parse_link_header(None) == {}
    assert parse_link_header("") == {}


def test_parses_real_captured_header():
    """Against the header GitHub actually sent, not a reconstruction."""
    response = response_from_fixture("commits_page_1")
    links = parse_link_header(response.headers.get("Link"))
    assert "next" in links
    assert "last" in links
    # The captured URL uses GitHub's internal numeric repository id rather than
    # the owner/name path that was requested. This is the finding that made
    # "follow the URL verbatim" a rule rather than a preference.
    assert "/repositories/" in links["next"]


def test_url_containing_a_comma_is_not_split():
    """Splitting on commas would break a URL that contains one.

    Query parameter values may contain commas. The regex anchors on the angle
    brackets, which cannot appear unencoded in a URL, so this parses correctly
    where a naive split would produce two broken links.
    """
    header = '<https://api.github.com/x?q=a,b,c&page=2>; rel="next"'
    links = parse_link_header(header)
    assert links["next"] == "https://api.github.com/x?q=a,b,c&page=2"


# --- Page iteration -------------------------------------------------------


def _page(records, next_url=None, last_page=None, remaining=4999):
    """Build one page response with the Link header a real one would carry."""
    parts = []
    if next_url:
        parts.append('<' + next_url + '>; rel="next"')
    if last_page:
        parts.append(
            '<https://api.github.com/x?page=' + str(last_page) + '>; rel="last"'
        )
    headers = {"X-RateLimit-Remaining": str(remaining), "X-RateLimit-Limit": "5000"}
    if parts:
        headers["Link"] = ", ".join(parts)
    return make_response(200, headers, records)


def test_follows_link_to_exhaustion():
    """Three pages, terminating on the absence of rel=next."""
    session = FakeSession([
        _page([{"n": 1}], next_url="https://api.github.com/x?page=2", last_page=3),
        _page([{"n": 2}], next_url="https://api.github.com/x?page=3", last_page=3),
        _page([{"n": 3}], last_page=3),
    ])
    stats = RunStats()

    pages = list(
        iter_pages(
            session, "https://api.github.com/x", resource="commits",
            headers={}, stats=stats,
        )
    )

    assert len(pages) == 3
    assert stats.resource("commits").records_retrieved == 3
    assert stats.resource("commits").pages_available == 3
    assert stats.resource("commits").complete


def test_follows_the_servers_url_verbatim():
    """The next URL comes from the Link header, not from a page counter.

    GitHub rewrites /repos/owner/name/ into /repositories/{id}/ and embeds the
    query parameters. Rebuilding the URL would discard the server's cursor, and
    the numeric form survives a repository rename where the named form does
    not.
    """
    rewritten = "https://api.github.com/repositories/53548867/commits?page=2&per_page=5"
    session = FakeSession([
        _page([{"n": 1}], next_url=rewritten),
        _page([{"n": 2}]),
    ])

    list(
        iter_pages(
            session, "https://api.github.com/repos/owner/repo/commits",
            resource="commits", headers={}, params={"per_page": 5},
            stats=RunStats(),
        )
    )

    assert session.calls[1]["url"] == rewritten
    # Parameters are sent on the first request only. The Link URL already
    # carries them, and sending them again would duplicate them.
    assert session.calls[0]["params"] == {"per_page": 5}
    assert session.calls[1]["params"] is None


def test_single_page_with_no_link_header_is_complete():
    """A result set that fits one page is complete, not incomplete.

    GitHub omits Link entirely here. pages_available stays None, and the
    completeness check has to treat that as done rather than unknown.
    """
    session = FakeSession([response_from_fixture("commits_single_page")])
    stats = RunStats()

    pages = list(
        iter_pages(
            session, "https://api.github.com/x", resource="commits",
            headers={}, stats=stats,
        )
    )

    assert len(pages) == 1
    assert stats.resource("commits").pages_available is None
    assert stats.resource("commits").complete


def test_empty_result_set_is_not_complete():
    """Zero pages fetched is not a complete run.

    This is the bug found in increment 6: an unknown page count with zero pages
    fetched reported complete, so a run that failed on its first request
    claimed success.
    """
    stats = RunStats()
    stats.resource("commits")
    assert not stats.resource("commits").complete
    assert not stats.complete


def test_max_pages_marks_the_result_incomplete():
    """Stopping early must be visible.

    A capped run looks exactly like a complete one unless something records
    why it stopped.
    """
    session = FakeSession([
        _page([{"n": 1}], next_url="https://api.github.com/x?page=2", last_page=10),
        _page([{"n": 2}], next_url="https://api.github.com/x?page=3", last_page=10),
    ])
    stats = RunStats()

    pages = list(
        iter_pages(
            session, "https://api.github.com/x", resource="commits",
            headers={}, max_pages=2, stats=stats,
        )
    )

    assert len(pages) == 2
    counters = stats.resource("commits")
    assert counters.stopped_early_reason == "MAX_PAGES=2"
    assert not counters.complete
    assert not stats.complete


def test_error_mid_pull_propagates_and_is_recorded():
    """A failure part-way through must stop the run, not truncate it silently.

    Swallowing the error would produce a partial result set indistinguishable
    from a complete one — the same failure this module exists to prevent,
    arriving from a different direction.
    """
    session = FakeSession([
        _page([{"n": 1}], next_url="https://api.github.com/x?page=2", last_page=5),
        response_from_fixture("error_404"),
    ])
    stats = RunStats()

    with pytest.raises(NotFoundError):
        list(
            iter_pages(
                session, "https://api.github.com/x", resource="commits",
                headers={}, stats=stats,
            )
        )

    assert len(stats.failures) == 1
    assert stats.failures[0].status_code == 404
    # The identifier GitHub support needs to find the call in their own logs.
    assert stats.failures[0].request_id is not None
    assert not stats.complete


def test_non_list_body_raises():
    """A 200 with an object body from a list endpoint is not zero records.

    Coercing it to an empty list would be a silent under-fetch. Raising makes
    it visible.
    """
    session = FakeSession([make_response(200, {}, {"message": "unexpected"})])

    with pytest.raises(TypeError):
        list(
            iter_pages(
                session, "https://api.github.com/x", resource="commits",
                headers={}, stats=RunStats(),
            )
        )
