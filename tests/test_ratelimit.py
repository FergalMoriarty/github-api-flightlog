"""Tests for rate limit arithmetic.

X-RateLimit-Reset is a Unix timestamp — an absolute moment, not a delay. The
wait is reset_at minus now, and both sides must be UTC. Getting that wrong in
Spain produces a sleep one or two hours out, or a negative one.
"""

from __future__ import annotations

import time

from flightlog.ratelimit import RateLimitState

from .conftest import make_response, response_from_fixture


def test_reads_headers_from_a_real_response():
    state = RateLimitState()
    state.update(response_from_fixture("commits_page_1"))
    assert state.limit == 5000
    assert state.remaining is not None
    assert state.reset_at is not None


def test_absent_headers_do_not_clear_known_state():
    """A 401 carries no rate limit headers at all.

    Forgetting what was known because one response omitted it would be worse
    than stale data.
    """
    state = RateLimitState()
    state.update(make_response(200, {"X-RateLimit-Remaining": "4999",
                                     "X-RateLimit-Limit": "5000"}, []))
    state.update(make_response(401, {}, {"message": "Bad credentials"}))
    assert state.remaining == 4999
    assert state.limit == 5000


def test_unauthenticated_limit_is_visible():
    """60 rather than 5000 means the request went out anonymously.

    A run that silently fell back to unauthenticated looks identical to an
    authenticated one until it hits the limit eighty times sooner.
    """
    state = RateLimitState()
    state.update(make_response(200, {"X-RateLimit-Limit": "60",
                                     "X-RateLimit-Remaining": "59"}, []))
    assert state.limit == 60


def test_should_wait_below_the_threshold():
    assert RateLimitState(remaining=3).should_wait()
    assert not RateLimitState(remaining=100).should_wait()


def test_unknown_state_does_not_deadlock():
    """No headers yet means the first request of a run.

    Refusing to proceed on missing information would deadlock before starting.
    """
    assert not RateLimitState().should_wait()


def test_seconds_until_reset_is_clamped_at_zero():
    """A reset in the past means the window rolled over, or the local clock is
    ahead of GitHub's. time.sleep(-3) raises ValueError."""
    past = RateLimitState(reset_at=int(time.time()) - 30)
    assert past.seconds_until_reset() == 0.0


def test_seconds_until_reset_uses_utc_on_both_sides():
    """time.time() is seconds since the epoch in UTC, the same basis as the
    header. No timezone conversion is involved or wanted."""
    state = RateLimitState(reset_at=int(time.time()) + 45)
    assert 43 <= state.seconds_until_reset() <= 46


def test_absurd_reset_is_refused_rather_than_obeyed():
    """A wait longer than a full window means a bad header or a skewed clock.

    Sleeping for a day on the strength of an unvalidated integer from the
    network is not a failure mode worth having.
    """
    state = RateLimitState(limit=5000, remaining=0,
                           reset_at=int(time.time()) + 86400)
    assert state.wait_if_needed() == 0.0
    assert state.waits == 0


def test_wait_records_its_duration():
    """The report cannot say "waited 47 seconds" unless something counted."""
    state = RateLimitState(limit=5000, remaining=0,
                           reset_at=int(time.time()) - 5)
    slept = state.wait_if_needed()
    assert slept > 0
    assert state.waits == 1
    assert state.seconds_waited == slept
    # The reading is stale after a wait. Clearing it stops should_wait firing
    # again on the pre-reset value and sleeping twice.
    assert state.remaining is None


def test_reset_datetime_is_timezone_aware():
    """Rendering a UTC timestamp in local time without saying so is how a log
    line becomes misleading."""
    state = RateLimitState(reset_at=1788885374)
    assert state.reset_datetime.tzinfo is not None
    assert state.reset_datetime.utcoffset().total_seconds() == 0
