"""Tests for the diagnostic report.

build_report returns a string rather than printing, so these need no stdout
capture. A formatter that printed directly could only be tested by capturing
output, which is the kind of friction that stops things being tested at all.
"""

from __future__ import annotations

from flightlog.report import build_report, build_verdict
from flightlog.stats import RunStats


def _run(resource="commits", fetched=42, available=42, records=4184):
    stats = RunStats()
    counters = stats.resource(resource)
    counters.pages_fetched = fetched
    counters.pages_available = available
    counters.records_retrieved = records
    counters.validation.records_checked = records
    counters.validation.records_accepted = records
    stats.retries.attempts = fetched
    stats.rate_limit.limit = 5000
    stats.rate_limit.remaining = 4958
    return stats


def test_complete_run_says_so(config):
    verdict, warnings = build_verdict(config, _run())
    assert "Complete" in verdict
    assert "4,184 commits" in verdict
    assert warnings == []


def test_incomplete_run_names_the_shortfall(config):
    """"Incomplete" alone leaves the reader to work out how incomplete, and
    3 of 42 is a different situation from 40 of 42."""
    stats = _run(fetched=3, available=42, records=300)
    stats.resource("commits").stopped_early_reason = "MAX_PAGES=3"
    verdict, _ = build_verdict(config, stats)
    assert "INCOMPLETE" in verdict
    assert "3 of 42" in verdict
    assert "MAX_PAGES=3" in verdict


def test_a_run_with_no_resources_is_a_failure(config):
    """all() over an empty sequence returns True, so an empty resources dict
    would otherwise report success for a run that fetched nothing."""
    verdict, _ = build_verdict(config, RunStats())
    assert "FAILED" in verdict


def test_null_rates_use_their_own_resource_denominator(config):
    """The increment 10 bug: one shared ValidationStats divided every null
    count by the total records from every resource. merged_at, which exists
    only on pull requests, was reported as null in 37.2% of 600 records when
    the true figure was 74.3% of the 300 pull requests."""
    stats = RunStats()

    commits = stats.resource("commits")
    commits.pages_fetched = 1
    commits.records_retrieved = 300
    commits.validation.records_checked = 300
    commits.validation.records_accepted = 300

    prs = stats.resource("pull_requests")
    prs.pages_fetched = 1
    prs.records_retrieved = 300
    prs.validation.records_checked = 300
    prs.validation.records_accepted = 300
    prs.validation.null_counts["merged_at"] = 223

    assert prs.validation.null_rate("merged_at") == 223 / 300
    report = build_report(config, stats)
    assert "223 of 300" in report
    assert "74.3%" in report


def test_warnings_name_their_resource(config):
    """"merged_at was null in 74.3% of records" is ambiguous across a
    multi-resource run."""
    stats = RunStats()
    prs = stats.resource("pull_requests")
    prs.pages_fetched = 1
    prs.pages_available = 1
    prs.records_retrieved = 300
    prs.validation.records_checked = 300
    prs.validation.records_accepted = 300
    prs.validation.null_counts["merged_at"] = 223

    _, warnings = build_verdict(config, stats)
    assert any("pull_requests" in w and "merged_at" in w for w in warnings)


def test_low_null_rates_are_not_flagged(config):
    """author at 1 in 4,184 is evidence, not a problem. Flagging every null
    would make the flag meaningless."""
    stats = _run()
    stats.resource("commits").validation.null_counts["author"] = 1

    _, warnings = build_verdict(config, stats)
    assert not any("author" in w for w in warnings)
    # Listed in the table regardless — the numbers are the evidence, the flag
    # is a hint.
    assert "`author`" in build_report(config, stats)


def test_unauthenticated_run_is_flagged(config):
    """A run that silently fell back to anonymous requests looks identical to
    an authenticated one until it hits the limit eighty times sooner."""
    stats = _run()
    stats.rate_limit.limit = 60
    stats.rate_limit.remaining = 18

    _, warnings = build_verdict(config, stats)
    assert any("unauthenticated" in w.lower() for w in warnings)


def test_recovered_failures_are_still_reported(config):
    """A run that succeeded after three 502s is healthy in its result and
    unhealthy in its behaviour. A degrading integration looks fine right up
    until it stops working."""
    stats = _run()
    failure = stats.record_failure(
        url="https://api.github.com/x", status_code=502,
        message="Bad Gateway", request_id="ABC:123",
    )
    failure.recovered = True

    _, warnings = build_verdict(config, stats)
    assert any("recovered" in w for w in warnings)
    # The identifier GitHub support needs to find the call in their own logs.
    assert "ABC:123" in build_report(config, stats)


def test_report_is_markdown(config):
    report = build_report(config, _run())
    assert report.startswith("# Ingestion run")
    assert "| Resource | Pages | Records | Complete |" in report
