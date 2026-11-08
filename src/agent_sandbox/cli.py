"""Command-line interface of agent-sandbox."""

import argparse
import os
import signal
import sys
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn

from agent_sandbox import __version__, merging, session
from agent_sandbox.diffing import diff_text, listdiffs_text
from agent_sandbox.errors import AgentSandboxError, ExitCode, Terminated
from agent_sandbox.options import (
    Command,
    DiffOptions,
    DiscardOptions,
    EnvArg,
    EnvArgKind,
    LayoutChoice,
    ListDiffsOptions,
    MergeOptions,
    MountArg,
    MountMode,
    RunOptions,
)
from agent_sandbox.pending import all_pending, find_pending, normalize_path
from agent_sandbox.state import state_dir_location

PROG = "agent-sandbox"

MAIN_DESCRIPTION = """\
Run AI agents and other programs in a bubblewrap sandbox. The program sees only
the host's system directories (read-only, so all installed tools keep working)
and the paths you allow. Writes to jailed paths are kept aside in an overlay, so
you can review them and then merge or discard them.
"""

MAIN_EPILOG = """\
typical workflow:
  agent-sandbox run -j ~/git/myproject -- my-agent   run the agent with jailed writes
  agent-sandbox listdiffs                            list paths with pending changes
  agent-sandbox diff ~/git/myproject                 review the changes
  agent-sandbox merge ~/git/myproject                apply them (or: discard)

Run 'agent-sandbox <subcommand> --help' for details on a subcommand.
No root access or sudo is needed for anything.

exit codes:
  0       success
  1       error in diff, listdiffs, merge or discard
  2       usage error
  125     run: the sandbox could not be set up
  127     run: PROGRAM not found
  N       run: exit code of PROGRAM
  128+S   run: PROGRAM was killed by signal S
"""

RUN_USAGE = "%(prog)s [options] [--] PROGRAM [ARGS...]"

RUN_DESCRIPTION = """\
Run PROGRAM with ARGS inside a bubblewrap sandbox.

Inside the sandbox the program sees:
  - the host's system directories, read-only, so installed tools work
    (FHS: /usr, /bin, /lib*, /opt, /etc; NixOS: /nix/store, /run/current-system,
    /run/wrappers, /etc, /bin, /usr)
  - the paths given with -r, -w and -j, at their original locations
  - an empty home directory, an empty /tmp, and its own /proc and /dev
Everything else on the host is invisible. The environment is passed on unchanged
unless --env or --env-file is used. The network is shared unless --no-network is
used.

PROGRAM is looked up in $PATH as seen inside the sandbox if it contains no '/'.
"""

RUN_EPILOG = """\
argument parsing:
  Options end at the first argument that is neither an option nor an option's
  value. That argument is PROGRAM; it and everything after it are passed on
  unchanged, so 'run -j ~/p bash -l' passes -l to bash. Use -- to end the
  options explicitly, e.g. when the program's arguments look like options.
  The argument after an option that takes a value is always that value.
  Unknown options before PROGRAM are an error.

environment entries (--env and --env-file):
  NAME        pass the host value of NAME (nothing is set if it is unset there)
  NAME=VALUE  set NAME to VALUE; VALUE is taken literally (no quote removal, no
              $ expansion) and may contain '=', spaces and '#'
  If any entry is given, only these variables and the baseline (PATH, HOME,
  USER, LOGNAME, SHELL, TERM, LANG, LC_ALL, TZ) are passed on. Entries are
  applied in command-line order; the last entry for a name wins and may override
  a baseline variable. Values are never printed, not even with --debug.
  An --env-file has one entry per line; blank lines and lines starting with #
  are ignored.

jailed writes (-j):
  The program reads and writes a jailed path as usual, but all changes are
  stored in the state directory and the real path stays untouched. Changes from
  several runs accumulate until you review them with 'agent-sandbox diff PATH'
  and apply them with 'agent-sandbox merge PATH' or drop them with
  'agent-sandbox discard PATH'. Only one session at a time may use a jailed path.

  Limitations if PATH is a single file instead of a directory:
    - replacing the file via rename, or deleting it, fails with
      "Device or resource busy"
    - new files created next to it are not kept; they are lost when the
      sandbox exits

requirements:
  bwrap (bubblewrap); for -j also fuse-overlayfs and fusermount3. Unprivileged
  user namespaces must be enabled. No root access or sudo is needed.

examples:
  agent-sandbox run -r ~/important_file -j ~/git/myproject \\
      -w ~/Documents/scratchspace /bin/bash -l
  agent-sandbox run -j ~/git/myproject -- /bin/some_program -j -w -v -r
  agent-sandbox run -j ~/git/myproject --env API_KEY --env DEBUG=1 -- my-agent
  agent-sandbox run --no-network -r ~/data -- python3 analyze.py
  agent-sandbox run --dry-run -r ~/docs -- ls ~/docs

exit codes:
  N       exit code of PROGRAM
  128+S   PROGRAM was killed by signal S
  2       usage error
  125     the sandbox could not be set up (bwrap or overlay failure, missing
          dependency, invalid path)
  127     PROGRAM not found
"""

