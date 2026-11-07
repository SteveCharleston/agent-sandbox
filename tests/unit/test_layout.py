"""Tests for host layout detection and system mounts (FR-25 to FR-28)."""

import os
from pathlib import Path

import pytest

from agent_sandbox.layout import (
    SYSTEM_PATHS,
    Layout,
    SystemMount,
    detect_layout,
    resolve_layout,
    system_mounts,
)
from agent_sandbox.options import LayoutChoice


def make_tree(root: Path, dirs: list[str], links: dict[str, str] | None = None) -> str:
    """Create directories and symlinks below ``root`` and return it as a string."""
    for directory in dirs:
        (root / directory.lstrip("/")).mkdir(parents=True, exist_ok=True)
    for link, target in (links or {}).items():
        path = root / link.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    return str(root)


def fake_nixos(root: Path) -> str:
    """A NixOS-like tree as observed in specs/NOTES.md."""
    return make_tree(
        root,
        ["/nix/store/abc-system/sw/bin", "/run/wrappers/bin", "/etc", "/bin", "/usr/bin"],
        {
            "/run/current-system": "/nix/store/abc-system",
            "/bin/sh": "/nix/store/def-bash/bin/sh",
            "/usr/bin/env": "/nix/store/ghi-coreutils/bin/env",
        },
    )


# --- detection (FR-25) ---


def test_fr25_plain_tree_is_fhs(tmp_path: Path) -> None:
    """FR-25: without NixOS markers the layout is FHS."""
    root = make_tree(tmp_path, ["/usr/bin", "/etc"])
    assert detect_layout(root) is Layout.FHS


def test_fr25_etc_nixos_marks_nixos(tmp_path: Path) -> None:
    """FR-25: ``/etc/NIXOS`` marks a NixOS host."""
    root = make_tree(tmp_path, ["/etc"])
    (tmp_path / "etc" / "NIXOS").touch()
    assert detect_layout(root) is Layout.NIXOS


def test_fr25_current_system_marks_nixos_even_if_dangling(tmp_path: Path) -> None:
    """FR-25: ``/run/current-system`` marks NixOS; it points into a store outside the root."""
    root = make_tree(tmp_path, [], {"/run/current-system": "/nix/store/does-not-exist"})
    assert detect_layout(root) is Layout.NIXOS


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        (LayoutChoice.AUTO, Layout.NIXOS),
        (LayoutChoice.FHS, Layout.FHS),
        (LayoutChoice.NIXOS, Layout.NIXOS),
    ],
)
def test_fr25_layout_option_overrides_detection(
    tmp_path: Path, choice: LayoutChoice, expected: Layout
) -> None:
    """FR-25, section 5.2: ``--layout`` overrides detection; ``auto`` detects."""
    root = fake_nixos(tmp_path)
    assert resolve_layout(choice, root) is expected


def test_fr25_auto_detects_fhs(tmp_path: Path) -> None:
    """FR-25: ``--layout auto`` on an FHS tree."""
    assert resolve_layout(LayoutChoice.AUTO, make_tree(tmp_path, ["/usr"])) is Layout.FHS


# --- tables (FR-28) ---


def test_fr28_every_layout_has_a_table() -> None:
    """FR-28: the system paths are a table with an entry for every layout."""
    assert set(SYSTEM_PATHS) == set(Layout)
    for paths in SYSTEM_PATHS.values():
        assert "/etc" in paths
        assert all(path.startswith("/") and not path.endswith("/") for path in paths)


# --- FHS (FR-26) ---


def test_fr26_classic_fhs_binds_existing_directories(tmp_path: Path) -> None:
    """FR-26: existing system directories are bound, missing ones are skipped."""
    root = make_tree(tmp_path, ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/etc"])
    assert system_mounts(Layout.FHS, root) == (
        SystemMount("/usr"),
        SystemMount("/bin"),
        SystemMount("/sbin"),
        SystemMount("/lib"),
        SystemMount("/lib64"),
        SystemMount("/opt"),
        SystemMount("/etc"),
    )


def test_fr26_merged_usr_symlinks_are_recreated(tmp_path: Path) -> None:
    """FR-26: merged-``/usr`` symlinks are recreated with the same target."""
    root = make_tree(
        tmp_path,
        ["/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib64", "/etc"],
        {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": "/usr/lib64"},
    )
    assert system_mounts(Layout.FHS, root) == (
        SystemMount("/usr"),
        SystemMount("/bin", "usr/bin"),
        SystemMount("/sbin", "usr/sbin"),
        SystemMount("/lib", "usr/lib"),
        SystemMount("/lib64", "/usr/lib64"),
        SystemMount("/etc"),
    )


def test_fr26_symlink_outside_system_paths_is_bound(tmp_path: Path) -> None:
    """FR-26: a symlink to a directory outside the system paths is bind-mounted."""
    root = make_tree(tmp_path, ["/usr", "/var/opt"], {"/opt": "var/opt"})
    assert system_mounts(Layout.FHS, root) == (SystemMount("/usr"), SystemMount("/opt"))


def test_fr26_dangling_symlink_is_skipped(tmp_path: Path) -> None:
    """FR-26: a symlink whose target does not exist is skipped."""
    root = make_tree(tmp_path, ["/usr"], {"/opt": "var/missing"})
    assert system_mounts(Layout.FHS, root) == (SystemMount("/usr"),)


def test_fr26_symlink_into_missing_system_path_is_skipped(tmp_path: Path) -> None:
    """FR-26: ``/bin -> usr/bin`` without ``/usr`` is not recreated (it would dangle)."""
    root = make_tree(tmp_path, ["/etc"], {"/bin": "usr/bin"})
    assert system_mounts(Layout.FHS, root) == (SystemMount("/etc"),)


# --- NixOS (FR-27) ---


def test_fr27_nixos_mounts(tmp_path: Path) -> None:
    """FR-27: NixOS mounts; ``/run/current-system`` is recreated as a symlink into the store."""
    root = fake_nixos(tmp_path)
    assert system_mounts(Layout.NIXOS, root) == (
        SystemMount("/nix/store"),
        SystemMount("/run/current-system", "/nix/store/abc-system"),
        SystemMount("/run/wrappers"),
        SystemMount("/etc"),
        SystemMount("/bin"),
        SystemMount("/usr"),
    )


def test_fr27_nixos_ignores_fhs_paths(tmp_path: Path) -> None:
    """FR-27, FR-28: only the paths of the chosen layout are used."""
    root = fake_nixos(tmp_path)
    make_tree(tmp_path, ["/lib", "/opt", "/sbin"])
    paths = [mount.path for mount in system_mounts(Layout.NIXOS, root)]
    assert "/lib" not in paths
    assert "/opt" not in paths


def test_fr26_fr27_host_layout_smoke() -> None:
    """FR-25 to FR-27: the real host yields a layout with at least ``/etc``."""
    layout = detect_layout()
    mounts = system_mounts(layout)
    assert SystemMount("/etc") in mounts
    for mount in mounts:
        assert os.path.lexists(mount.path)
