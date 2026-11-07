"""Helpers for normalized absolute paths, as used inside the sandbox."""

from pathlib import PurePosixPath


def is_within(path: str, parent: str) -> bool:
    """Return whether ``path`` is ``parent`` or lies below it (both normalized, absolute)."""
    return parent == "/" or path == parent or path.startswith(parent + "/")


def depth(path: str) -> int:
    """Return the number of components of an absolute path; ``/`` has depth 0."""
    return len(PurePosixPath(path).parts) - 1
