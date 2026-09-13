"""Run accounting.

One object per run, holding everything the diagnostic report needs. Passed down
into the fetch loop rather than assembled from return values, because the
interesting facts — a retry on page 7, a rate limit wait between pages 20 and
21 — happen inside the loop and would otherwise be lost by the time it ends.

The design rule for what belongs here: anything that would let someone reading
the report afterwards explain why a run produced the records it did. Counts,
because a partial pull must be distinguishable from a complete one. Causes,
because "3 retries" and "3 retries, all 502" call for different responses.
Identifiers, because a failure GitHub can look up is worth more than one it
cannot.

RunStats owns the sub-objects rather than duplicating their fields. RateLimitState
already tracks waits and RetryStats already tracks retries; copying those numbers
up would create two sources of truth that drift apart the moment one is updated
and the other is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ratelimit import RateLimitState
from .retry import RetryStats


@dataclass
class FailedRequest:
    """A request that failed, with what is needed to investigate it later.

    Recorded even when the failure was retried successfully. A run that
    eventually succeeded after three 502s is healthy in its result and unhealthy
    in its behaviour, and the report should say so — otherwise a degrading
    integration looks fine right up until it stops working.
    """

    url: str
    status_code: int | None
    message: str
    # GitHub's own identifier, from x-github-request-id. Present on every
    # response including failures. This is what GitHub support needs to find
    # the call in their logs, and almost every client discards it.
    request_id: str | None
    # Whether a later attempt succeeded. Distinguishes a transient blip from
    # the failure that ended the run.
    recovered: bool = False


@dataclass
class ResourceStats:
    """Counts for one resource — commits, pull requests, and so on.

    Per-resource rather than one flat set of totals, because increment 9 adds a
    second endpoint and "4,172 records" would then be an answer to a question
    nobody asked. Which records, from where, and was that all of them.
    """

    name: str
    pages_fetched: int = 0
    # From rel="last" on the first response. None when the result set fits in
    # one page, since GitHub omits the Link header entirely in that case —
    # which is not the same as zero pages available.
    pages_available: int | None = None
    records_retrieved: int = 0
    # Set when MAX_PAGES stopped the pull, or when any other early exit did.
    # Kept separately from the pages comparison because the report should be
    # able to say WHY a pull was incomplete, not merely that it was.
    stopped_early_reason: str | None = None

    @property
    def complete(self) -> bool:
        """Whether the result set was exhausted.

        Three cases, and the third is the one that was wrong first time round:

        Stopped early — incomplete, and the reason is recorded.

        Known page count — compare fetched against available.

        Unknown page count — GitHub omits the Link header when a result set
        fits in one page, so an unknown count with one page fetched means
        everything was retrieved. But an unknown count with ZERO pages fetched
        means no successful response ever arrived: the pull raised before it
        got anywhere. Treating that as complete reported success for a run that
        fetched nothing, which is precisely the silent wrong answer this whole
        tool exists to make impossible. Caught by triggering a 404 mid-run and
        reading the accounting rather than assuming it.
        """
        if self.stopped_early_reason is not None:
            return False
        if self.pages_available is None:
            return self.pages_fetched > 0
        return self.pages_fetched >= self.pages_available

@dataclass
class RunStats:
    """Everything about one run.

    Constructed by the caller and passed down. The sub-objects are shared by
    every resource in the run, deliberately: commits and pull requests bill the
    same `core` quota, so two separate RateLimitState objects would each see
    half the spend and neither would wait when it should.
    """

    # monotonic, not time.time(). Measuring elapsed time with a wall clock is
    # wrong if NTP adjusts it mid-run — the result can come out negative.
    started_monotonic: float = field(default_factory=time.monotonic)
    # Wall clock, separately, because the report needs to say WHEN the run
    # happened and monotonic time is meaningless outside this process.
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    rate_limit: RateLimitState = field(default_factory=RateLimitState)
    retries: RetryStats = field(default_factory=RetryStats)

    resources: dict[str, ResourceStats] = field(default_factory=dict)
    failures: list[FailedRequest] = field(default_factory=list)

    # From github-authentication-token-expiration. GitHub reports this on every
    # authenticated response and almost nothing reads it. A run that warns
    # "credentials expire in 3 days" prevents a failure nobody was watching for.
    token_expires_at: str | None = None

    def resource(self, name: str) -> ResourceStats:
        """Get or create the counters for a named resource."""
        if name not in self.resources:
            self.resources[name] = ResourceStats(name=name)
        return self.resources[name]

    def record_failure(
        self,
        *,
        url: str,
        status_code: int | None,
        message: str,
        request_id: str | None,
    ) -> FailedRequest:
        """Record a failed request and return it, so it can be marked recovered."""
        failure = FailedRequest(
            url=url,
            status_code=status_code,
            message=message,
            request_id=request_id,
        )
        self.failures.append(failure)
        return failure

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    @property
    def total_records(self) -> int:
        return sum(r.records_retrieved for r in self.resources.values())

    @property
    def total_pages(self) -> int:
        return sum(r.pages_fetched for r in self.resources.values())

    @property
    def complete(self) -> bool:
        """Whether every resource was fetched to exhaustion.

        The single most important line in the report. A run that fetched 300 of
        4,172 records and exited zero is the failure this whole tool exists to
        make visible.

        An empty resources dict is NOT complete. `all()` over an empty sequence
        returns True, so a run that raised before its first request was ever
        counted would have reported success — the same bug as the unknown page
        count case below, arriving by a different route.
        """
        if not self.resources:
            return False
        return all(r.complete for r in self.resources.values())
    
    @property
    def unrecovered_failures(self) -> list[FailedRequest]:
        """Failures that no later attempt fixed."""
        return [f for f in self.failures if not f.recovered]

    @property
    def token_days_remaining(self) -> int | None:
        """Days until the token expires, or None if not reported.

        GitHub formats the header as "2026-10-03 20:59:49 UTC". Parsed
        defensively: the format is not documented as a contract, and a parse
        failure should cost a warning line in the report rather than the run.
        """
        if not self.token_expires_at:
            return None
        try:
            stamp = self.token_expires_at.replace(" UTC", "")
            expires = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
        return (expires - datetime.now(timezone.utc)).days
