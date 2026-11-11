"""Command-line interface: ``slo-ledger statement`` and ``slo-ledger month``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from slo_ledger import __version__
from slo_ledger._time import format_timestamp, minutes
from slo_ledger.intervals import month_window, parse_month
from slo_ledger.statement import ConfigError, build_statement, render_markdown


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slo-ledger",
        description="Reliability and usage statements from raw telemetry.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    statement = commands.add_parser("statement", help="settle a month from a TOML configuration")
    statement.add_argument("config", type=Path, help="the statement's TOML configuration")
    statement.add_argument(
        "--format", choices=("markdown", "json"), default="markdown", help="output format"
    )
    statement.add_argument("-o", "--output", type=Path, help="write to a file instead of stdout")

    month = commands.add_parser("month", help="show a calendar month's window in a time zone")
    month.add_argument("month", help="the month, YYYY-MM")
    month.add_argument("timezone", help="an IANA time zone, e.g. America/New_York")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "month":
            year, number = parse_month(args.month)
            window = month_window(year, number, args.timezone)
            text = (
                f"{format_timestamp(window.start)} to {format_timestamp(window.end)}: "
                f"{minutes(window.duration):.0f} minutes\n"
            )
        else:
            statement = build_statement(args.config)
            json_output = args.format == "json"
            text = statement.to_json_text() if json_output else render_markdown(statement)
    except (ConfigError, ValueError, KeyError) as exc:
        print(f"slo-ledger: error: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "output", None):
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0
