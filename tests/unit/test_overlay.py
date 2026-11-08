"""Tests for the overlay lifecycle with faked mounts (FR-60 to FR-63, FR-74, FR-75)."""

import os
import subprocess
from pathlib import Path

import pytest

from agent_sandbox import overlay
from agent_sandbox.errors import SetupError
from agent_sandbox.mounts import MountSpec, PathKind
from agent_sandbox.options import MountMode
from agent_sandbox.overlay import (
    MOUNT_OPTIONS,
    OverlayTools,
    describe_mount,
    is_mounted,
    mount_command,
    mount_points,
    open_jail,
    unmount_overlay,
)
from agent_sandbox.state import JailDir, jail_dir, locked, read_meta

TOOLS = OverlayTools("/bin/fuse-overlayfs", "/bin/fusermount3")

MOUNTINFO = (
    "22 1 0:21 / /proc rw,nosuid - proc proc rw\n"
    "135 36 0:91 / /tmp/state/jails/abc/merged rw,nosuid - fuse.fuse-overlayfs x rw\n"
    "136 36 0:92 / /home/u/my\\040dir\\134x rw - ext4 /dev/sda1 rw\n"
)


class FakeMounts:
    """Records mount and unmount calls instead of running fuse-overlayfs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fail_on: set[str] | None = None) -> None:
        self.mounted: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on or set()
        monkeypatch.setattr(overlay, "mount_overlay", self.mount)
        monkeypatch.setattr(overlay, "unmount_overlay", self.unmount)
        monkeypatch.setattr(overlay, "is_mounted", lambda path: path in self.mounted)

    def mount(self, spec: MountSpec, jail: JailDir, tools: OverlayTools) -> None:
        """Fake ``mount_overlay``."""
        del tools
        self.calls.append(("mount", spec.path))
        if spec.path in self.fail_on:
            raise SetupError(f"cannot mount the overlay for {spec.describe()}: fake")
        self.mounted.add(jail.merged)

    def unmount(self, merged: str, fusermount: str) -> None:
        """Fake ``unmount_overlay``."""
        del fusermount
        self.calls.append(("unmount", merged))
        self.mounted.discard(merged)


def jailed(path: Path, kind: PathKind = PathKind.DIRECTORY) -> MountSpec:
    """A jailed mount spec."""
    return MountSpec(str(path), MountMode.JAILED, kind)


def no_debug(message: str) -> None:
    """Discard debug messages."""
    del message


def test_fr74_mount_command_uses_fixed_relative_names() -> None:
    """FR-74: no path appears in the mount options."""
    assert mount_command(TOOLS) == ["/bin/fuse-overlayfs", "-o", MOUNT_OPTIONS, "merged"]
    assert MOUNT_OPTIONS == "lowerdir=lower,upperdir=upper,workdir=work"


def test_describe_mount_for_dry_run() -> None:
    """Section 5.2: ``--dry-run`` shows the overlay setup."""
    text = describe_mount(jailed(Path("/data/my proj")), JailDir("/s/jails/x"))
    assert text == (
        "# overlay for -j /data/my proj: lower -> /data/my proj\n"
        "(cd /s/jails/x && fuse-overlayfs -o lowerdir=lower,upperdir=upper,workdir=work merged)"
    )


def test_fr62_mount_points_are_unescaped() -> None:
    """FR-62: mount points from mountinfo, with octal escapes decoded."""
    assert mount_points(MOUNTINFO) == {
        "/proc",
        "/tmp/state/jails/abc/merged",
        "/home/u/my dir\\x",
    }


def test_fr62_is_mounted(tmp_path: Path) -> None:
    """FR-62: ``is_mounted`` reads a mountinfo file."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(MOUNTINFO, encoding="utf-8")
    assert is_mounted("/tmp/state/jails/abc/merged", str(mountinfo))
    assert not is_mounted("/tmp/state/jails/abc", str(mountinfo))
    assert is_mounted("/proc")


def test_fr61_open_jail_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-60, FR-61, FR-85: lock, meta, mount; unmount and unlock afterwards."""
    fake = FakeMounts(monkeypatch)
    state = str(tmp_path / "state")
    spec = jailed(tmp_path / "proj")
    jail = jail_dir(state, spec.path)
    with open_jail(spec, state, TOOLS, no_debug) as opened:
        assert opened == jail
        assert fake.calls == [("mount", spec.path)]
        meta = read_meta(jail)
        assert meta is not None
        assert meta.path == spec.path
        with pytest.raises(SetupError, match="in use by another agent-sandbox session"):
            with locked(jail.lock, "x"):
                pass
    assert fake.calls == [("mount", spec.path), ("unmount", jail.merged)]
    with locked(jail.lock, "x"):
        pass


def test_fr61_unmount_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-61: the overlay is unmounted when the session fails."""
    fake = FakeMounts(monkeypatch)
    spec = jailed(tmp_path / "proj")
    with pytest.raises(RuntimeError):
        with open_jail(spec, str(tmp_path / "state"), TOOLS, no_debug):
            raise RuntimeError("boom")
    assert fake.calls[-1][0] == "unmount"
    assert not fake.mounted


