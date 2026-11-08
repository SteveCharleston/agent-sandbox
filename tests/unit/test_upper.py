"""Tests for reading upper layers with hand-built trees (FR-72, FR-45, FR-37)."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_sandbox.upper import (
    OPAQUE_MARKER,
    OPAQUE_XATTR,
    ChangeKind,
    EntryType,
    compute_changes,
    is_opaque,
)

ADDED, DELETED, MODIFIED = ChangeKind.ADDED, ChangeKind.DELETED, ChangeKind.MODIFIED


@dataclass(frozen=True)
class Link:
    """A symlink in a tree description."""

    target: str


@dataclass(frozen=True)
class Mode:
    """A file with explicit permission bits."""

    content: str
    mode: int


class Whiteout:  # pylint: disable=too-few-public-methods
    """A deletion marker (character device 0:0) in a tree description."""


WHITEOUT = Whiteout()

Tree = dict[str, "str | bytes | Link | Mode | Whiteout | Tree"]


def build(root: Path, tree: Tree) -> Path:
    """Create ``tree`` below ``root``; a ``dict`` value is a directory."""
    root.mkdir(parents=True, exist_ok=True)
    for name, value in tree.items():
        path = root / name
        if isinstance(value, dict):
            build(path, value)
        elif isinstance(value, Link):
            path.symlink_to(value.target)
        elif isinstance(value, Whiteout):
            try:
                os.mknod(path, stat.S_IFCHR | 0o600, 0)
            except PermissionError:
                pytest.skip("cannot create whiteouts (character devices 0:0) here")
        elif isinstance(value, Mode):
            path.write_text(value.content, encoding="utf-8")
            path.chmod(value.mode)
        elif isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(value, encoding="utf-8")
    return root


def opaque(tree: Tree) -> Tree:
    """Mark a directory as opaque with fuse-overlayfs's marker file."""
    return {OPAQUE_MARKER: "", **tree}


def changes(
    tmp_path: Path, lower: Tree | None, upper: Tree, only: str | None = None
) -> list[tuple[str, ChangeKind]]:
    """The changes of ``upper`` over ``lower`` as ``(path, kind)`` pairs."""
    lower_dir = str(build(tmp_path / "lower", lower)) if lower is not None else None
    upper_dir = str(build(tmp_path / "upper", upper))
    return [(change.path, change.kind) for change in compute_changes(lower_dir, upper_dir, only)]


LOWER: Tree = {
    "file.txt": "original\n",
    "same.txt": "same\n",
    "script.sh": Mode("#!/bin/sh\n", 0o644),
    "link": Link("file.txt"),
    "dir": {"a.txt": "a\n", "sub": {"deep.txt": "deep\n"}},
}


def test_no_upper_layer(tmp_path: Path) -> None:
    """FR-14: a jail that was never mounted has no changes."""
    assert not compute_changes(str(tmp_path), str(tmp_path / "missing"))


def test_fr72_added_entries(tmp_path: Path) -> None:
    """FR-12: new files, symlinks and directories (directory before its contents)."""
    upper: Tree = {
        "new.txt": "new\n",
        "newlink": Link("new.txt"),
        "made": opaque({"inner": opaque({"f": "f\n"})}),
    }
    assert changes(tmp_path, LOWER, upper) == [
        ("made", ADDED),
        ("made/inner", ADDED),
        ("made/inner/f", ADDED),
        ("new.txt", ADDED),
        ("newlink", ADDED),
    ]


def test_fr72_modified_entries(tmp_path: Path) -> None:
    """FR-12: content, symlink target and permission changes."""
    upper: Tree = {
        "file.txt": "changed\n",
        "script.sh": Mode("#!/bin/sh\n", 0o755),
        "link": Link("other"),
    }
    assert changes(tmp_path, LOWER, upper) == [
        ("file.txt", MODIFIED),
        ("link", MODIFIED),
        ("script.sh", MODIFIED),
    ]


def test_fr72_unchanged_copies_are_not_changes(tmp_path: Path) -> None:
    """FR-72: copy-ups after ``touch`` or a ``chmod`` back and forth are not changes."""
    upper: Tree = {
        "same.txt": "same\n",
        "script.sh": Mode("#!/bin/sh\n", 0o644),
        "link": Link("file.txt"),
        "dir": {"sub": {}},
    }
    lower = build(tmp_path / "lower", LOWER)
    os.utime(lower / "same.txt", (0, 0))
    assert not compute_changes(str(lower), str(build(tmp_path / "upper", upper)))


@pytest.mark.parametrize("marker", ["char-device", "wh-file"])
def test_fr72_deleted_file(tmp_path: Path, marker: str) -> None:
    """FR-72: whiteouts as character devices 0:0, or ``.wh.NAME`` files."""
    upper: Tree = {"file.txt": WHITEOUT} if marker == "char-device" else {".wh.file.txt": ""}
    assert changes(tmp_path, LOWER, upper) == [("file.txt", DELETED)]


def test_fr72_deleted_directory_bottom_up(tmp_path: Path) -> None:
    """FR-12: a deleted directory lists its contents first, then itself."""
    assert changes(tmp_path, LOWER, {"dir": WHITEOUT}) == [
        ("dir/a.txt", DELETED),
        ("dir/sub/deep.txt", DELETED),
        ("dir/sub", DELETED),
        ("dir", DELETED),
    ]


