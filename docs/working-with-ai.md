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
