"""Command-line interface of agent-sandbox."""

import argparse
import sys
from collections.abc import Sequence

from agent_sandbox import __version__

PROG = "agent-sandbox"

EXIT_OK = 0
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Run AI agents and other programs in a bubblewrap sandbox that only sees the "
            "files and directories you allow, with reviewable, jailed writes."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    return parser


def _exit_code(exc: SystemExit) -> int:
    """Translate a ``SystemExit`` raised by argparse into an exit code."""
    if exc.code is None:
        return EXIT_OK
    if isinstance(exc.code, int):
        return exc.code
    return EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)
    parser.print_usage(sys.stderr)
    print(f"{PROG}: error: a subcommand is required", file=sys.stderr)
    return EXIT_USAGE
