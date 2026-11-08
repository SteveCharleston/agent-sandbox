"""Checks for the host programs and kernel features that ``run`` needs (FR-90, FR-91)."""

import os
import shutil
import stat

from agent_sandbox.errors import SetupError
from agent_sandbox.overlay import OverlayTools

# sysctls that disable unprivileged user namespaces when they are 0.
_USERNS_SYSCTLS = (
    "kernel/unprivileged_userns_clone",
    "user/max_user_namespaces",
)


def find_bwrap(path: str | None = None) -> str:
    """Return the absolute path of ``bwrap`` found in ``path`` (default: ``$PATH``)."""
    found = shutil.which("bwrap", path=path)
    if found is None:
        raise SetupError(
            "bwrap (bubblewrap) not found in PATH; install it first (see README: requirements)"
        )
    return os.path.abspath(found)


def find_overlay_tools(path: str | None = None) -> OverlayTools:
    """Return ``fuse-overlayfs`` and ``fusermount3`` (or ``fusermount``) for ``-j`` (FR-90)."""
    fuse_overlayfs = shutil.which("fuse-overlayfs", path=path)
    if fuse_overlayfs is None:
        raise SetupError(
            "fuse-overlayfs not found in PATH; -j needs it (see README: requirements)"
        )
    fusermount = shutil.which("fusermount3", path=path) or shutil.which("fusermount", path=path)
    if fusermount is None:
        raise SetupError(
            "fusermount3 (or fusermount) not found in PATH; -j needs it "
            "(see README: requirements)"
        )
    return OverlayTools(os.path.abspath(fuse_overlayfs), os.path.abspath(fusermount))


def check_user_namespaces(bwrap: str, proc_sys: str = "/proc/sys") -> None:
    """Fail clearly if unprivileged user namespaces are disabled (FR-91).

    A setuid ``bwrap`` does not need them, so it is not checked.
    """
    if os.stat(bwrap).st_mode & stat.S_ISUID:
        return
    for name in _USERNS_SYSCTLS:
        try:
            with open(os.path.join(proc_sys, name), encoding="ascii") as file:
                value = file.read().strip()
        except OSError:
            continue
        if value == "0":
            raise SetupError(
                f"unprivileged user namespaces are disabled ({name.replace('/', '.')} = 0); "
                "bwrap needs them (see README: requirements)"
            )
