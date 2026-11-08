"""The optional configuration file with named profiles (section 5.7)."""

import os
import re
import stat
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from agent_sandbox.errors import AgentSandboxError, UsageError
from agent_sandbox.options import (
    EnvArg,
    EnvArgKind,
    LayoutChoice,
    MountArg,
    MountMode,
    ProfilesAction,
    ProfilesOptions,
    RunOptions,
)

CONFIG_VARIABLE = "AGENT_SANDBOX_CONFIG"
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

Log = Callable[[str], None]

EXAMPLE_CONFIG = """\
# agent-sandbox configuration: named profiles for `agent-sandbox run --profile NAME`.
#
# Profiles only apply when you select them; without --profile nothing here is used.
# Several profiles can be combined (--profile a --profile b, or extends = [...]):
# lists add up, single values and the command are taken from the last one that sets
# them, and options on the command line come last.
#
# Paths: "~" is your home directory; relative paths are relative to the directory
# you start agent-sandbox in, so "." is the project you are working on.
#
# Keys (all optional):
#   description = "text"            shown by `agent-sandbox profiles`
#   extends     = ["other"]          profiles applied before this one
#   ro / rw / jail = ["path", ...]   like -r / -w / -j
#   env         = ["NAME", "NAME=VALUE"]  like --env (keep this file private: chmod 600)
#   env-file    = ["path"]           like --env-file
#   network     = true | false       false like --no-network
#   chdir       = "dir"              like --chdir
#   layout      = "auto" | "fhs" | "nixos"
#   state-dir   = "dir"              like --state-dir (diff/merge then need it too)
#   command     = ["program", "arg"] used when `run` gets no PROGRAM

[profiles.example]
description = "Example: the current directory jailed, a login shell, no network"
jail = ["."]
network = false
command = ["bash", "-l"]

# [profiles.claude]
# description = "Claude Code on the current project"
# extends = ["example"]
# rw = ["~/.claude", "~/.claude.json"]
# env = ["ANTHROPIC_API_KEY"]
# network = true
# command = ["claude"]
"""


@dataclass(frozen=True)
class Profile:  # pylint: disable=too-many-instance-attributes
    """One ``[profiles.NAME]`` table (FR-112); one field per key."""

    name: str
    description: str = ""
    extends: tuple[str, ...] = ()
    ro: tuple[str, ...] = ()
    rw: tuple[str, ...] = ()
    jail: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    env_files: tuple[str, ...] = ()
    network: bool | None = None
    chdir: str | None = None
    layout: LayoutChoice | None = None
    state_dir: str | None = None
    command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Config:
    """A loaded configuration file; ``exists`` is false for a missing default file."""

    path: str
    exists: bool
    profiles: Mapping[str, Profile] = field(default_factory=dict)


def config_location(option: str | None, env: Mapping[str, str]) -> tuple[str, bool]:
    """Return the configuration file path and whether it was named explicitly (FR-110)."""
    if option:
        return os.path.abspath(os.path.expanduser(option)), True
    if env.get(CONFIG_VARIABLE):
        return os.path.abspath(os.path.expanduser(env[CONFIG_VARIABLE])), True
    xdg_config_home = env.get("XDG_CONFIG_HOME", "")
    if os.path.isabs(xdg_config_home):
        base = xdg_config_home
    else:
        base = os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    return os.path.join(base, "agent-sandbox", "config.toml"), False


class _Reader:
    """Validates one profile table, naming the file and profile in errors (FR-112)."""

    def __init__(self, path: str, name: str, table: Mapping[str, Any]) -> None:
        self.path, self.name, self.table = path, name, table

    def error(self, problem: str) -> UsageError:
        """An error about this profile."""
        return UsageError(f"{self.path}: profile {self.name}: {problem}")

    def string(self, key: str) -> str | None:
        """An optional string value."""
        value = self.table.get(key)
        if value is not None and not isinstance(value, str):
            raise self.error(f"'{key}' must be a string")
        return value

    def strings(self, key: str, non_empty: bool = False) -> tuple[str, ...] | None:
        """An optional list of strings."""
        value = self.table.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise self.error(f"'{key}' must be a list of strings, like {key} = [\"...\"]")
        if non_empty and not value:
            raise self.error(f"'{key}' must not be empty")
        return tuple(value)

    def boolean(self, key: str) -> bool | None:
        """An optional boolean."""
        value = self.table.get(key)
        if value is not None and not isinstance(value, bool):
            raise self.error(f"'{key}' must be true or false")
        return value


