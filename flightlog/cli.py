"""Command line entry point.

Thin by design. The CLI's job is to parse arguments, load configuration, set up
logging, and dispatch — nothing else. Ingestion logic lives in modules that can
be imported and tested without going through argparse.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Config, ConfigError, load_config
from .fetch import ingest
from .probe import probe

log = logging.getLogger(__name__)


def cmd_check(config: Config) -> int:
    """Print resolved configuration without making any network calls.

    "What does the tool think it is about to do" is the first question when a
    run misbehaves, and answering it should not cost a request.
    """
    print("Configuration loaded")
    print(f"  API root      : {config.api_url}")
    print(f"  Repository    : {config.repo}")
    # redacted_token, never config.token. The one place the token is displayed,
    # and it is displayed safely.
    print(f"  Token         : {config.redacted_token}")
    print(f"  Authenticated : {config.authenticated}")
    print(f"  Per page      : {config.per_page}")
    print(f"  Page cap      : {config.max_pages or 'none (fetch to exhaustion)'}")
    print(f"  Since         : {config.since_iso}")
    print(f"  Log level     : {config.log_level}")

    print()
    print("  Database")
    print(f"    Host        : {config.pg_host}:{config.pg_port}")
    print(f"    Database    : {config.pg_database}")
    print(f"    User        : {config.pg_user}")
    # Password deliberately not shown, redacted or otherwise. A habit of
    # displaying credentials in a diagnostic command is worth not forming.

    # Unauthenticated is a legitimate mode, not an error — hence a note and
    # exit 0. But it changes the rate limit by a factor of 80, so it should
    # never be silent.
    if not config.authenticated:
        print()
        print("  Note: no token set. Unauthenticated requests are limited to 60/hour.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Separate from main() so a test can inspect the parser without running
    anything, and so adding commands touches one function.
    """
    parser = argparse.ArgumentParser(
        # Without prog, argparse prints "__main__.py" under `python -m`.
        prog="flightlog",
        description="Ingest GitHub repository activity and report on the run.",
    )
    # Subcommands rather than flags, because these are different operations,
    # not modifiers of one operation.
    #
    # required=True makes a bare invocation print usage and exit 2 rather than
    # falling through main() with command=None.
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="validate configuration and exit")

    probe_parser = subparsers.add_parser(
        "probe", help="make one request and print the full response"
    )
    probe_parser.add_argument(
        "--repo",
        help="override TARGET_REPO for this call, e.g. dbt-labs/no-such-repo",
    )

    ingest_parser = subparsers.add_parser(
        "ingest", help="fetch commits and pull requests, and write a run report"
    )
    ingest_parser.add_argument(
        "--load",
        action="store_true",
        help="load validated records into PostgreSQL",
    )
    ingest_parser.add_argument(
        "--no-report-file",
        action="store_true",
        help="print the report but do not write it to reports/",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load config, dispatch.

    argv defaults to None, which argparse resolves to sys.argv[1:]. Accepting
    it explicitly lets a test call main(["check"]) directly.

    Returns an exit code:
        0  success
        1  run failure, including an incomplete result set
        2  configuration error (argparse also uses 2 for bad arguments)
    """
    args = build_parser().parse_args(argv)

    # Config loads before logging is configured, which is why the error below
    # is printed rather than logged — there is no handler yet, and the log
    # level itself comes from the config that just failed.
    #
    # Only ConfigError is caught. An unexpected exception here is a bug, and a
    # bug should produce a traceback rather than a tidy message hiding it.
    try:
        config = load_config()
    except ConfigError as exc:
        # stderr, so errors survive redirection and are not mistaken for output.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    # basicConfig is a one-shot: the first call configures the root logger and
    # later calls are ignored. Done once at the entry point. Library modules
    # never call it — they only getLogger.
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "check":
        return cmd_check(config)

    if args.command == "probe":
        return probe(config, repo_override=args.repo)

    if args.command == "ingest":
        return ingest(config, load=args.load, write_file=not args.no_report_file)

    # Every registered subcommand should have a branch above. Reaching here
    # means one was added to the parser and not dispatched. Naming it beats
    # exiting 1 in silence.
    print(f"No handler for command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    # SystemExit rather than sys.exit() — identical effect, since sys.exit
    # raises SystemExit, but it makes the exit code's path out of main()
    # explicit. Runs only under `python -m flightlog.cli`; importing does not
    # trigger it.
    raise SystemExit(main())
