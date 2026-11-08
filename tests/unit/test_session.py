"""Tests for preparing ``run`` without starting bwrap (FR-9, FR-29, FR-60, --dry-run)."""

import os
import shutil
import signal
from pathlib import Path

import pytest

from agent_sandbox import overlay, session
from agent_sandbox.cli import main, parse_command
from agent_sandbox.errors import ProgramNotFoundError, SetupError, Terminated
from agent_sandbox.mounts import MountSpec
from agent_sandbox.options import RunOptions
from agent_sandbox.overlay import OverlayTools
from agent_sandbox.session import logical_cwd
from agent_sandbox.state import JailDir, jail_dir

TOOLS = OverlayTools("/bin/fuse-overlayfs", "/bin/fusermount3")


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


# --- jails with faked overlays (FR-60, FR-61, FR-63) ---


class FakeSession:
    """Fakes bwrap, the tools and the overlays; records what happens in which order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fail_on: set[str] | None = None) -> None:
        self.events: list[str] = []
        self.fail_on = fail_on or set()
        self.exit_code = 0
        self.raise_in_supervise: BaseException | None = None
        self.command: list[str] = []
        monkeypatch.setattr(session, "find_bwrap", lambda path: "/usr/bin/bwrap")
        monkeypatch.setattr(session, "check_user_namespaces", lambda bwrap: None)
        monkeypatch.setattr(session, "find_overlay_tools", lambda path: TOOLS)
        monkeypatch.setattr(session, "supervise", self.supervise)
        monkeypatch.setattr(overlay, "mount_overlay", self.mount)
        monkeypatch.setattr(overlay, "unmount_overlay", self.unmount)
        monkeypatch.setattr(overlay, "is_mounted", lambda path: False)

    def mount(self, spec: MountSpec, jail: JailDir, tools: OverlayTools) -> None:
        """Fake mount: create the merged directory from the upper layer only."""
        del tools
        if spec.path in self.fail_on:
            raise SetupError(f"cannot mount the overlay for {spec.describe()}: fake")
        self.events.append(f"mount {os.path.basename(spec.path)}")
        shutil.copytree(jail.upper if os.path.isdir(jail.upper) else spec.path, jail.merged)

    def unmount(self, merged: str, tools: OverlayTools) -> None:
        """Fake unmount."""
        del tools
        self.events.append("unmount")
        shutil.rmtree(merged, ignore_errors=True)

    def supervise(self, command: list[str], env: object) -> int:
        """Fake supervise."""
        del env
        self.command = command
        self.events.append("bwrap")
        if self.raise_in_supervise is not None:
            raise self.raise_in_supervise
        return self.exit_code


def run_options(*argv: str) -> RunOptions:
    """Parse ``run ARGV``."""
    command = parse_command(["run", *argv])
    assert isinstance(command, RunOptions)
    return command


def run_session(*argv: str) -> int:
    """Run ``session.run`` for ``run ARGV`` in the current directory."""
    return session.run(run_options(*argv), warn=no_log, debug=no_log)


def no_log(message: str) -> None:
    """Discard messages."""
    del message


def test_fr60_jails_are_mounted_before_and_unmounted_after_bwrap(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-60, FR-36: mount, run bwrap with the merged view bound, unmount."""
    fake = FakeSession(monkeypatch)
    fake.exit_code = 3
    (project / "a").mkdir()
    (project / "b").mkdir()
    assert run_session("-j", "a", "-j", "b", "--", "true") == 3
    assert fake.events == ["mount a", "mount b", "bwrap", "unmount", "unmount"]
    state = str(project.parent / "state")
    merged = jail_dir(state, str(project / "a")).merged
    assert ["--bind", merged, str(project / "a")] == fake.command[
        fake.command.index(merged) - 1 : fake.command.index(merged) + 2
    ]


def test_fr63_failed_mount_unmounts_the_others(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-63: if one overlay fails, the run aborts and mounted overlays are unmounted."""
    (project / "a").mkdir()
    (project / "b").mkdir()
    fake = FakeSession(monkeypatch, fail_on={str(project / "b")})
    with pytest.raises(SetupError, match="cannot mount the overlay for -j"):
        run_session("-j", "a", "-j", "b", "--", "true")
    assert fake.events == ["mount a", "unmount"]


@pytest.mark.parametrize("exception", [Terminated(15), KeyboardInterrupt()])
def test_fr61_signals_still_unmount(
    project: Path, monkeypatch: pytest.MonkeyPatch, exception: BaseException
) -> None:
    """FR-61: termination by signal still unmounts the overlays."""
    (project / "a").mkdir()
    fake = FakeSession(monkeypatch)
    fake.raise_in_supervise = exception
    with pytest.raises(type(exception)):
        run_session("-j", "a", "--", "true")
    assert fake.events == ["mount a", "bwrap", "unmount"]


def test_fr61_sigterm_handler_raises_terminated() -> None:
    """FR-61: ``SIGTERM`` and ``SIGHUP`` raise ``Terminated`` only while the guard is active."""
    before = signal.getsignal(signal.SIGTERM)
    with session.terminate_on_signals():
        with pytest.raises(Terminated) as info:
            signal.raise_signal(signal.SIGHUP)
        assert info.value.signum == signal.SIGHUP
    assert signal.getsignal(signal.SIGTERM) == before


def test_fr9_program_from_pending_changes_is_found(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-9: a program created in an earlier session exists in the merged view."""
    fake = FakeSession(monkeypatch)
    state = str(project.parent / "state")
    upper = jail_dir(state, str(project)).upper
    os.makedirs(upper)
    tool = Path(upper, "built-tool")
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    assert run_session("-j", ".", "--", "./built-tool") == 0
    assert fake.events == ["mount project", "bwrap", "unmount"]


def test_fr9_program_missing_in_jail_unmounts(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-9, FR-61: exit 127 inside a jail still unmounts."""
    del project
    fake = FakeSession(monkeypatch)
    with pytest.raises(ProgramNotFoundError):
        run_session("-j", ".", "--", "./missing")
    assert fake.events == ["mount project", "unmount"]


def test_dry_run_with_jail_creates_nothing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Section 5.2: ``--dry-run`` shows the overlay setup but creates no state."""
    assert main(["run", "--dry-run", "-j", ".", "--", "sh"]) == 0
    out = capsys.readouterr().out
    state = str(project.parent / "state")
    jail = jail_dir(state, str(project))
    assert out.startswith(f"# overlay for -j {project}: lower -> {project}\n(cd {jail.root} && ")
    assert f"--bind {jail.merged} {project}" in out
    assert not os.path.exists(state)