def test_fr62_stale_mount_is_unmounted_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-62: a mount left by a dead session is removed before mounting again."""
    fake = FakeMounts(monkeypatch)
    state = str(tmp_path / "state")
    spec = jailed(tmp_path / "proj")
    jail = jail_dir(state, spec.path)
    fake.mounted.add(jail.merged)
    with open_jail(spec, state, TOOLS, no_debug):
        pass
    assert fake.calls == [
        ("unmount", jail.merged),
        ("mount", spec.path),
        ("unmount", jail.merged),
    ]


def test_fr63_failed_mount_releases_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-63: a failed mount raises a setup error and leaves nothing locked or mounted."""
    spec = jailed(tmp_path / "proj")
    fake = FakeMounts(monkeypatch, fail_on={spec.path})
    state = str(tmp_path / "state")
    with pytest.raises(SetupError, match="cannot mount"):
        with open_jail(spec, state, TOOLS, no_debug):
            pass
    assert fake.calls == [("mount", spec.path)]
    with locked(jail_dir(state, spec.path).lock, "x"):
        pass


def test_fr75_pending_changes_survive_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-62, FR-75: sessions and stale-mount recovery never touch the upper layer."""
    fake = FakeMounts(monkeypatch)
    state = str(tmp_path / "state")
    spec = jailed(tmp_path / "proj")
    jail = jail_dir(state, spec.path)
    with open_jail(spec, state, TOOLS, no_debug):
        os.makedirs(jail.upper, exist_ok=True)
        Path(jail.upper, "pending.txt").write_text("change\n", encoding="utf-8")
    fake.mounted.add(jail.merged)
    with open_jail(spec, state, TOOLS, no_debug):
        pass
    assert Path(jail.upper, "pending.txt").read_text(encoding="utf-8") == "change\n"


# A callable test double: its state matters, not its methods.
class FakeRun:  # pylint: disable=too-few-public-methods
    """Stands in for ``overlay._run``: fusermount fails ``failures`` times."""

    def __init__(self, failures: int, mounted: set[str]) -> None:
        self.failures = failures
        self.mounted = mounted
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], cwd: str | None = None
    ) -> "subprocess.CompletedProcess[str]":
        del cwd
        self.commands.append(command)
        if "-z" not in command and self.failures > 0:
            self.failures -= 1
            return subprocess.CompletedProcess(command, 1, "", "Device or resource busy")
        self.mounted.discard(command[-1])
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.mark.parametrize(("failures", "lazy"), [(0, False), (2, False), (10, True)])
def test_fr61_unmount_retries_then_lazy(
    monkeypatch: pytest.MonkeyPatch, failures: int, lazy: bool
) -> None:
    """FR-61, FR-62: a busy mount is retried, then unmounted lazily."""
    mounted = {"/m"}
    fake = FakeRun(failures, mounted)
    monkeypatch.setattr(overlay, "_run", fake)
    monkeypatch.setattr(overlay, "is_mounted", lambda path: path in mounted)
    unmount_overlay("/m", TOOLS.fusermount, delay=0)
    assert not mounted
    assert (["/bin/fusermount3", "-u", "-z", "/m"] in fake.commands) is lazy
    assert len(fake.commands) == min(failures, 10) + 1


def test_fr61_unmount_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-61: a mount that cannot be removed at all is an error."""
    monkeypatch.setattr(
        overlay,
        "_run",
        lambda command, cwd=None: subprocess.CompletedProcess(command, 1, "", "nope"),
    )
    monkeypatch.setattr(overlay, "is_mounted", lambda path: True)
    with pytest.raises(SetupError, match="cannot unmount /m: nope"):
        unmount_overlay("/m", TOOLS.fusermount, delay=0)


def test_fr61_unmount_of_unmounted_path_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-61: nothing to do if the overlay is already gone."""
    monkeypatch.setattr(overlay, "is_mounted", lambda path: False)
    monkeypatch.setattr(overlay, "_run", lambda command, cwd=None: pytest.fail("ran"))
    unmount_overlay("/m", TOOLS.fusermount)