DIFF_DESCRIPTION = """\
Show the pending changes of the jailed path PATH as a unified diff on stdout.

PATH is the path that was given to 'run -j'. Text changes can be applied with
patch or git apply; changed binary files are listed by name only. Nothing is
printed if there are no pending changes. Works even if PATH no longer exists.
"""

DIFF_EPILOG = """\
examples:
  agent-sandbox diff ~/git/myproject
  agent-sandbox diff --stat ~/git/myproject
  agent-sandbox diff ~/git/myproject | less

exit codes:
  0       success, also if there are no pending changes
  1       error
  2       usage error
"""

LISTDIFFS_DESCRIPTION = """\
List all jailed paths with pending changes on stdout, one per line. Each line
holds the path, the number of changed entries and the time of the last change,
separated by tabs. Nothing is printed if there are no pending changes.
"""

LISTDIFFS_EPILOG = """\
examples:
  agent-sandbox listdiffs
  agent-sandbox listdiffs | cut -f1

exit codes:
  0       success, also if there are no pending changes
  1       error
  2       usage error
"""

MERGE_DESCRIPTION = """\
Apply the pending changes of the jailed path PATH to the real path, then clear
them. New and modified files are copied (keeping mode, modification time and
symlinks), deleted files and directories are removed.

Shows a summary and asks for confirmation first, unless --yes is given; the
question goes to stderr and the answer is read from stdin. If merging fails
halfway, the pending changes are kept and merge can safely be run again.
Refuses to run while a session uses PATH (--dry-run works anyway).

Conflicts: if a file changed on the host after the sandbox changed it, merge
lists these files and refuses, unless --force is given.
"""

MERGE_EPILOG = """\
examples:
  agent-sandbox merge ~/git/myproject
  agent-sandbox merge --dry-run ~/git/myproject
  agent-sandbox merge -y ~/git/myproject
  agent-sandbox merge --force ~/git/myproject

exit codes:
  0       success
  1       error, no pending changes for PATH, PATH is in use by a session,
          conflicts without --force, or the question was not answered with y
  2       usage error
"""

DISCARD_DESCRIPTION = """\
Delete the pending changes of the jailed path PATH. The real path is not
changed. Asks for confirmation first, unless --yes is given. Refuses to run
while a session uses PATH.
"""

DISCARD_EPILOG = """\
examples:
  agent-sandbox discard ~/git/myproject
  agent-sandbox discard -y ~/git/myproject

exit codes:
  0       success
  1       error, no pending changes for PATH, PATH is in use by a session, or
          the question was not answered with y
  2       usage error
"""


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser whose error messages use the format ``agent-sandbox: error: ...``."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(ExitCode.USAGE, f"{PROG}: error: {message}\n")


@dataclass(frozen=True)
class _Parsers:
    """The top-level parser, the subcommand parsers and the ``run`` value options."""

    main: _ArgumentParser
    subcommands: Mapping[str, _ArgumentParser]
    run_value_options: Mapping[str, str]


def _mount_type(mode: MountMode) -> Callable[[str], MountArg]:
    def convert(path: str) -> MountArg:
        return MountArg(mode, path)

    return convert


def _env_type(kind: EnvArgKind) -> Callable[[str], EnvArg]:
    def convert(value: str) -> EnvArg:
        return EnvArg(kind, value)

    return convert


