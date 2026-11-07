"""Environment handling for ``run``: ``--env``, ``--env-file`` and the baseline (section 6.4)."""

import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from agent_sandbox.errors import UsageError
from agent_sandbox.options import EnvArg, EnvArgKind

BASELINE = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "TZ")

MASK = "***"

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

Warn = Callable[[str], None]


@dataclass(frozen=True)
class EnvEntry:
    """One environment entry; ``value`` is ``None`` to pass the host value of ``name``."""

    name: str
    value: str | None = None


@dataclass(frozen=True)
class EnvSpec:
    """The environment request of a ``run`` invocation.

    ``restricted`` is true as soon as any ``--env`` or ``--env-file`` is given (FR-51),
    even if no entries result from it.
    """

    restricted: bool
    entries: tuple[EnvEntry, ...] = ()


def parse_entry(text: str, source: str) -> EnvEntry:
    """Parse ``NAME`` or ``NAME=VALUE`` (FR-53); ``source`` names the origin for errors."""
    name, separator, value = text.partition("=")
    if not _NAME.fullmatch(name):
        shown = f" '{name}'" if separator else ""
        raise UsageError(
            f"{source}: invalid variable name{shown}; names must match {_NAME.pattern}"
        )
    if not separator:
        return EnvEntry(name)
    if "\0" in value:
        raise UsageError(f"{source}: the value of {name} contains a NUL character")
    return EnvEntry(name, value)


def parse_env_file_text(text: str, filename: str) -> list[EnvEntry]:
    """Parse the contents of an ``--env-file`` (FR-54)."""
    entries: list[EnvEntry] = []
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.removesuffix("\r").lstrip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            line = line.rstrip()
        entries.append(parse_entry(line, f"{filename}, line {number}"))
    return entries


def read_env_file(path: str, warn: Warn) -> list[EnvEntry]:
    """Read and parse an ``--env-file``; warn if others can read it (FR-54, FR-58)."""
    try:
        with open(path, "rb") as file:
            mode = os.fstat(file.fileno()).st_mode
            data = file.read()
    except OSError as exc:
        raise UsageError(f"cannot read env file {path}: {exc.strerror}") from exc
    if stat.S_ISREG(mode) and mode & (stat.S_IRGRP | stat.S_IROTH):
        warn(
            f"env file {path} is readable by group or others "
            f"(mode {stat.S_IMODE(mode):04o}); consider 'chmod 600 {path}'"
        )
    return parse_env_file_text(os.fsdecode(data), path)


def resolve_env_args(args: Sequence[EnvArg], warn: Warn) -> EnvSpec:
    """Turn ``--env``/``--env-file`` arguments into entries, keeping their order (FR-55)."""
    entries: list[EnvEntry] = []
    env_count = 0
    for arg in args:
        if arg.kind is EnvArgKind.ENTRY:
            env_count += 1
            entries.append(parse_entry(arg.value, f"--env #{env_count}"))
        else:
            entries.extend(read_env_file(arg.value, warn))
    return EnvSpec(restricted=bool(args), entries=tuple(entries))


def build_environment(host_env: Mapping[str, str], spec: EnvSpec) -> dict[str, str]:
    """Compute the sandbox environment from the host environment (FR-50 to FR-56)."""
    if not spec.restricted:
        return dict(host_env)
    env = {name: host_env[name] for name in BASELINE if name in host_env}
    for entry in spec.entries:
        value = host_env.get(entry.name) if entry.value is None else entry.value
        if value is None:
            env.pop(entry.name, None)
        else:
            env[entry.name] = value
    return env


def mask_values(env: Mapping[str, str]) -> dict[str, str]:
    """Return ``env`` with every value replaced by ``***`` for display (FR-58)."""
    return dict.fromkeys(env, MASK)
