"""Tests for the diff output (FR-11 to FR-16)."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from agent_sandbox.diffing import (
    diff_text,
    format_diff,
    format_stat,
    git_hash,
    listdiffs_text,
    quote_path,
    split_lines,
)
from agent_sandbox.mounts import MountSpec, PathKind
from agent_sandbox.options import MountMode
from agent_sandbox.pending import Pending
from agent_sandbox.state import JailMeta, jail_dir, make_jail_root, write_meta
from agent_sandbox.upper import compute_changes
from tests.unit.test_upper import WHITEOUT, Link, Mode, Tree, build, opaque


def diff_of(tmp_path: Path, lower: Tree, upper: Tree) -> str:
    """The diff of ``upper`` over ``lower``."""
    lower_dir = build(tmp_path / "lower", lower)
    upper_dir = build(tmp_path / "upper", upper)
    return format_diff(compute_changes(str(lower_dir), str(upper_dir)))


EMPTY_HASH = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
NULL = "0" * 40


def test_git_hash() -> None:
    """FR-13: index lines use git blob hashes."""
    assert git_hash(b"") == EMPTY_HASH
    assert git_hash(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


@pytest.mark.parametrize(
    ("path", "quoted"),
    [
        ("a/plain.txt", "a/plain.txt"),
        ("a/with space", '"a/with space"'),
        ("a/tab\tname", '"a/tab\\tname"'),
        ("a/new\nline", '"a/new\\nline"'),
        ('a/quote"back\\slash', '"a/quote\\"back\\\\slash"'),
        ("a/ü", '"a/\\303\\274"'),
        (os.fsdecode(b"a/caf\xe9"), '"a/caf\\351"'),
    ],
)
def test_fr13_paths_are_quoted_like_git(path: str, quoted: str) -> None:
    """FR-13: C-style quoting for special characters, and for spaces."""
    assert quote_path(path) == quoted


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("", []),
        ("a\n", ["a\n"]),
        ("a\nb", ["a\n", "b"]),
        ("a\r\nb\fc\x1cd\n", ["a\r\n", "b\fc\x1cd\n"]),
        ("\n\n", ["\n", "\n"]),
    ],
)
def test_fr13_lines_split_on_newline_only(text: str, lines: list[str]) -> None:
    """FR-13: ``\\r``, form feeds and other separators are not line ends."""
    assert split_lines(text) == lines


def test_fr12_modified_text_file(tmp_path: Path) -> None:
    """FR-12, FR-13: a modified file is a git diff with hunks and no-newline markers."""
    text = diff_of(
        tmp_path,
        {"f.txt": "one\ntwo\nthree\n"},
        {"f.txt": "one\n2\nthree\nfour"},
    )
    assert text == (
        "diff --git a/f.txt b/f.txt\n"
        f"index {git_hash(b'one\ntwo\nthree\n')}..{git_hash(b'one\n2\nthree\nfour')} 100644\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,3 +1,4 @@\n"
        " one\n"
        "-two\n"
        "+2\n"
        " three\n"
        "+four\n"
        "\\ No newline at end of file\n"
    )


def test_fr12_added_deleted_and_empty_files(tmp_path: Path) -> None:
    """FR-12: added and deleted files, an empty new file without hunks."""
    text = diff_of(
        tmp_path, {"old.txt": "old\n"}, {"old.txt": WHITEOUT, "new.txt": "new\n", "e": ""}
    )
    assert text == (
        "diff --git a/e b/e\n"
        "new file mode 100644\n"
        f"index {NULL}..{EMPTY_HASH}\n"
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        f"index {NULL}..{git_hash(b'new\n')}\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1 @@\n"
        "+new\n"
        "diff --git a/old.txt b/old.txt\n"
        "deleted file mode 100644\n"
        f"index {git_hash(b'old\n')}..{NULL}\n"
        "--- a/old.txt\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-old\n"
    )


def test_fr12_mode_change_only(tmp_path: Path) -> None:
    """FR-12, FR-13: a permission change shows the exact modes."""
    text = diff_of(tmp_path, {"s": Mode("x\n", 0o644)}, {"s": Mode("x\n", 0o600)})
    assert text == "diff --git a/s b/s\nold mode 100644\nnew mode 100600\n"


def test_fr12_symlink_change(tmp_path: Path) -> None:
    """FR-12, FR-13: symlinks are mode 120000 with the target as content."""
    text = diff_of(tmp_path, {"l": Link("a")}, {"l": Link("b")})
    assert text.startswith(
        f"diff --git a/l b/l\nindex {git_hash(b'a')}..{git_hash(b'b')} 120000\n--- a/l\n"
    )
    assert text.endswith("-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n")


def test_fr12_binary_files_are_a_one_line_note(tmp_path: Path) -> None:
    """FR-12: changed binary files are named, not shown."""
    text = diff_of(tmp_path, {"b": b"\0old"}, {"b": b"\0new", "n": b"x\0"})
    assert text == "# binary file b modified\n# binary file n added\n"


def test_fr12_binary_mode_change_is_shown(tmp_path: Path) -> None:
    """FR-12: a mode change of an unchanged binary file is a normal mode diff."""
    text = diff_of(tmp_path, {"b": b"\0x"}, {"b": b"\0x"})
    assert text == ""
    lower = build(tmp_path / "l2", {"b": b"\0x"})
    upper = build(tmp_path / "u2", {"b": b"\0x"})
    (upper / "b").chmod(0o755)
    text = format_diff(compute_changes(str(lower), str(upper)))
    assert text == "diff --git a/b b/b\nold mode 100644\nnew mode 100755\n"


def test_fr12_directories_are_comment_lines(tmp_path: Path) -> None:
    """FR-12, FR-13: added, deleted and changed directories are comments."""
    upper = build(tmp_path / "upper", {"made": opaque({}), "gone": WHITEOUT, "d": {}})
    (upper / "d").chmod(0o700)
    lower = build(tmp_path / "lower", {"gone": {}, "d": {}})
    text = format_diff(compute_changes(str(lower), str(upper)))
    assert text == (
        "# changed mode of directory d/ from 0755 to 0700\n"
        "# deleted directory gone/\n"
        "# added directory made/ (mode 0755)\n"
    )


def test_fr13_type_change_deletes_before_adding(tmp_path: Path) -> None:
    """FR-13: for one path, the deletion is shown before the addition."""
    text = diff_of(tmp_path, {"x": "file\n"}, {"x": Link("target")})
    assert text.index("deleted file mode 100644") < text.index("new file mode 120000")


def test_fr13_special_files_are_comment_lines(tmp_path: Path) -> None:
    """FR-12: special files are named in a comment."""
    upper = build(tmp_path / "upper", {})
    os.mkfifo(upper / "fifo")
    lower = build(tmp_path / "lower", {})
    assert format_diff(compute_changes(str(lower), str(upper))) == "# added special file fifo\n"


def test_fr13_non_utf8_content_is_passed_through(tmp_path: Path) -> None:
    """FR-13: bytes that are not UTF-8 survive the diff unchanged."""
    text = diff_of(tmp_path, {"f": b"caf\xe9\n"}, {"f": b"caf\xe8\n"})
    raw = text.encode("utf-8", "surrogateescape")
    assert b"-caf\xe9\n+caf\xe8\n" in raw


def test_fr15_stat(tmp_path: Path) -> None:
    """FR-15: one line per change with line counts, then a summary."""
    lower: Tree = {"f.txt": "a\nb\n", "old.txt": "x\ny\n", "bin": b"\0a", "d": {}}
    upper: Tree = {
        "f.txt": "a\nc\nd\n",
        "old.txt": WHITEOUT,
        "bin": b"\0b",
        "made": opaque({"n": "n\n"}),
        "with space": "s\n",
    }
    lower_dir, upper_dir = build(tmp_path / "lower", lower), build(tmp_path / "upper", upper)
    assert format_stat(compute_changes(str(lower_dir), str(upper_dir))) == (
        "M\t-\t-\tbin\n"
        "M\t2\t1\tf.txt\n"
        "A\t-\t-\tmade/\n"
        "A\t1\t0\tmade/n\n"
        "D\t0\t2\told.txt\n"
        'A\t1\t0\t"with space"\n'
        "6 changes: 3 added, 2 modified, 1 deleted\n"
    )


def test_fr14_no_changes_no_output(tmp_path: Path) -> None:
    """FR-14: no state or no changes print nothing."""
    assert format_diff([]) == ""
    assert format_stat([]) == ""
    assert diff_text(None, stat_only=False) == ""
    assert diff_text(None, stat_only=True) == ""
    assert diff_of(tmp_path, {"f": "x\n"}, {"f": "x\n"}) == ""


PATCH_LOWER: Tree = {
    "file.txt": "line1\nline2\nline3\n",
    "nonl.txt": "no newline",
    "old.txt": "old\n",
    "mode.sh": Mode("echo\n", 0o644),
    "link": Link("file.txt"),
    "tab\tname.txt": "t\n",
    "with space.txt": "s\n",
    "crlf.txt": "a\r\nb\r\n",
    "gone": {"x": "x\n"},
}

PATCH_UPPER: Tree = {
    "file.txt": "line1\nchanged\nline3\nline4",
    "nonl.txt": "now with newline\n",
    "old.txt": WHITEOUT,
    "mode.sh": Mode("echo\n", 0o755),
    "link": Link("other"),
    "tab\tname.txt": "t2\n",
    "with space.txt": "s2\n",
    "crlf.txt": "a\r\nc\r\n",
    "gone": WHITEOUT,
    "new file.txt": "new\n",
    "ümlaut.txt": "ü\n",
    "empty.txt": "",
    "made": opaque({"inner": opaque({"f": "made\n"})}),
}


PATCH_EXPECTED: Tree = {
    "file.txt": "line1\nchanged\nline3\nline4",
    "nonl.txt": "now with newline\n",
    "mode.sh": Mode("echo\n", 0o755),
    "link": Link("other"),
    "tab\tname.txt": "t2\n",
    "with space.txt": "s2\n",
    "crlf.txt": "a\r\nc\r\n",
    "new file.txt": "new\n",
    "ümlaut.txt": "ü\n",
    "empty.txt": "",
    "made": {"inner": {"f": "made\n"}},
}


def tree_state(root: Path) -> dict[str, tuple[int, bytes]]:
    """Mode and content (or symlink target) of every file and symlink below ``root``."""
    result = {}
    for directory, _, files in os.walk(root):
        for name in files:
            path = Path(directory, name)
            mode = path.lstat().st_mode
            content = os.fsencode(os.readlink(path)) if stat.S_ISLNK(mode) else path.read_bytes()
            result[str(path.relative_to(root))] = (mode, content)
    return result


@pytest.mark.parametrize(
    "tool",
    [["git", "apply", "--unsafe-paths", "-"], ["patch", "-p1", "--quiet", "--force"]],
    ids=["git-apply", "patch"],
)
def test_fr13_output_applies_with_git_and_patch(tmp_path: Path, tool: list[str]) -> None:
    """FR-13: ``git apply`` and ``patch -p1`` turn the lower tree into the merged view."""
    if shutil.which(tool[0]) is None:
        pytest.skip(f"{tool[0]} is not installed")
    lower = build(tmp_path / "lower", PATCH_LOWER)
    upper = build(tmp_path / "upper", PATCH_UPPER)
    patch = format_diff(compute_changes(str(lower), str(upper))).encode("utf-8", "surrogateescape")
    target = tmp_path / "target"
    shutil.copytree(lower, target, symlinks=True)
    result = subprocess.run(tool, cwd=target, input=patch, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()
    expected = build(tmp_path / "expected", PATCH_EXPECTED)
    assert tree_state(target) == tree_state(expected)


def make_pending(
    state: Path, path: Path, upper: Tree, kind: PathKind = PathKind.DIRECTORY
) -> Pending:
    """A jail with the given upper layer in ``state``."""
    jail = jail_dir(str(state), str(path))
    make_jail_root(str(state), jail)
    spec = MountSpec(str(path), MountMode.JAILED, kind)
    meta = JailMeta.new(spec)
    write_meta(jail, meta)
    build(Path(jail.upper), upper)
    return Pending(jail, meta)


def test_fr16_listdiffs_lines(tmp_path: Path) -> None:
    """FR-16, FR-17: path, change count and time; jails without changes are left out."""
    proj = build(tmp_path / "proj", {"a": "a\n"})
    other = build(tmp_path / "other dir", {})
    unchanged = build(tmp_path / "unchanged", {"a": "a\n"})
    pendings = [
        make_pending(tmp_path / "state", proj, {"a": "b\n", "n": "n\n"}),
        make_pending(tmp_path / "state", other, {"x": "x\n"}),
        make_pending(tmp_path / "state", unchanged, {"a": "a\n"}),
    ]
    os.utime(Path(pendings[0].jail.upper) / "n", (1_700_000_000, 1_700_000_000))
    lines = listdiffs_text(pendings).splitlines()
    assert len(lines) == 2
    path, count, when = lines[0].split("\t")
    assert (path, count) == (str(proj), "2")
    assert when == pendings[0].last_change().isoformat(timespec="seconds")
    assert lines[1].startswith(f'"{other}"\t1\t')


def test_fr18_listdiffs_empty() -> None:
    """FR-18: nothing pending prints nothing."""
    assert listdiffs_text([]) == ""
