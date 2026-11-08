"""Integration tests for jailed writes (section 7, AC-8 to AC-10, AC-14, AC-16)."""

import os
import shutil
import signal
import stat
import subprocess
from pathlib import Path

import pytest

from agent_sandbox.overlay import is_mounted
from agent_sandbox.state import jail_dir
from tests.integration.conftest import Cli

pytestmark = pytest.mark.integration


@pytest.fixture(name="overlay_available", scope="session")
def fixture_overlay_available(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Skip if fuse-overlayfs cannot be mounted here as an unprivileged user."""
    tools = [shutil.which(name) for name in ("fuse-overlayfs", "fusermount3")]
    if None in tools:
        pytest.skip("fuse-overlayfs or fusermount3 is not installed")
    root = tmp_path_factory.mktemp("overlay-check")
    for name in ("lower", "upper", "work", "merged"):
        (root / name).mkdir()
    result = subprocess.run(
        [str(tools[0]), "-o", "lowerdir=lower,upperdir=upper,workdir=work", "merged"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"fuse-overlayfs cannot mount: {result.stderr.strip()}")
    subprocess.run([str(tools[1]), "-u", str(root / "merged")], check=True)


@pytest.fixture(autouse=True)
def require_overlay(overlay_available: None) -> None:
    """Apply the overlay check to every test in this module."""
    del overlay_available


@pytest.fixture(name="proj")
def fixture_proj(tmp_path: Path) -> Path:
    """A project with files, a subdirectory and a sibling file outside it."""
    proj = tmp_path / "proj"
    (proj / "dir" / "sub").mkdir(parents=True)
    (proj / "file.txt").write_text("original\n", encoding="utf-8")
    (proj / "old.txt").write_text("old\n", encoding="utf-8")
    (proj / "notes.txt").write_text("notes\n", encoding="utf-8")
    (proj / "sibling.txt").write_text("sibling\n", encoding="utf-8")
    (proj / "dir" / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")
    return proj


def snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Mode (with type) and content (or symlink target) of every entry below ``root``."""
    result = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory, name)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                content = os.readlink(path)
            elif stat.S_ISREG(mode):
                content = path.read_text(encoding="utf-8")
            else:
                content = ""
            result[str(path.relative_to(root))] = (mode, content)
    return result


def merged_of(cli: Cli, path: Path) -> str:
    """The mount point of the jail of ``path``."""
    return jail_dir(str(cli.state_dir), str(path)).merged


def upper_of(cli: Cli, path: Path) -> Path:
    """The upper layer of the jail of ``path``."""
    return Path(jail_dir(str(cli.state_dir), str(path)).upper)


TRANSPARENCY_SCRIPT = """
set -e
echo new > new.txt
cat new.txt
echo more >> file.txt
mv new.txt renamed.txt
rm old.txt
mkdir -p made/inner
echo made > made/inner/f
chmod 600 file.txt
ln -s file.txt link
rm -r dir
test ! -e old.txt
test ! -e dir
cat link
ls -A
pwd
"""


def test_ac8_fr70_fr71_transparent_writes_leave_lower_untouched(cli: Cli, proj: Path) -> None:
    """AC-8, FR-70, FR-71: all operations work in place; the real directory is unchanged."""
    before = snapshot(proj)
    result = cli("run", "-j", ".", "--", "sh", "-c", TRANSPARENCY_SCRIPT, cwd=proj)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\n") == [
        "new",
        "original",
        "more",
        "file.txt",
        "link",
        "made",
        "notes.txt",
        "renamed.txt",
        "sibling.txt",
        str(proj),
        "",
    ]
    assert snapshot(proj) == before
    upper = upper_of(cli, proj)
    assert (upper / "renamed.txt").read_text(encoding="utf-8") == "new\n"
    assert (upper / "file.txt").read_text(encoding="utf-8") == "original\nmore\n"
    assert stat.S_ISCHR((upper / "old.txt").lstat().st_mode)
    assert not is_mounted(merged_of(cli, proj))


def test_ac9_fr75_changes_accumulate(cli: Cli, proj: Path) -> None:
    """AC-9, FR-75: a later session sees the pending changes of earlier ones."""
    assert cli("run", "-j", str(proj), "--", "sh", "-c", f"echo A > {proj}/x").returncode == 0
    second = cli("run", "-j", str(proj), "--", "sh", "-c", f"cat {proj}/x; rm {proj}/notes.txt")
    assert second.stdout == "A\n"
    third = cli("run", "-j", str(proj), "--", "sh", "-c", f"ls {proj}")
    assert third.stdout.split() == ["dir", "file.txt", "old.txt", "sibling.txt", "x"]
    assert (proj / "notes.txt").exists()
    assert not (proj / "x").exists()


