"""The ``run`` subcommand: from parsed options to a running sandbox (section 6)."""

import os
import shutil
from collections.abc import Callable, Mapping

from agent_sandbox.bwrap import SandboxPlan, build_command, format_command, plan_sandbox
from agent_sandbox.deps import check_user_namespaces, find_bwrap
from agent_sandbox.envspec import build_environment, resolve_env_args
from agent_sandbox.errors import ProgramNotFoundError
from agent_sandbox.layout import resolve_layout, system_mounts
from agent_sandbox.mounts import plan_mounts
from agent_sandbox.options import RunOptions
from agent_sandbox.programs import find_program, invisible_path_entries
from agent_sandbox.state import state_dir_location
from agent_sandbox.supervisor import supervise

Log = Callable[[str], None]


def logical_cwd(cwd: str, env: Mapping[str, str]) -> str | None:
    """Return ``$PWD`` if it names the same directory as ``cwd`` (it may contain symlinks)."""
    pwd = env.get("PWD", "")
    if os.path.isabs(pwd) and os.path.realpath(pwd) == cwd:
        return os.path.normpath(pwd)
    return None


def prepare(
    options: RunOptions, *, host_env: Mapping[str, str], cwd: str, warn: Log, debug: Log
) -> SandboxPlan:
    """Validate everything and return the sandbox plan, without starting anything."""
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
    plan = plan_sandbox(
        system_mounts=system,
        mounts=mounts,
        argv=(options.program, *options.args),
        env=env,
        cwd=cwd,
        logical_cwd=logical_cwd(cwd, host_env),
        state_dir=state_dir,
        state_dir_exists=os.path.isdir(state_dir),
        chdir=options.chdir,
        network=options.network,
    )
    missing = invisible_path_entries(plan)
    if missing:
        warn(f"PATH entries not visible in the sandbox (add them with -r): {', '.join(missing)}")
    program = find_program(plan)
    if program is None:
        raise ProgramNotFoundError(f"{options.program}: command not found in the sandbox")
    debug(f"program: {program}")
    debug(f"working directory: {plan.chdir}")
    return plan


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
    plan = prepare(options, host_env=host_env, cwd=cwd, warn=warn, debug=debug)
    if options.dry_run:
        bwrap = shutil.which("bwrap", path=host_env.get("PATH")) or "bwrap"
        print(format_command(plan, bwrap))
        return 0
    bwrap = find_bwrap(host_env.get("PATH"))
    check_user_namespaces(bwrap)
    debug(f"bwrap command:\n{format_command(plan, bwrap)}")
    return supervise(build_command(plan, bwrap), host_env)
