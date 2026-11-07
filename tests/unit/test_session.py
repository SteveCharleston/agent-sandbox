"""Tests for preparing ``run`` without starting bwrap (FR-9, FR-29, FR-60, --dry-run)."""

import os
from pathlib import Path

import pytest

from agent_sandbox.cli import main
from agent_sandbox.session import logical_cwd


@pytest.fixture(name="project")
def fixture_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory as the current directory, and a private state directory."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("AGENT_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    return project


def test_dry_run_prints_masked_command(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 5.2, FR-58: ``--dry-run`` prints the command with masked values and exits 0."""
    monkeypatch.setenv("SECRET_TOKEN", "s3cr3t")
    assert main(["run", "--dry-run", "-w", ".", "--", "sh", "-c", "echo hi"]) == 0
    captured = capsys.readouterr()
    assert "s3cr3t" not in captured.out
    assert "--setenv SECRET_TOKEN '***'" in captured.out
    assert f"--bind {project} {project}" in captured.out
    assert f"--chdir {project}" in captured.out
    assert captured.out.rstrip().endswith("-- sh -c 'echo hi'")


def test_debug_prints_steps_without_values(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-93, FR-58: ``--debug`` shows layout, state directory and program, never values."""
    monkeypatch.setenv("SECRET_TOKEN", "s3cr3t")
    assert main(["--debug", "run", "--dry-run", "-r", str(project), "sh"]) == 0
    err = capsys.readouterr().err
    assert "agent-sandbox: debug: layout: " in err
    assert f"agent-sandbox: debug: state directory: {project.parent / 'state'}" in err
    assert "agent-sandbox: debug: program: /" in err
    assert "s3cr3t" not in err


def test_fr9_program_not_found_exits_127(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """FR-9, AC-15: a program that does not exist in the sandbox exits with 127."""
    del project
    assert main(["run", "--dry-run", "--", "nonexistent_prog_4711"]) == 127
    assert capsys.readouterr().err.endswith(
        "agent-sandbox: error: nonexistent_prog_4711: command not found in the sandbox\n"
    )


def test_fr9_program_outside_sandbox_exits_127(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """FR-9: a program that exists on the host but is not mounted is not found."""
    script = project / "script.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    assert main(["run", "--dry-run", "--", str(script)]) == 127
    assert main(["run", "--dry-run", "-r", str(project), "--", str(script)]) == 0
    capsys.readouterr()


def test_fr32_invalid_mount_exits_125(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """FR-32, AC-15: ``-w /nonexistent`` exits with 125."""
    del project
    assert main(["run", "-w", "/nonexistent-4711", "--", "true"]) == 125
    assert capsys.readouterr().err == (
        "agent-sandbox: error: -w /nonexistent-4711: No such file or directory\n"
    )


def test_fr29_warns_about_invisible_path_entries(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-29: existing PATH directories that are not visible are reported once."""
    tools = project / "tools"
    tools.mkdir()
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}:{tools}")
    assert main(["run", "--dry-run", "--", "sh"]) == 0
    err = capsys.readouterr().err
    assert (
        f"agent-sandbox: warning: PATH entries not visible in the sandbox (add them with -r): "
        f"{tools}" in err
    )
    assert err.count(str(tools)) == 1


def test_fr39_state_dir_mount_is_rejected(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """FR-39: the state directory cannot be mounted."""
    state = project.parent / "state"
    state.mkdir()
    assert main(["run", "--dry-run", "-r", str(state), "--", "sh"]) == 125
    assert "inside the agent-sandbox state directory" in capsys.readouterr().err


def test_logical_cwd(tmp_path: Path) -> None:
    """Section 5.2: ``$PWD`` is used if it names the current directory."""
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    real = str((tmp_path / "real").resolve())
    assert logical_cwd(real, {"PWD": str(tmp_path / "link")}) == str(tmp_path / "link")
    assert logical_cwd(real, {"PWD": str(tmp_path)}) is None
    assert logical_cwd(real, {"PWD": "relative"}) is None
    assert logical_cwd(real, {}) is None


def test_fr92_unexpected_errors_are_one_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-92: unexpected errors are one line without traceback, unless ``--debug``."""

    def explode(*_: object, **__: object) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr("agent_sandbox.session.run", explode)
    assert main(["run", "true"]) == 125
    assert capsys.readouterr().err == (
        "agent-sandbox: error: unexpected RuntimeError: boom (run with --debug for details)\n"
    )
    assert main(["run", "--debug", "true"]) == 125
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert err.endswith("agent-sandbox: error: unexpected RuntimeError: boom\n")
