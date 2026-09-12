"""Retry with exponential backoff and jitter.

The reactive half of failure handling. Increment 4 avoids requests that will be
refused; this recovers from refusals that happen anyway — which they do, because
the preemptive check cannot see quota spent by other clients using the same
token or IP, and because a transient 5xx gives no warning at all.

What is retryable is decided by exception type, not by a list of status codes
maintained here. That decision was made in errors.py, where ClientError and
ServerError split 4xx from 5xx precisely so that this module can ask
`isinstance(exc, ServerError)` and be right.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import requests

from .errors import GitHubError, RateLimitError, ServerError

log = logging.getLogger(__name__)

T = TypeVar("T")

# Total attempts, not retries after the first. 4 means one request and up to
# three more.
#
# Chosen against the delay schedule below: with a 1s base, four attempts spend
# at most 1 + 2 + 4 = 7 seconds before giving up. More attempts would mean a
# single bad page could stall a 42-page run for minutes with no output, which
# is worse than failing and letting the operator decide.
DEFAULT_MAX_ATTEMPTS = 4

# First delay, doubling each time: 1s, 2s, 4s.
BASE_DELAY_SECONDS = 1.0

# Ceiling on any single delay. Without it, a higher attempt count produces
# delays that grow without bound — 2^10 seconds is seventeen minutes for one
# page.
MAX_DELAY_SECONDS = 60.0

# Jitter as a fraction of the computed delay: 0.25 means the actual wait falls
# somewhere in [0.75d, 1.25d].
#
# Jitter exists for the case where many clients fail at the same moment — a
# server restart, a network blip. Without it they all back off by the same
# amount and retry in synchronised waves, which is the load pattern the backoff
# was meant to avoid. With it they spread out.
#
# It matters less for a single-process tool than for a fleet, and it is here
# anyway: it costs nothing, and the behaviour under concurrency is then correct
# by construction rather than by accident.
JITTER_FRACTION = 0.25

# Exceptions raised by requests itself, before any response exists. A timeout
# or a dropped connection may never have reached the server, so retrying is
# reasonable — with the caveat below.
RETRYABLE_NETWORK_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)


@dataclass
class RetryStats:
    """What the retry logic did, for the run report.

    "3 retries, two on 502 and one on a read timeout" is a statement the report
    can only make if something counted. Per-cause counts rather than a single
    total, because the causes call for different responses: repeated 502s are
    GitHub's problem, repeated timeouts are probably the network at this end.
    """

    attempts: int = 0
    retries: int = 0
    # Keyed by status code as a string, or by exception class name for network
    # failures that produced no response.
    by_cause: dict[str, int] = field(default_factory=dict)
    seconds_waited: float = 0.0

    def record(self, cause: str, delay: float) -> None:
        self.retries += 1
        self.by_cause[cause] = self.by_cause.get(cause, 0) + 1
        self.seconds_waited += delay


def compute_delay(
    attempt: int,
    *,
    base: float = BASE_DELAY_SECONDS,
    maximum: float = MAX_DELAY_SECONDS,
    jitter: float = JITTER_FRACTION,
) -> float:
    """Delay before the next attempt, in seconds.

    attempt is 1-based: the delay after the first failure is `base`.

    Exponential rather than fixed, because a fixed delay against an overloaded
    server adds load at the worst possible moment. Doubling gives it room.

    Capped before jitter is applied, so the jitter can still push a capped
    delay slightly above the ceiling — by design. The cap bounds the growth
    curve, not the exact value, and a hard ceiling applied after jitter would
    reintroduce synchronisation at the top of the range, which is exactly what
    jitter is for.
    """
    delay = min(base * (2 ** (attempt - 1)), maximum)
    if jitter:
        # Uniform in [1-j, 1+j]. Symmetric rather than "full jitter" (uniform
        # in [0, d]) because the lower bound matters here: a retry 0.1s after a
        # 502 is close enough to no backoff at all.
        delay *= random.uniform(1.0 - jitter, 1.0 + jitter)
    return delay


def is_retryable(exc: BaseException) -> bool:
    """Whether this failure is worth trying again.

    Type-based, not status-code-based. errors.py split ClientError from
    ServerError along the 4xx/5xx line for exactly this question, so the answer
    lives in the hierarchy rather than in a second list here that could drift
    out of step with it.
    """
    if isinstance(exc, RateLimitError):
        # Retryable, but the waiting is RateLimitState's job — it knows the
        # reset time, where backoff would only guess. Handled at the call site.
        return True
    if isinstance(exc, ServerError):
        # 5xx. The server failed and may not fail again.
        return True
    if isinstance(exc, RETRYABLE_NETWORK_ERRORS):
        # No response at all.
        #
        # Worth being honest about the risk: a read timeout means the request
        # may have been received and processed while the response was lost.
        # Retrying a GET is safe because GET is idempotent — asking twice
        # yields the same data. This helper must not be used for POST or DELETE
        # without thought, and this tool only issues GETs.
        return True
    if isinstance(exc, GitHubError):
        # Every other API error is a 4xx: the request was wrong, and sending it
        # again produces the same answer more slowly.
        return False
    # Anything else is a bug in this code, not a transient fault. Raising
    # immediately beats retrying a TypeError four times and burying the
    # traceback.
    return False


def cause_label(exc: BaseException) -> str:
    """Short label for the retry accounting.

    Status code where there was a response, exception class name where there
    was not. Both are useful in a report and they are different kinds of fact:
    "502" points at GitHub, "ConnectTimeout" points at this end of the wire.
    """
    if isinstance(exc, GitHubError):
        return str(exc.status_code)
    return type(exc).__name__


def with_retry(
    operation: Callable[[], T],
    *,
    stats: RetryStats | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_rate_limit: Callable[[RateLimitError], float] | None = None,
) -> T:
    """Run `operation`, retrying transient failures with exponential backoff.

    Takes a zero-argument callable rather than a URL and parameters, so that it
    knows nothing about HTTP. The retry policy and the request construction are
    separate concerns, and keeping them apart means this is testable with a
    function that raises on command rather than with a mocked HTTP layer.

    on_rate_limit, if given, is called with a RateLimitError and returns the
    seconds it waited. That is RateLimitState.wait_if_needed's job: it has the
    reset timestamp, where backoff would be guessing. Without the callback a
    RateLimitError falls through to ordinary backoff, which is better than
    nothing and much worse than waiting for the actual reset.

    Raises the last exception when attempts are exhausted, rather than
    returning a sentinel. A caller that forgets to check a sentinel gets a
    silent wrong answer; one that ignores an exception gets a traceback.
    """
    stats = stats if stats is not None else RetryStats()
    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        stats.attempts += 1
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 — re-raised below if not retryable
            last_exc = exc

            if not is_retryable(exc):
                # Fail immediately. Retrying a 401 sends the same bad token
                # four times and produces four identical failures more slowly.
                raise

            if attempt >= max_attempts:
                log.error(
                    "giving up after %d attempts; last failure: %s",
                    attempt,
                    exc,
                )
                raise

            if isinstance(exc, RateLimitError) and on_rate_limit is not None:
                # Wait for the actual reset, not a guessed backoff.
                delay = on_rate_limit(exc)
                log.warning(
                    "attempt %d/%d hit the rate limit; waited %.0fs before retrying",
                    attempt,
                    max_attempts,
                    delay,
                )
            else:
                delay = compute_delay(attempt)
                log.warning(
                    "attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    max_attempts,
                    cause_label(exc),
                    delay,
                )
                time.sleep(delay)

            stats.record(cause_label(exc), delay)

    # Unreachable: the loop either returns, or raises on the final attempt.
    # Present so that the function has no path that falls off the end
    # returning None, which would be a silent wrong answer of the worst kind.
    raise last_exc if last_exc else RuntimeError("with_retry exhausted with no exception")