def test_ac10_fr76_concurrent_session_is_refused(cli: Cli, proj: Path, tmp_path: Path) -> None:
    """AC-10, FR-76: a second session on the same path exits 125 naming the PID."""
    other = tmp_path / "other"
    other.mkdir()
    with cli.start("run", "-j", str(proj), "--", "sh", "-c", "echo ready; sleep 30") as first:
        assert first.stdout is not None
        assert first.stdout.readline() == "ready\n"
        second = cli("run", "-j", str(proj), "--", "true")
        assert second.returncode == 125
        assert second.stderr.endswith(
            f"agent-sandbox: error: -j {proj} is in use by another agent-sandbox session "
            f"(PID {first.pid})\n"
        )
        assert cli("run", "-j", str(other), "--", "true").returncode == 0
        first.send_signal(signal.SIGTERM)
        assert first.wait(timeout=10) == 128 + signal.SIGTERM
    assert not is_mounted(merged_of(cli, proj))


def test_ac14_fr37_single_file_jail(cli: Cli, proj: Path) -> None:
    """AC-14, FR-37: only the file is visible; changes are jailed; replacing it fails."""
    notes = proj / "notes.txt"
    script = (
        f"echo appended >> {notes}; cat {notes}; ls -A {proj};"
        f" rm {notes} 2>/dev/null || echo rm-failed"
    )
    result = cli("run", "-j", str(notes), "--", "sh", "-c", script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "notes\nappended\nnotes.txt\nrm-failed\n"
    assert notes.read_text(encoding="utf-8") == "notes\n"
    upper = upper_of(cli, notes)
    assert sorted(path.name for path in upper.iterdir()) == ["notes.txt"]
    assert (upper / "notes.txt").read_text(encoding="utf-8") == "notes\nappended\n"


def test_ac16_cleanup_after_sigterm(cli: Cli, proj: Path) -> None:
    """AC-16, FR-61: after SIGTERM the overlay is gone and the changes are kept."""
    script = f"echo kept > {proj}/new.txt; echo ready; sleep 30"
    with cli.start("run", "-j", str(proj), "--", "sh", "-c", script) as process:
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 128 + signal.SIGTERM
    assert not is_mounted(merged_of(cli, proj))
    assert (upper_of(cli, proj) / "new.txt").read_text(encoding="utf-8") == "kept\n"


def test_fr62_stale_mount_after_sigkill_is_recovered(cli: Cli, proj: Path) -> None:
    """FR-62: after ``kill -9`` the mount stays; the next run cleans it up, keeping changes."""
    script = f"echo kept > {proj}/new.txt; echo ready; sleep 30"
    with cli.start("run", "-j", str(proj), "--", "sh", "-c", script) as process:
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        process.kill()
        process.wait(timeout=10)
        assert process.stdout.read() == ""  # the sandbox died with its parent
    assert is_mounted(merged_of(cli, proj))
    result = cli("run", "-j", str(proj), "--", "cat", f"{proj}/new.txt")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "kept\n"
    assert not is_mounted(merged_of(cli, proj))


def test_fr63_failed_overlay_aborts_and_unmounts_others(
    cli: Cli, proj: Path, tmp_path: Path
) -> None:
    """FR-63: if the second overlay fails, the run exits 125 and the first is unmounted."""
    broken = tmp_path / "broken"
    broken.mkdir()
    jail = jail_dir(str(cli.state_dir), str(broken))
    os.makedirs(jail.root)
    Path(jail.upper).write_text("not a directory", encoding="utf-8")
    result = cli("run", "-j", str(proj), "-j", str(broken), "--", "true")
    assert result.returncode == 125
    assert f"cannot mount the overlay for -j {broken}" in result.stderr
    assert not is_mounted(merged_of(cli, proj))
    assert not is_mounted(jail.merged)


def test_fr9_program_from_pending_changes_runs(cli: Cli, proj: Path) -> None:
    """FR-9: a program created in an earlier session is found and runs."""
    create = "printf '#!/bin/sh\\necho built\\n' > tool && chmod +x tool"
    assert cli("run", "-j", ".", "--", "sh", "-c", create, cwd=proj).returncode == 0
    assert not (proj / "tool").exists()
    result = cli("run", "-j", ".", "--", "./tool", cwd=proj)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "built\n"


@pytest.mark.parametrize("mode", ["-j", "-w"])
def test_fr39_state_dir_hidden_when_created_by_this_run(
    cli: Cli, tmp_path: Path, mode: str
) -> None:
    """FR-39: a state directory created for this run's jails is hidden too."""
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    state = home / ".local" / "state" / "agent-sandbox"
    mounts = ["-j", str(home)] if mode == "-j" else ["-w", str(home), "-j", str(home / "proj")]
    script = f"ls -A {state}; echo x > {state}/x 2>/dev/null || echo cannot-write"
    result = cli("run", "--state-dir", str(state), *mounts, "--", "sh", "-c", script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "cannot-write\n"
    assert not (state / "x").exists()


def test_nfr6_jail_path_with_special_characters(cli: Cli, tmp_path: Path) -> None:
    """FR-74, NFR-6: ``,`` and ``:`` in the jailed path do not break the mount options."""
    weird = tmp_path / "a, b:c 'd'"
    weird.mkdir()
    result = cli("run", "-j", str(weird), "--", "sh", "-c", f'echo x > "{weird}/f"')
    assert result.returncode == 0, result.stderr
    assert not (weird / "f").exists()
    assert (upper_of(cli, weird) / "f").read_text(encoding="utf-8") == "x\n"
