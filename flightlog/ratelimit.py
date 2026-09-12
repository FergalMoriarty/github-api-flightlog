"""Preemptive rate limit handling.

Reads the quota state GitHub reports on every response and waits before the
limit is reached rather than after. The reactive path — recovering from a
refusal that already happened — is increment 5, and both are needed: this one
cannot see quota spent by anything else using the same token, and that is not
hypothetical. The first probe run of this project showed 33 requests already
used, none of them made by this tool.

GitHub reports quota on every authenticated response:

    X-RateLimit-Limit      5000    the quota for the current window
    X-RateLimit-Used         33    consumed so far
    X-RateLimit-Remaining  4967    what is left
    X-RateLimit-Reset  17888...    when the window resets, as a Unix timestamp
    X-RateLimit-Resource   core    which quota bucket was billed

Absent entirely from a 401 response, because GitHub authenticates before it
does anything else and had no token to bill. Missing rate limit headers are
therefore a signal in their own right: the request was never attributed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

# Wait when remaining drops to this or below, rather than at zero.
#
# Not zero, because the check is based on the PREVIOUS response. Anything else
# using the same token — the GitHub web UI, an editor's git integration, another
# process — spends quota between our requests, so a reading of 1 does not
# guarantee 1 is still available. A small margin absorbs that.
DEFAULT_THRESHOLD = 5

# Never sleep longer than this in one wait, in seconds.
#
# A rate limit window is at most an hour, so a computed wait longer than that
# means something is wrong: a malformed header, a badly skewed clock, or a
# response from something that is not GitHub. Sleeping for a day on the
# strength of an unvalidated integer from the network is not a failure mode
# worth having.
MAX_WAIT_SECONDS = 3900  # 65 minutes

# Added to every computed wait.
#
# Waking at exactly the reset second is a coin flip — the server's window may
# not have rolled over yet, and the retry is refused, which costs another
# request and another wait. Two seconds removes the race.
WAIT_BUFFER_SECONDS = 2


@dataclass
class RateLimitState:
    """Quota as of the most recent response.

    Deliberately a snapshot rather than a running total maintained locally. The
    server is the authority: it sees requests from every client using this
    token, and a locally-maintained count would drift away from the truth in
    exactly the situation where accuracy matters.
    """

    limit: int | None = None
    remaining: int | None = None
    reset_at: int | None = None
    resource: str | None = None

    # Accounting for the run report.
    waits: int = 0
    seconds_waited: float = 0.0

    @property
    def known(self) -> bool:
        """Whether any quota state has been observed yet.

        False before the first response, and false throughout a run where the
        headers never appear — which is what a 401 looks like.
        """
        return self.remaining is not None

    @property
    def reset_datetime(self) -> datetime | None:
        """Reset time as an aware UTC datetime, for display.

        Aware, not naive. The timestamp is UTC by definition and rendering it
        in local time without saying so is how a log line becomes misleading.
        """
        if self.reset_at is None:
            return None
        return datetime.fromtimestamp(self.reset_at, tz=timezone.utc)

    def update(self, response: requests.Response) -> None:
        """Refresh from a response's headers.

        Absent headers leave the previous values in place rather than clearing
        them. A 401 carries no quota headers at all, and forgetting what was
        known because one response omitted it would be worse than stale data.
        """
        limit = _header_int(response, "X-RateLimit-Limit")
        remaining = _header_int(response, "X-RateLimit-Remaining")
        reset_at = _header_int(response, "X-RateLimit-Reset")
        resource = response.headers.get("X-RateLimit-Resource")

        if limit is not None:
            self.limit = limit
        if remaining is not None:
            self.remaining = remaining
        if reset_at is not None:
            self.reset_at = reset_at
        if resource is not None:
            self.resource = resource

    def seconds_until_reset(self, now: float | None = None) -> float:
        """Seconds from now until the quota window resets.

        Zero when the reset is unknown or already past.

        time.time() returns seconds since the Unix epoch in UTC, which is the
        same basis as the header. No timezone conversion is involved or wanted
        — introducing one is how this calculation goes two hours wrong in
        Spain.

        A negative result means the window has already rolled over, or the
        local clock is ahead of GitHub's. Both are handled by clamping to zero
        rather than by trusting the sign.
        """
        if self.reset_at is None:
            return 0.0
        return max(0.0, self.reset_at - (now if now is not None else time.time()))

    def should_wait(self, threshold: int = DEFAULT_THRESHOLD) -> bool:
        """Whether to pause before the next request.

        False when quota state is unknown — no headers means either the first
        request of a run or an unauthenticated context, and refusing to
        proceed on missing information would deadlock the run before it
        started.
        """
        if self.remaining is None:
            return False
        return self.remaining <= threshold

    def wait_if_needed(self, threshold: int = DEFAULT_THRESHOLD) -> float:
        """Sleep until the quota resets, if the threshold has been reached.

        Returns the seconds actually slept, which the run report needs — "hit
        the rate limit twice and waited 47 seconds" is only sayable if
        something counted.
        """
        if not self.should_wait(threshold):
            return 0.0

        wait = self.seconds_until_reset() + WAIT_BUFFER_SECONDS

        if wait > MAX_WAIT_SECONDS:
            # Refuse rather than obey. A wait longer than a full window means
            # the header or the local clock is wrong, and sleeping on it would
            # hang the process for hours with no explanation.
            log.error(
                "computed a wait of %.0fs, longer than the %ds ceiling; "
                "not sleeping. Reset header was %s, local time is %s",
                wait,
                MAX_WAIT_SECONDS,
                self.reset_at,
                int(time.time()),
            )
            return 0.0

        # WARNING, not INFO. A run that pauses for 40 minutes should say so
        # loudly — a silent pause is indistinguishable from a hang, and
        # somebody watching will kill the process.
        log.warning(
            "rate limit nearly exhausted (%s of %s remaining); "
            "waiting %.0fs until reset at %s",
            self.remaining,
            self.limit,
            wait,
            self.reset_datetime.isoformat() if self.reset_datetime else "unknown",
        )

        started = time.monotonic()
        # monotonic for measuring elapsed time, not time.time(). A wall clock
        # can be adjusted mid-sleep by NTP, which would make the measured
        # duration wrong or negative. monotonic only ever moves forward.
        time.sleep(wait)
        slept = time.monotonic() - started

        self.waits += 1
        self.seconds_waited += slept

        # The reading is stale now. The next response will report the refreshed
        # quota; clearing it here prevents should_wait from firing again
        # immediately on the pre-reset value and sleeping a second time.
        self.remaining = None

        return slept


def _header_int(response: requests.Response, name: str) -> int | None:
    """Read an integer header, tolerating absence and garbage.

    Header lookup in requests is case-insensitive, which matters: GitHub serves
    over HTTP/2 where header names are lowercase on the wire.
    """
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        log.debug("could not parse %s as an integer: %r", name, raw)
        return None
