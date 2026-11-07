"""Host layout detection and the system directories shared read-only (section 6.2)."""

import os
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from agent_sandbox.options import LayoutChoice
from agent_sandbox.paths import is_within


class Layout(StrEnum):
    """Host system layout."""

    FHS = "fhs"
    NIXOS = "nixos"


# FR-26, FR-27, FR-28: the system paths per layout, shared read-only if they exist.
SYSTEM_PATHS: Mapping[Layout, tuple[str, ...]] = {
    Layout.FHS: (
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib32",
        "/lib64",
        "/libx32",
        "/opt",
        "/etc",
    ),
    Layout.NIXOS: (
        "/nix/store",
        "/run/current-system",
        "/run/wrappers",
        "/etc",
        "/bin",
        "/usr",
    ),
}

_NIXOS_MARKERS = ("/etc/NIXOS", "/run/current-system")


@dataclass(frozen=True)
class SystemMount:
    """A system path: bind-mounted read-only, or recreated as a symlink to ``symlink_target``."""

    path: str
    symlink_target: str | None = None


def _on_host(root: str, path: str) -> str:
    return os.path.join(root, path.lstrip("/"))


def detect_layout(root: str = "/") -> Layout:
    """Detect the host layout below ``root`` (FR-25)."""
    if any(os.path.lexists(_on_host(root, marker)) for marker in _NIXOS_MARKERS):
        return Layout.NIXOS
    return Layout.FHS


def resolve_layout(choice: LayoutChoice, root: str = "/") -> Layout:
    """Return the layout chosen with ``--layout``, detecting it for ``auto``."""
    if choice is LayoutChoice.AUTO:
        return detect_layout(root)
    return Layout(choice.value)


def system_mounts(layout: Layout, root: str = "/") -> tuple[SystemMount, ...]:
    """Return the system mounts of ``layout`` that exist below ``root`` (FR-26, FR-27).

    A system path that is a symlink is recreated as the same symlink if its target lies
    inside a system path that is bind-mounted, so merged-``/usr`` links and
    ``/run/current-system`` behave as on the host. Other symlinks are bind-mounted (bwrap
    follows them), unless their target does not exist.
    """
    present = [path for path in SYSTEM_PATHS[layout] if os.path.lexists(_on_host(root, path))]
    bound = [path for path in present if not os.path.islink(_on_host(root, path))]
    mounts: list[SystemMount] = []
    for path in present:
        host_path = _on_host(root, path)
        if not os.path.islink(host_path):
            mounts.append(SystemMount(path))
            continue
        target = os.readlink(host_path)
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        if any(is_within(resolved, other) for other in bound):
            mounts.append(SystemMount(path, target))
        elif os.path.exists(host_path):
            mounts.append(SystemMount(path))
    return tuple(mounts)
