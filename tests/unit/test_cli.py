"""Tests for the top-level command-line interface and the non-``run`` subcommands."""

import subprocess
import sys

import pytest

from agent_sandbox import __version__
from agent_sandbox.cli import main, parse_command
from agent_sandbox.options import (
    DiffOptions,
    DiscardOptions,
    ListDiffsOptions,
    MergeOptions,
)

SUBCOMMANDS = ["run", "diff", "listdiffs", "merge", "discard"]

COMMON_OPTIONS = ["-h", "--help", "--state-dir", "--debug"]

SUBCOMMAND_OPTIONS = {
    "run": [
        "-r",
        "--ro",
        "-w",
        "--rw",
        "-j",
        "--jail",
        "--env",
        "--env-file",
        "--no-network",
        "--chdir",
        "--layout",
        "--dry-run",
    ],
    "diff": ["--stat"],
    "listdiffs": [],
    "merge": ["-y", "--yes", "--dry-run"],
    "discard": ["-y", "--yes"],
}


def test_fr2_version_prints_version_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-2: ``--version`` prints the version to stdout and exits with 0."""
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"agent-sandbox {__version__}"
    assert captured.err == ""


def test_fr1_help_goes_to_stdout_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-1: ``--help`` prints help to stdout and exits with 0."""
    assert main(["--help"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("usage: agent-sandbox")
    assert captured.err == ""


def test_fr1_top_level_help_lists_subcommands_and_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-1: the top-level help lists all subcommands, an example and the exit codes."""
    main(["--help"])
    out = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert f"\n    {name} " in out
    for option in ["--version", "--debug", "--help"]:
        assert option in out
    assert "agent-sandbox run -j" in out
    assert "exit codes:" in out
    for code in ["125", "127", "128+S"]:
        assert code in out


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_fr1_subcommand_help_is_comprehensive(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """FR-1: every subcommand's help shows all options, examples and exit codes."""
    assert main([name, "--help"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert out.startswith(f"usage: agent-sandbox {name}")
    for option in COMMON_OPTIONS + SUBCOMMAND_OPTIONS[name]:
        assert option in out, option
    assert f"examples:\n  agent-sandbox {name}" in out
    assert "exit codes:" in out


def test_fr1_run_help_documents_rules_and_limitations(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-1, FR-37, FR-52: run help shows defaults, parsing rules, env baseline, limitations."""
    main(["run", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "Default: auto" in text
    assert "Default: the network is shared" in text
    assert "Use -- to end the options explicitly" in text
    assert "PATH, HOME, USER, LOGNAME, SHELL, TERM, LANG, LC_ALL, TZ" in text
    assert "Device or resource busy" in text
    assert "they are lost when the sandbox exits" in text


def test_fr3_missing_subcommand_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-3: no subcommand prints a usage hint to stderr and exits with 2."""
    assert main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: agent-sandbox" in captured.err
    assert "agent-sandbox: error: a subcommand is required" in captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["--no-such-flag"],
        ["bogus"],
        ["--debug"],
        ["--deb", "listdiffs"],
        ["listdiffs", "extra"],
        ["listdiffs", "--bogus"],
        ["diff"],
        ["diff", "/p", "--bogus"],
        ["diff", "/p", "/q"],
        ["merge"],
        ["merge", "--ye", "/p"],
        ["discard"],
        ["discard", "--dry-run", "/p"],
    ],
)
def test_fr3_usage_errors_exit_2(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    """FR-3, FR-10: unknown subcommands/flags and missing arguments exit with 2."""
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: agent-sandbox")
    assert "\nagent-sandbox: error: " in captured.err


def test_diff_options() -> None:
    """Section 5.3: ``diff`` takes PATH, ``--stat`` and the common options."""
    assert parse_command(["diff", "/p"]) == DiffOptions(
        path="/p", stat=False, state_dir=None, debug=False
    )
    assert parse_command(["diff", "/p", "--stat", "--state-dir", "/s", "--debug"]) == DiffOptions(
        path="/p", stat=True, state_dir="/s", debug=True
    )


def test_listdiffs_options() -> None:
    """Section 5.4: ``listdiffs`` takes only the common options."""
    assert parse_command(["listdiffs"]) == ListDiffsOptions(state_dir=None, debug=False)
    assert parse_command(["listdiffs", "--state-dir=/s"]) == ListDiffsOptions(
        state_dir="/s", debug=False
    )


def test_merge_options() -> None:
    """Section 5.5: ``merge`` takes PATH, ``-y``, ``--dry-run`` and the common options."""
    assert parse_command(["merge", "/p"]) == MergeOptions(
        path="/p", yes=False, dry_run=False, state_dir=None, debug=False
    )
    assert parse_command(["merge", "-y", "--dry-run", "/p"]) == MergeOptions(
        path="/p", yes=True, dry_run=True, state_dir=None, debug=False
    )


def test_discard_options() -> None:
    """Section 5.5: ``discard`` takes PATH, ``-y`` and the common options."""
    assert parse_command(["discard", "/p"]) == DiscardOptions(
        path="/p", yes=False, state_dir=None, debug=False
    )
    assert parse_command(["discard", "--yes", "/p"]) == DiscardOptions(
        path="/p", yes=True, state_dir=None, debug=False
    )


def test_path_starting_with_dash_after_double_dash() -> None:
    """FR-10: a PATH that starts with ``-`` can be given after ``--``."""
    assert parse_command(["diff", "--", "-p"]) == DiffOptions(
        path="-p", stat=False, state_dir=None, debug=False
    )


@pytest.mark.parametrize(
    "argv", [["diff", "/p"], ["listdiffs"], ["merge", "/p"], ["discard", "/p"]]
)
def test_fr93_debug_before_subcommand(argv: list[str]) -> None:
    """FR-93: ``--debug`` before the subcommand applies to it."""
    command = parse_command(["--debug", *argv])
    assert command.debug is True


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        (["run", "prog"], "run"),
        (["diff", "/p"], "diff"),
        (["listdiffs"], "listdiffs"),
        (["merge", "/p"], "merge"),
        (["discard", "/p"], "discard"),
    ],
)
def test_subcommands_are_not_implemented_yet(
    capsys: pytest.CaptureFixture[str], argv: list[str], name: str
) -> None:
    """Step 2 stubs: every subcommand reports that it is not implemented and exits with 1."""
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"agent-sandbox: error: '{name}' is not implemented yet\n"


def test_nfr3_module_entry_point_runs() -> None:
    """NFR-3: the package can be run as ``python -m agent_sandbox``."""
    result = subprocess.run(
        [sys.executable, "-m", "agent_sandbox", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"agent-sandbox {__version__}"