def _value_options(actions: Iterable[argparse.Action]) -> dict[str, str]:
    """Map every option string that takes a value to its canonical long form."""
    result: dict[str, str] = {}
    for action in actions:
        if action.nargs == 0 or not action.option_strings:
            continue
        canonical = max(action.option_strings, key=len)
        result.update(dict.fromkeys(action.option_strings, canonical))
    return result


def _add_common_options(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [
        parser.add_argument("-h", "--help", action="help", help="Show this help and exit."),
        parser.add_argument(
            "--state-dir",
            metavar="DIR",
            help=(
                "Directory that holds jailed changes. Default: $AGENT_SANDBOX_STATE_DIR if "
                "set, else $XDG_STATE_HOME/agent-sandbox, else ~/.local/state/agent-sandbox."
            ),
        ),
        parser.add_argument(
            "--debug",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Print detailed steps and tracebacks to stderr.",
        ),
    ]


def _add_run_options(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    mounts = parser.add_argument_group("mount options (repeatable)")
    env = parser.add_argument_group("environment options (repeatable)")
    sandbox = parser.add_argument_group("sandbox options")
    return [
        mounts.add_argument(
            "-r",
            "--ro",
            dest="mounts",
            action="append",
            type=_mount_type(MountMode.READ_ONLY),
            metavar="PATH",
            help="Make PATH visible read-only.",
        ),
        mounts.add_argument(
            "-w",
            "--rw",
            dest="mounts",
            action="append",
            type=_mount_type(MountMode.WRITABLE),
            metavar="PATH",
            help="Make PATH visible and writable. Writes go directly to the host.",
        ),
        mounts.add_argument(
            "-j",
            "--jail",
            dest="mounts",
            action="append",
            type=_mount_type(MountMode.JAILED),
            metavar="PATH",
            help=(
                "Make PATH visible with jailed writes: changes are kept in an overlay and "
                "reach the host only via 'agent-sandbox merge'. PATH may be a directory "
                "or a single file (see limitations below)."
            ),
        ),
        env.add_argument(
            "--env",
            dest="env",
            action="append",
            type=_env_type(EnvArgKind.ENTRY),
            metavar="NAME[=VALUE]",
            help=(
                "Pass the host variable NAME, or set NAME to VALUE. Restricts the "
                "environment to the given variables plus a baseline (see below)."
            ),
        ),
        env.add_argument(
            "--env-file",
            dest="env",
            action="append",
            type=_env_type(EnvArgKind.FILE),
            metavar="FILE",
            help="Read --env entries from FILE, one per line.",
        ),
        sandbox.add_argument(
            "--no-network",
            dest="network",
            action="store_false",
            help="Disable network access. Default: the network is shared.",
        ),
        sandbox.add_argument(
            "--chdir",
            metavar="DIR",
            help=(
                "Working directory inside the sandbox. Default: the current directory if "
                "it is visible in the sandbox, else /."
            ),
        ),
        sandbox.add_argument(
            "--layout",
            choices=[choice.value for choice in LayoutChoice],
            default=LayoutChoice.AUTO.value,
            help=(
                "Host layout that decides which system directories are shared. "
                "Default: auto (detected)."
            ),
        ),
        sandbox.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Print the bwrap command line and overlay setup, then exit without "
                "running anything. Environment values are masked."
            ),
        ),
    ]


