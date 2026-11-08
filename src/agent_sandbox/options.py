"""Typed results of command-line parsing, one dataclass per subcommand."""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, TypeAlias


class MountMode(StrEnum):
    """How a path is made available in the sandbox (``-r``, ``-w``, ``-j``)."""

    READ_ONLY = "ro"
    WRITABLE = "rw"
    JAILED = "jail"


@dataclass(frozen=True)
class MountArg:
    """A mount flag as given on the command line; the path is not resolved yet."""

    mode: MountMode
    path: str


class EnvArgKind(StrEnum):
    """Source of an environment entry: ``--env`` or ``--env-file``."""

    ENTRY = "env"
    FILE = "env-file"


@dataclass(frozen=True)
class EnvArg:
    """An ``--env`` entry or ``--env-file`` path as given on the command line."""

    kind: EnvArgKind
    value: str


class LayoutChoice(StrEnum):
    """Value of ``--layout``."""

    AUTO = "auto"
    FHS = "fhs"
    NIXOS = "nixos"


# One field per flag of `run`; grouping them would only obscure the mapping to the CLI.
@dataclass(frozen=True)
class RunOptions:  # pylint: disable=too-many-instance-attributes
    """Options of ``agent-sandbox run``."""

    name: ClassVar[str] = "run"

    program: str
    args: tuple[str, ...]
    mounts: tuple[MountArg, ...]
    env: tuple[EnvArg, ...]
    network: bool
    chdir: str | None
    layout: LayoutChoice
    state_dir: str | None
    dry_run: bool
    debug: bool


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


Command: TypeAlias = RunOptions | DiffOptions | ListDiffsOptions | MergeOptions | DiscardOptions
