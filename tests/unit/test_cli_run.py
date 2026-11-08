"""Tests for parsing the ``run`` subcommand (FR-6 to FR-10, section 5.2)."""

import pytest

from agent_sandbox.cli import main, parse_command, split_run_args
from agent_sandbox.options import (
    EnvArg,
    EnvArgKind,
    LayoutChoice,
    MountArg,
    MountMode,
    RunOptions,
)

RO = MountMode.READ_ONLY
RW = MountMode.WRITABLE
JAIL = MountMode.JAILED


def parse_run(*argv: str) -> RunOptions:
    """Parse ``agent-sandbox run ARGV`` and return the run options."""
    command = parse_command(["run", *argv])
    assert isinstance(command, RunOptions)
    return command


def usage_error(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    """Run ``agent-sandbox run ARGV``, assert a usage error and return stderr."""
    assert main(["run", *argv]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: agent-sandbox run")
    return captured.err


def test_ac1_fr6_program_arguments_after_first_non_option() -> None:
    """AC-1, FR-6: options end at the program; ``-l`` belongs to bash."""
    options = parse_run(
        "-r",
        "/home/u/important_file",
        "-j",
        "/home/u/git/myproject",
        "-w",
        "/home/u/Documents/scratchspace",
        "/bin/bash",
        "-l",
    )
    assert options.program == "/bin/bash"
    assert options.args == ("-l",)
    assert options.mounts == (
        MountArg(RO, "/home/u/important_file"),
        MountArg(JAIL, "/home/u/git/myproject"),
        MountArg(RW, "/home/u/Documents/scratchspace"),
    )


def test_ac2_fr7_double_dash_ends_options() -> None:
    """AC-2, FR-7: everything after ``--`` is passed verbatim, even if it looks like flags."""
    options = parse_run("-j", "/p", "--", "/bin/some_program", "-j", "-w", "-v", "-r")
    assert options.program == "/bin/some_program"
    assert options.args == ("-j", "-w", "-v", "-r")
    assert options.mounts == (MountArg(JAIL, "/p"),)


def test_fr6_mount_like_flags_after_program_belong_to_program() -> None:
    """FR-6: flags after the program are not parsed as mount flags, even without ``--``."""
    options = parse_run("-j", "/p", "prog", "-j", "/x", "-w", "/y", "--dry-run", "--help")
    assert options.program == "prog"
    assert options.args == ("-j", "/x", "-w", "/y", "--dry-run", "--help")
    assert options.mounts == (MountArg(JAIL, "/p"),)
    assert options.dry_run is False


def test_fr7_double_dash_after_program_is_passed_on() -> None:
    """FR-7: a ``--`` after the program is an argument of the program."""
    options = parse_run("prog", "--", "x")
    assert options.program == "prog"
    assert options.args == ("--", "x")


def test_fr7_program_starting_with_dash_after_double_dash() -> None:
    """FR-6, FR-7: a program whose name starts with ``-`` can follow ``--``."""
    options = parse_run("--", "-weird", "-x")
    assert options.program == "-weird"
    assert options.args == ("-x",)


def test_fr6_program_without_options() -> None:
    """FR-6: the first argument is the program if there are no options."""
    options = parse_run("ls", "-la", "/")
    assert options.program == "ls"
    assert options.args == ("-la", "/")
    assert not options.mounts


def test_fr6_repeated_mount_flags_keep_command_line_order() -> None:
    """FR-6: mount flags are repeatable and keep their order across modes."""
    options = parse_run("-r", "/a", "-w", "/b", "-r", "/c", "-j", "/d", "-j", "/e", "prog")
    assert options.mounts == (
        MountArg(RO, "/a"),
        MountArg(RW, "/b"),
        MountArg(RO, "/c"),
        MountArg(JAIL, "/d"),
        MountArg(JAIL, "/e"),
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["-r", "/p"], MountArg(RO, "/p")),
        (["-r/p"], MountArg(RO, "/p")),
        (["-r=/p"], MountArg(RO, "/p")),
        (["--ro", "/p"], MountArg(RO, "/p")),
        (["--ro=/p"], MountArg(RO, "/p")),
        (["-w", "/p"], MountArg(RW, "/p")),
        (["-w/p"], MountArg(RW, "/p")),
        (["--rw", "/p"], MountArg(RW, "/p")),
        (["--rw=/p"], MountArg(RW, "/p")),
        (["-j", "/p"], MountArg(JAIL, "/p")),
        (["-j/p"], MountArg(JAIL, "/p")),
        (["--jail", "/p"], MountArg(JAIL, "/p")),
        (["--jail=/p"], MountArg(JAIL, "/p")),
    ],
)
def test_fr10_short_and_long_forms(argv: list[str], expected: MountArg) -> None:
    """FR-10: ``-r PATH``, ``-rPATH``, ``-r=PATH``, ``--ro PATH`` and ``--ro=PATH``."""
    assert parse_run(*argv, "prog").mounts == (expected,)


