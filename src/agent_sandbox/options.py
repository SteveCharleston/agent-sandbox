"""Typed results of command-line parsing, one dataclass per subcommand."""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, TypeAlias


class MountMode(StrEnum):
    """How a path is made available in the sandbox (``-r``, ``-w``, ``-j``)."""

    READ_ONLY = "ro"
    WRITABLE = "rw"
    JAILED = "jail"


COMMAND_LINE = "command line"


@dataclass(frozen=True)
class MountArg:
    """A mount flag as given; the path is not resolved yet.

    ``source`` is ``"command line"`` or ``"profile NAME"`` (FR-115).
    """

    mode: MountMode
    path: str
    source: str = COMMAND_LINE


class EnvArgKind(StrEnum):
    """Source of an environment entry: ``--env`` or ``--env-file``."""

    ENTRY = "env"
    FILE = "env-file"


@dataclass(frozen=True)
class EnvArg:
    """An ``--env`` entry or ``--env-file`` path, from the command line or a profile."""

    kind: EnvArgKind
    value: str
    source: str = COMMAND_LINE


class LayoutChoice(StrEnum):
    """Value of ``--layout``."""

    AUTO = "auto"
    FHS = "fhs"
    NIXOS = "nixos"


# One field per flag of `run`; grouping them would only obscure the mapping to the CLI.
@dataclass(frozen=True)
class RunOptions:  # pylint: disable=too-many-instance-attributes
    """Options of ``agent-sandbox run``.

    ``None`` means "not given": ``program`` may come from a profile, ``network`` and
    ``layout`` from a profile or the defaults (shared, ``auto``). See FR-115.
    """

    name: ClassVar[str] = "run"

    program: str | None
    args: tuple[str, ...]
    mounts: tuple[MountArg, ...]
    env: tuple[EnvArg, ...]
    network: bool | None
    chdir: str | None
    layout: LayoutChoice | None
    state_dir: str | None
    dry_run: bool
    debug: bool
    profiles: tuple[str, ...] = ()
    config: str | None = None


@dataclass(frozen=True)
class DiffOptions:
    """Options of ``agent-sandbox diff``."""

    name: ClassVar[str] = "diff"

    path: str
    stat: bool
    state_dir: str | None
    debug: bool


@dataclass(frozen=True)
class ListDiffsOptions:
    """Options of ``agent-sandbox listdiffs``."""

    name: ClassVar[str] = "listdiffs"

    state_dir: str | None
    debug: bool


@dataclass(frozen=True)
class MergeOptions:
    """Options of ``agent-sandbox merge``."""

    name: ClassVar[str] = "merge"

    path: str
    yes: bool
    dry_run: bool
    state_dir: str | None
    debug: bool
    force: bool = False


@dataclass(frozen=True)
class DiscardOptions:
    """Options of ``agent-sandbox discard``."""

    name: ClassVar[str] = "discard"

    path: str
    yes: bool
    state_dir: str | None
    debug: bool


class ProfilesAction(StrEnum):
    """What ``agent-sandbox profiles`` does."""

    LIST = "list"
    INIT = "init"


@dataclass(frozen=True)
class ProfilesOptions:
    """Options of ``agent-sandbox profiles``."""

    name: ClassVar[str] = "profiles"

    action: ProfilesAction
    config: str | None
    debug: bool


Command: TypeAlias = (
    RunOptions | DiffOptions | ListDiffsOptions | MergeOptions | DiscardOptions | ProfilesOptions
)
