"""The sandbox plan and the bwrap command line built from it (sections 6.1 to 6.4)."""

import os
import posixpath
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from agent_sandbox.envspec import MASK
from agent_sandbox.errors import SetupError
from agent_sandbox.layout import SystemMount
from agent_sandbox.mounts import MountSpec
from agent_sandbox.options import MountMode
from agent_sandbox.paths import depth, is_within

# FR-20: the namespaces every sandbox gets; the network namespace depends on --no-network.
NAMESPACES = (
    "--unshare-user",
    "--unshare-pid",
    "--unshare-ipc",
    "--unshare-uts",
    "--unshare-cgroup",
)

# Order of operations on the same depth: base file systems first, then system mounts,
# the home tmpfs, mount specs, alias symlinks, and finally hiding the state directory.
_BASE, _SYSTEM, _HOME, _SPEC, _ALIAS, _HIDE, _HIDE_RO = range(7)


@dataclass(frozen=True)
class Bind:
    """A bind mount of ``source`` on the host at ``dest`` in the sandbox."""

    source: str
    dest: str
    writable: bool


# Mirrors the parts of a sandbox; splitting it would only add indirection.
@dataclass(frozen=True)
class SandboxPlan:  # pylint: disable=too-many-instance-attributes
    """Everything needed to build the bwrap command line."""

    system_mounts: tuple[SystemMount, ...]
    binds: tuple[Bind, ...]
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    chdir: str = "/"
    network: bool = True
    home_tmpfs: str | None = None
    aliases: tuple[tuple[str, str], ...] = ()
    hidden: tuple[str, ...] = ()

    def visible_roots(self) -> tuple[str, ...]:
        """Paths below which host content is visible in the sandbox."""
        return (
            *(mount.path for mount in self.system_mounts),
            *(bind.dest for bind in self.binds),
            *(alias for alias, _ in self.aliases),
        )

    def is_visible(self, path: str) -> bool:
        """Whether the host's ``path`` (absolute, normalized) is visible in the sandbox."""
        return _visible(path, self.visible_roots(), self.hidden)


def _visible(path: str, roots: Iterable[str], hidden: Iterable[str]) -> bool:
    return any(is_within(path, root) for root in roots) and not any(
        is_within(path, hidden_path) for hidden_path in hidden
    )


def _binds(mounts: Iterable[MountSpec], jail_sources: Mapping[str, str]) -> list[Bind]:
    binds = []
    for spec in mounts:
        if spec.mode is MountMode.JAILED:
            source = jail_sources.get(spec.path)
            if source is None:
                raise SetupError(f"{spec.describe()}: jailed writes are not available yet")
            binds.append(Bind(source, spec.path, writable=True))
        else:
            binds.append(Bind(spec.path, spec.path, writable=spec.mode is MountMode.WRITABLE))
    return binds


# Keyword-only inputs of a pure function; grouping them would only move the problem.
def plan_sandbox(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    system_mounts: Sequence[SystemMount],
    mounts: Sequence[MountSpec],
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: str,
    state_dir: str,
    state_dir_exists: bool,
    jail_sources: Mapping[str, str] | None = None,
    logical_cwd: str | None = None,
    chdir: str | None = None,
    network: bool = True,
) -> SandboxPlan:
    """Combine validated inputs into a sandbox plan.

    - ``$HOME`` (of the sandbox environment) becomes an empty tmpfs unless a mount covers
      it (FR-22).
    - Aliases of symlinked mount specs become symlinks unless already covered (FR-31).
    - The state directory is hidden if a mount would show it (FR-39).
    - The working directory is ``chdir`` if given, else the current directory if it is
      visible (preferring the logical ``$PWD`` spelling), else ``/`` (section 5.2).
    """
    binds = _binds(mounts, jail_sources or {})
    bound = [mount.path for mount in system_mounts] + [bind.dest for bind in binds]

    def covered(path: str) -> bool:
        return any(is_within(path, root) for root in bound)

    hidden = (state_dir,) if state_dir_exists and covered(state_dir) else ()
    aliases = tuple(
        (alias, spec.path)
        for spec in mounts
        for alias in spec.aliases
        if not covered(alias) and not any(is_within(alias, path) for path in hidden)
    )
    home = env.get("HOME", "")
    home = posixpath.normpath(home) if os.path.isabs(home) else ""
    home_tmpfs = home if home not in ("", "/") and not covered(home) else None

    roots = bound + [alias for alias, _ in aliases]
    if chdir:
        workdir = posixpath.normpath(os.path.join(cwd, chdir))
    else:
        candidates = [path for path in (logical_cwd, cwd) if path]
        workdir = next((path for path in candidates if _visible(path, roots, hidden)), "/")

    return SandboxPlan(
        system_mounts=tuple(system_mounts),
        binds=tuple(binds),
        argv=tuple(argv),
        env=dict(env),
        chdir=workdir,
        network=network,
        home_tmpfs=home_tmpfs,
        aliases=aliases,
        hidden=hidden,
    )


def _filesystem_options(plan: SandboxPlan) -> list[list[str]]:
    """Mount operations, ordered by depth so parents come before children (FR-38)."""
    operations: list[tuple[tuple[int, int, str], list[str]]] = []

    def add(path: str, rank: int, option: list[str]) -> None:
        operations.append(((depth(path), rank, path), option))

    add("/proc", _BASE, ["--proc", "/proc"])
    add("/dev", _BASE, ["--dev", "/dev"])
    add("/tmp", _BASE, ["--tmpfs", "/tmp"])
    for mount in plan.system_mounts:
        if mount.symlink_target is None:
            add(mount.path, _SYSTEM, ["--ro-bind", mount.path, mount.path])
        else:
            add(mount.path, _SYSTEM, ["--symlink", mount.symlink_target, mount.path])
    if plan.home_tmpfs:
        add(plan.home_tmpfs, _HOME, ["--tmpfs", plan.home_tmpfs])
    for bind in plan.binds:
        add(bind.dest, _SPEC, ["--bind" if bind.writable else "--ro-bind", bind.source, bind.dest])
    for alias, target in plan.aliases:
        add(alias, _ALIAS, ["--symlink", target, alias])
    for path in plan.hidden:
        add(path, _HIDE, ["--tmpfs", path])
        add(path, _HIDE_RO, ["--remount-ro", path])
    return [option for _, option in sorted(operations, key=lambda operation: operation[0])]


def build_options(plan: SandboxPlan, *, mask_env: bool = False) -> list[list[str]]:
    """Return the bwrap options grouped per option, ending with ``-- PROGRAM ARGS``."""
    options: list[list[str]] = [[namespace] for namespace in NAMESPACES]
    if not plan.network:
        options.append(["--unshare-net"])
    options.append(["--die-with-parent"])
    options.extend(_filesystem_options(plan))
    options.append(["--clearenv"])
    for name in sorted(plan.env):
        options.append(["--setenv", name, MASK if mask_env else plan.env[name]])
    options.append(["--chdir", plan.chdir])
    options.append(["--", *plan.argv])
    return options


def build_command(plan: SandboxPlan, bwrap: str = "bwrap") -> list[str]:
    """Return the complete bwrap command line (FR-20 to FR-22, FR-57)."""
    return [bwrap, *(arg for option in build_options(plan) for arg in option)]


def format_command(plan: SandboxPlan, bwrap: str = "bwrap") -> str:
    """Return the command line for display: one option per line, environment values masked."""
    lines = [shlex.quote(bwrap)] + [
        "    " + shlex.join(option) for option in build_options(plan, mask_env=True)
    ]
    return " \\\n".join(lines)