_KEYS = {
    "description",
    "extends",
    "ro",
    "rw",
    "jail",
    "env",
    "env-file",
    "network",
    "chdir",
    "layout",
    "state-dir",
    "command",
}


def _profile(path: str, name: str, table: Any) -> Profile:
    if not PROFILE_NAME.fullmatch(name):
        raise UsageError(
            f"{path}: invalid profile name {name!r}; names must match {PROFILE_NAME.pattern}"
        )
    if not isinstance(table, dict):
        raise UsageError(f"{path}: profile {name}: must be a table, like [profiles.{name}]")
    reader = _Reader(path, name, table)
    unknown = sorted(set(table) - _KEYS)
    if unknown:
        raise reader.error(f"unknown key '{unknown[0]}'; known keys: {', '.join(sorted(_KEYS))}")
    layout = reader.string("layout")
    if layout is not None and layout not in {choice.value for choice in LayoutChoice}:
        raise reader.error('\'layout\' must be "auto", "fhs" or "nixos"')
    for env_entry in reader.strings("env") or ():
        if not env_entry:
            raise reader.error("'env' entries must not be empty")
    return Profile(
        name=name,
        description=reader.string("description") or "",
        extends=reader.strings("extends") or (),
        ro=reader.strings("ro") or (),
        rw=reader.strings("rw") or (),
        jail=reader.strings("jail") or (),
        env=reader.strings("env") or (),
        env_files=reader.strings("env-file") or (),
        network=reader.boolean("network"),
        chdir=reader.string("chdir"),
        layout=None if layout is None else LayoutChoice(layout),
        state_dir=reader.string("state-dir"),
        command=reader.strings("command", non_empty=True),
    )


def _warn_if_secrets_readable(config: Config, mode: int, warn: Log) -> None:
    """Warn if values in ``env`` entries are readable by group or others (FR-117)."""
    has_values = any("=" in entry for profile in config.profiles.values() for entry in profile.env)
    if has_values and mode & (stat.S_IRGRP | stat.S_IROTH):
        warn(
            f"configuration file {config.path} contains environment values and is readable "
            f"by group or others (mode {stat.S_IMODE(mode):04o}); consider "
            f"'chmod 600 {config.path}'"
        )


def load_config(path: str, explicit: bool, warn: Log) -> Config:
    """Read and validate the configuration file (FR-111, FR-112)."""
    try:
        with open(path, "rb") as file:
            mode = os.fstat(file.fileno()).st_mode
            data = tomllib.load(file)
    except FileNotFoundError as exc:
        if explicit:
            raise UsageError(f"configuration file {path} does not exist") from exc
        return Config(path, exists=False)
    except OSError as exc:
        raise UsageError(f"cannot read the configuration file {path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{path}: invalid TOML: {exc}") from exc
    unknown = sorted(set(data) - {"profiles"})
    if unknown:
        raise UsageError(f"{path}: unknown top-level key '{unknown[0]}'; only [profiles.NAME]")
    tables = data.get("profiles", {})
    if not isinstance(tables, dict):
        raise UsageError(f"{path}: 'profiles' must be a table of profiles")
    config = Config(
        path, True, {name: _profile(path, name, table) for name, table in tables.items()}
    )
    for name in config.profiles:
        expand(config, [name])
    _warn_if_secrets_readable(config, mode, warn)
    return config


def expand(config: Config, names: Sequence[str]) -> list[Profile]:
    """Selected profiles with their ``extends``, depth-first, each once (FR-114)."""
    result: list[Profile] = []

    def visit(name: str, chain: list[str]) -> None:
        if name in chain:
            cycle = " -> ".join([*chain[chain.index(name) :], name])
            raise UsageError(f"{config.path}: profile {name}: 'extends' forms a cycle: {cycle}")
        profile = config.profiles.get(name)
        if profile is None:
            raise _unknown_profile(config, name, chain[-1] if chain else None)
        for parent in profile.extends:
            visit(parent, [*chain, name])
        if profile not in result:
            result.append(profile)

    for name in names:
        visit(name, [])
    return result


