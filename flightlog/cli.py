"""Command line entry point.

Thin by design. The CLI's job is to parse arguments, load configuration, set up
logging, and dispatch — nothing else. Ingestion logic lives in modules that can
be imported and tested without going through argparse.

That separation matters at increment 11: a test that has to construct a fake
argv to exercise pagination is testing the wrong thing.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Config, ConfigError, load_config
from .config import Config, ConfigError, load_config
from .probe import probe
from .fetch import fetch_commits, fetch_pull_requests

# Module-level logger named after the module ("flightlog.cli"). Using
# logging.getLogger(__name__) throughout rather than the root logger means log
# output identifies which part of the tool produced it — useful once the client
# and the loader are both logging during a run.
#
# Unused in this increment; declared now so it is in place from the start
# rather than added in three separate commits later.
log = logging.getLogger(__name__)


def cmd_check(config: Config) -> int:
    """Print resolved configuration without making any network calls.

    This exists because "what does the tool think it is about to do" is the
    first question when a run misbehaves, and answering it should not require
    spending a request or reading the source.

    Returns an exit code rather than calling sys.exit(). Command functions that
    return codes can be called from a test and asserted on; ones that exit kill
    the test runner.
    """
    print("Configuration loaded")
    # Fixed-width labels so the values line up in a terminal. Trivial, but a
    # ragged block is harder to scan, and this output exists to be scanned.
    print(f"  API root      : {config.api_url}")
    print(f"  Repository    : {config.repo}")
    # redacted_token, never config.token. This is the one place the token is
    # displayed and it is displayed safely.
    print(f"  Token         : {config.redacted_token}")
    print(f"  Authenticated : {config.authenticated}")
    print(f"  Per page      : {config.per_page}")
    # `or` handles the None case: an unset cap prints the words rather than
    # "None", which reads like a bug. Safe here because 0 is already rejected
    # by validation — otherwise `0 or ...` would take the same branch as None.
    print(f"  Page cap      : {config.max_pages or 'none (fetch to exhaustion)'}")
    print(f"  Since         : {config.since_iso}")
    print(f"  Log level     : {config.log_level}")
    print()
    print("  Database")
    print(f"    Host        : {config.pg_host}:{config.pg_port}")
    print(f"    Database    : {config.pg_database}")
    print(f"    User        : {config.pg_user}")
    # Password deliberately not shown, redacted or otherwise. Nothing about a
    # local development password is worth printing, and a habit of displaying
    # credentials in a diagnostic command is worth not forming.
    
    # Unauthenticated is a legitimate mode, not an error — hence a note rather
    # than a warning, and exit 0. But it changes the rate limit by a factor of
    # 80, so it should never be a silent condition.
    if not config.authenticated:
        print()
        print("  Note: no token set. Unauthenticated requests are limited to 60/hour.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Separate from main() so a test can inspect the parser without running
    anything, and so adding commands in later increments touches one function.
    """
    parser = argparse.ArgumentParser(
        # prog controls the name in usage and error messages. Without it,
        # argparse prints "__main__.py" when invoked via `python -m`, which
        # tells the reader nothing.
        prog="flightlog",
        description="Ingest GitHub repository activity and report on the run.",
    )
    # Subcommands rather than flags, because the commands to come — fetch,
    # load, report — are different operations, not modifiers of one operation.
    #
    # required=True makes a bare `python -m flightlog.cli` print usage and exit
    # 2 rather than falling through to the end of main() with command=None.
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="validate configuration and exit")
    probe_parser = subparsers.add_parser(
        "probe", help="make one request and print the full response"
    )
    probe_parser.add_argument(
        "--repo",
        help="override TARGET_REPO for this call, e.g. dbt-labs/no-such-repo",
    )
    fetch_parser = subparsers.add_parser(
        "fetch", help="page through all commits and report counts"
    )
    fetch_parser.add_argument(
        "--load",
        action="store_true",
        help="load validated records into PostgreSQL",
    )
    pulls_parser = subparsers.add_parser(
        "pulls", help="page through pull requests and report counts"
    )
    pulls_parser.add_argument(
        "--load",
        action="store_true",
        help="load validated records into PostgreSQL",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load config, dispatch.

    argv defaults to None, which argparse resolves to sys.argv[1:]. Accepting
    it explicitly lets a test call main(["check"]) directly.

    Returns an exit code:
        0  success
        1  run failure
        2  configuration error (argparse also uses 2 for bad arguments)
    """
    args = build_parser().parse_args(argv)

    # Config loads before logging is configured, which is why the error below
    # is printed rather than logged — there is no configured handler yet, and
    # the log level itself comes from the config that just failed.
    #
    # Only ConfigError is caught. An unexpected exception here is a bug, and a
    # bug should produce a traceback, not a tidy message that hides it.
    try:
        config = load_config()
    except ConfigError as exc:
        # stderr, not stdout: errors should survive `... > output.txt` and
        # should not be mistaken for output by anything parsing the tool.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    # basicConfig is a one-shot: the first call configures the root logger and
    # later calls are ignored. Doing it once here, at the entry point, is the
    # convention. Library modules never call it — they only getLogger.
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "check":
        return cmd_check(config)

    if args.command == "probe":
        return probe(config, repo_override=args.repo)

    if args.command == "fetch":
            return fetch_commits(config, load=args.load)
    
    if args.command == "pulls":
        return fetch_pull_requests(config, load=args.load)

    # Unreachable while every subcommand has a branch above and required=True
    # is set. Kept as a defensive default so that adding a subparser and
    # forgetting the dispatch branch fails visibly with exit 1 rather than
    # succeeding silently.
    return 1
    
if __name__ == "__main__":
    # SystemExit rather than sys.exit() — identical effect, since sys.exit
    # raises SystemExit, but this makes the exit code's path out of main()
    # explicit. This block runs only under `python -m flightlog.cli`; importing
    # the module does not trigger it.
    raise SystemExit(main())
