"""Tests for record validation.

Built against real records from the recorded fixtures, then mutated. Testing
against a fabricated record would only prove the validator agrees with whatever
shape the test invented.
"""

from __future__ import annotations

import copy

from flightlog.schema import (
    COMMIT_SCHEMA,
    PULL_REQUEST_SCHEMA,
    ValidationStats,
    validate_record,
)


def test_a_real_commit_record_passes(commit_record):
    assert validate_record(commit_record, COMMIT_SCHEMA) == []


def test_a_real_pr_record_passes(pr_record):
    assert validate_record(pr_record, PULL_REQUEST_SCHEMA) == []


def test_null_author_is_permitted(commit_record):
    """The case that justified the whole schema-over-sampling approach.

    Checking 100 records found zero null authors. A full 4,185-record pull
    found exactly one — a dbt Labs developer whose commit email is not
    registered on GitHub. 0.02%, invisible in any reasonable sample, and a
    validator built by sampling would have crashed on it mid-run.
    """
    record = copy.deepcopy(commit_record)
    record["author"] = None
    assert validate_record(record, COMMIT_SCHEMA) == []


def test_required_field_absent_is_rejected(commit_record):
    record = copy.deepcopy(commit_record)
    del record["sha"]
    rejections = validate_record(record, COMMIT_SCHEMA)
    assert len(rejections) == 1
    assert rejections[0].field_path == "sha"
    assert "absent" in rejections[0].reason


def test_wrong_type_is_rejected(commit_record):
    record = copy.deepcopy(commit_record)
    record["sha"] = 12345
    rejections = validate_record(record, COMMIT_SCHEMA)
    assert rejections[0].reason == "expected str, got int"
    # The offending value, not just its type. "expected str, got dict" is less
    # use than seeing the dict.
    assert rejections[0].value_excerpt == "12345"


def test_nullable_field_arriving_as_another_type_is_rejected(commit_record):
    """The brief's own example: a nullable field arrives as something else.

    Nullable means null is permitted. It does not mean any type is permitted.
    """
    record = copy.deepcopy(commit_record)
    record["author"] = "octocat"
    rejections = validate_record(record, COMMIT_SCHEMA)
    assert rejections[0].field_path == "author"
    assert "expected dict, got str" in rejections[0].reason


def test_null_where_null_is_not_permitted_is_rejected(commit_record):
    record = copy.deepcopy(commit_record)
    record["commit"]["message"] = None
    rejections = validate_record(record, COMMIT_SCHEMA)
    assert rejections[0].field_path == "commit.message"
    assert "null" in rejections[0].reason


def test_null_intermediate_object_rejects_rather_than_crashing(commit_record):
    """A null on the path to a nested field must not raise.

    record["commit"]["author"]["name"] on a null commit raises TypeError. The
    isinstance guard in _resolve turns that into rejections naming each
    unreachable field.
    """
    record = copy.deepcopy(commit_record)
    record["commit"] = None
    rejections = validate_record(record, COMMIT_SCHEMA)
    assert len(rejections) >= 5
    assert all(r.field_path.startswith("commit.") for r in rejections)


def test_a_non_object_record_is_rejected_not_raised():
    """One malformed entry must not end a 42-page run."""
    rejections = validate_record("not a record", COMMIT_SCHEMA)
    assert len(rejections) == 1
    assert "expected a JSON object" in rejections[0].reason


def test_null_rate_is_tracked_for_accepted_records(commit_record):
    """A permitted null is not a rejection, and the rate is still the signal.

    A field normally null 2% of the time and suddenly null 40% of the time
    indicates an upstream change, and no individual record failed validation
    to reveal it.
    """
    stats = ValidationStats()
    for i in range(100):
        record = copy.deepcopy(commit_record)
        if i < 12:
            record["author"] = None
        validate_record(record, COMMIT_SCHEMA, stats=stats)

    assert stats.records_checked == 100
    assert stats.records_accepted == 100
    assert stats.records_rejected == 0
    assert stats.null_rate("author") == 0.12


def test_absence_is_counted_separately_from_null(commit_record):
    """Null is a value the schema permits; absence means the record does not
    match the schema at all. A report that merged them would hide the more
    serious one."""
    stats = ValidationStats()

    nulled = copy.deepcopy(commit_record)
    nulled["author"] = None
    validate_record(nulled, COMMIT_SCHEMA, stats=stats)

    absent = copy.deepcopy(commit_record)
    del absent["author"]
    validate_record(absent, COMMIT_SCHEMA, stats=stats)

    assert stats.null_counts["author"] == 1
    assert stats.absent_counts["author"] == 1
