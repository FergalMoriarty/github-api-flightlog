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

## 2026-09-12 — Module constants as default parameters cannot be patched at runtime

Second occurrence of the same mistake. A test script set
`flightlog.retry.BASE_DELAY_SECONDS = 0.05` to make the backoff demonstration
finish quickly, and the retries ran at the production delays of 1.2s and 1.9s
regardless.

`compute_delay(attempt, *, base: float = BASE_DELAY_SECONDS, ...)` binds the
constant's value when the function is defined. Rebinding the module attribute
afterwards changes the attribute and nothing else. The same error had already
been made with

## 2026-09-13 — The completeness check reported success for a run that fetched nothing

`ResourceStats.complete` treated an unknown page count as complete, on the
reasoning that GitHub omits the `Link` header when a result set fits in one
page. Sound for a small result set. False for a resource that never returned a
page at all: a pull that raised a 404 on its first request had zero pages
fetched, zero pages available, and reported `complete: True`.

A second instance of the same bug sat in `RunStats.complete`, arriving by a
different route. `all()` over an empty sequence returns `True`, so a run that
raised before any resource was registered would also have reported success.

Both are the exact failure class this tool exists to make impossible — a
partial or failed run that exits reporting success — sitting inside the code
that does the preventing. Neither raised, neither logged, and the summary
looked entirely normal.

Fixed: unknown page count is complete only when at least one page was fetched,
and an empty resources set is never complete.

Caught by triggering a 404 mid-run and reading the accounting afterwards,
rather than by testing only the paths that were expected to work.

## 2026-09-13 — The null `author` finally appeared: 1 record in 4,185

An earlier entry recorded that the assistant predicted null `author` values
would be visible on the first page of results, and that checking all 100
records found none. The conclusion drawn then was that nullability must come
from the API's declared schema rather than from a sample.

A full 42-page pull confirmed it. One record in 4,185 has a null top-level
`author`: a dbt Labs developer committing from `vadim.rybak@dbtlabs.com`, an
address GitHub cannot match to a user account. A real contributor with a
corporate email, not a bot or imported history.

0.02%. A validator built by sampling — even a generous sample of 500 records —
would have concluded the field is always populated. The failure would have
arrived 4,185 records into a production run, as a KeyError on
`record["author"]["login"]`, after a partial write.

The run instead validated the record (null is permitted by the schema), loaded
it (the column is nullable), and reported the rate. The design decision
survived contact with the data it was made about.

## 2026-09-13 — Client-side date filtering without an early exit fetched 4x the data needed

The pull requests endpoint has no `since` parameter, unlike the commits
endpoint. The assistant's approach was to sort by `updated` descending and
discard records older than the window on arrival, which is correct as far as
it goes and omitted the consequence of the sort order.

Measured against `dbt-labs/dbt-core` with a one-year window: the first run
fetched all 67 pages, 6,652 records, and kept 1,444. From page 16 onward every
single record on every page was outside the window and discarded on arrival.
Descending sort means that was knowable at page 16 — nothing later could
qualify.

52 wasted requests and roughly three minutes, for data thrown away as it
arrived.

Fixed by breaking when an entire page falls outside the window. 67 pages became
16, 240 seconds became 29, and the loaded records are identical: same 1,444
rows, same open/merged/unmerged counts, same 4.9-hour median time to merge.

Two things the fix had to get right. The break is correct ONLY because of
`direction=desc`; removing the sort makes it wrong rather than merely
unhelpful, and that constraint is stated in the code. And the completeness
check had to be told the window was exhausted — otherwise it compares 16 pages
fetched against `rel="last"` of 67 and reports a deliberate, correct early exit
as a failed run.

Caught by reading the per-page output rather than only the summary. The summary
said "67 of 67, complete, exit 0" and was entirely accurate.

## 2026-09-13 — Null rates were divided by the wrong denominator

When the report gained a second resource, the null rates became wrong in a way
that read as entirely plausible. `RunStats` held one shared `ValidationStats`,
so every null count was divided by the total records from every resource.
`merged_at` — a field that exists only on pull requests — was reported as null
in 37.2% of 600 records. The true figure was 223 of the 300 pull requests:
74.3%.

Precise, plausible, and wrong by a factor of two, in the section of the report
whose entire purpose is making data quality visible. 48% versus 24% is the
difference between "most PRs are not merged, as expected" and "something
changed upstream".

Fixed by moving `ValidationStats` onto `ResourceStats`, so each rate is against
its own denominator, and by naming the resource in every warning.

Caught by reading the report's numbers against what was known about the data
rather than checking only that the report rendered.

## 2026-09-13 — A regex that removed dead functions removed main() as well

Four superseded functions needed deleting from `fetch.py` and two subcommands
from `cli.py`. The assistant wrote a regex substitution to do it rather than
giving line boundaries to edit by hand.

The `fetch.py` pattern — from a `def` line to the next top-level `def` — worked
for all four. The `cli.py` pattern for the subparser blocks matched greedily
across the end of `build_parser` and consumed most of `main()`, leaving a file
that raised `NameError: name 'main' is not defined`. The second subparser
pattern matched nothing at all, which was the visible clue that the approach
was unsound.

Recovered with `git checkout flightlog/cli.py` and replaced the file wholesale.
Committing after each increment is what made a one-command recovery possible.

The lesson is about the tool, not the regex: structural edits to code call for
whole-unit replacement or an AST-aware tool, not pattern matching across
function boundaries. A greedy quantifier does not know where a function ends.