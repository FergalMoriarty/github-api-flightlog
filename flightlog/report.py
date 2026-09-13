"""The diagnostic report.

The thing that makes this a tool rather than a fetch script. After a run it
states what happened in enough detail that a failed or partial sync can be
diagnosed without re-running it: pages against pages available, records
rejected with the reason and an example, rate limit waits and their duration,
retries by the status code that caused each, and any field null more often than
expected.

One formatter for every resource. The two provisional summaries this replaces
had diverged slightly within a day of each other, which is what always happens
to duplicated presentation code.

Markdown rather than plain text: it renders in a browser, a terminal, a GitHub
issue, or a Slack paste without transformation, and the structure survives being
pasted into any of them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .load import LoadStats
from .stats import RunStats

log = logging.getLogger(__name__)

# Null rate above which a field is called out rather than merely listed.
#
# 20% is a judgement, not a measurement. It is set high enough that genuinely
# optional fields — merged_at is null for every open PR — do not cry wolf, and
# low enough to catch a field that has started failing upstream. The threshold
# being arbitrary is the reason the report prints every null rate and flags
# only some: the numbers are the evidence, the flag is a hint.
NULL_RATE_FLAG = 0.20

# Warn when a token expires within this many days.
TOKEN_EXPIRY_WARNING_DAYS = 14

# How many rejection examples to show per distinct failure.
MAX_EXAMPLES = 3


def _hms(seconds: float) -> str:
    """Format a duration readably.

    "47s" and "3m 12s" rather than "192.4 seconds". The report is read by
    people.
    """
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"

def build_verdict(config: Config, stats: RunStats) -> tuple[str, list[str]]:
    """The headline outcome and any warnings.

    Returns (verdict, warnings). The verdict is one sentence stating whether
    the run did its job; the warnings are things that did not fail the run but
    should be read.

    This is the part that makes the report a report rather than a table of
    counts. Anything the tool can conclude on its own behalf, it should state,
    because the alternative is a reader deriving it from numbers — and a reader
    skimming a scheduled run's output will not derive anything.
    """
    warnings: list[str] = []

    # --- The verdict -----------------------------------------------------
    incomplete = [r for r in stats.resources.values() if not r.complete]

    if not stats.resources:
        verdict = "**FAILED.** No resources were fetched."
    elif incomplete:
        names = ", ".join(f"`{r.name}`" for r in incomplete)
        # Specific about the shortfall. "Incomplete" alone leaves the reader
        # to work out how incomplete, and the difference between 40 of 42 and
        # 3 of 42 is the difference between a blip and a broken run.
        detail = "; ".join(
            f"{r.name} got {r.pages_fetched} of "
            f"{r.pages_available if r.pages_available is not None else '?'} pages"
            + (f" ({r.stopped_early_reason})" if r.stopped_early_reason else "")
            for r in incomplete
        )
        verdict = f"**INCOMPLETE.** {names} did not retrieve the full result set — {detail}."
    else:
        counts = ", ".join(
            f"{r.records_retrieved:,} {r.name}" for r in stats.resources.values()
        )
        verdict = f"**Complete.** Retrieved {counts} in {_hms(stats.elapsed_seconds)}."

    # --- Warnings --------------------------------------------------------
    # Per resource, so each rate is against its own denominator. Warnings name
    # the resource for the same reason: "merged_at was null in 74.3% of
    # records" is ambiguous across a multi-resource run in a way that
    # "pull_requests: merged_at ..." is not.
    for counters in stats.resources.values():
        v = counters.validation

        if v.records_rejected:
            # Names the most common reason rather than only the count. "3
            # records rejected" prompts a question the report already answers.
            grouped: dict[str, int] = {}
            for r in v.rejections:
                key = f"{r.field_path} {r.reason}"
                grouped[key] = grouped.get(key, 0) + 1
            top = max(grouped.items(), key=lambda kv: kv[1])
            warnings.append(
                f"`{counters.name}`: {v.records_rejected:,} of {v.records_checked:,} "
                f"records failed validation. Most common: {top[0]} ({top[1]:,} records)."
            )

        for path, count in sorted(v.null_counts.items()):
            rate = v.null_rate(path)
            if rate >= NULL_RATE_FLAG:
                warnings.append(
                    f"`{counters.name}`: `{path}` was null in {rate:.1%} of records "
                    f"({count:,} of {v.records_checked:,}). Expected for a genuinely "
                    "optional field; worth checking against previous runs if not."
                )
    if stats.unrecovered_failures:
        warnings.append(
            f"{len(stats.unrecovered_failures)} request(s) failed and were not "
            "recovered by a retry."
        )
    elif stats.failures:
        # Recovered failures still matter. A run that succeeded after three
        # 502s is healthy in its result and unhealthy in its behaviour, and a
        # degrading integration looks fine right up until it stops working.
        warnings.append(
            f"{len(stats.failures)} request(s) failed but were recovered by a retry. "
            "Worth watching if this becomes routine."
        )

    if stats.rate_limit.waits:
        warnings.append(
            f"The rate limit was reached {stats.rate_limit.waits} time(s), costing "
            f"{_hms(stats.rate_limit.seconds_waited)} of waiting."
        )

    days = stats.token_days_remaining
    if days is not None and days <= TOKEN_EXPIRY_WARNING_DAYS:
        warnings.append(
            f"The API token expires in {days} day(s), on {stats.token_expires_at}."
        )

    if stats.rate_limit.limit and stats.rate_limit.limit <= 60:
        # 60 is the unauthenticated limit. Worth stating plainly, because a run
        # that silently fell back to anonymous requests looks identical to an
        # authenticated one until it hits the limit eighty times sooner.
        warnings.append(
            "This run was unauthenticated (60 requests/hour rather than 5,000). "
            "Check that GITHUB_TOKEN is set."
        )

    return verdict, warnings

def build_report(
    config: Config,
    stats: RunStats,
    load_stats: dict[str, LoadStats] | None = None,
    table_counts: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Render the run as Markdown.

    Returns a string rather than printing, so the same output can go to stdout,
    a file, or a test assertion. A formatter that printed directly could only
    be tested by capturing stdout, which is the kind of friction that stops
    things being tested at all.
    """
    lines: list[str] = []
    add = lines.append

    add(f"# Ingestion run — {config.repo}")
    add("")
    add(f"**Started** {stats.started_at:%Y-%m-%d %H:%M:%S} UTC  ")
    add(f"**Duration** {_hms(stats.elapsed_seconds)}  ")
    add(f"**Window** since {config.since_iso}")
    add("")
    verdict, warnings = build_verdict(config, stats)
    add(verdict)
    add("")

    if warnings:
        add("## Worth reading")
        add("")
        for warning in warnings:
            add(f"- {warning}")
        add("")
    # --- Resources -------------------------------------------------------
    add("## What was fetched")
    add("")
    add("| Resource | Pages | Records | Complete |")
    add("| --- | --- | --- | --- |")
    for counters in stats.resources.values():
        available = (
            str(counters.pages_available)
            if counters.pages_available is not None
            else "?"
        )
        mark = "yes" if counters.complete else "**NO**"
        add(
            f"| {counters.name} | {counters.pages_fetched} of {available} "
            f"| {counters.records_retrieved:,} | {mark} |"
        )
    add("")

    for counters in stats.resources.values():
        if counters.stopped_early_reason:
            add(
                f"`{counters.name}` stopped early: {counters.stopped_early_reason}. "
                "The result set is incomplete."
            )
            add("")

    # --- Requests --------------------------------------------------------
    add("## Requests")
    add("")
    # Requests, not pages. They differ whenever anything was retried, and the
    # quota was billed for the requests.
    add(f"- {stats.retries.attempts} requests made")

    if stats.retries.retries:
        causes = ", ".join(
            f"{cause} x{count}" for cause, count in sorted(stats.retries.by_cause.items())
        )
        add(
            f"- {stats.retries.retries} retries ({causes}), "
            f"{_hms(stats.retries.seconds_waited)} spent backing off"
        )
    else:
        add("- No retries")

    if stats.rate_limit.waits:
        add(
            f"- Rate limit reached {stats.rate_limit.waits} times, "
            f"{_hms(stats.rate_limit.seconds_waited)} spent waiting"
        )
    else:
        # Stated rather than omitted. "Not throttled" is a fact about the run;
        # silence would be indistinguishable from the report having nothing to
        # say on the subject.
        add("- Rate limit not reached")

    if stats.rate_limit.limit:
        used = (stats.rate_limit.limit or 0) - (stats.rate_limit.remaining or 0)
        add(
            f"- Quota: {stats.rate_limit.remaining:,} of {stats.rate_limit.limit:,} "
            f"remaining ({used:,} used this window, by all clients on this token)"
        )
    if stats.rate_limit.reset_datetime:
        add(f"- Quota resets {stats.rate_limit.reset_datetime:%H:%M:%S} UTC")
    add("")

    # --- Failures --------------------------------------------------------
    if stats.failures:
        recovered = len(stats.failures) - len(stats.unrecovered_failures)
        add("## Failed requests")
        add("")
        add(
            f"{len(stats.failures)} requests failed, {recovered} of which a later "
            "attempt recovered."
        )
        add("")
        add("| Status | Message | Recovered | GitHub request id |")
        add("| --- | --- | --- | --- |")
        for failure in stats.failures[:10]:
            mark = "yes" if failure.recovered else "**no**"
            # The request id is what GitHub support needs to find the call in
            # their own logs. Almost every client discards it.
            add(
                f"| {failure.status_code} | {failure.message} | {mark} "
                f"| `{failure.request_id or 'n/a'}` |"
            )
        if len(stats.failures) > 10:
            add(f"| ... | and {len(stats.failures) - 10} more | | |")
        add("")

    # --- Validation ------------------------------------------------------
    #
    # Per resource, because the denominator matters. A single shared
    # ValidationStats divided every null count by the total records from every
    # resource, and reported merged_at — a field that exists only on pull
    # requests — as null in 37.2% of 600 records when the true figure was 74.3%
    # of the 300 pull requests. Plausible, precise, and wrong by a factor of
    # two.
    add("## Validation")
    add("")

    for counters in stats.resources.values():
        v = counters.validation
        add(f"### {counters.name}")
        add("")
        add(f"- {v.records_checked:,} records checked, {v.records_accepted:,} accepted")

        if v.records_rejected:
            add(f"- **{v.records_rejected:,} rejected**")
            add("")
            # Grouped by field and reason. Four thousand records failing the
            # same check is one problem, and listing it four thousand times
            # buries everything else in the report.
            grouped: dict[tuple[str, str], list] = {}
            for r in v.rejections:
                grouped.setdefault((r.field_path, r.reason), []).append(r)
            for (path, reason), group in sorted(grouped.items()):
                add(f"**`{path}`** — {reason} ({len(group):,} records)")
                add("")
                for sample in group[:MAX_EXAMPLES]:
                    # The offending value, not just its type. "expected str,
                    # got dict" is less use than seeing the dict.
                    add(f"- `{sample.identity}`: {sample.value_excerpt}")
                add("")
        else:
            add("- No records rejected")

        if v.null_counts:
            add("")
            add("| Field | Null | Rate | |")
            add("| --- | --- | --- | --- |")
            for path, count in sorted(v.null_counts.items()):
                rate = v.null_rate(path)
                flag = "flagged" if rate >= NULL_RATE_FLAG else ""
                add(
                    f"| `{path}` | {count:,} of {v.records_checked:,} "
                    f"| {rate:.1%} | {flag} |"
                )

        if v.absent_counts:
            add("")
            # Absence is distinct from null: null is a value the schema
            # permits, absence means the record does not match the schema at
            # all. A report that merged them would hide the more serious one.
            for path, count in sorted(v.absent_counts.items()):
                add(f"- `{path}` absent from {count:,} records")

        add("")

    add(
        "Null and absence rates are over every record the API returned for that "
        "resource, including any this run discarded before loading."
    )
    add("")
    
    if load_stats:
        add("## Loaded to PostgreSQL")
        add("")
        for name, ls in load_stats.items():
            add(
                f"- `{name}`: {ls.rows_submitted:,} rows submitted "
                f"in {ls.batches} batches, {ls.rows_affected:,} affected"
            )
        add("")

    if table_counts:
        add("### Table state after the run")
        add("")
        for name, counts in table_counts.items():
            add(f"**{name}**")
            add("")
            for key, value in counts.items():
                if value is None:
                    continue
                label = key.replace("_", " ")
                if isinstance(value, float):
                    add(f"- {label}: {value:,.1f}")
                elif isinstance(value, datetime):
                    add(f"- {label}: {value:%Y-%m-%d}")
                else:
                    add(f"- {label}: {value:,}" if isinstance(value, int) else f"- {label}: {value}")
            add("")

    return "\n".join(lines)


def write_report(content: str, directory: str = "reports") -> Path:
    """Write the report to a timestamped file and return its path.

    Timestamped rather than overwritten, so consecutive runs can be compared —
    which is the only way a null rate moving from 0% to 12% becomes visible.
    """
    path = Path(directory)
    path.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = path / f"run-{stamp}.md"
    filename.write_text(content, encoding="utf-8")
    return filename
