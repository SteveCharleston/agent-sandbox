"""The state directory that holds jailed changes (section 8)."""

import fcntl
import hashlib
import json
import os
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from agent_sandbox import __version__
from agent_sandbox.errors import AgentSandboxError, SetupError
from agent_sandbox.mounts import MountSpec, PathKind

STATE_DIR_VARIABLE = "AGENT_SANDBOX_STATE_DIR"

META_VERSION = 1


def state_dir_location(option: str | None, env: Mapping[str, str]) -> str:
    """Return the absolute, resolved state directory (FR-80).

    Precedence: ``--state-dir``, ``$AGENT_SANDBOX_STATE_DIR``,
    ``$XDG_STATE_HOME/agent-sandbox`` (only if absolute, as the XDG specification
    requires), ``~/.local/state/agent-sandbox``. The directory need not exist.
    """
    xdg_state_home = env.get("XDG_STATE_HOME", "")
    if option:
        location = option
    elif env.get(STATE_DIR_VARIABLE):
        location = env[STATE_DIR_VARIABLE]
    elif os.path.isabs(xdg_state_home):
        location = os.path.join(xdg_state_home, "agent-sandbox")
    else:
        home = env.get("HOME") or os.path.expanduser("~")
        location = os.path.join(home, ".local", "state", "agent-sandbox")
    return os.path.realpath(os.path.expanduser(location))


def jail_id(path: str) -> str:
    """The name of a jail's directory: the SHA-256 of its resolved path (FR-81, FR-84)."""
    return hashlib.sha256(os.fsencode(path)).hexdigest()


@dataclass(frozen=True)
class JailDir:
    """The files of one jail below ``<state-dir>/jails/<id>/`` (FR-81)."""

    root: str

    @property
    def meta(self) -> str:
        """``meta.json``."""
        return os.path.join(self.root, "meta.json")

    @property
    def upper(self) -> str:
        """The upper layer."""
        return os.path.join(self.root, "upper")

    @property
    def work(self) -> str:
        """The overlay work directory."""
        return os.path.join(self.root, "work")

    @property
    def lock(self) -> str:
        """The lock file."""
        return os.path.join(self.root, "lock")

    @property
    def lower_link(self) -> str:
        """The symlink to the lower directory (FR-74)."""
        return os.path.join(self.root, "lower")

    @property
    def merged(self) -> str:
        """The mount point of the merged view."""
        return os.path.join(self.root, "merged")


def jail_dir(state_dir: str, path: str) -> JailDir:
    """Return the jail directory of the resolved ``path``."""
    return JailDir(os.path.join(state_dir, "jails", jail_id(path)))


def lower_dir(spec: MountSpec) -> str:
    """The overlay's lower directory: the path, or the parent of a file (FR-37, FR-81)."""
    return spec.path if spec.kind is PathKind.DIRECTORY else os.path.dirname(spec.path)


def bind_source(jail: JailDir, spec: MountSpec) -> str:
    """What is bound at the jailed path: the merged view, or the file inside it."""
    if spec.kind is PathKind.DIRECTORY:
        return jail.merged
    return os.path.join(jail.merged, os.path.basename(spec.path))


def make_private_dir(path: str) -> None:
    """Create ``path`` with mode 0700 if it does not exist (FR-82)."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass


def make_jail_root(state_dir: str, jail: JailDir) -> None:
    """Create the state directory and the jail's root directory, all with mode 0700."""
    try:
        os.makedirs(os.path.dirname(state_dir), exist_ok=True)
        for path in (state_dir, os.path.dirname(jail.root), jail.root):
            make_private_dir(path)
    except OSError as exc:
        raise SetupError(
            f"cannot create the state directory {exc.filename}: {exc.strerror}; choose "
            "another location with --state-dir or $AGENT_SANDBOX_STATE_DIR"
        ) from exc


