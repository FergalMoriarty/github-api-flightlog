# Working with AI on this project

A running record of where AI assistance was wrong, incomplete, or needed
correcting. Kept from the first commit rather than reconstructed afterwards.

Format: date, what was suggested, what was actually true, how it was caught.

---

## 2026-09-03 — Rate limit assumption in the project plan

The plan assumed the 5,000 requests/hour authenticated rate limit would be hit
during normal runs, and treated recovering from it as a routine code path.

That is wrong for this workload. At `per_page=100`, a repository with roughly
10,000 commits needs about 100 requests for a full history pull. A year-bounded
pull needs far fewer. The limit is nowhere near reachable.

Consequence for the design: the rate-limit handling is still worth building —
it is not dead code, and an unbounded pull against a larger target would reach
the limit — but it cannot be verified by simply running the tool. It gets
exercised two other ways: unauthenticated, where the limit is 60/hour and is
reached in under a minute, and against recorded fixtures in the test suite.

Caught by working out the arithmetic before writing the code rather than after.

## 2026-09-03 — Repository name implied a generality the code does not have

An earlier name for this repo was `git-api-flightlog`. Git and GitHub are
different things: git is the version control system, GitHub is the hosting
service whose REST API this tool calls. There is no "git API" to write a client
for, and the name invited that reading.

Renamed to `github-api-flightlog`. The underlying point survives the rename —
"flightlog" is generic, but the code is GitHub-specific: it parses `Link`
headers as GitHub formats them and reads `X-RateLimit-*` as GitHub sets them.
That constraint is stated in the README opening rather than left to the
limitations section, so the name does not promise something the code does not
deliver.

## 2026-09-08 — Pagination URLs are rewritten by GitHub, and must be followed verbatim

Observed in the `Link` header on the first live request. The request went to
`/repos/dbt-labs/dbt-core/commits`, but the `rel="next"` URL came back as
`/repositories/53548867/commits?since=...&per_page=100&page=2` — GitHub had
rewritten the named path into its internal numeric repository ID, and carried
the query parameters forward itself.

The obvious implementation — building each page's URL from config and an
incrementing page number — would have worked and been wrong. It discards the
server's own cursor, and the numeric ID form survives a repository rename where
the named form does not.

The rule for the pagination increment: follow the URL GitHub supplies, without
reconstructing or re-appending parameters to it.

Caught by printing every response header on the first request rather than
reaching only for the ones expected to matter.

## 2026-09-08 — Null `author` predicted on the first page; not present

The assistant stated that GitHub's top-level commit `author` field would be
null often enough to see on the first page of results, as the worked example
for schema validation.

Checked across all 100 records of page one of `dbt-labs/dbt-core`: zero null
`author`, zero null `committer`. The field is genuinely nullable — GitHub's
schema declares it so, and it is null wherever a commit email cannot be matched
to a GitHub account — but in a corporate repository where contributors have
linked accounts, it is rare rather than common.

The substantive point survives the correction and is arguably strengthened:
nullability cannot be established by observing one page of one repository. A
validator built from "it was populated every time I looked" passes for thousands
of records and then fails on the one where it is not, part-way through a run.
Presence checks come from the API's declared schema; the observed null rate goes
in the run report, which is why the report tracks fields that are null more often
than expected.

Caught by checking the claim against all 100 records rather than the single
record the probe output happened to print.
