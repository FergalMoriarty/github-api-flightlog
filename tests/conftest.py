"""Shared test fixtures and helpers.

pytest discovers this file automatically and injects anything decorated with
@pytest.fixture into any test that names it as a parameter. No imports needed
in the test files themselves.

Nothing here touches the network. The recorded responses under fixtures/ were
captured once by capture_fixtures.py, which is run by hand and is the only
thing in this directory that makes a request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from flightlog.config import Config

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Read a recorded response from disk."""
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def make_response(
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    body: Any = None,
    url: str = "https://api.github.com/repos/owner/repo/commits",
) -> requests.Response:
    """Build a requests.Response without making a request.

    Constructing the real class rather than a mock object, because the code
    under test reads real attributes — .ok, .headers, .json(), .status_code,
    .url — and a mock would confirm only that those attribute names were
    spelled consistently in the test and the implementation.

    In particular .headers must be a CaseInsensitiveDict: GitHub serves over
    HTTP/2, where header names are lowercase on the wire, while the code looks
    them up as "X-RateLimit-Remaining". A plain dict would pass a test that the
    real API fails.
    """
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    if headers:
        response.headers.update(headers)
    if body is not None:
        # _content is the private attribute .json() and .text read from.
        # Setting it is how a Response is populated without a transport layer.
        response._content = json.dumps(body).encode("utf-8")
    else:
        response._content = b""
    return response


def response_from_fixture(name: str) -> requests.Response:
    """Rebuild a Response from a recorded fixture.

    This is what makes the tests meaningful: the headers and body are exactly
    what GitHub sent, not an approximation of what it was assumed to send.
    """
    data = load_fixture(name)
    return make_response(
        status_code=data["status_code"],
        headers=data["headers"],
        body=data["body"],
    )


class FakeSession:
    """A requests.Session substitute that replays queued responses.

    iter_pages takes a session and calls .get() on it. Supplying an object with
    a .get() method is enough — no HTTP layer to intercept, no library to patch.
    That is a consequence of iter_pages accepting the session as a parameter
    rather than creating one internally, which was worth doing for its own sake
    and turns out to make this trivial.

    Records every call, so a test can assert on the URLs requested — which is
    how "did it follow the Link header or rebuild the URL itself" gets checked.
    """

    def __init__(self, responses: list[requests.Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError(
                f"FakeSession ran out of responses; call {len(self.calls)} was {url}. "
                "Either the code requested more pages than expected — which is the "
                "bug this catches — or the test queued too few."
            )
        return self._responses.pop(0)


@pytest.fixture
def config() -> Config:
    """A Config that touches nothing.

    Built directly rather than through load_config so the tests do not depend
    on a .env file, on environment variables, or on a token being present.
    """
    return Config(
        token="test-token-value",
        api_url="https://api.github.com",
        repo="owner/repo",
        per_page=100,
        max_pages=None,
        since_months=12,
        log_level="INFO",
        pg_host="localhost",
        pg_port=5434,
        pg_database="flightlog",
        pg_user="flightlog",
        pg_password="flightlog",
    )


@pytest.fixture
def commit_record() -> dict[str, Any]:
    """One real commit record, from the recorded first page."""
    return load_fixture("commits_page_1")["body"][0]


@pytest.fixture
def pr_record() -> dict[str, Any]:
    """One real pull request record."""
    return load_fixture("pulls_page_1")["body"][0]