@dataclass(frozen=True)
class JailMeta:
    """Contents of ``meta.json`` (FR-81, FR-83)."""

    path: str
    kind: PathKind
    created: str
    tool_version: str
    version: int = META_VERSION

    @classmethod
    def new(cls, spec: MountSpec) -> "JailMeta":
        """Metadata for a jail that is created now."""
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return cls(spec.path, spec.kind, created, __version__)


def read_meta(jail: JailDir) -> JailMeta | None:
    """Read ``meta.json``; ``None`` if it does not exist."""
    try:
        with open(jail.meta, encoding="utf-8") as file:
            data = json.load(file)
        return JailMeta(
            path=data["path"],
            kind=PathKind(data["kind"]),
            created=data["created"],
            tool_version=data["tool_version"],
            version=data["version"],
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SetupError(f"cannot read {jail.meta}: {exc.strerror}") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise SetupError(f"corrupt state in {jail.root}: cannot read meta.json ({exc})") from exc


def write_meta(jail: JailDir, meta: JailMeta) -> None:
    """Write ``meta.json`` atomically with mode 0600."""
    temporary = f"{jail.meta}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(asdict(meta), file, indent=2)
        file.write("\n")
    os.replace(temporary, jail.meta)


def check_or_write_meta(jail: JailDir, spec: MountSpec) -> JailMeta:
    """Make sure ``meta.json`` matches ``spec``, creating it for a new jail (FR-81a)."""
    meta = read_meta(jail)
    if meta is None:
        meta = JailMeta.new(spec)
        write_meta(jail, meta)
        return meta
    if meta.path != spec.path:
        raise SetupError(
            f"corrupt state in {jail.root}: it belongs to {meta.path}, not to {spec.path}"
        )
    if meta.version > META_VERSION:
        raise SetupError(
            f"the state of {spec.path} was written by a newer agent-sandbox "
            f"({meta.tool_version}); update agent-sandbox"
        )
    if meta.kind is not spec.kind:
        raise SetupError(
            f"{spec.describe()}: the pending changes were recorded for a {meta.kind}, but "
            f"the path is now a {spec.kind}; merge or discard them first"
        )
    return meta


@contextmanager
def locked(
    path: str, description: str, error: type[AgentSandboxError] = SetupError
) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``path`` (FR-76, FR-85).

    The kernel releases the lock when the process dies, so it can never be stale.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise error(f"cannot open the lock file {path}: {exc.strerror}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 32, 0).decode(errors="replace").strip() or "unknown"
            raise error(
                f"{description} is in use by another agent-sandbox session (PID {holder})"
            ) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        yield
    finally:
        os.close(fd)


@contextmanager
def lock_if_free(path: str) -> Iterator[bool]:
    """Hold the lock on ``path`` if nobody else does; yields whether it is held."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError:
        yield False
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _open_up(directory: str) -> None:
    """Give the owner full access to a directory tree, opening each level before entering."""
    os.chmod(directory, 0o700)
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                _open_up(entry.path)


def _remove_tree(path: str) -> None:
    """Remove a directory tree, even if the program made parts of it read-only."""
    if not os.path.lexists(path):
        return
    if not os.path.isdir(path) or os.path.islink(path):
        os.unlink(path)
        return
    _open_up(path)
    shutil.rmtree(path)


def remove_jail(jail: JailDir) -> None:
    """Delete a jail's state after merge or discard (FR-40, FR-44).

    The upper layer goes first and ``meta.json`` last but one, so an interrupted removal
    leaves a jail that ``listdiffs`` still shows. The lock file is removed while held.
    """
    _remove_tree(jail.upper)
    _remove_tree(jail.work)
    if os.path.isdir(jail.merged):
        os.rmdir(jail.merged)
    for path in (jail.lower_link, f"{jail.lower_link}.tmp", f"{jail.meta}.tmp", jail.meta):
        if os.path.lexists(path):
            os.unlink(path)
    if os.path.lexists(jail.lock):
        os.unlink(jail.lock)
    os.rmdir(jail.root)
