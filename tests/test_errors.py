"""Tests for the status code taxonomy.

Every case here was observed against the live API during development. The 403
cases in particular could not have been written from the HTTP specification:
GitHub returns 403 both for insufficient permission and for quota exhaustion,
where the spec has 429 for the latter.
"""

from __future__ import annotations

import pytest
import requests

from flightlog.errors import (
    AuthenticationError,
    ClientError,
    NotFoundError,
    PermissionError_,
    RateLimitError,
    ServerError,
    ValidationError,
    classify,
)

from .conftest import make_response, response_from_fixture


def test_success_raises_nothing():
    assert classify(make_response(200, {}, [])) is None


def test_401_is_an_authentication_error():
    """Observed message: "Bad credentials".

    Note what a 401 does NOT carry: no rate limit headers, no
    x-accepted-github-permissions. GitHub authenticates before anything else,
    so a 401 says nothing about the rest of the request — it was never
    examined.
    """
    response = make_response(
        401,
        {"X-GitHub-Request-Id": "ABC:123"},
        {"message": "Bad credentials"},
    )
    with pytest.raises(AuthenticationError) as exc:
        classify(response)
    assert exc.value.status_code == 401
    assert exc.value.request_id == "ABC:123"
    # Not retryable: the same token produces the same answer.
    assert not isinstance(exc.value, ServerError)


def test_403_with_quota_remaining_is_a_permission_error():
    """Authenticated successfully, then refused.

    Observed message: "Resource not accessible by personal access token" —
    about entitlement, not identity. The response carries rate limit headers
    because the request WAS attributed and billed, and
    x-accepted-github-permissions names what was required.
    """
    response = make_response(
        403,
        {
            "X-RateLimit-Remaining": "4990",
            "X-Accepted-GitHub-Permissions": "administration=write",
        },
        {"message": "Resource not accessible by personal access token"},
    )
    with pytest.raises(PermissionError_) as exc:
        classify(response)
    assert exc.value.required_permission == "administration=write"


def test_403_with_no_quota_is_a_rate_limit_error():
    """The ambiguous case. Same status code, different meaning.

    GitHub returns 403 for quota exhaustion as well as for permission, so the
    status code alone is insufficient and the headers decide.
    """
    response = make_response(
        403,
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1788885374"},
        {"message": "API rate limit exceeded for user ID 1."},
    )
    with pytest.raises(RateLimitError) as exc:
        classify(response)
    assert exc.value.reset_at == 1788885374


def test_429_is_a_rate_limit_error():
    """The spec-compliant path, which GitHub uses sometimes and not always."""
    with pytest.raises(RateLimitError):
        classify(make_response(429, {"X-RateLimit-Reset": "1788885374"},
                               {"message": "Too Many Requests"}))


def test_404_from_a_real_response():
    """Three distinct causes produce byte-identical 404s.

    Nonexistent repository, private repository the token cannot see, and a
    parameter that does not resolve. GitHub refuses to distinguish them —
    doing so would let anyone enumerate private repositories by watching
    status codes.
    """
    with pytest.raises(NotFoundError) as exc:
        classify(response_from_fixture("error_404"))
    assert exc.value.status_code == 404
    assert exc.value.request_id is not None


def test_422_is_a_validation_error():
    """NOT observed against the commits endpoint during development.

    An invalid branch name returned 404 rather than 422 — GitHub treats an
    unresolvable ref as a missing resource. Kept because 422 is documented
    behaviour on write endpoints.
    """
    with pytest.raises(ValidationError):
        classify(make_response(422, {}, {"message": "Validation Failed"}))


def test_5xx_is_a_server_error_and_retryable():
    with pytest.raises(ServerError):
        classify(make_response(503, {}, {"message": "Service Unavailable"}))


def test_non_json_error_body_does_not_crash_the_classifier():
    """A 5xx often comes from a proxy that never reached GitHub.

    Those return HTML or plain text. An exception raised while constructing an
    exception would lose the original failure entirely.
    """
    response = requests.Response()
    response.status_code = 502
    response.url = "https://api.github.com/x"
    response._content = b"<html>502 Bad Gateway</html>"

    with pytest.raises(ServerError):
        classify(response)


def test_unmapped_4xx_still_raises_with_context():
    """An unanticipated status code fails loudly rather than passing through.

    Returning the body for the caller to parse as data would be the silent
    wrong answer.
    """
    with pytest.raises(ClientError) as exc:
        classify(make_response(418, {"X-GitHub-Request-Id": "XYZ:9"},
                               {"message": "I'm a teapot"}))
    assert exc.value.request_id == "XYZ:9"
