"""Record validation against a declared schema.

Checks that a record has the fields the loader expects, in the types it
expects, before anything tries to use it. A record that fails is rejected and
counted with a reason and an example, rather than becoming a KeyError several
hundred records into a load with a partial write left behind.

The schema comes from what the API DECLARES, not from what a sample happened to
contain. That distinction was learned the hard way on this project: checking all
100 records of page one of dbt-labs/dbt-core for a null commit `author` found
zero, and the obvious conclusion — that the field is always present — is wrong.
GitHub's schema declares it nullable, and it is null wherever a commit email
cannot be matched to a GitHub account. Rare in a corporate repository where
contributors have linked accounts; not rare everywhere. A validator built from
observation would pass for thousands of records and then fail on the one where
it does not hold.

What this does NOT check is business correctness. A commit dated 1970 with an
empty message and a made-up SHA passes every check here, because it is
structurally exactly what it claims to be. Presence and type, nothing more.
That is a real limitation and it is documented as one rather than papered over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    """One field the loader depends on.

    `path` is dot-separated for nested access: "commit.author.name" reaches
    record["commit"]["author"]["name"]. Nested because GitHub's commit records
    are two and three levels deep, and flattening them into the validator would
    mean the schema no longer resembles the thing it describes.

    `nullable` means the API may legitimately send null. Distinct from a field
    being absent: null is a value the schema permits, absence is a record that
    does not match the schema at all. Conflating them is how a null author
    becomes an unexplained KeyError.

    `types` is the set of Python types the parsed JSON value may have. None
    means any type is acceptable — used where the shape is genuinely open, and
    a deliberate choice rather than an omission.
    """

    path: str
    types: tuple[type, ...] | None = None
    nullable: bool = False
    # Fields that are present on most records but not required. Tracked for the
    # null-rate report without their absence failing a record.
    required: bool = True
    # Why this field matters, for the rejection message. A reason beats a path:
    # "sha — the primary key for the commits table" is more use to whoever
    # reads the report than "sha missing".
    note: str = ""


# The commit record fields this tool depends on.
#
# Deliberately a subset. A GitHub commit record has around sixty fields across
# its nested objects, and validating all of them would reject records over
# fields nothing reads. Every entry here is one the loader at increment 8 or
# the analysis afterwards actually uses.
COMMIT_SCHEMA: tuple[Field, ...] = (
    Field(
        "sha",
        types=(str,),
        note="primary key for the commits table",
    ),
    Field(
        "commit.message",
        types=(str,),
        note="commit message",
    ),
    Field(
        "commit.author.name",
        types=(str,),
        note="author name as recorded by git itself",
    ),
    Field(
        "commit.author.email",
        types=(str,),
        note="author email as recorded by git itself",
    ),
    Field(
        "commit.author.date",
        types=(str,),
        note="ISO 8601 timestamp; parsed at load time, not here",
    ),
    Field(
        "commit.committer.name",
        types=(str,),
        note="committer name; often 'GitHub' for web-merged commits",
    ),
    Field(
        "commit.committer.date",
        types=(str,),
        note="ISO 8601 timestamp",
    ),
    # The top-level author is GitHub's attempt to match the commit email to a
    # user account, and is a DIFFERENT thing from commit.author above.
    # commit.author is what git recorded locally and is essentially always
    # present, because git requires it to make a commit at all. This one is
    # null whenever the match fails — an unregistered work email, a bot, old
    # imported history. The single most important nullable field in the schema,
    # and the one that will crash a loader reaching for record["author"]["login"].
    Field(
        "author",
        types=(dict,),
        nullable=True,
        note="GitHub user matched to the commit email; null when unmatched",
    ),
    Field(
        "committer",
        types=(dict,),
        nullable=True,
        note="GitHub user matched to the committer email; null when unmatched",
    ),
    Field(
        "html_url",
        types=(str,),
        note="link back to the commit on github.com",
    ),
    # Present on every record observed, but not depended on by the loader.
    # required=False so its absence is noted rather than fatal.
    Field(
        "commit.verification.verified",
        types=(bool,),
        required=False,
        note="whether GitHub verified the commit signature",
    ),
)

# The pull request record fields this tool depends on.
#
# A PR record is considerably larger than a commit record — around a hundred
# fields, including fully nested `head` and `base` objects each carrying a
# complete repository representation. As with commits, only what the loader
# uses is validated: rejecting a record over a field nothing reads would be
# noise dressed up as rigour.
PULL_REQUEST_SCHEMA: tuple[Field, ...] = (
    Field(
        "id",
        types=(int,),
        note="GitHub's globally unique PR id; the primary key",
    ),
    # Distinct from id, and the distinction matters. `number` is what people
    # call the PR — "#16245" — and it is unique only within a repository, so
    # ingesting a second repo would collide on it. `id` is unique everywhere.
    Field(
        "number",
        types=(int,),
        note="PR number, unique per repository only",
    ),
    Field(
        "title",
        types=(str,),
        note="PR title",
    ),
    # "open" or "closed". There is no "merged" state: a merged PR is closed
    # with merged_at set. Treating state alone as the merge indicator would
    # count every abandoned PR as merged.
    Field(
        "state",
        types=(str,),
        note="open or closed; merged is closed with merged_at set",
    ),
    Field(
        "draft",
        types=(bool,),
        required=False,
        note="whether the PR is a draft",
    ),
    # Nullable for the same reason the commit author is: GitHub returns null
    # when the account has been deleted. Rarer here — opening a PR requires an
    # account — but the schema declares it nullable and that is what governs,
    # not how often it has been observed. The commits case proved the point:
    # one null in 4,185 records, invisible in any reasonable sample.
    Field(
        "user",
        types=(dict,),
        nullable=True,
        note="PR author; null when the account has been deleted",
    ),
    Field(
        "created_at",
        types=(str,),
        note="ISO 8601 timestamp",
    ),
    Field(
        "updated_at",
        types=(str,),
        note="ISO 8601 timestamp",
    ),
    # Null while the PR is open. Not an error — it is the normal state of an
    # open PR, and the column is nullable to match.
    Field(
        "closed_at",
        types=(str,),
        nullable=True,
        note="null while the PR is open",
    ),
    # Null for any PR that is open, and for any that was closed without
    # merging. The presence of this field is the only reliable merge
    # indicator.
    Field(
        "merged_at",
        types=(str,),
        nullable=True,
        note="null unless merged; the only reliable merge indicator",
    ),
    # The branch being merged into. Always present.
    Field(
        "base.ref",
        types=(str,),
        note="target branch",
    ),
    # The source branch. Nullable in effect: when a PR comes from a fork whose
    # repository has since been deleted, GitHub nulls the `head.repo` object,
    # and `head.ref` can become unreliable. required=False rather than
    # nullable, so its absence is counted rather than fatal.
    Field(
        "head.ref",
        types=(str,),
        required=False,
        note="source branch; unreliable when a fork has been deleted",
    ),
    Field(
        "html_url",
        types=(str,),
        note="link back to the PR on github.com",
    ),
)
# Sentinel distinguishing "the path does not exist" from "the value is None".
# A plain None return could not tell those apart, and they are exactly the two
# cases this module exists to separate.
_MISSING = object()


def _resolve(record: dict[str, Any], path: str) -> Any:
    """Follow a dot-separated path into a record.

    Returns _MISSING if any segment is absent, or if an intermediate value is
    not a dict and so cannot be descended into.

    The intermediate check matters: when `author` is null, the path
    "author.login" hits None at the first segment. Without the isinstance guard
    that raises TypeError, which would turn a validation result into a crash
    inside the validator.
    """
    current: Any = record
    for segment in path.split("."):
        if not isinstance(current, dict):
            return _MISSING
        if segment not in current:
            return _MISSING
        current = current[segment]
    return current


@dataclass
class Rejection:
    """One record that failed validation.

    Carries the record's identity where it has one, so the report can point at
    a specific commit rather than saying "a record failed".
    """

    identity: str
    field_path: str
    reason: str
    # The offending value, truncated. Included because "expected str, got dict"
    # is less useful than seeing what the dict actually was — the brief's own
    # example is a nullable field arriving as an object, which is only
    # diagnosable if the object is visible.
    value_excerpt: str


@dataclass
class ValidationStats:
    """Validation outcomes for a run.

    Null counts are tracked for every nullable field, not just failures,
    because the rate is the signal. A field that is normally null 2% of the
    time and is suddenly null 40% of the time indicates an upstream change,
    and no individual record failed validation to reveal it.
    """

    records_checked: int = 0
    records_accepted: int = 0
    rejections: list[Rejection] = field(default_factory=list)
    # field path -> count of records where it was null
    null_counts: dict[str, int] = field(default_factory=dict)
    # field path -> count of records where it was absent entirely. Separate
    # from null, because absence means the record does not match the schema
    # while null is a value the schema permits.
    absent_counts: dict[str, int] = field(default_factory=dict)

    @property
    def records_rejected(self) -> int:
        return self.records_checked - self.records_accepted

    def null_rate(self, path: str) -> float:
        """Fraction of checked records where this field was null."""
        if not self.records_checked:
            return 0.0
        return self.null_counts.get(path, 0) / self.records_checked


def _excerpt(value: Any, limit: int = 80) -> str:
    """Render a value for a rejection message, bounded in length.

    Bounded because a commit message can be several kilobytes and a rejection
    report full of them is unreadable.
    """
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def validate_record(
    record: Any,
    schema: Iterable[Field] = COMMIT_SCHEMA,
    *,
    identity_path: str = "sha",
    stats: ValidationStats | None = None,
) -> list[Rejection]:
    """Check one record against the schema.

    Returns the list of rejections — empty means the record passed. A list
    rather than a bool, because a record failing three checks should report
    three reasons rather than the first one encountered; the others are still
    true and still useful.

    Updates `stats` if given, including the null and absence counts for records
    that pass. Those are the numbers the report needs, and a record being valid
    does not make its null fields uninteresting.
    """
    rejections: list[Rejection] = []

    if not isinstance(record, dict):
        # A non-dict in the records array should not happen. Rejected rather
        # than raised, so one malformed entry does not end a 42-page run.
        rejection = Rejection(
            identity="(not an object)",
            field_path="",
            reason=f"expected a JSON object, got {type(record).__name__}",
            value_excerpt=_excerpt(record),
        )
        if stats is not None:
            stats.records_checked += 1
            stats.rejections.append(rejection)
        return [rejection]

    identity_value = _resolve(record, identity_path)
    identity = (
        str(identity_value)[:12] if identity_value is not _MISSING else "(no identity)"
    )

    for spec in schema:
        value = _resolve(record, spec.path)

        if value is _MISSING:
            # Absent. Counted separately from null in every case, because the
            # two mean different things and a report that merged them would
            # hide the more serious one.
            if stats is not None:
                stats.absent_counts[spec.path] = stats.absent_counts.get(spec.path, 0) + 1
            if spec.required:
                rejections.append(
                    Rejection(
                        identity=identity,
                        field_path=spec.path,
                        reason=(
                            f"required field absent"
                            + (f" ({spec.note})" if spec.note else "")
                        ),
                        value_excerpt="(absent)",
                    )
                )
            continue

        if value is None:
            if stats is not None:
                stats.null_counts[spec.path] = stats.null_counts.get(spec.path, 0) + 1
            if not spec.nullable:
                rejections.append(
                    Rejection(
                        identity=identity,
                        field_path=spec.path,
                        reason=(
                            "null where the schema does not permit null"
                            + (f" ({spec.note})" if spec.note else "")
                        ),
                        value_excerpt="None",
                    )
                )
            # A permitted null needs no type check — None is not of the
            # declared type and checking it would reject every legitimate null.
            continue

        if spec.types is not None and not isinstance(value, spec.types):
            expected = " or ".join(t.__name__ for t in spec.types)
            rejections.append(
                Rejection(
                    identity=identity,
                    field_path=spec.path,
                    reason=f"expected {expected}, got {type(value).__name__}",
                    value_excerpt=_excerpt(value),
                )
            )

    if stats is not None:
        stats.records_checked += 1
        if not rejections:
            stats.records_accepted += 1
        else:
            stats.rejections.extend(rejections)

    return rejections