def _build_parsers() -> _Parsers:
    top = _ArgumentParser(
        prog=PROG,
        usage="%(prog)s [-h] [--version] [--debug] <subcommand> [options] [arguments]",
        description=MAIN_DESCRIPTION,
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        add_help=False,
    )
    top.add_argument("-h", "--help", action="help", help="Show this help and exit.")
    top.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
        help="Show the version and exit.",
    )
    top.add_argument(
        "--debug", action="store_true", help="Print detailed steps and tracebacks to stderr."
    )
    subcommands = top.add_subparsers(dest="command", title="subcommands", metavar="<subcommand>")

    def add_subcommand(name: str, summary: str, description: str, epilog: str) -> _ArgumentParser:
        parser: _ArgumentParser = subcommands.add_parser(
            name,
            prog=f"{PROG} {name}",
            help=summary,
            description=description,
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            allow_abbrev=False,
            add_help=False,
        )
        return parser

    run = add_subcommand("run", "Run a program inside the sandbox.", RUN_DESCRIPTION, RUN_EPILOG)
    run.usage = RUN_USAGE
    run_actions = _add_common_options(run) + _add_run_options(run)

    diff = add_subcommand(
        "diff", "Show pending changes of a jailed path.", DIFF_DESCRIPTION, DIFF_EPILOG
    )
    _add_common_options(diff)
    diff.add_argument("path", metavar="PATH", help="The jailed path, as given to 'run -j'.")
    diff.add_argument(
        "--stat",
        action="store_true",
        help="Show a summary of changed, added and deleted entries instead of the diff.",
    )

    listdiffs = add_subcommand(
        "listdiffs",
        "List all jailed paths with pending changes.",
        LISTDIFFS_DESCRIPTION,
        LISTDIFFS_EPILOG,
    )
    _add_common_options(listdiffs)

    merge = add_subcommand(
        "merge",
        "Apply pending changes to the real path.",
        MERGE_DESCRIPTION,
        MERGE_EPILOG,
    )
    _add_common_options(merge)
    merge.add_argument("path", metavar="PATH", help="The jailed path, as given to 'run -j'.")
    merge.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation.")
    merge.add_argument(
        "--dry-run", action="store_true", help="Show what would change and modify nothing."
    )
    merge.add_argument(
        "--force",
        action="store_true",
        help=(
            "Merge even if files changed on the host after the sandbox changed them; "
            "the pending versions replace the host's."
        ),
    )

    discard = add_subcommand(
        "discard", "Delete pending changes of a jailed path.", DISCARD_DESCRIPTION, DISCARD_EPILOG
    )
    _add_common_options(discard)
    discard.add_argument("path", metavar="PATH", help="The jailed path, as given to 'run -j'.")
    discard.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation.")

    return _Parsers(
        main=top,
        subcommands={
            "run": run,
            "diff": diff,
            "listdiffs": listdiffs,
            "merge": merge,
            "discard": discard,
        },
        run_value_options=_value_options(run_actions),
    )


