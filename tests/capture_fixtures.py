"""Capture real API responses to disk for use as test fixtures.

Run manually, not as part of the test suite. The suite reads what this wrote
and never touches the network — that is the whole point. This exists so the
fixtures are real API responses rather than someone's recollection of their
shape, which is the failure mode that makes fixture-based tests worse than no
tests: they encode an assumption and then confirm it forever.

Re-run it when GitHub's response shape changes, and commit the diff. The diff
IS the record of what changed.

    python tests/capture_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from flightlog.config import load_config
from flightlog.probe import build_commits_url, build_headers, build_pulls_url, TIMEOUT

FIXTURES = Path(__file__).parent / "fixtures"


def capture(name: str, url: str, params: dict) -> None:
    """Save one response's status, headers and body.

    Headers are saved as well as the body, and they are the more important
    half: pagination reads Link, rate limiting reads X-RateLimit-*, and
    diagnosis reads X-GitHub-Request-Id. A fixture of the body alone would
    test none of that.
    """
    config = load_config()
    response = requests.get(
        url, headers=build_headers(config), params=params, timeout=TIMEOUT
    )

    payload = {
        "status_code": response.status_code,
        # dict() because requests' CaseInsensitiveDict is not JSON
        # serialisable. Case is not lost in any way that matters — the test
        # helper rebuilds a CaseInsensitiveDict on load, which is also what
        # the real code sees.
        "headers": dict(response.headers),
        "body": response.json(),
    }

    path = FIXTURES / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"{name}: {response.status_code}, {len(json.dumps(payload)):,} bytes")


def main() -> None:
    config = load_config()
    FIXTURES.mkdir(exist_ok=True)

    # First page of commits: has both rel=next and rel=last in its Link header.
    capture(
        "commits_page_1",
        build_commits_url(config),
        {"since": config.since_iso, "per_page": 5},
    )

    # A result set small enough to fit one page, so GitHub omits the Link
    # header entirely. Absence of Link is a real case and NOT the same as an
    # empty result — code that assumes Link is always present fails on every
    # small repository.
    # Trimmed to three records after capture. The property under test is
    # structural — records present, no Link header — and 71 real records
    # demonstrate it no better than 3 while adding 300KB to the repository.
    # Re-running this script restores the full response; trim again if the size
    # matters.
    capture(
        "commits_single_page",
        build_commits_url(config),
        {"per_page": 100, "path": "CONTRIBUTING.md"},
    )

    # Empty result set: 200, no Link header, zero records. Distinct from the
    # case above, and the pair is what separates a loop terminating on
    # "fewer than per_page records" from one terminating on the absence of
    # rel="next". The first handles these two identically and is wrong; the
    # second handles both correctly.
    capture(
        "commits_empty",
        build_commits_url(config),
        {"per_page": 100, "path": "no-such-file-anywhere.xyz"},
    )

    # Pull requests: different shape, deeper nesting, different nullable
    # fields.
    capture(
        "pulls_page_1",
        build_pulls_url(config),
        {"state": "all", "sort": "updated", "direction": "desc", "per_page": 5},
    )

    # 404. Error responses are JSON objects where success is an array, and
    # that structural difference is itself diagnostic.
    capture(
        "error_404",
        f"{config.api_url}/repos/dbt-labs/no-such-repository-xyz/commits",
        {"per_page": 1},
    )


if __name__ == "__main__":
    main()
