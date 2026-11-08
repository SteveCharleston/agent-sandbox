"""Showing pending changes: ``diff`` and ``listdiffs`` (sections 5.3 and 5.4)."""

import difflib
import hashlib
import os
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from agent_sandbox.errors import AgentSandboxError
from agent_sandbox.pending import Pending
from agent_sandbox.upper import Change, ChangeKind, Entry, EntryType

CONTEXT_LINES = 3
BINARY_CHECK_BYTES = 8000
NO_NEWLINE = "\\ No newline at end of file\n"
NULL_HASH = "0" * 40

_GIT_TYPE_BITS = {
    EntryType.FILE: stat.S_IFREG,
    EntryType.SYMLINK: stat.S_IFLNK,
    EntryType.DIRECTORY: stat.S_IFDIR,
}

_ESCAPES = {'"': '\\"', "\\": "\\\\", "\a": "\\a", "\b": "\\b", "\t": "\\t", "\n": "\\n"}
_ESCAPES |= {"\v": "\\v", "\f": "\\f", "\r": "\\r"}


def quote_path(path: str) -> str:
    """Quote a path like git does, if it contains special characters (FR-13).

    Quotes, backslashes, control characters and non-ASCII bytes are C-escaped inside
    double quotes; other paths are returned unchanged. Unlike git, paths with spaces
    are quoted too, because GNU patch otherwise cuts the name at the space.
    """
    raw = os.fsencode(path)
    if all(0x20 < byte < 0x7F and byte not in b'"\\' for byte in raw):
        return path
    parts = []
    for byte in raw:
        char = chr(byte)
        if char in _ESCAPES:
            parts.append(_ESCAPES[char])
        elif 0x20 <= byte < 0x7F:
            parts.append(char)
        else:
            parts.append(f"\\{byte:03o}")
    return '"' + "".join(parts) + '"'


def git_mode(entry: Entry) -> str:
    """The mode in git headers: ``120000`` for symlinks, else type and permission bits."""
    if entry.type is EntryType.SYMLINK:
        return "120000"
    return f"{_GIT_TYPE_BITS.get(entry.type, 0) | entry.mode:06o}"


def git_hash(data: bytes) -> str:
    """The git blob hash of ``data``, for ``index`` lines."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _content(entry: Entry | None) -> bytes:
    if entry is None:
        return b""
    if entry.type is EntryType.SYMLINK:
        return os.fsencode(os.readlink(entry.path))
    with open(entry.path, "rb") as file:
        return file.read()


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_CHECK_BYTES]


def split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only, keeping line ends (``str.splitlines`` splits on more)."""
    lines = [line + "\n" for line in text.split("\n")]
    lines[-1] = lines[-1][:-1]
    return lines if lines[-1] else lines[:-1]


def _range(start: int, stop: int) -> str:
    """A hunk range as in ``difflib.unified_diff``."""
    beginning, length = start + 1, stop - start
    if length == 1:
        return str(beginning)
    if length == 0:
        beginning -= 1
    return f"{beginning},{length}"


def _line(prefix: str, line: str) -> Iterator[str]:
    yield prefix + line
    if not line.endswith("\n"):
        yield "\n" + NO_NEWLINE


def hunks(old: list[str], new: list[str]) -> Iterator[str]:
    """Unified diff hunks, with "No newline at end of file" markers (FR-13)."""
    matcher = difflib.SequenceMatcher(None, old, new)
    for group in matcher.get_grouped_opcodes(CONTEXT_LINES):
        first, last = group[0], group[-1]
        yield f"@@ -{_range(first[1], last[2])} +{_range(first[3], last[4])} @@\n"
        for tag, old_start, old_stop, new_start, new_stop in group:
            if tag == "equal":
                for line in old[old_start:old_stop]:
                    yield from _line(" ", line)
                continue
            for line in old[old_start:old_stop]:
                yield from _line("-", line)
            for line in new[new_start:new_stop]:
                yield from _line("+", line)


@dataclass(frozen=True)
class FileDiff:
    """The diff of one change: header lines, body lines and line counts."""

    header: list[str]
    body: list[str]
    added_lines: int | None
    deleted_lines: int | None


def _git_header(change: Change, old_data: bytes, new_data: bytes) -> list[str]:
    """``diff --git``, mode and ``index`` lines, as git writes them."""
    old, new = change.old, change.new
    header = [f"diff --git {quote_path('a/' + change.path)} {quote_path('b/' + change.path)}\n"]
    if old is None and new is not None:
        header.append(f"new file mode {git_mode(new)}\n")
    elif new is None and old is not None:
        header.append(f"deleted file mode {git_mode(old)}\n")
    elif old is not None and new is not None and git_mode(old) != git_mode(new):
        header += [f"old mode {git_mode(old)}\n", f"new mode {git_mode(new)}\n"]
    old_hash = git_hash(old_data) if old is not None else NULL_HASH
    new_hash = git_hash(new_data) if new is not None else NULL_HASH
    if old_hash != new_hash:
        unchanged_mode = old is not None and new is not None and git_mode(old) == git_mode(new)
        mode = f" {git_mode(new)}" if unchanged_mode and new is not None else ""
        header.append(f"index {old_hash}..{new_hash}{mode}\n")
    return header


