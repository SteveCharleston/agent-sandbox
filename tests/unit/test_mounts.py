"""Tests for resolving and validating mount specs (FR-30 to FR-35, FR-38, FR-39, FR-94)."""

import os
import socket
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from agent_sandbox.errors import ExitCode, SetupError
from agent_sandbox.mounts import MountSpec, PathKind, plan_mounts, resolve_mount
from agent_sandbox.options import MountArg, MountMode
from agent_sandbox.paths import depth, is_within

RO = MountMode.READ_ONLY
RW = MountMode.WRITABLE
JAIL = MountMode.JAILED

DIR = PathKind.DIRECTORY
FILE = PathKind.FILE


@pytest.fixture(name="tree")
def fixture_tree(tmp_path: Path) -> Path:
    """A small tree: ``a/b/c/``, ``a/file.txt``, ``other/``, ``usr/``, ``state/``."""
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "file.txt").write_text("x")
    (tmp_path / "a" / "sibling.txt").write_text("y")
    (tmp_path / "other").mkdir()
    (tmp_path / "usr" / "share").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    return tmp_path.resolve()


def plan(tree: Path, *args: tuple[MountMode, str]) -> tuple[MountSpec, ...]:
    """Plan mounts below ``tree`` with ``tree/usr`` as system path and ``tree/state``."""
    return plan_mounts(
        [MountArg(mode, path) for mode, path in args],
        cwd=str(tree),
        system_paths=[str(tree / "usr")],
        state_dir=str(tree / "state"),
    )


def setup_error(tree: Path, *args: tuple[MountMode, str]) -> str:
    """Plan mounts, expect a setup error and return its message."""
    with pytest.raises(SetupError) as info:
        plan(tree, *args)
    return str(info.value)


# --- path helpers ---


@pytest.mark.parametrize(
    ("path", "parent", "expected"),
    [
        ("/a", "/a", True),
        ("/a/b", "/a", True),
        ("/ab", "/a", False),
        ("/a", "/a/b", False),
        ("/anything", "/", True),
        ("/", "/", True),
    ],
)
def test_is_within(path: str, parent: str, expected: bool) -> None:
    """``is_within`` compares whole path components."""
    assert is_within(path, parent) is expected


@pytest.mark.parametrize(("path", "expected"), [("/", 0), ("/a", 1), ("/a/b/c", 3)])
def test_depth(path: str, expected: int) -> None:
    """``depth`` counts path components below ``/``."""
    assert depth(path) == expected


# --- resolution (FR-31, FR-32, FR-94) ---


def test_fr31_absolute_directory_and_file(tree: Path) -> None:
    """FR-31, FR-32: directories and files are valid; the kind is recorded."""
    assert resolve_mount(MountArg(RO, str(tree / "a")), "/") == MountSpec(str(tree / "a"), RO, DIR)
    assert resolve_mount(MountArg(RW, str(tree / "a/file.txt")), "/") == MountSpec(
        str(tree / "a/file.txt"), RW, FILE
    )


@pytest.mark.parametrize("path", ["a/b", "./a/b", "a/b/", "a/b/c/..", "other/../a/b", "a//b"])
def test_fr31_relative_paths_are_normalized(tree: Path, path: str) -> None:
    """FR-31: relative paths are resolved against the current directory and normalized."""
    assert resolve_mount(MountArg(RO, path), str(tree)).path == str(tree / "a" / "b")


def test_fr31_symlinks_are_resolved_and_kept_as_alias(tree: Path) -> None:
    """FR-31: a symlinked path is mounted at its real location; the given path is an alias."""
    (tree / "link").symlink_to(tree / "a" / "b")
    assert resolve_mount(MountArg(JAIL, "link/c"), str(tree)) == MountSpec(
        str(tree / "a/b/c"), JAIL, DIR, aliases=(str(tree / "link/c"),)
    )


@pytest.mark.parametrize("path", ["a/b", "a/b/c/..", "./a//b/"])
def test_fr31_no_alias_without_symlinks(tree: Path, path: str) -> None:
    """FR-31: paths without symlinks have no alias, however they are spelled."""
    assert not resolve_mount(MountArg(RO, path), str(tree)).aliases


