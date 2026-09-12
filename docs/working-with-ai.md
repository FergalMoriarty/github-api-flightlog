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

**Confirmed by measurement, 2026-09-08.** A full year-bounded pull of
`dbt-labs/dbt-core` cost 42 requests — 4,172 commits at 100 per page — against
a 5,000/hour quota. 0.84%. The original estimate of ~100 requests was for a
full history pull and was high for this workload; the conclusion that the limit
is unreachable in normal operation holds, and is stronger than estimated.

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

## 2026-09-08 — 422 predicted for an invalid branch; GitHub returns 404

The plan for the error taxonomy listed 422 as the expected response to a
well-formed request carrying an invalid parameter, and an invalid `sha` was
proposed as the way to trigger it.

GitHub returns 404. An unresolvable ref is treated as a missing resource, not
as a bad request. 422 is reserved for requests that are syntactically valid and
semantically impossible — a value outside a permitted range, a required field
omitted from a write — and the commits endpoint is read-only with permissive
parameters, so it is difficult to provoke one from it at all.

`ValidationError` stays in the taxonomy because 422 is documented behaviour and
appears on write endpoints, but it is marked in the code as unobserved.
Claiming to handle a status code the tool has never seen is the kind of claim
this project is meant to avoid.

The more useful finding is what 404 turned out to cover. Three distinct causes
produced byte-identical responses, down to `content-length: 132`:

  1. the repository does not exist
  2. the repository exists and the token cannot see it (`github/github`)
  3. repository and token both fine, but a parameter did not resolve

Case 2 is deliberate. Distinguishing "forbidden" from "nonexistent" would let
anyone enumerate private repositories by watching status codes. The cost falls
on whoever is debugging: a 404 cannot be resolved from the response alone, and
a user who insists the repository exists is often right.

Caught by triggering each failure and reading the response rather than writing
the exception classes from the HTTP specification.

## 2026-09-10 — A test that monkey-patched a module constant proved nothing

To demonstrate the rate limit wait without exhausting a real quota, the
assistant wrote a test that reassigned `flightlog.ratelimit.DEFAULT_THRESHOLD`
at runtime and then called the pagination loop.

The test ran clean and the wait never fired. `iter_pages` calls
`wait_if_needed()` with no argument, so the threshold comes from the default
parameter value — and Python evaluates default arguments once, when the
function is defined. Rebinding the module attribute afterwards changes the
module attribute and nothing else. `MAX_WAIT_SECONDS`, read inside the function
body, was patched successfully; `DEFAULT_THRESHOLD`, captured as a default, was
not.

The failure mode is the one this whole project is about: no error, no warning,
a test that passed and demonstrated nothing. Fixed by passing the threshold
explicitly as an argument.

Caught by noticing that `waits=0` after a run that should have waited.