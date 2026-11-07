"""Tests for the top-level command-line interface."""

import subprocess
import sys

import pytest

from agent_sandbox import __version__
from agent_sandbox.cli import main


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


def test_fr3_missing_subcommand_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-3: no subcommand prints a usage hint to stderr and exits with 2."""
    assert main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: agent-sandbox" in captured.err
    assert "agent-sandbox: error: a subcommand is required" in captured.err


def test_fr3_unknown_flag_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-3: an unknown flag prints a usage hint to stderr and exits with 2."""
    assert main(["--no-such-flag"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agent-sandbox: error:" in captured.err


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
