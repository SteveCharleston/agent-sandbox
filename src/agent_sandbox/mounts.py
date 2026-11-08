"""Mount specs given with ``-r``, ``-w`` and ``-j``: resolution and validation (section 6.3)."""

import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from agent_sandbox.errors import SetupError
from agent_sandbox.options import COMMAND_LINE, MountArg, MountMode
from agent_sandbox.paths import depth, is_within

FLAGS = {MountMode.READ_ONLY: "-r", MountMode.WRITABLE: "-w", MountMode.JAILED: "-j"}


class PathKind(StrEnum):
    """What a mount spec path points to."""

    DIRECTORY = "directory"
    FILE = "file"


@dataclass(frozen=True)
class MountSpec:
    """A validated mount spec with an absolute, resolved path.

    ``aliases`` are the given absolute paths that differ from ``path`` because they
    contain symlinks; they are recreated as symlinks in the sandbox (FR-31).
    """

    path: str
    mode: MountMode
    kind: PathKind
    aliases: tuple[str, ...] = ()
    source: str = field(default=COMMAND_LINE, compare=False)

    @property
    def flag(self) -> str:
        """The short command-line flag of the mode, for messages."""
        return FLAGS[self.mode]

    def describe(self) -> str:
        """Describe the spec for messages, with the given path if it differs."""
        origin = "" if self.source == COMMAND_LINE else f" ({self.source})"
        if self.aliases:
            return f"{self.flag} {self.aliases[0]}{origin} (resolves to {self.path})"
        return f"{self.flag} {self.path}{origin}"


def resolve_mount(arg: MountArg, cwd: str) -> MountSpec:
    """Resolve and check one mount argument (FR-31, FR-32, FR-94)."""
    origin = "" if arg.source == COMMAND_LINE else f" ({arg.source})"
    label = f"{FLAGS[arg.mode]} {arg.path}{origin}"
    if not arg.path:
        raise SetupError(f"{FLAGS[arg.mode]}{origin}: empty path")
    path = os.path.abspath(os.path.join(cwd, os.path.expanduser(arg.path)))
    try:
        resolved = os.path.realpath(path, strict=True)
        mode = os.stat(resolved).st_mode
    except OSError as exc:
        raise SetupError(f"{label}: {exc.strerror or exc}") from exc
    if stat.S_ISDIR(mode):
        kind = PathKind.DIRECTORY
        access = os.R_OK | os.X_OK
    elif stat.S_ISREG(mode):
        kind = PathKind.FILE
        access = os.R_OK
    else:
        raise SetupError(f"{label}: not a regular file or directory")
    if not os.access(resolved, access):
        raise SetupError(f"{label}: Permission denied")
    aliases = (path,) if path != resolved else ()
    return MountSpec(resolved, arg.mode, kind, aliases, arg.source)


def _deduplicate(specs: Iterable[MountSpec]) -> list[MountSpec]:
    """Merge repeated specs of the same path (FR-34, FR-115).

    Different modes from one source are an error; a later source overrides the mode.
    """
    by_path: dict[str, MountSpec] = {}
    for spec in specs:
        known = by_path.setdefault(spec.path, spec)
        if known.mode is not spec.mode:
            if known.source == spec.source:
                where = "" if spec.source == COMMAND_LINE else f" in {spec.source}"
                raise SetupError(
                    f"{spec.path} is given with both {known.flag} and {spec.flag}{where}"
                )
            aliases = known.aliases + tuple(a for a in spec.aliases if a not in known.aliases)
            by_path[spec.path] = replace(spec, aliases=aliases)
            continue
        new_aliases = tuple(alias for alias in spec.aliases if alias not in known.aliases)
        if new_aliases:
            by_path[spec.path] = replace(known, aliases=known.aliases + new_aliases)
    return list(by_path.values())


def _check_writable_outside(specs: Iterable[MountSpec], system_paths: Iterable[str]) -> None:
    """Reject ``-w`` and ``-j`` on or inside read-only system directories (FR-30)."""
    for spec in specs:
        if spec.mode is MountMode.READ_ONLY:
            continue
        for system_path in system_paths:
            if is_within(spec.path, system_path):
                raise SetupError(
                    f"{spec.describe()}: inside the read-only system directory "
                    f"{system_path}; use -r"
                )


def _check_state_dir(specs: Iterable[MountSpec], state_dir: str) -> None:
    """Reject mount specs on or inside the state directory (FR-39)."""
    for spec in specs:
        if is_within(spec.path, state_dir):
            raise SetupError(
                f"{spec.describe()}: inside the agent-sandbox state directory "
                f"{state_dir}, which is never visible in the sandbox"
            )


def _check_protected_files(specs: Iterable[MountSpec], protected_files: Iterable[str]) -> None:
    """Reject a mount spec on the configuration file (FR-116)."""
    protected = set(protected_files)
    for spec in specs:
        if spec.path in protected:
            raise SetupError(
                f"{spec.describe()}: the agent-sandbox configuration file is never "
                "visible in the sandbox"
            )


def _check_nested_jails(specs: Sequence[MountSpec]) -> None:
    """Reject a ``-j`` nested inside another ``-j`` (FR-35)."""
    jails = [spec for spec in specs if spec.mode is MountMode.JAILED]
    for inner in jails:
        for outer in jails:
            if inner is not outer and is_within(inner.path, outer.path):
                raise SetupError(
                    f"{inner.describe()} is inside {outer.describe()}; "
                    "nested jails are not supported"
                )


def plan_mounts(
    args: Iterable[MountArg],
    *,
    cwd: str,
    system_paths: Iterable[str],
    state_dir: str,
    protected_files: Iterable[str] = (),
) -> tuple[MountSpec, ...]:
    """Resolve, validate and order the mount specs (FR-30 to FR-35, FR-38, FR-39).

    ``state_dir`` must be absolute and normalized. The result is ordered by depth,
    shallow first, then by path, independent of the command-line order.
    """
    specs = _deduplicate(resolve_mount(arg, cwd) for arg in args)
    _check_writable_outside(specs, system_paths)
    _check_state_dir(specs, state_dir)
    _check_protected_files(specs, protected_files)
    _check_nested_jails(specs)
    return tuple(sorted(specs, key=lambda spec: (depth(spec.path), spec.path)))
