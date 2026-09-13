"""Configuration loading and validation.

Settings come from a .env file at the repo root, overridden by real
environment variables. Every value is validated at load time so that a
misconfigured run fails immediately rather than part-way through a fetch.

The design principle here: a fetch that runs for two minutes and then dies
because PER_PAGE was a typo has wasted two minutes and left a partial load
behind. Validating up front costs nothing and makes the failure obvious.
"""

from __future__ import annotations

# `from __future__ import annotations` makes all type hints lazy strings rather
# than evaluated objects. The practical effect is that `int | None` works on
# Python 3.9, where that syntax would otherwise be a syntax error. Harmless on
# newer versions, and it keeps the file portable.

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Resolve the repo root from this file's own location rather than from the
# current working directory. Without this, `python -m flightlog.cli check` would
# find .env when run from the repo root and silently fail to find it when run
# from anywhere else — the kind of bug that looks like "it works on my machine".
# __file__ is flightlog/config.py, so .parent is flightlog/ and .parent.parent
# is the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

# GitHub rejects per_page above 100 with a 422. Encoding the API's own limit
# here means we fail locally with a clear message instead of making a request
# that was always going to be refused.
MAX_PER_PAGE = 100


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed.

    A distinct exception type so the CLI can catch configuration problems
    specifically and exit 2, separate from runtime failures which exit 1.
    Catching bare RuntimeError would also swallow genuine bugs.
    """


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration.

    frozen=True makes instances immutable. Config is read in a dozen places
    once the client exists; nothing should be able to mutate it half-way
    through a run and leave two parts of the code disagreeing about the target
    repository. If a value needs to change, that is a new Config object.
    """

    # These are the raw settings, all validated before the object is built.
    # Nothing in this class re-validates — by the time you hold a Config, the
    # values are known good.
    token: str
    api_url: str
    repo: str
    per_page: int
    max_pages: int | None  # None means "no cap, fetch to exhaustion"
    since_months: int
    log_level: str
    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str

    # Everything below is derived. These are properties rather than stored
    # fields so there is one source of truth: `repo` is the setting, `owner`
    # and `repo_name` are views onto it. Storing all three would let them
    # drift apart.

    @property
    def authenticated(self) -> bool:
        """Whether a token was supplied.

        Drives the rate limit the run is subject to: 5,000 requests/hour with
        a token, 60/hour without. Running unauthenticated on purpose is how
        the rate-limit handling gets exercised.
        """
        return bool(self.token)

    @property
    def owner(self) -> str:
        """Owner half of owner/name.

        Safe to split unconditionally because load_config() has already
        rejected anything without exactly one slash.
        """
        return self.repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        """Name half of owner/name."""
        return self.repo.split("/", 1)[1]

    @property
    def since(self) -> datetime:
        """Start of the pull window, as an aware UTC datetime.

        Two decisions worth defending:

        Aware, not naive — it carries an explicit UTC offset. GitHub returns
        timestamps in UTC and expects them in UTC. A naive datetime here would
        silently pick up the local machine's timezone, which in Spain is one or
        two hours off depending on the season, and would shift the window
        without any error.

        30-day months — deliberately approximate. Good enough for "roughly the
        last year", wrong for anything needing calendar accuracy. Documented as
        a limitation rather than left to look more precise than it is.
        """
        return datetime.now(timezone.utc) - timedelta(days=30 * self.since_months)

    @property
    def since_iso(self) -> str:
        """The pull window start in the ISO 8601 form GitHub expects.

        GitHub's `since` parameter wants YYYY-MM-DDTHH:MM:SSZ. The trailing Z
        means UTC. Building it with strftime rather than isoformat() because
        isoformat() on an aware datetime emits '+00:00' instead of 'Z' —
        GitHub accepts both, but the Z form is what its documentation shows and
        it is easier to eyeball in logs.
        """
        return self.since.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def redacted_token(self) -> str:
        """Token rendered for display. Never print self.token directly.

        The single approved way to show the token, so there is one place to
        audit. Shows enough to confirm which token is loaded — useful when you
        have several and suspect the wrong one is in .env — without exposing a
        usable credential. The length is included because a truncated paste is
        a common cause of a mystifying 401.

        Tokens leak through logs and screenshots far more often than through
        commits, and .gitignore does nothing about either.
        """
        if not self.token:
            return "(none — unauthenticated)"
        return f"{self.token[:4]}…{self.token[-4:]} ({len(self.token)} chars)"

    @property
    def pg_dsn(self) -> str:
        """Connection string for psycopg2.

        Assembled here rather than configured as one string, so each part can be
        overridden independently and so the password never has to appear in a
        setting that might be logged whole.
        """
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_database} "
            f"user={self.pg_user} password={self.pg_password}"
        )


# ---------------------------------------------------------------------------
# Environment readers
#
# Two small helpers so that every setting is read the same way. The alternative
# — os.environ.get() scattered through load_config with ad hoc int() calls —
# ends up with inconsistent whitespace handling and error messages that name
# the wrong variable.
# ---------------------------------------------------------------------------


