"""Jailed writes with fuse-overlayfs: mount, unmount and recovery (section 7, FR-60 to FR-63)."""

import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from agent_sandbox.errors import SetupError
from agent_sandbox.mounts import MountSpec
from agent_sandbox.state import (
    JailDir,
    check_or_write_meta,
    jail_dir,
    locked,
    lower_dir,
    make_jail_root,
    make_private_dir,
)

# FR-74: fixed relative names, so that no path ever appears inside the mount options.
MOUNT_OPTIONS = "lowerdir=lower,upperdir=upper,workdir=work"
MOUNT_POINT = "merged"
TIMEOUT = 60

_ESCAPE = re.compile(r"\\([0-7]{3})")

Log = Callable[[str], None]


@dataclass(frozen=True)
class OverlayTools:
    """The host programs needed for jailed writes (FR-90, FR-97)."""

    fuse_overlayfs: str
    fusermount: str


def mount_command(tools: OverlayTools) -> list[str]:
    """The fuse-overlayfs command, run inside the jail directory."""
    return [tools.fuse_overlayfs, "-o", MOUNT_OPTIONS, MOUNT_POINT]


def describe_mount(spec: MountSpec, jail: JailDir, fuse_overlayfs: str = "fuse-overlayfs") -> str:
    """The overlay setup for ``--dry-run``: a comment and the command."""
    command = shlex.join([fuse_overlayfs, "-o", MOUNT_OPTIONS, MOUNT_POINT])
    return (
        f"# overlay for {spec.describe()}: lower -> {lower_dir(spec)}\n"
        f"(cd {shlex.quote(jail.root)} && {command})"
    )


def mount_points(mountinfo: str) -> set[str]:
    """The mount points listed in the contents of ``/proc/self/mountinfo``."""
    points = set()
    for line in mountinfo.splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            points.add(_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), fields[4]))
    return points


def is_mounted(path: str, mountinfo: str = "/proc/self/mountinfo") -> bool:
    """Whether something is mounted on ``path``; works for dead FUSE mounts too."""
    with open(mountinfo, encoding="utf-8", errors="surrogateescape") as file:
        return path in mount_points(file.read())


def _run(command: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"{command[0]} failed: {exc}") from exc


def _last_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1] if lines else "no error message"


def _set_lower_link(jail: JailDir, lower: str) -> None:
    temporary = f"{jail.lower_link}.tmp"
    if os.path.lexists(temporary):
        os.unlink(temporary)
    os.symlink(lower, temporary)
    os.replace(temporary, jail.lower_link)


def mount_overlay(spec: MountSpec, jail: JailDir, tools: OverlayTools) -> None:
    """Mount the merged view of ``spec`` on the jail's ``merged/`` (FR-36, FR-37, FR-74)."""
    for path in (jail.upper, jail.work, jail.merged):
        make_private_dir(path)
    _set_lower_link(jail, lower_dir(spec))
    result = _run(mount_command(tools), cwd=jail.root)
    if result.returncode != 0 or not is_mounted(jail.merged):
        raise SetupError(
            f"cannot mount the overlay for {spec.describe()}: {_last_line(result.stderr)}"
        )


def unmount_overlay(
    merged: str, tools: OverlayTools, *, attempts: int = 10, delay: float = 0.1
) -> None:
    """Unmount ``merged``; retry while busy, then unmount lazily (FR-61, FR-62)."""
    result = None
    for _ in range(attempts):
        if not is_mounted(merged):
            return
        result = _run([tools.fusermount, "-u", merged])
        if result.returncode == 0:
            return
        time.sleep(delay)
    if is_mounted(merged):
        result = _run([tools.fusermount, "-u", "-z", merged])
        if result.returncode != 0 and is_mounted(merged):
            raise SetupError(f"cannot unmount {merged}: {_last_line(result.stderr)}")


@contextmanager
def open_jail(
    spec: MountSpec, state_dir: str, tools: OverlayTools, debug: Log
) -> Iterator[JailDir]:
    """Lock the jail, recover a stale mount, mount the overlay, unmount it afterwards.

    Pending changes in the upper layer are never touched here (FR-62, FR-75).
    """
    jail = jail_dir(state_dir, spec.path)
    make_jail_root(state_dir, jail)
    with locked(jail.lock, spec.describe()):
        if is_mounted(jail.merged):
            debug(f"unmounting stale overlay {jail.merged}")
            unmount_overlay(jail.merged, tools)
        check_or_write_meta(jail, spec)
        debug(f"mounting overlay for {spec.describe()} at {jail.merged}")
        mount_overlay(spec, jail, tools)
        try:
            yield jail
        finally:
            debug(f"unmounting overlay {jail.merged}")
            unmount_overlay(jail.merged, tools)
