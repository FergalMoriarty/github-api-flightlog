"""Exception hierarchy for GitHub API failures.

Written against responses actually observed from the API rather than from its
documentation. Where the two disagree — and on rate limiting they do — the
observed behaviour wins, because that is what the code will meet.

The hierarchy exists to answer one operational question at each level:

    GitHubError            something went wrong
      ├─ ClientError       4xx — the request was wrong. Never retry.
      └─ ServerError       5xx — the server failed. Retry is reasonable.

That split is the whole basis of increment 5. Retrying a 401 sends the same bad
token five times and produces five identical failures; retrying a 500 often
succeeds, because the fault was transient and not ours. Encoding the difference
in the type means the retry logic can ask `isinstance(exc, ServerError)` rather
than maintaining its own list of status codes.
"""

from __future__ import annotations

from typing import Any

import requests


class GitHubError(RuntimeError):
    """Base for every API failure.

    Carries the diagnostic context from the response rather than just a
    message, because the whole point of this tool is that a failure is
    diagnosable afterwards. A bare `raise RuntimeError("404")` discards the
    request ID, and the request ID is the only thing that lets GitHub find the
    call in their own logs.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        url: str,
        request_id: str | None = None,
        documentation_url: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.url = url
        # GitHub's own identifier for this request, from x-github-request-id.
        # Present on every response including failures. This is what you quote
        # in a support ticket, and it is the single most useful field to keep.
        self.request_id = request_id
        # GitHub points at the specific endpoint's documentation on an error —
        # on a 403 for DELETE it links to the delete-a-repository page, not the
        # API root. Free context; no reason to discard it.
        self.documentation_url = documentation_url
        self.body = body

    def __str__(self) -> str:
        # The request ID goes in the string form so it survives into a log line
        # or a traceback without the caller having to remember to include it.
        parts = [f"{self.status_code}: {self.message}"]
        if self.request_id:
            parts.append(f"request id {self.request_id}")
        return " — ".join(parts)


class ClientError(GitHubError):
    """4xx — the request was wrong. Retrying will not help."""


class ServerError(GitHubError):
    """5xx — the server failed. Retrying is reasonable."""


class AuthenticationError(ClientError):
    """401 — the credentials were not accepted.

    Observed message: "Bad credentials".

    Note what a 401 response does NOT contain: no rate limit headers, no
    x-accepted-github-permissions. GitHub authenticates before it authorises
    and before it looks up the resource, so a 401 says nothing about the rest
    of the request — the rest of the request was never examined.

    Practical consequence for diagnosis: 401s appearing across several
    different endpoints point at one credential, not several broken calls.

    Fix: replace the token. Granting permissions will not help; the token was
    not recognised as belonging to anyone.
    """


class PermissionError_(ClientError):
    """403 — authenticated successfully, then refused.

    Observed message: "Resource not accessible by personal access token".

    The contrast with 401 is the distinction this tool exists to demonstrate.
    401 is about identity; 403 is about entitlement. GitHub knew exactly who we
    were and declined anyway — which is why a 403 response carries rate limit
    headers (the request was attributed and billed) and why it carries
    x-accepted-github-permissions naming what was required. Observed:
    `administration=write` for a DELETE, against `contents=read` for a GET.

    Fix: re-scope the token, or obtain the rights. A new token with the same
    permissions changes nothing.

    Named with a trailing underscore to avoid shadowing Python's builtin
    PermissionError, which means something different (an OS filesystem error)
    and would be genuinely confusing to catch by accident.
    """

    def __init__(self, *args: Any, required_permission: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # From x-accepted-github-permissions. The single most actionable field
        # on a 403 — it names precisely what to grant.
        self.required_permission = required_permission


class RateLimitError(ClientError):
    """Quota exhausted.

    Separate from PermissionError_ despite GitHub returning 403 for both.

    This is the place where the API's behaviour and the HTTP specification
    diverge, and where a taxonomy built from the spec alone would be wrong. The
    spec has 429 Too Many Requests for exactly this; GitHub returns 429 in some
    circumstances and 403 in others. So the status code alone cannot tell the
    two 403 cases apart, and classification has to read the response body and
    the x-ratelimit-remaining header instead.

    Retry IS appropriate here, unlike its ClientError siblings — but only after
    waiting until the reset time. That makes it the one exception the retry
    logic treats specially rather than by its base class, which is increment 4.
    """

    def __init__(self, *args: Any, reset_at: int | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # x-ratelimit-reset, a Unix timestamp — an absolute moment, not a
        # duration. The wait is `reset_at - now`, computed in UTC on both
        # sides. Using local time here would be two hours out in Spain.
        self.reset_at = reset_at


class NotFoundError(ClientError):
    """404 — no such resource, as far as this token is concerned.

    Observed message: "Not Found".

    The qualifier matters. Three distinct causes were observed producing byte
    for byte identical responses, down to content-length: 132:

      1. The repository does not exist
      2. The repository exists but the token cannot see it (tested against
         github/github, GitHub's own private monorepo)
      3. Repository and token both fine, but a parameter did not resolve
         (tested with sha=not-a-real-branch-name)

    Case 2 is deliberate on GitHub's part, not an oversight. Returning 403 for
    a private repository and 404 for a nonexistent one would turn status codes
    into an enumeration oracle: anyone could map the contents of a private
    organisation by watching which name gave which code.

    The cost is borne by whoever is debugging. A 404 cannot be resolved from
    the response alone — it needs information from outside the API. When a user
    insists the repository exists and the integration returns 404, they are
    often right, and the token is the problem.

    Case 3 is the one that wastes the most time in practice, because every
    visible part of the request is correct.
    """


class ValidationError(ClientError):
    """422 — understood, well-formed, and semantically impossible.

    NOT OBSERVED against the commits endpoint during development. Attempting to
    provoke one with an invalid branch name returned 404 instead: GitHub treats
    an unresolvable ref as a missing resource rather than a bad request.

    Kept in the taxonomy because it is documented behaviour and appears on
    write endpoints and on parameter values outside permitted ranges. Flagged
    as unobserved so that neither this code nor the README claims to handle
    something that was never seen. A 422 body carries an `errors` array naming
    the offending fields, which is worth surfacing if one ever arrives.
    """


def _header_int(response: requests.Response, name: str) -> int | None:
    """Read an integer header, returning None if absent or unparseable.

    Header lookup in requests is case-insensitive, which matters more than it
    looks: GitHub serves over HTTP/2, where header names are lowercase on the
    wire. Code that indexed a plain dict with "X-RateLimit-Remaining" would
    find nothing.
    """
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _error_body(response: requests.Response) -> dict[str, Any]:
    """Extract GitHub's error object, tolerating a body that is not JSON.

    Errors come back as a JSON object with `message` and `documentation_url` —
    a dict, where a successful list endpoint returns an array. That structural
    difference is itself diagnostic: a dict from /commits means something went
    wrong, before the status code is even read.

    The guard matters because a 5xx is often served by a proxy or load balancer
    that never reached GitHub's application, and returns HTML or plain text. A
    .json() call there raises, and an exception raised while constructing an
    exception loses the original failure entirely.
    """
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_rate_limited(response: requests.Response, body: dict[str, Any]) -> bool:
    """Decide whether a 403 is a quota problem or a permission problem.

    Two signals, either sufficient:

    Remaining == 0. Definitive when the header is present, which it is on any
    authenticated request.

    The message text. GitHub's rate limit messages mention the rate limit
    explicitly, while the permission message observed during development was
    "Resource not accessible by personal access token". Matching on message
    text is fragile — it is not a contract and GitHub can reword it — so it is
    the fallback, not the primary test, and it exists to cover the case where
    the header is absent.
    """
    remaining = _header_int(response, "X-RateLimit-Remaining")
    if remaining == 0:
        return True

    message = str(body.get("message", "")).lower()
    return "rate limit" in message or "abuse detection" in message


def classify(response: requests.Response) -> None:
    """Raise the appropriate exception for a failed response, or return.

    Returns None for any 2xx. Callers therefore call classify() and continue if
    nothing was raised, rather than checking a return value — a check that is
    easy to forget and silent when forgotten.

    3xx is not handled because requests follows redirects by default, so a 3xx
    never reaches here. Worth knowing that it is a decision and not an
    oversight: passing allow_redirects=False would change that.
    """
    if response.ok:  # any 2xx
        return

    body = _error_body(response)
    # GitHub's own message where available. The fallback covers non-JSON error
    # bodies from proxies, which have no message field.
    message = body.get("message") or response.reason or "unknown error"

    context = {
        "status_code": response.status_code,
        "url": response.url,
        "request_id": response.headers.get("X-GitHub-Request-Id"),
        "documentation_url": body.get("documentation_url"),
        "body": body,
    }

    if response.status_code == 401:
        raise AuthenticationError(message, **context)

    if response.status_code == 403:
        # The ambiguous case. GitHub uses 403 for both "you lack permission"
        # and "you are out of quota", so the status code alone is insufficient
        # and the body and headers decide.
        if _is_rate_limited(response, body):
            raise RateLimitError(
                message,
                reset_at=_header_int(response, "X-RateLimit-Reset"),
                **context,
            )
        raise PermissionError_(
            message,
            required_permission=response.headers.get("X-Accepted-GitHub-Permissions"),
            **context,
        )

    if response.status_code == 404:
        raise NotFoundError(message, **context)

    if response.status_code == 422:
        raise ValidationError(message, **context)

    if response.status_code == 429:
        # The status code the HTTP specification defines for this, which GitHub
        # uses in some circumstances and not others. Handled alongside the 403
        # case above rather than instead of it.
        raise RateLimitError(
            message,
            reset_at=_header_int(response, "X-RateLimit-Reset"),
            **context,
        )

    if 500 <= response.status_code < 600:
        raise ServerError(message, **context)

    # An unmapped 4xx. Raising ClientError rather than passing it through means
    # an unanticipated code still fails loudly and still carries the request ID,
    # instead of returning a body the caller will try to parse as data.
    raise ClientError(message, **context)