def _env_str(key: str, default: str = "") -> str:
    """Read a string setting, stripped.

    The strip matters: `PER_PAGE=100 ` with a trailing space is invisible in an
    editor and breaks int() with a confusing message. Same for a value pasted
    with a leading space.
    """
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int | None) -> int | None:
    """Read an integer setting, or return the default if unset or blank.

    Blank is treated as unset. That is what makes `MAX_PAGES=` in .env mean
    "no cap" rather than raising — an empty value is a deliberate way to say
    nothing, and .env has no way to express a missing key other than deleting
    the line.

    The `raise ... from exc` preserves the original ValueError as the cause, so
    a traceback shows both the friendly message and what actually failed.
    """
    raw = _env_str(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def load_config(env_path: Path | None = None) -> Config:
    """Load and validate configuration.

    The env_path argument exists for tests: a test can point at a fixture .env
    without touching the real one. Production callers pass nothing.

    Every check below raises ConfigError with the variable name, the rule, and
    the offending value. All three matter — "invalid configuration" tells the
    person nothing about which line of .env to look at.
    """
    # override=False means a variable already set in the shell wins over .env.
    # This is the dotenv convention and it is what makes a one-off override
    # work without editing the file:
    #
    #     MAX_PAGES=1 python -m flightlog.cli check
    #
    # It also means an exported variable left over in a shell session will
    # quietly beat .env, which is worth remembering when a setting appears not
    # to take effect.
    load_dotenv(dotenv_path=env_path or ENV_PATH, override=False)

    # Repository: must be exactly owner/name. The `all(...)` catches the cases
    # a slash count alone misses — "/dbt-core" and "dbt-labs/" both have one
    # slash and both produce an empty half, which would build a URL like
    # /repos//dbt-core/commits and return a puzzling 404.
    repo = _env_str("TARGET_REPO", "dbt-labs/dbt-core")
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise ConfigError(f"TARGET_REPO must be in owner/name form, got {repo!r}")

    # Page size. Default 100 rather than GitHub's own default of 30: a full
    # pull at 30 needs three times the requests for the same data, and request
    # count is the scarce resource here, not bandwidth.
    per_page = _env_int("PER_PAGE", 100)
    if not 1 <= per_page <= MAX_PER_PAGE:
        raise ConfigError(f"PER_PAGE must be between 1 and {MAX_PER_PAGE}, got {per_page}")

    # Page cap. Defaults to None — unbounded — because that is correct
    # behaviour for a real run. The cap is a development convenience, set in
    # .env while iterating, and an explicit setting rather than a magic number
    # buried in the fetch loop.
    max_pages = _env_int("MAX_PAGES", None)
    if max_pages is not None and max_pages < 1:
        raise ConfigError(f"MAX_PAGES must be at least 1 if set, got {max_pages}")

    # Pull window. The `is None` check is not redundant: _env_int returns
    # int | None, and while the default of 12 means None cannot occur in
    # practice, the type says it can. Checking keeps the code honest and keeps
    # type checkers quiet.
    since_months = _env_int("SINCE_MONTHS", 12)
    if since_months is None or since_months < 1:
        raise ConfigError(f"SINCE_MONTHS must be at least 1, got {since_months}")

    # API root. rstrip("/") normalises the trailing slash so that later URL
    # building can assume there is none — otherwise a value ending in "/"
    # produces "https://api.github.com//repos/..." which mostly works and
    # occasionally does not.
    api_url = _env_str("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not api_url.startswith(("http://", "https://")):
        raise ConfigError(f"GITHUB_API_URL must include a scheme, got {api_url!r}")

    # Log level. Validated against the set logging accepts, upper-cased first
    # so "debug" in .env works. Without this check, logging.basicConfig would
    # raise a less helpful error later, after the config had appeared to load
    # successfully.
    log_level = _env_str("LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"LOG_LEVEL is not a valid level: {log_level!r}")

    pg_port = _env_int("POSTGRES_PORT", 5434)
    if pg_port is None or not 1 <= pg_port <= 65535:
        raise ConfigError(f"POSTGRES_PORT must be a valid port number, got {pg_port}")
    # Construct only after every value has passed. A Config object therefore
    # always represents a valid configuration — nothing downstream needs to
    # re-check, and nothing downstream should.
    #
    # The token is the one setting with no validation: empty is legitimate
    # (unauthenticated mode), and there is no way to tell a good token from a
    # bad one without asking GitHub. Whether it works is a 401 at increment 2,
    # not a config error here.
    return Config(
        token=_env_str("GITHUB_TOKEN"),
        api_url=api_url,
        repo=repo,
        per_page=per_page,
        max_pages=max_pages,
        since_months=since_months,
        log_level=log_level,
        pg_host=_env_str("POSTGRES_HOST", "localhost"),
        pg_port=pg_port,
        pg_database=_env_str("POSTGRES_DB", "flightlog"),
        pg_user=_env_str("POSTGRES_USER", "flightlog"),
        pg_password=_env_str("POSTGRES_PASSWORD", "flightlog"),
    )