@pytest.mark.parametrize(
    ("argv", "path"),
    [
        (["-r", "-x"], "-x"),
        (["--ro", "-x"], "-x"),
        (["--ro=-x"], "-x"),
        (["-r-x"], "-x"),
        (["-r", "--"], "--"),
        (["-r", "--ro"], "--ro"),
        (["--ro="], ""),
        (["-r="], ""),
        (["-r", "my dir"], "my dir"),
        (["--ro=a=b"], "a=b"),
    ],
)
def test_fr10_value_is_taken_verbatim(argv: list[str], path: str) -> None:
    """FR-10: the argument after a value option is its value, even if it looks like a flag."""
    options = parse_run(*argv, "prog")
    assert options.mounts == (MountArg(RO, path),)
    assert options.program == "prog"


def test_fr8_missing_program_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-8: ``run`` without a program fails with exit code 2 and a clear message."""
    assert "agent-sandbox: error: no PROGRAM given" in usage_error(capsys)


@pytest.mark.parametrize("argv", [["-r", "/p"], ["--"], ["-j", "/p", "--"], ["--dry-run"]])
def test_fr8_options_without_program_is_usage_error(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """FR-8: options alone, or a trailing ``--``, are not enough."""
    assert "no PROGRAM given" in usage_error(capsys, *argv)


@pytest.mark.parametrize("flag", ["-r", "--ro", "-w", "--env", "--env-file", "--chdir"])
def test_fr3_missing_option_value_is_usage_error(
    capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    """FR-3: an option at the end without its value is a usage error."""
    assert "expected one argument" in usage_error(capsys, flag)


@pytest.mark.parametrize(
    "argv",
    [
        ["--bogus", "prog"],
        ["--bogus=1", "prog"],
        ["-x", "prog"],
        ["-r", "/p", "--bogus", "prog"],
        ["--jai", "/p", "prog"],
        ["--dry", "prog"],
        ["--dry-run=yes", "prog"],
    ],
)
def test_fr6_unknown_option_before_program_is_usage_error(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """FR-3, FR-6, FR-10: unknown or abbreviated options are errors, never the program."""
    assert "agent-sandbox: error:" in usage_error(capsys, *argv)


def test_fr55_env_entries_keep_command_line_order() -> None:
    """FR-55: ``--env`` and ``--env-file`` entries keep their relative order."""
    options = parse_run(
        "--env", "A", "--env-file", "one.env", "--env=B=1", "--env-file=two.env", "prog"
    )
    assert options.env == (
        EnvArg(EnvArgKind.ENTRY, "A"),
        EnvArg(EnvArgKind.FILE, "one.env"),
        EnvArg(EnvArgKind.ENTRY, "B=1"),
        EnvArg(EnvArgKind.FILE, "two.env"),
    )


@pytest.mark.parametrize("argv", [["--env", "A=b=c"], ["--env=A=b=c"]])
def test_fr53_env_value_with_equals_signs_is_kept(argv: list[str]) -> None:
    """FR-53: only the option's own ``=`` is consumed; the entry is kept unchanged."""
    assert parse_run(*argv, "prog").env == (EnvArg(EnvArgKind.ENTRY, "A=b=c"),)