def split_run_args(
    args: Sequence[str], value_options: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    """Split the arguments after ``run`` into options and the program with its arguments.

    Options end at ``--`` or at the first argument that is neither an option nor an
    option's value (FR-6, FR-7). Options that take a value are normalized to
    ``--long=value`` (FR-10), so argparse accepts values that start with ``-``.
    Unknown options are passed on unchanged for argparse to report.
    """
    options: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return options, list(args[index + 1 :])
        if not arg.startswith("-") or arg == "-":
            return options, list(args[index:])
        if arg.startswith("--"):
            name, separator, value = arg.partition("=")
            has_value = bool(separator)
        else:
            name, attached = arg[:2], arg[2:]
            has_value = bool(attached)
            value = attached.removeprefix("=")
        canonical = value_options.get(name)
        if canonical is None:
            options.append(arg)
        elif has_value:
            options.append(f"{canonical}={value}")
        elif index + 1 < len(args):
            index += 1
            options.append(f"{canonical}={args[index]}")
        else:
            options.append(arg)
        index += 1
    return options, []


def _subcommand_index(argv: Sequence[str]) -> int | None:
    """Return the index of the subcommand; top-level options take no values."""
    for index, arg in enumerate(argv):
        if arg == "--":
            return None
        if not arg.startswith("-") or arg == "-":
            return index
    return None


def parse_command(argv: Sequence[str]) -> Command:
    """Parse the command line into the options of one subcommand.

    Raises ``SystemExit`` for ``--help``, ``--version`` and usage errors.
    """
    parsers = _build_parsers()
    argv = list(argv)
    program: list[str] = []
    index = _subcommand_index(argv)
    if index is not None and argv[index] == "run":
        options, program = split_run_args(argv[index + 1 :], parsers.run_value_options)
        argv = argv[: index + 1] + options

    args, unknown = parsers.main.parse_known_args(argv)
    if args.command is None:
        parsers.main.error("a subcommand is required")
    subparser = parsers.subcommands[args.command]
    if unknown:
        subparser.error(f"unrecognized arguments: {' '.join(unknown)}")
    match args.command:
        case "run":
            if not program:
                subparser.error("no PROGRAM given")
            return RunOptions(
                program=program[0],
                args=tuple(program[1:]),
                mounts=tuple(args.mounts or ()),
                env=tuple(args.env or ()),
                network=args.network,
                chdir=args.chdir,
                layout=LayoutChoice(args.layout),
                state_dir=args.state_dir,
                dry_run=args.dry_run,
                debug=args.debug,
            )
        case "diff":
            return DiffOptions(
                path=args.path, stat=args.stat, state_dir=args.state_dir, debug=args.debug
            )
        case "listdiffs":
            return ListDiffsOptions(state_dir=args.state_dir, debug=args.debug)
        case "merge":
            return MergeOptions(
                path=args.path,
                yes=args.yes,
                dry_run=args.dry_run,
                state_dir=args.state_dir,
                debug=args.debug,
                force=args.force,
            )
        case "discard":
            return DiscardOptions(
                path=args.path, yes=args.yes, state_dir=args.state_dir, debug=args.debug
            )
    raise AssertionError(f"unhandled subcommand: {args.command}")


def _exit_code(exc: SystemExit) -> int:
    """Translate a ``SystemExit`` raised by argparse into an exit code."""
    if exc.code is None:
        return ExitCode.OK
    if isinstance(exc.code, int):
        return exc.code
    return ExitCode.USAGE


def warn(message: str) -> None:
    """Print a warning to stderr."""
    print(f"{PROG}: warning: {message}", file=sys.stderr)


def _debug_logger(enabled: bool) -> Callable[[str], None]:
    def debug(message: str) -> None:
        if enabled:
            print(f"{PROG}: debug: {message}", file=sys.stderr)

    return debug


def write_output(text: str) -> None:
    """Write output for programs to stdout, keeping undecodable bytes as they are (FR-5)."""
    if not text:
        return
    sys.stdout.flush()
    try:
        sys.stdout.buffer.write(text.encode("utf-8", "surrogateescape"))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        # The reader is gone (``diff | head``): stop quietly, like other command-line tools.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)


def _diff(command: DiffOptions) -> int:
    debug = _debug_logger(command.debug)
    state_dir = state_dir_location(command.state_dir, os.environ)
    path = normalize_path(command.path, os.getcwd())
    pending = find_pending(state_dir, path)
    debug(f"state directory: {state_dir}")
    debug(f"no state for {path}" if pending is None else f"jail: {pending.jail.root}")
    write_output(diff_text(pending, command.stat))
    return ExitCode.OK


def _listdiffs(command: ListDiffsOptions) -> int:
    state_dir = state_dir_location(command.state_dir, os.environ)
    _debug_logger(command.debug)(f"state directory: {state_dir}")
    write_output(listdiffs_text(all_pending(state_dir, warn)))
    return ExitCode.OK


def ask(question: str) -> bool:
    """Ask on stderr, read the answer from stdin; only ``y`` or ``yes`` confirm (FR-40)."""
    print(question, end="", file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline()
    except (OSError, ValueError):
        answer = ""
    if not answer.endswith("\n"):
        print(file=sys.stderr)
    return answer.strip().lower() in ("y", "yes")


_IO = merging.Io(out=write_output, ask=ask, warn=warn)


def _dispatch(command: Command) -> int:
    match command:
        case RunOptions():
            return session.run(command, warn=warn, debug=_debug_logger(command.debug))
        case DiffOptions():
            return _diff(command)
        case ListDiffsOptions():
            return _listdiffs(command)
        case MergeOptions():
            return merging.merge(command, _IO, host_env=os.environ, cwd=os.getcwd())
        case DiscardOptions():
            return merging.discard(command, _IO, host_env=os.environ, cwd=os.getcwd())


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    try:
        command = parse_command(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return _exit_code(exc)
    if os.geteuid() == 0:
        warn("running as root is not supported; agent-sandbox is meant for normal users")
    try:
        return _dispatch(command)
    except AgentSandboxError as exc:
        if command.debug:
            traceback.print_exc()
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 128 + signal.SIGINT
    except Terminated as exc:
        return 128 + exc.signum
    # FR-92: unexpected errors are reported in one line; the traceback only with --debug.
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if command.debug:
            traceback.print_exc()
        hint = "" if command.debug else " (run with --debug for details)"
        print(f"{PROG}: error: unexpected {type(exc).__name__}: {exc}{hint}", file=sys.stderr)
        return ExitCode.SETUP_FAILED if isinstance(command, RunOptions) else ExitCode.ERROR
