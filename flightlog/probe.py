"""A single, unadorned request against the GitHub API.

The purpose is to see exactly what GitHub sends back before writing any code
that assumes something about it. No pagination, no retries, no error taxonomy —
those are increments 3, 5 and 2, and each of them is built on something visible
in this output.

This module survives into the finished tool as a diagnostic: "what does the API
actually return for this repository, right now" is the first question when an
ingestion run looks wrong.
"""

from __future__ import annotations

import json
import logging

import requests

from .config import Config

log = logging.getLogger(__name__)

# How long to wait for the server, in seconds, as (connect, read).
#
# requests has NO timeout by default. A call with no timeout can hang
# indefinitely if the server accepts the connection and then never replies —
# not an error, not a failure, just a process that never returns. That is the
# single most common way a scheduled ingestion job dies silently, and it is a
# one-argument fix.
#
# Split into two because the failures are different: 5s to establish a TCP
# connection is generous and a failure there means the host is unreachable,
# while 30s to receive a response accommodates GitHub doing real work.
TIMEOUT = (5, 30)


def build_headers(config: Config) -> dict[str, str]:
    """Assemble the request headers.

    Separate from the request itself because every endpoint in this tool sends
    the same three, and because a function returning a dict can be asserted on
    in a test without a network call.
    """
    headers = {
        # Content negotiation: the client states what representation it wants.
        # GitHub versions its API through this header rather than through the
        # URL path. The vendor prefix "vnd.github" identifies the media type as
        # GitHub's own rather than generic JSON.
        "Accept": "application/vnd.github+json",
        # Pins the API version. Without it you get whatever GitHub's current
        # default is, which means the response shape can change under you
        # without any action on your part.
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # The Authorization header is added only when a token exists. Sending
    # "Bearer " with nothing after it is worse than sending nothing at all:
    # GitHub reads it as a malformed credential and returns 401, where an
    # absent header would have been accepted as an anonymous request.
    #
    # "Bearer" is the authentication scheme — it means "whoever bears this
    # token is authorised", with no further proof required. That is precisely
    # why the token must never be logged: possession is the entire security
    # model.
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    return headers


def build_commits_url(config: Config) -> str:
    """Construct the commits endpoint URL.

    Path only — query parameters are passed separately to requests, which
    handles percent-encoding. Building a query string by hand is how a colon in
    a timestamp becomes a malformed URL.
    """
    return f"{config.api_url}/repos/{config.owner}/{config.repo_name}/commits"


def probe(config: Config) -> int:
    """Make one request and print everything about the response.

    Deliberately has no error handling. If this raises, the traceback is the
    information you want at this stage. Increment 2 replaces that with a proper
    error taxonomy, built from responses observed here rather than from
    assumptions about what GitHub returns.
    """
    url = build_commits_url(config)

    # Query parameters as a dict rather than appended to the URL string, so
    # requests percent-encodes them. The `since` value contains colons, which
    # are legal in a query string but only because requests knows the rules
    # better than either of us does.
    params = {
        "since": config.since_iso,
        "per_page": config.per_page,
    }

    print("REQUEST")
    print(f"  URL        : {url}")
    print(f"  Parameters : {params}")
    # Header names only. Printing the values would put the token on screen and
    # in your shell scrollback — the exact leak `redacted_token` exists to
    # prevent, undone by a debug print.
    print(f"  Headers    : {sorted(build_headers(config))}")
    print()

    response = requests.get(
        url,
        headers=build_headers(config),
        params=params,
        timeout=TIMEOUT,
    )

    print("RESPONSE")
    # response.url is the URL requests actually sent, with parameters encoded.
    # Worth printing separately from the URL above: when a request returns
    # something unexpected, the first question is whether it asked for what you
    # thought it asked for.
    print(f"  Sent URL   : {response.url}")
    print(f"  Status     : {response.status_code} {response.reason}")
    print(f"  Elapsed    : {response.elapsed.total_seconds():.2f}s")
    print()

    # Every header, unfiltered. Increments 3, 4 and 5 are each built on one of
    # these, and reading them once in real output is worth more than reading a
    # description of them.
    print("RESPONSE HEADERS")
    for name in sorted(response.headers):
        print(f"  {name:<32} {response.headers[name]}")
    print()

    # .json() parses the body and raises if it is not valid JSON. No guard here
    # for the same reason as above: at this stage a traceback tells you more
    # than a caught exception would.
    payload = response.json()

    print("BODY")
    print(f"  Type          : {type(payload).__name__}")
    if isinstance(payload, list):
        print(f"  Records       : {len(payload)}")
        if payload:
            print()
            print("FIRST RECORD")
            print(json.dumps(payload[0], indent=2))
    else:
        # A dict here means an error response — GitHub returns errors as a
        # JSON object with "message" and "documentation_url", not as an array.
        # That shape difference is itself diagnostic.
        print(json.dumps(payload, indent=2))

    return 0