def test_fr72_whiteout_without_lower_entry_is_ignored(tmp_path: Path) -> None:
    """FR-72: deleting something that only existed in upper leaves nothing to report."""
    assert changes(tmp_path, LOWER, {"ghost": WHITEOUT}) == []


def test_fr72_opaque_directory_hides_lower_contents(tmp_path: Path) -> None:
    """FR-72: ``rm -r dir; mkdir dir`` deletes the lower contents not recreated."""
    upper: Tree = {"dir": opaque({"a.txt": "a\n", "b.txt": "b\n"})}
    assert changes(tmp_path, LOWER, upper) == [
        ("dir/b.txt", ADDED),
        ("dir/sub/deep.txt", DELETED),
        ("dir/sub", DELETED),
    ]


def test_fr72_opaque_xattr(tmp_path: Path) -> None:
    """FR-72: the ``user.fuseoverlayfs.opaque`` xattr marks an opaque directory."""
    upper = build(tmp_path / "upper", {"dir": {}})
    try:
        os.setxattr(upper / "dir", OPAQUE_XATTR, b"y")
    except OSError:
        pytest.skip("user xattrs are not supported here")
    assert is_opaque(str(upper / "dir"))
    lower = str(build(tmp_path / "lower", LOWER))
    result = [(c.path, c.kind) for c in compute_changes(lower, str(upper))]
    assert result == [("dir/a.txt", DELETED), ("dir/sub/deep.txt", DELETED), ("dir/sub", DELETED)]


def test_fr72_non_opaque_directory_merges(tmp_path: Path) -> None:
    """FR-72: without the opaque mark, lower contents stay."""
    assert not is_opaque(str(build(tmp_path / "plain", {})))
    assert changes(tmp_path, LOWER, {"dir": {"c.txt": "c\n"}}) == [("dir/c.txt", ADDED)]


def test_fr72_marker_file_is_never_a_change(tmp_path: Path) -> None:
    """FR-72: the opaque marker is never reported."""
    result = changes(tmp_path, LOWER, {"made": opaque({})})
    assert result == [("made", ADDED)]


@pytest.mark.parametrize(
    ("upper", "expected"),
    [
        ({"file.txt": opaque({"x": "x\n"})}, [("file.txt", DELETED), ("file.txt", ADDED)]),
        (
            {"dir": "now a file\n"},
            [
                ("dir/a.txt", DELETED),
                ("dir/sub/deep.txt", DELETED),
                ("dir/sub", DELETED),
                ("dir", DELETED),
                ("dir", ADDED),
            ],
        ),
        ({"file.txt": Link("same.txt")}, [("file.txt", DELETED), ("file.txt", ADDED)]),
    ],
)
def test_fr72_type_change_is_delete_then_add(
    tmp_path: Path, upper: Tree, expected: list[tuple[str, ChangeKind]]
) -> None:
    """FR-72: a changed type is a deletion followed by an addition."""
    result = changes(tmp_path, LOWER, upper)
    assert result[: len(expected)] == expected


def test_fr72_directory_mode_change(tmp_path: Path) -> None:
    """FR-12: a permission change of a directory is a modification."""
    upper = build(tmp_path / "upper", {"dir": {}})
    (upper / "dir").chmod(0o700)
    lower = str(build(tmp_path / "lower", LOWER))
    [change] = compute_changes(lower, str(upper))
    assert (change.path, change.kind) == ("dir", MODIFIED)
    assert change.old is not None and change.new is not None
    assert (change.old.mode, change.new.mode) == (0o755, 0o700)


@pytest.mark.parametrize("lower", [None, "missing"])
def test_fr45_missing_lower_means_everything_is_added(tmp_path: Path, lower: str | None) -> None:
    """FR-45: without the real directory, all pending entries count as added."""
    upper = build(tmp_path / "upper", {"file.txt": "x\n", "gone": WHITEOUT, "d": {"f": ""}})
    lower_dir = None if lower is None else str(tmp_path / lower)
    result = [(c.path, c.kind) for c in compute_changes(lower_dir, str(upper))]
    assert result == [("d", ADDED), ("d/f", ADDED), ("file.txt", ADDED)]


def test_fr37_only_the_jailed_file_counts(tmp_path: Path) -> None:
    """FR-37: for a jailed file, other names in the upper layer are ignored."""
    upper: Tree = {"file.txt": "changed\n", "same.txt": "other\n", "dir": WHITEOUT}
    assert changes(tmp_path, LOWER, upper, only="file.txt") == [("file.txt", MODIFIED)]
    assert changes(tmp_path / "2", LOWER, {"file.txt": WHITEOUT}, only="file.txt") == [
        ("file.txt", DELETED)
    ]


def test_special_files_are_reported(tmp_path: Path) -> None:
    """FR-12: a FIFO in the upper layer is an added special entry."""
    upper = build(tmp_path / "upper", {})
    os.mkfifo(upper / "fifo")
    [change] = compute_changes(str(build(tmp_path / "lower", {})), str(upper))
    assert change.kind is ADDED
    assert change.new is not None and change.new.type is EntryType.OTHER


def test_unusual_names(tmp_path: Path) -> None:
    """FR-12: names with spaces, newlines and non-ASCII characters are handled."""
    names = ["with space", "new\nline", "ümlaut", os.fsdecode(b"caf\xe9")]
    upper: Tree = {name: "x\n" for name in names}
    assert sorted(path for path, _ in changes(tmp_path, {}, upper)) == sorted(names)