def _unknown_profile(config: Config, name: str, extended_by: str | None) -> UsageError:
    if extended_by is not None:
        return UsageError(
            f"{config.path}: profile {extended_by}: 'extends' names unknown profile {name}"
        )
    if not config.exists:
        return UsageError(
            f"unknown profile {name}: there is no configuration file at {config.path} "
            "(create one with 'agent-sandbox profiles init')"
        )
    defined = ", ".join(sorted(config.profiles)) or "none"
    return UsageError(f"unknown profile {name}; profiles in {config.path}: {defined}")


def apply_profiles(options: RunOptions, profiles: Sequence[Profile]) -> RunOptions:
    """Layer the profiles, then the command line (FR-113, FR-115)."""
    mounts: list[MountArg] = []
    env: list[EnvArg] = []
    settings: dict[str, Any] = {}
    command: tuple[str, ...] | None = None
    for profile in profiles:
        source = f"profile {profile.name}"
        for mode, paths in (
            (MountMode.READ_ONLY, profile.ro),
            (MountMode.WRITABLE, profile.rw),
            (MountMode.JAILED, profile.jail),
        ):
            mounts += [MountArg(mode, path, source) for path in paths]
        env += [EnvArg(EnvArgKind.FILE, path, source) for path in profile.env_files]
        env += [EnvArg(EnvArgKind.ENTRY, entry, source) for entry in profile.env]
        for key in ("network", "chdir", "layout", "state_dir"):
            if getattr(profile, key) is not None:
                settings[key] = getattr(profile, key)
        command = profile.command or command
    for key in ("network", "chdir", "layout", "state_dir"):
        if getattr(options, key) is not None:
            settings[key] = getattr(options, key)
    program, args = options.program, options.args
    if program is None and command is not None:
        program, args = command[0], command[1:]
    return replace(
        options,
        program=program,
        args=tuple(args),
        mounts=(*mounts, *options.mounts),
        env=(*env, *options.env),
        network=settings.get("network"),
        chdir=settings.get("chdir"),
        layout=settings.get("layout"),
        state_dir=settings.get("state_dir"),
    )


def resolve_run(
    options: RunOptions, host_env: Mapping[str, str], warn: Log, debug: Log
) -> RunOptions:
    """Apply the selected profiles; without ``--profile`` the file is not read (FR-111)."""
    if not options.profiles:
        return options
    path, explicit = config_location(options.config, host_env)
    config = load_config(path, explicit, warn)
    profiles = expand(config, options.profiles)
    debug(f"configuration file: {path}")
    debug(f"profiles applied in this order: {', '.join(profile.name for profile in profiles)}")
    resolved = apply_profiles(options, profiles)
    if resolved.program is None:
        raise UsageError("no PROGRAM given, and none of the selected profiles has a 'command'")
    return resolved


def init_config(path: str) -> None:
    """Write the example file with mode 0600, never overwriting (FR-119)."""
    missing = []
    directory = os.path.dirname(path)
    while directory and not os.path.exists(directory):
        missing.append(directory)
        directory = os.path.dirname(directory)
    try:
        for directory in reversed(missing):
            os.mkdir(directory, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AgentSandboxError(f"{path} already exists; it was not changed") from exc
    except OSError as exc:
        raise AgentSandboxError(f"cannot create {exc.filename}: {exc.strerror}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(EXAMPLE_CONFIG)


def profiles_command(
    options: ProfilesOptions, out: Log, warn: Log, host_env: Mapping[str, str]
) -> int:
    """``agent-sandbox profiles [list|init]`` (FR-118, FR-119)."""
    path, explicit = config_location(options.config, host_env)
    if options.action is ProfilesAction.INIT:
        init_config(path)
        out(f"Created {path}\n")
        return 0
    config = load_config(path, explicit, warn)
    if not config.exists:
        warn(f"no configuration file at {path}; create one with 'agent-sandbox profiles init'")
        return 0
    out(
        "".join(
            f"{name}\t{config.profiles[name].description}\n" for name in sorted(config.profiles)
        )
    )
    return 0
