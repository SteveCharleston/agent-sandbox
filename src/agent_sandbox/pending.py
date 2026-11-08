"""Finding jails with pending changes in the state directory (FR-16, FR-45, FR-84)."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from agent_sandbox.errors import AgentSandboxError
from agent_sandbox.mounts import PathKind
from agent_sandbox.state import JailDir, JailMeta, jail_dir, jail_id, read_meta
from agent_sandbox.upper import Change, compute_changes


def normalize_path(path: str, cwd: str) -> str:
    """Resolve a user-given path like ``run`` does; it need not exist (FR-45, FR-84)."""
    return os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))


@dataclass(frozen=True)
class Pending:
    """A jail in the state directory and what it records."""

    jail: JailDir
    meta: JailMeta

    @property
    def lower(self) -> str:
        """The lower directory: the path, or the parent directory of a jailed file."""
        if self.meta.kind is PathKind.DIRECTORY:
            return self.meta.path
        return os.path.dirname(self.meta.path)

    @property
    def only(self) -> str | None:
        """For a jailed file: its name, the only entry that counts in the upper layer."""
        return None if self.meta.kind is PathKind.DIRECTORY else os.path.basename(self.meta.path)

    def changes(self) -> list[Change]:
        """The pending changes, compared with the lower directory as it is now."""
        return compute_changes(self.lower, self.jail.upper, self.only)

    def last_change(self) -> datetime:
        """The latest modification time in the upper layer, in local time (FR-16)."""
        latest = os.lstat(self.jail.upper).st_mtime
        for directory, dirs, files in os.walk(self.jail.upper):
            for name in dirs + files:
                latest = max(latest, os.lstat(os.path.join(directory, name)).st_mtime)
        return datetime.fromtimestamp(latest).astimezone()


def find_pending(state_dir: str, path: str) -> Pending | None:
    """The jail of the resolved ``path``, or ``None`` if there is no state for it."""
    jail = jail_dir(state_dir, path)
    try:
        meta = read_meta(jail)
    except AgentSandboxError as exc:
        raise AgentSandboxError(str(exc)) from exc
    if meta is None:
        return None
    if meta.path != path:
        raise AgentSandboxError(
            f"corrupt state in {jail.root}: it belongs to {meta.path}, not to {path}"
        )
    return Pending(jail, meta)


def all_pending(state_dir: str, warn: Callable[[str], None]) -> list[Pending]:
    """All jails in the state directory, sorted by path; broken ones are skipped with a warning."""
    jails_dir = os.path.join(state_dir, "jails")
    try:
        names = sorted(os.listdir(jails_dir))
    except FileNotFoundError:
        return []
    result = []
    for name in names:
        jail = JailDir(os.path.join(jails_dir, name))
        try:
            meta = read_meta(jail)
        except AgentSandboxError as exc:
            warn(str(exc))
            continue
        if meta is None:
            continue
        if jail_id(meta.path) != name:
            warn(f"corrupt state in {jail.root}: it does not belong to {meta.path}")
            continue
        result.append(Pending(jail, meta))
    return sorted(result, key=lambda pending: pending.meta.path)