def test_fr31_tilde_is_expanded(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-31: a leading ``~`` is expanded, as in ``--ro=~/x``."""
    monkeypatch.setenv("HOME", str(tree / "a"))
    assert resolve_mount(MountArg(RO, "~/b"), "/").path == str(tree / "a" / "b")


def test_fr32_missing_path(tree: Path) -> None:
    """FR-32: a path that does not exist is a setup error (exit 125)."""
    with pytest.raises(SetupError) as info:
        resolve_mount(MountArg(RO, "missing"), str(tree))
    assert str(info.value) == "-r missing: No such file or directory"
    assert info.value.exit_code is ExitCode.SETUP_FAILED


def test_fr32_dangling_symlink(tree: Path) -> None:
    """FR-32: a symlink to a missing target counts as missing."""
    (tree / "dangling").symlink_to(tree / "missing")
    with pytest.raises(SetupError, match="No such file or directory"):
        resolve_mount(MountArg(RW, "dangling"), str(tree))


def test_fr32_empty_path(tree: Path) -> None:
    """FR-32: an empty path (``--jail=``) is rejected."""
    with pytest.raises(SetupError, match="^-j: empty path$"):
        resolve_mount(MountArg(JAIL, ""), str(tree))


def test_fr94_symlink_loop(tree: Path) -> None:
    """FR-94: a symlink loop is reported clearly."""
    (tree / "loop1").symlink_to("loop2")
    (tree / "loop2").symlink_to("loop1")
    with pytest.raises(SetupError, match="^-r loop1: Too many levels of symbolic links$"):
        resolve_mount(MountArg(RO, "loop1"), str(tree))


def test_fr94_component_not_a_directory(tree: Path) -> None:
    """FR-94: a path through a file is reported clearly."""
    with pytest.raises(SetupError, match="Not a directory"):
        resolve_mount(MountArg(RO, "a/file.txt/x"), str(tree))


@pytest.fixture(name="unix_socket")
def fixture_unix_socket(tmp_path: Path) -> Iterator[Path]:
    """A listening Unix socket in ``tmp_path``."""
    path = tmp_path / "sock"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))
        yield path


def test_fr94_socket_is_rejected(unix_socket: Path) -> None:
    """FR-94: sockets cannot be mounted."""
    with pytest.raises(SetupError, match="not a regular file or directory"):
        resolve_mount(MountArg(RW, str(unix_socket)), "/")


def test_fr94_fifo_is_rejected(tree: Path) -> None:
    """FR-94: FIFOs cannot be mounted."""
    os.mkfifo(tree / "fifo")
    with pytest.raises(SetupError, match="not a regular file or directory"):
        resolve_mount(MountArg(RO, "fifo"), str(tree))


def test_fr94_device_is_rejected() -> None:
    """FR-94: device files cannot be mounted."""
    with pytest.raises(SetupError, match="^-r /dev/null: not a regular file or directory$"):
        resolve_mount(MountArg(RO, "/dev/null"), "/")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
@pytest.mark.parametrize(("name", "mode"), [("a/b", 0o300), ("a/file.txt", 0o200)])
def test_fr94_unreadable_path(tree: Path, name: str, mode: int) -> None:
    """FR-94: a path that cannot be read is reported clearly."""
    path = tree / name
    path.chmod(mode)
    try:
        with pytest.raises(SetupError, match="Permission denied"):
            resolve_mount(MountArg(RO, name), str(tree))
    finally:
        path.chmod(0o700)


# --- duplicates and conflicts (FR-34) ---


def test_fr34_duplicates_are_removed(tree: Path) -> None:
    """FR-34: the same path with the same mode, even spelled differently, is mounted once."""
    (tree / "link").symlink_to(tree / "a")
    (tree / "link2").symlink_to(tree / "a")
    assert plan(
        tree,
        (RO, "a"),
        (RO, str(tree / "a")),
        (RO, "link"),
        (RO, "a/b/.."),
        (RO, "link2"),
        (RO, "link"),
    ) == (MountSpec(str(tree / "a"), RO, DIR, aliases=(str(tree / "link"), str(tree / "link2"))),)


def test_fr34_same_path_different_modes(tree: Path) -> None:
    """FR-34: the same path with different modes is an error."""
    (tree / "link").symlink_to(tree / "a")
    message = setup_error(tree, (RO, "a"), (JAIL, "link"))
    assert message == f"{tree / 'a'} is given with both -r and -j"


# --- nesting (FR-35) ---


def test_fr35_nested_modes_are_allowed(tree: Path) -> None:
    """FR-35: ``-w``/``-j`` inside ``-r``, ``-r`` inside ``-j`` and ``-j`` inside ``-w``."""
    specs = plan(tree, (RO, "a"), (RW, "a/b"), (JAIL, "a/b/c"), (JAIL, "other"))
    assert [(spec.path, spec.mode) for spec in specs] == [
        (str(tree / "a"), RO),
        (str(tree / "other"), JAIL),
        (str(tree / "a/b"), RW),
        (str(tree / "a/b/c"), JAIL),
    ]


@pytest.mark.parametrize(("outer", "inner"), [("a", "a/b"), ("a", "a/file.txt"), ("a", "a")])
def test_fr35_jail_inside_jail_is_rejected(tree: Path, outer: str, inner: str) -> None:
    """FR-35: a ``-j`` nested in another ``-j`` is an error; equal paths deduplicate."""
    if outer == inner:
        assert len(plan(tree, (JAIL, outer), (JAIL, inner))) == 1
        return
    assert setup_error(tree, (JAIL, inner), (JAIL, outer)) == (
        f"-j {tree / inner} is inside -j {tree / outer}; nested jails are not supported"
    )


def test_fr37_sibling_file_jails_are_allowed(tree: Path) -> None:
    """FR-35, FR-37: jails on two files in the same directory are not nested."""
    specs = plan(tree, (JAIL, "a/file.txt"), (JAIL, "a/sibling.txt"))
    assert [spec.kind for spec in specs] == [FILE, FILE]


# --- system directories (FR-30) ---


@pytest.mark.parametrize("mode", [RW, JAIL])
@pytest.mark.parametrize("path", ["usr", "usr/share"])
def test_fr30_writable_inside_system_path_is_rejected(
    tree: Path, mode: MountMode, path: str
) -> None:
    """FR-30: ``-w`` and ``-j`` on or inside a system directory are errors."""
    message = setup_error(tree, (mode, path))
    assert message.endswith(f"inside the read-only system directory {tree / 'usr'}; use -r")


def test_fr30_read_only_inside_system_path_is_allowed(tree: Path) -> None:
    """FR-30: ``-r`` inside a system directory is allowed."""
    assert plan(tree, (RO, "usr/share")) == (MountSpec(str(tree / "usr/share"), RO, DIR),)


def test_fr30_symlink_into_system_path_is_rejected(tree: Path) -> None:
    """FR-30, FR-31: the check uses the resolved path, so symlinks do not bypass it."""
    (tree / "a" / "sneaky").symlink_to(tree / "usr" / "share")
    assert setup_error(tree, (RW, "a/sneaky")) == (
        f"-w {tree / 'a/sneaky'} (resolves to {tree / 'usr/share'}): inside the read-only "
        f"system directory {tree / 'usr'}; use -r"
    )


def test_fr30_system_path_contained_in_writable_mount_is_allowed(tree: Path) -> None:
    """FR-30: a ``-w`` that contains a system directory is allowed (it stays read-only)."""
    assert plan(tree, (RW, str(tree))) == (MountSpec(str(tree), RW, DIR),)


# --- state directory (FR-39) ---


@pytest.mark.parametrize("mode", [RO, RW, JAIL])
def test_fr39_mount_inside_state_dir_is_rejected(tree: Path, mode: MountMode) -> None:
    """FR-39: no mount spec may expose the state directory or anything inside it."""
    (tree / "state" / "jails").mkdir()
    for path in ["state", "state/jails"]:
        assert "inside the agent-sandbox state directory" in setup_error(tree, (mode, path))


def test_fr39_mount_containing_state_dir_is_allowed(tree: Path) -> None:
    """FR-39: a mount that contains the state directory is allowed (it gets hidden)."""
    assert plan(tree, (RW, str(tree))) == (MountSpec(str(tree), RW, DIR),)


# --- ordering (FR-38) ---


@pytest.mark.parametrize(
    "order",
    [
        ["a/b/c", "a", "other", "a/b"],
        ["a", "a/b", "a/b/c", "other"],
        ["other", "a/b", "a/b/c", "a"],
    ],
)
def test_fr38_ordered_by_depth_then_path(tree: Path, order: Sequence[str]) -> None:
    """FR-38: specs are ordered shallow first, independent of the command-line order."""
    specs = plan(tree, *[(RO, path) for path in order])
    assert [spec.path for spec in specs] == [
        str(tree / "a"),
        str(tree / "other"),
        str(tree / "a/b"),
        str(tree / "a/b/c"),
    ]


def test_no_mounts(tree: Path) -> None:
    """Section 5.2: running without mount specs is allowed."""
    assert not plan(tree)
