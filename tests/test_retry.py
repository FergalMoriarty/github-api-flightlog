"""Tests for retry and backoff.

with_retry takes a zero-argument callable rather than a URL, so these tests
need no HTTP layer at all — a function that raises on command is enough. That
separation was worth having for its own sake and makes this straightforward.
"""

from __future__ import annotations

import requests

import pytest

from flightlog.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    ServerError,
)
from flightlog.retry import (
    RetryStats,
    compute_delay,
    is_retryable,
    with_retry,
)

CTX = {"status_code": 500, "url": "https://api.github.com/x"}


def test_delay_grows_exponentially():
    """Fixed delays against an overloaded server add load at the worst moment.

    Compared without jitter so the schedule itself is what is asserted.
    """
    assert compute_delay(1, base=1.0, jitter=0) == 1.0
    assert compute_delay(2, base=1.0, jitter=0) == 2.0
    assert compute_delay(3, base=1.0, jitter=0) == 4.0
    assert compute_delay(4, base=1.0, jitter=0) == 8.0


def test_delay_is_capped():
    """Without a ceiling, a higher attempt count produces delays that grow
    without bound — 2^10 seconds is seventeen minutes for one page."""
    assert compute_delay(20, base=1.0, maximum=60.0, jitter=0) == 60.0


def test_jitter_spreads_the_delay():
    """When many clients fail at once — a server restart, a network blip —
    identical backoff means they retry in synchronised waves, which is the
    load pattern backoff exists to avoid."""
    delays = {compute_delay(3, base=1.0, jitter=0.25) for _ in range(50)}
    assert len(delays) > 1
    assert all(3.0 <= d <= 5.0 for d in delays)


def test_server_errors_are_retryable():
    assert is_retryable(ServerError("boom", **CTX))


def test_rate_limit_is_retryable():
    assert is_retryable(RateLimitError("slow down", **{**CTX, "status_code": 403}))


def test_network_failures_are_retryable():
    """A timeout may never have reached the server.

    Safe to retry because GET is idempotent — asking twice yields the same
    data. This would need thought for POST or DELETE, and this tool issues
    only GETs.
    """
    assert is_retryable(requests.exceptions.ConnectTimeout("no route"))
    assert is_retryable(requests.exceptions.ConnectionError("dropped"))


def test_client_errors_are_not_retryable():
    """Retrying a 401 sends the same bad token four times and produces four
    identical failures more slowly."""
    assert not is_retryable(AuthenticationError("bad creds", **{**CTX, "status_code": 401}))
    assert not is_retryable(NotFoundError("gone", **{**CTX, "status_code": 404}))


def test_bugs_are_not_retryable():
    """An unexpected exception is a bug, not a transient fault. Retrying a
    TypeError four times buries the traceback."""
    assert not is_retryable(ValueError("a bug in our code"))
    assert not is_retryable(KeyError("author"))


def test_succeeds_after_transient_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServerError("502", **{**CTX, "status_code": 502})
        return "page data"

    stats = RetryStats()
    result = with_retry(flaky, stats=stats, max_attempts=4)

    assert result == "page data"
    assert stats.attempts == 3
    assert stats.retries == 2
    # Per-cause, because repeated 502s are GitHub's problem and repeated
    # timeouts are probably the network at this end.
    assert stats.by_cause == {"502": 2}


def test_gives_up_and_raises_the_last_failure():
    """Raising rather than returning a sentinel. A caller who forgets to check
    a sentinel gets a silent wrong answer; one who ignores an exception gets a
    traceback."""
    def always_fails():
        raise ServerError("503", **{**CTX, "status_code": 503})

    stats = RetryStats()
    with pytest.raises(ServerError):
        with_retry(always_fails, stats=stats, max_attempts=3)
    assert stats.attempts == 3
    assert stats.retries == 2


def test_client_error_fails_on_the_first_attempt():
    def bad_token():
        raise AuthenticationError("Bad credentials", **{**CTX, "status_code": 401})

    stats = RetryStats()
    with pytest.raises(AuthenticationError):
        with_retry(bad_token, stats=stats, max_attempts=4)
    assert stats.attempts == 1
    assert stats.retries == 0


def test_rate_limit_uses_the_callback_not_backoff():
    """RateLimitState knows the reset timestamp; backoff would be guessing at
    a number the server already supplied."""
    calls = {"n": 0, "waited": 0}

    def limited():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError("rate limited", **{**CTX, "status_code": 403})
        return "ok"

    def on_rate_limit(_exc):
        calls["waited"] += 1
        return 0.0

    stats = RetryStats()
    assert with_retry(limited, stats=stats, on_rate_limit=on_rate_limit) == "ok"
    assert calls["waited"] == 1