def test_defaults() -> None:
    """Section 5.2, FR-115: nothing given is None, so profiles can fill it in."""
    options = parse_run("prog")
    assert options == RunOptions(
        program="prog",
        args=(),
        mounts=(),
        env=(),
        network=None,
        chdir=None,
        layout=None,
        state_dir=None,
        dry_run=False,
        debug=False,
        profiles=(),
        config=None,
    )


def test_all_other_options() -> None:
    """Section 5.2: ``--no-network``, ``--chdir``, ``--layout``, ``--state-dir``, ``--dry-run``."""
    options = parse_run(
        "--no-network",
        "--chdir",
        "/work",
        "--layout",
        "nixos",
        "--state-dir=/state",
        "--dry-run",
        "--debug",
        "prog",
    )
    assert options.network is False
    assert options.chdir == "/work"
    assert options.layout is LayoutChoice.NIXOS
    assert options.state_dir == "/state"
    assert options.dry_run is True
    assert options.debug is True


@pytest.mark.parametrize("layout", ["auto", "fhs", "nixos"])
def test_layout_choices(layout: str) -> None:
    """Section 5.2: ``--layout`` accepts auto, fhs and nixos."""
    assert parse_run("--layout", layout, "prog").layout is LayoutChoice(layout)


def test_invalid_layout_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-3: an invalid ``--layout`` is a usage error."""
    assert "invalid choice: 'guix'" in usage_error(capsys, "--layout", "guix", "prog")


def test_fr93_debug_before_subcommand() -> None:
    """FR-93: ``--debug`` is also accepted before the subcommand."""
    command = parse_command(["--debug", "run", "prog"])
    assert isinstance(command, RunOptions)
    assert command.debug is True


def test_fr1_help_option_before_program(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-1: ``run --help`` prints the run help, even with other options."""
    assert main(["run", "-r", "/p", "--help"]) == 0
    assert capsys.readouterr().out.startswith("usage: agent-sandbox run")


VALUE_OPTIONS = {"-r": "--ro", "--ro": "--ro", "--env": "--env"}


@pytest.mark.parametrize(
    ("args", "options", "program"),
    [
        ([], [], []),
        (["prog"], [], ["prog"]),
        (["-r", "/a", "prog", "-r"], ["--ro=/a"], ["prog", "-r"]),
        (["-r/a", "--ro=/b", "--ro", "/c"], ["--ro=/a", "--ro=/b", "--ro=/c"], []),
        (["--flag", "-h", "prog"], ["--flag", "-h"], ["prog"]),
        (["--", "-r"], [], ["-r"]),
        (["-"], [], ["-"]),
        (["-r"], ["-r"], []),
        (["--env", "A=1", "x"], ["--env=A=1"], ["x"]),
    ],
)
def test_fr6_fr7_fr10_split_run_args(
    args: list[str], options: list[str], program: list[str]
) -> None:
    """FR-6, FR-7, FR-10: the pre-split normalizes value options and keeps the rest."""
    assert split_run_args(args, VALUE_OPTIONS) == (options, program)


def test_fr114_profiles_and_config_are_parsed() -> None:
    """FR-114, FR-110: -p/--profile is repeatable; --config names the file."""
    options = parse_run("-p", "base", "--profile=tool", "--config", "/c.toml", "prog")
    assert options.profiles == ("base", "tool")
    assert options.config == "/c.toml"


def test_fr8_program_is_optional_with_a_profile() -> None:
    """FR-8: with --profile, the program may come from the profile."""
    options = parse_run("-p", "claude")
    assert options.program is None
    assert not options.args


@pytest.mark.parametrize(
    ("argv", "network"),
    [
        ([], None),
        (["--no-network"], False),
        (["--network"], True),
        (["--network", "--no-network"], False),
    ],
)
def test_fr115_network_flags(argv: list[str], network: bool | None) -> None:
    """FR-115: --network and --no-network set the value; the last one wins."""
    assert parse_run(*argv, "prog").network is network