def _file_diff(change: Change) -> FileDiff:
    """The git-style diff of a file or symlink change; binary files get a comment line."""
    old_data, new_data = _content(change.old), _content(change.new)
    if old_data != new_data and (_is_binary(old_data) or _is_binary(new_data)):
        return FileDiff(
            [f"# binary file {quote_path(change.path)} {change.kind}\n"], [], None, None
        )
    header = _git_header(change, old_data, new_data)
    if old_data == new_data:
        return FileDiff(header, [], 0, 0)
    from_name = quote_path("a/" + change.path) if change.old is not None else "/dev/null"
    to_name = quote_path("b/" + change.path) if change.new is not None else "/dev/null"
    lines = list(
        hunks(
            split_lines(old_data.decode("utf-8", "surrogateescape")),
            split_lines(new_data.decode("utf-8", "surrogateescape")),
        )
    )
    added = sum(1 for line in lines if line.startswith("+"))
    deleted = sum(1 for line in lines if line.startswith("-"))
    return FileDiff(header, [f"--- {from_name}\n", f"+++ {to_name}\n", *lines], added, deleted)


def _directory_note(change: Change) -> list[str]:
    """Directories have no representation in git patches; a comment line describes them."""
    path = quote_path(change.path + "/")
    if change.kind is ChangeKind.ADDED and change.new is not None:
        return [f"# added directory {path} (mode {change.new.mode:04o})\n"]
    if change.kind is ChangeKind.DELETED:
        return [f"# deleted directory {path}\n"]
    assert change.old is not None and change.new is not None
    return [
        f"# changed mode of directory {path} from {change.old.mode:04o} to {change.new.mode:04o}\n"
    ]


def _is_directory(change: Change) -> bool:
    entry = change.new or change.old
    return entry is not None and entry.type is EntryType.DIRECTORY


def _is_special(change: Change) -> bool:
    entry = change.new or change.old
    return entry is not None and entry.type is EntryType.OTHER


def display_order(changes: Iterable[Change]) -> list[Change]:
    """Sort by path components; for one path, a deletion comes before an addition."""
    return sorted(
        changes,
        key=lambda change: (change.path.split("/"), change.kind is not ChangeKind.DELETED),
    )


def format_diff(changes: Iterable[Change]) -> str:
    """The unified diff of all changes (FR-11 to FR-13)."""
    lines: list[str] = []
    for change in display_order(changes):
        if _is_directory(change):
            lines += _directory_note(change)
        elif _is_special(change):
            lines.append(f"# {change.kind} special file {quote_path(change.path)}\n")
        else:
            file_diff = _file_diff(change)
            lines += file_diff.header + file_diff.body
    return "".join(lines)


_STATUS = {ChangeKind.ADDED: "A", ChangeKind.DELETED: "D", ChangeKind.MODIFIED: "M"}


def format_stat(changes: Iterable[Change]) -> str:
    """One line per change plus a summary (FR-15).

    Lines are ``STATUS<TAB>ADDED<TAB>DELETED<TAB>PATH``: status ``A``, ``D`` or ``M``;
    line counts, or ``-`` for directories and binary files; directories end in ``/``.
    """
    ordered = display_order(changes)
    if not ordered:
        return ""
    lines = []
    for change in ordered:
        added = deleted = "-"
        suffix = "/" if _is_directory(change) else ""
        if not _is_directory(change) and not _is_special(change):
            file_diff = _file_diff(change)
            if file_diff.added_lines is not None and file_diff.deleted_lines is not None:
                added, deleted = str(file_diff.added_lines), str(file_diff.deleted_lines)
        path = quote_path(change.path + suffix)
        lines.append(f"{_STATUS[change.kind]}\t{added}\t{deleted}\t{path}\n")
    totals = {kind: sum(1 for change in ordered if change.kind is kind) for kind in ChangeKind}
    lines.append(
        f"{len(ordered)} changes: {totals[ChangeKind.ADDED]} added, "
        f"{totals[ChangeKind.MODIFIED]} modified, {totals[ChangeKind.DELETED]} deleted\n"
    )
    return "".join(lines)


def diff_text(pending: Pending | None, stat_only: bool) -> str:
    """The output of ``diff``: nothing without state or changes (FR-14)."""
    if pending is None:
        return ""
    with pending.reading() as modes:
        changes = pending.changes(modes)
        try:
            return format_stat(changes) if stat_only else format_diff(changes)
        except OSError as exc:
            raise AgentSandboxError(
                f"cannot read {exc.filename}: {exc.strerror} (while a session runs, entries "
                "the program made unreadable cannot be shown)"
            ) from exc


def listdiffs_text(pending: Iterable[Pending]) -> str:
    """The output of ``listdiffs``: ``PATH<TAB>COUNT<TAB>TIME`` per jail with changes (FR-16)."""
    lines = []
    for jail in pending:
        with jail.reading() as modes:
            count = len(jail.changes(modes))
        if count:
            when = jail.last_change().isoformat(timespec="seconds")
            lines.append(f"{quote_path(jail.meta.path)}\t{count}\t{when}\n")
    return "".join(lines)
