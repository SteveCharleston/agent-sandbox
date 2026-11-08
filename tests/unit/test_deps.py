"""Tests for dependency checks (FR-90, FR-91)."""

from pathlib import Path

import pytest

from agent_sandbox.deps import check_user_namespaces, find_bwrap, find_overlay_tools
from agent_sandbox.errors import ExitCode, SetupError


@pytest.fixture(name="bwrap")
def fixture_bwrap(tmp_path: Path) -> Path:
    """A fake, executable ``bwrap`` in ``tmp_path/bin``."""
    path = tmp_path / "bin" / "bwrap"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def write_sysctls(tmp_path: Path, values: dict[str, str]) -> str:
    """Create fake ``/proc/sys`` files and return the directory."""
    proc_sys = tmp_path / "sys"
    for name, value in values.items():
        (proc_sys / name).parent.mkdir(parents=True, exist_ok=True)
        (proc_sys / name).write_text(f"{value}\n")
    return str(proc_sys)


def test_fr90_find_bwrap(bwrap: Path, tmp_path: Path) -> None:
    """FR-90: bwrap is looked up in PATH."""
    assert find_bwrap(f"/nonexistent:{tmp_path / 'bin'}") == str(bwrap)


def test_fr90_missing_bwrap(tmp_path: Path) -> None:
    """FR-90: a missing bwrap is a setup error (exit 125) naming the binary."""
    with pytest.raises(SetupError) as info:
        find_bwrap(str(tmp_path))
    assert str(info.value).startswith("bwrap (bubblewrap) not found in PATH")
    assert info.value.exit_code is ExitCode.SETUP_FAILED


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"user/max_user_namespaces": "1000"},
        {"kernel/unprivileged_userns_clone": "1", "user/max_user_namespaces": "1000"},
    ],
)
def test_fr91_user_namespaces_available(
    bwrap: Path, tmp_path: Path, values: dict[str, str]
) -> None:
    """FR-91: enabled or unknown settings pass."""
    check_user_namespaces(str(bwrap), write_sysctls(tmp_path, values))


@pytest.mark.parametrize(
    ("values", "name"),
    [
        ({"kernel/unprivileged_userns_clone": "0"}, "kernel.unprivileged_userns_clone"),
        ({"user/max_user_namespaces": "0"}, "user.max_user_namespaces"),
    ],
)
def test_fr91_user_namespaces_disabled(
    bwrap: Path, tmp_path: Path, values: dict[str, str], name: str
) -> None:
    """FR-91: disabled user namespaces are reported with the likely cause."""
    with pytest.raises(SetupError) as info:
        check_user_namespaces(str(bwrap), write_sysctls(tmp_path, values))
    assert str(info.value).startswith(f"unprivileged user namespaces are disabled ({name} = 0)")


def test_fr96_setuid_bwrap_does_not_need_user_namespaces(bwrap: Path, tmp_path: Path) -> None:
    """FR-96: a setuid bwrap works without unprivileged user namespaces."""
    bwrap.chmod(0o4755)
    check_user_namespaces(str(bwrap), write_sysctls(tmp_path, {"user/max_user_namespaces": "0"}))


def make_tools(directory: Path, names: list[str]) -> None:
    """Create fake executables."""
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).write_text("#!/bin/sh\n")
        (directory / name).chmod(0o755)


@pytest.mark.parametrize("fusermount", ["fusermount3", "fusermount"])
def test_fr90_find_overlay_tools(tmp_path: Path, fusermount: str) -> None:
    """FR-90, FR-97: fuse-overlayfs and fusermount3, or fusermount as fallback."""
    make_tools(tmp_path / "bin", ["fuse-overlayfs", fusermount])
    tools = find_overlay_tools(str(tmp_path / "bin"))
    assert tools.fuse_overlayfs == str(tmp_path / "bin" / "fuse-overlayfs")
    assert tools.fusermount == str(tmp_path / "bin" / fusermount)


@pytest.mark.parametrize(
    ("present", "missing"),
    [(["fusermount3"], "fuse-overlayfs"), (["fuse-overlayfs"], "fusermount3")],
)
def test_fr90_missing_overlay_tools(tmp_path: Path, present: list[str], missing: str) -> None:
    """FR-90: a missing tool for ``-j`` is named in the error."""
    make_tools(tmp_path / "bin", present)
    with pytest.raises(SetupError, match=f"^{missing}.* not found in PATH; -j needs it"):
        find_overlay_tools(str(tmp_path / "bin"))
