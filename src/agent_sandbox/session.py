"""The ``run`` subcommand: from parsed options to a running sandbox (section 6)."""

import os
import shutil
import signal
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from types import FrameType

from agent_sandbox.bwrap import SandboxPlan, build_command, format_command, plan_sandbox
from agent_sandbox.deps import check_user_namespaces, find_bwrap, find_overlay_tools
from agent_sandbox.envspec import build_environment, resolve_env_args
from agent_sandbox.errors import ProgramNotFoundError, Terminated
from agent_sandbox.layout import resolve_layout, system_mounts
from agent_sandbox.mounts import MountSpec, plan_mounts
from agent_sandbox.options import MountMode, RunOptions
from agent_sandbox.overlay import describe_mount, open_jail
from agent_sandbox.programs import find_program, invisible_path_entries
from agent_sandbox.seccomp import tty_injection_filter
from agent_sandbox.state import bind_source, jail_dir, state_dir_location
from agent_sandbox.supervisor import supervise

Log = Callable[[str], None]


@dataclass(frozen=True)
class Prepared:
    """A validated ``run``: the sandbox plan and the jails it needs."""

    plan: SandboxPlan
    jails: tuple[MountSpec, ...]
    state_dir: str


def logical_cwd(cwd: str, env: Mapping[str, str]) -> str | None:
    """Return ``$PWD`` if it names the same directory as ``cwd`` (it may contain symlinks)."""
    pwd = env.get("PWD", "")
    if os.path.isabs(pwd) and os.path.realpath(pwd) == cwd:
        return os.path.normpath(pwd)
    return None


def prepare(
    options: RunOptions, *, host_env: Mapping[str, str], cwd: str, warn: Log, debug: Log
) -> Prepared:
    """Validate everything and plan the sandbox, without creating or starting anything."""
    env = build_environment(host_env, resolve_env_args(options.env, warn))
    layout = resolve_layout(options.layout)
    system = system_mounts(layout)
    state_dir = state_dir_location(options.state_dir, host_env)
    debug(f"layout: {layout}")
    debug(f"state directory: {state_dir}")
    mounts = plan_mounts(
        options.mounts,
        cwd=cwd,
        system_paths=[mount.path for mount in system],
        state_dir=state_dir,
    )
    jails = tuple(spec for spec in mounts if spec.mode is MountMode.JAILED)
    plan = plan_sandbox(
        system_mounts=system,
        mounts=mounts,
        jail_sources={
            spec.path: bind_source(jail_dir(state_dir, spec.path), spec) for spec in jails
        },
        argv=(options.program, *options.args),
        env=env,
        cwd=cwd,
        logical_cwd=logical_cwd(cwd, host_env),
        state_dir=state_dir,
        # Jails create the state directory before bwrap starts; it must be hidden then too.
        state_dir_exists=os.path.isdir(state_dir) or bool(jails),
        chdir=options.chdir,
        network=options.network,
    )
    missing = invisible_path_entries(plan)
    if missing:
        warn(f"PATH entries not visible in the sandbox (add them with -r): {', '.join(missing)}")
    debug(f"working directory: {plan.chdir}")
    return Prepared(plan, jails, state_dir)


def check_program(plan: SandboxPlan, debug: Log, *, mounted: bool) -> None:
    """Fail with exit 127 if the program does not exist in the sandbox (FR-9).

    With ``mounted`` overlays, paths inside jails are checked in the merged view.
    """
    program = find_program(plan, plan.host_path) if mounted else find_program(plan)
    if program is None:
        raise ProgramNotFoundError(f"{plan.argv[0]}: command not found in the sandbox")
    debug(f"program: {program}")


@contextmanager
def terminate_on_signals() -> Iterator[None]:
    """Turn ``SIGTERM`` and ``SIGHUP`` into ``Terminated``, so cleanup runs (FR-61)."""

    def handler(signum: int, frame: FrameType | None) -> None:
        del frame
        raise Terminated(signum)

    previous = {
        signum: signal.signal(signum, handler) for signum in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        yield
    finally:
        for signum, old_handler in previous.items():
            signal.signal(signum, old_handler)


def run(
    options: RunOptions,
    *,
    warn: Log,
    debug: Log,
    host_env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> int:
    """Run the program in the sandbox and return its exit code (FR-60)."""
    host_env = os.environ if host_env is None else host_env
    cwd = os.getcwd() if cwd is None else cwd
    prepared = prepare(options, host_env=host_env, cwd=cwd, warn=warn, debug=debug)
    plan = prepared.plan
    path = host_env.get("PATH")
    if options.dry_run:
        check_program(plan, debug, mounted=False)
        fuse_overlayfs = shutil.which("fuse-overlayfs", path=path) or "fuse-overlayfs"
        for spec in prepared.jails:
            jail = jail_dir(prepared.state_dir, spec.path)
            print(describe_mount(spec, jail, fuse_overlayfs))
        print(format_command(plan, shutil.which("bwrap", path=path) or "bwrap"))
        return 0
    bwrap = find_bwrap(path)
    check_user_namespaces(bwrap)
    tools = find_overlay_tools(path) if prepared.jails else None
    with terminate_on_signals(), ExitStack() as stack:
        if tools is not None:
            for spec in prepared.jails:
                stack.enter_context(open_jail(spec, prepared.state_dir, tools, debug))
        check_program(plan, debug, mounted=True)
        seccomp = tty_injection_filter()
        if seccomp is None:
            debug("no seccomp filter against TIOCSTI/TIOCLINUX for this architecture")
        else:
            debug("seccomp filter: TIOCSTI and TIOCLINUX fail with EPERM (--seccomp FD)")
        debug(f"bwrap command:\n{format_command(plan, bwrap)}")
        return supervise(build_command(plan, bwrap), host_env, seccomp)
