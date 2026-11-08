"""Unprivileged operation and footprint (AC-16b, FR-4, FR-33, FR-70a, FR-73, FR-95, FR-98)."""

import os
import signal
import sys
from pathlib import Path

import pytest

from agent_sandbox.state import jail_dir
from tests.integration.conftest import Cli

pytestmark = pytest.mark.integration

ESCALATION_TOOLS = {"sudo", "su", "doas", "pkexec", "run0"}


@pytest.fixture(name="proj")
def fixture_proj(tmp_path: Path) -> Path:
    """A small project."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("a\n", encoding="utf-8")
    return proj


# Runs agent-sandbox in a child process and prints every program it starts, using
# Python's audit hooks. strace -f cannot be used: tracing disables the setuid bit of
# fusermount3, so mounting fails under strace.
AUDIT_DRIVER = """
import os, sys
EVENTS = {"subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn", "os.system"}
def hook(event, args):
    if event in EVENTS:
        print("EXEC", os.fsdecode(args[0]), file=sys.stderr, flush=True)
sys.addaudithook(hook)
from agent_sandbox.cli import main
sys.exit(main(sys.argv[1:]))
"""


@pytest.mark.usefixtures("overlay_available")
def test_ac16b_fr95_no_privilege_escalation(cli: Cli, proj: Path) -> None:
    """AC-16b, FR-95: a full run, diff, merge cycle never starts sudo, su, doas or pkexec."""
    executed: list[str] = []
    steps = [
        ["run", "-j", str(proj), "--", "sh", "-c", f"echo x > {proj}/new"],
        ["listdiffs"],
        ["diff", str(proj)],
        ["merge", "-y", str(proj)],
    ]
    for step in steps:
        result = cli.run_raw([sys.executable, "-c", AUDIT_DRIVER, *step])
        assert result.returncode == 0, result.stderr
        executed += [line[5:] for line in result.stderr.splitlines() if line.startswith("EXEC ")]
    names = {os.path.basename(path) for path in executed}
    assert {"bwrap", "fuse-overlayfs"} <= names
    assert names & {"fusermount3", "fusermount"}
    assert not names & ESCALATION_TOOLS
    assert (proj / "new").read_text(encoding="utf-8") == "x\n"


@pytest.mark.usefixtures("overlay_available")
def test_fr4_fr98_only_the_state_directory_is_written(
    cli: Cli, proj: Path, tmp_path: Path
) -> None:
    """FR-4, FR-98: with the default state directory, nothing else appears in $HOME."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "AGENT_SANDBOX_STATE_DIR": "", "XDG_STATE_HOME": ""}
    script = f"echo x > {proj}/new; echo y > $HOME/in-sandbox-home"
    assert cli("run", "-j", str(proj), "--", "sh", "-c", script, env=env).returncode == 0
    assert cli("diff", str(proj), env=env).returncode == 0
    assert cli("merge", "-y", str(proj), env=env).returncode == 0
    created = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
    assert created == [
        ".local",
        ".local/state",
        ".local/state/agent-sandbox",
        ".local/state/agent-sandbox/jails",
    ]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["home", "proj"]


def test_fr33_parent_directories_of_mount_points(cli: Cli, tmp_path: Path) -> None:
    """FR-33: parents of a mount point exist in the sandbox and show nothing else."""
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (tmp_path / "a" / "secret").write_text("s\n", encoding="utf-8")
    result = cli(
        "run", "-r", str(deep), "--", "sh", "-c", f"ls -A {tmp_path}/a; ls -A {tmp_path}/a/b"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "b\nc\n"


@pytest.mark.usefixtures("overlay_available")
def test_fr73_fr97_overlay_is_a_user_fuse_mount(cli: Cli, proj: Path) -> None:
    """FR-73, FR-97: the overlay is a FUSE mount of the invoking user."""
    merged = jail_dir(str(cli.state_dir), str(proj)).merged
    with cli.start("run", "-j", str(proj), "--", "sh", "-c", "echo ready; sleep 30") as session:
        assert session.stdout is not None
        assert session.stdout.readline() == "ready\n"
        lines = [
            line
            for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
            if line.split(" ")[4] == merged
        ]
        session.send_signal(signal.SIGTERM)
        session.wait(timeout=10)
    assert len(lines) == 1
    fstype, options = lines[0].split(" - ")[1].split(" ")[0], lines[0].split(" ")[-1]
    assert fstype == "fuse.fuse-overlayfs"
    assert f"user_id={os.getuid()}" in options.split(",")


@pytest.mark.usefixtures("overlay_available")
@pytest.mark.skipif(os.geteuid() == 0, reason="root is not limited by permissions")
def test_fr70a_read_only_lower_directories_cannot_be_changed(cli: Cli, proj: Path) -> None:
    """FR-70a: documents the fuse-overlayfs limitation for read-only real directories."""
    (proj / "ro").mkdir()
    (proj / "ro" / "f").write_text("f\n", encoding="utf-8")
    (proj / "ro").chmod(0o555)
    try:
        script = f"echo x > {proj}/ro/new 2>&1 || echo refused; cat {proj}/ro/f"
        result = cli("run", "-j", str(proj), "--", "sh", "-c", script)
    finally:
        (proj / "ro").chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == ["refused", "f"]
