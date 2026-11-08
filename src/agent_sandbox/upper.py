"""Reading an upper layer: what changed compared to the lower directory (FR-72)."""

import filecmp
import os
import posixpath
import stat
from dataclasses import dataclass
from enum import StrEnum

# fuse-overlayfs's representation (specs/NOTES.md, section 2).
OPAQUE_MARKER = ".wh..wh..opq"
OPAQUE_XATTR = "user.fuseoverlayfs.opaque"
WHITEOUT_PREFIX = ".wh."


class EntryType(StrEnum):
    """Type of a file system entry."""

    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True)
class Entry:
    """A file system entry on the host: its location, type and permission bits."""

    path: str
    type: EntryType
    mode: int

    @classmethod
    def from_stat(cls, path: str, info: os.stat_result) -> "Entry":
        """Build an entry from ``lstat`` information."""
        if stat.S_ISREG(info.st_mode):
            kind = EntryType.FILE
        elif stat.S_ISDIR(info.st_mode):
            kind = EntryType.DIRECTORY
        elif stat.S_ISLNK(info.st_mode):
            kind = EntryType.SYMLINK
        else:
            kind = EntryType.OTHER
        return cls(path, kind, stat.S_IMODE(info.st_mode))

    @classmethod
    def at(cls, path: str) -> "Entry | None":
        """The entry at ``path``, or ``None`` if there is none (symlinks not followed)."""
        try:
            return cls.from_stat(path, os.lstat(path))
        except FileNotFoundError:
            return None


class ChangeKind(StrEnum):
    """How an entry changed."""

    ADDED = "added"
    DELETED = "deleted"
    MODIFIED = "modified"


@dataclass(frozen=True)
class Change:
    """One changed entry; ``path`` is relative to the lower directory, ``/``-separated.

    ``MODIFIED`` means same type, different content, target or mode. A change of type
    is a ``DELETED`` followed by an ``ADDED`` change of the same path.
    """

    path: str
    kind: ChangeKind
    old: Entry | None
    new: Entry | None


def is_whiteout(info: os.stat_result) -> bool:
    """Whether an upper entry marks a deletion: a character device 0:0."""
    return stat.S_ISCHR(info.st_mode) and info.st_rdev == 0


def is_opaque(upper_dir: str) -> bool:
    """Whether an upper directory hides the lower directory's contents."""
    try:
        if os.getxattr(upper_dir, OPAQUE_XATTR, follow_symlinks=False) == b"y":
            return True
    except OSError:
        pass
    return os.path.lexists(os.path.join(upper_dir, OPAQUE_MARKER))


def _upper_children(upper_dir: str) -> tuple[dict[str, Entry], set[str]]:
    """The entries of an upper directory and the names it marks as deleted."""
    entries: dict[str, Entry] = {}
    whiteouts: set[str] = set()
    for name in os.listdir(upper_dir):
        if name == OPAQUE_MARKER:
            continue
        if name.startswith(WHITEOUT_PREFIX):
            whiteouts.add(name[len(WHITEOUT_PREFIX) :])
            continue
        path = os.path.join(upper_dir, name)
        info = os.lstat(path)
        if is_whiteout(info):
            whiteouts.add(name)
        else:
            entries[name] = Entry.from_stat(path, info)
    return entries, whiteouts


def same_content(old: Entry, new: Entry) -> bool:
    """Whether two entries of the same type have the same mode and content."""
    if old.mode != new.mode:
        return False
    if old.type is EntryType.SYMLINK:
        return os.readlink(old.path) == os.readlink(new.path)
    if old.type is EntryType.FILE:
        return filecmp.cmp(old.path, new.path, shallow=False)
    return True


class _Collector:
    """Walks upper and lower directories together and collects the changes."""

    def __init__(self) -> None:
        self.changes: list[Change] = []

    def directory(
        self, rel: str, lower_dir: str | None, upper_dir: str, opaque: bool, only: str | None
    ) -> None:
        """Compare an upper directory with its lower counterpart (if any)."""
        entries, whiteouts = _upper_children(upper_dir)
        lower_names = set(os.listdir(lower_dir)) if lower_dir else set()
        names = set(entries) | whiteouts | (lower_names if opaque else set())
        if only is not None:
            names &= {only}
        for name in sorted(names):
            child = posixpath.join(rel, name)
            old = None
            if lower_dir is not None and name in lower_names:
                old = Entry.at(os.path.join(lower_dir, name))
            new = entries.get(name)
            if new is None:
                if old is not None:
                    self.deleted(child, old)
            else:
                self.compare(child, old, new)

    def compare(self, rel: str, old: Entry | None, new: Entry) -> None:
        """Compare an upper entry with the lower entry of the same path (if any)."""
        if new.type is EntryType.DIRECTORY:
            if old is not None and old.type is EntryType.DIRECTORY:
                if old.mode != new.mode:
                    self.changes.append(Change(rel, ChangeKind.MODIFIED, old, new))
                self.directory(rel, old.path, new.path, is_opaque(new.path), None)
                return
            if old is not None:
                self.deleted(rel, old)
            self.changes.append(Change(rel, ChangeKind.ADDED, None, new))
            self.directory(rel, None, new.path, True, None)
        elif old is None:
            self.changes.append(Change(rel, ChangeKind.ADDED, None, new))
        elif old.type is not new.type:
            self.deleted(rel, old)
            self.changes.append(Change(rel, ChangeKind.ADDED, None, new))
        elif not same_content(old, new):
            self.changes.append(Change(rel, ChangeKind.MODIFIED, old, new))

    def deleted(self, rel: str, old: Entry) -> None:
        """Record a deletion; a directory's contents come first (bottom-up)."""
        if old.type is EntryType.DIRECTORY:
            for name in sorted(os.listdir(old.path)):
                child = Entry.at(os.path.join(old.path, name))
                if child is not None:
                    self.deleted(posixpath.join(rel, name), child)
        self.changes.append(Change(rel, ChangeKind.DELETED, old, None))


def compute_changes(lower: str | None, upper: str, only: str | None = None) -> list[Change]:
    """Return the changes the upper layer makes to the lower directory (FR-72).

    Only the upper layer is walked, plus the lower parts it hides or replaces, so a
    large lower directory costs nothing. Unchanged copies in the upper layer (after
    ``touch`` or a ``chmod`` back and forth) are not changes. ``lower`` may be ``None``
    or missing (FR-45): then everything in the upper layer is added. ``only``
    restricts the result to one top-level name, for single-file jails (FR-37).
    """
    if not os.path.isdir(upper):
        return []
    collector = _Collector()
    lower_dir = lower if lower is not None and os.path.isdir(lower) else None
    collector.directory("", lower_dir, upper, False, only)
    return collector.changes
