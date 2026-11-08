"""Tests for the configuration file and profiles (section 5.7, FR-110 to FR-119)."""

import os
import stat
from pathlib import Path

import pytest

from agent_sandbox.config import (
    EXAMPLE_CONFIG,
    Config,
    Profile,
    apply_profiles,
    config_location,
    expand,
    init_config,
    load_config,
    profiles_command,
    resolve_run,
)
from agent_sandbox.errors import AgentSandboxError, ExitCode, UsageError
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

RO, RW, JAIL = MountMode.READ_ONLY, MountMode.WRITABLE, MountMode.JAILED


class Log(list[str]):
    """Collects messages."""

    def __call__(self, message: str) -> None:
        self.append(message)


def write_config(tmp_path: Path, text: str, mode: int = 0o600) -> str:
    """Write a configuration file and return its path."""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return str(path)


def load(tmp_path: Path, text: str) -> Config:
    """Load a configuration text."""
    return load_config(write_config(tmp_path, text), True, Log())


def config_error(tmp_path: Path, text: str) -> str:
    """Load a broken configuration and return the error message."""
    with pytest.raises(UsageError) as info:
        load(tmp_path, text)
    assert info.value.exit_code is ExitCode.USAGE
    return str(info.value)


def run_options(**changes: object) -> RunOptions:
    """Parsed ``run`` options with nothing given."""
    values: dict[str, object] = {
        "program": None,
        "args": (),
        "mounts": (),
        "env": (),
        "network": None,
        "chdir": None,
        "layout": None,
        "state_dir": None,
        "dry_run": False,
        "debug": False,
    }
    values.update(changes)
    return RunOptions(**values)  # type: ignore[arg-type]


# --- location (FR-110) ---


@pytest.mark.parametrize(
    ("option", "env", "expected"),
    [
        ("/etc/x.toml", {"AGENT_SANDBOX_CONFIG": "/env.toml"}, ("/etc/x.toml", True)),
        (None, {"AGENT_SANDBOX_CONFIG": "/env.toml"}, ("/env.toml", True)),
        (
            None,
            {"XDG_CONFIG_HOME": "/xdg", "HOME": "/h"},
            ("/xdg/agent-sandbox/config.toml", False),
        ),
        (
            None,
            {"XDG_CONFIG_HOME": "rel", "HOME": "/h"},
            ("/h/.config/agent-sandbox/config.toml", False),
        ),
        (None, {"HOME": "/h"}, ("/h/.config/agent-sandbox/config.toml", False)),
    ],
)
def test_fr110_location(
    option: str | None, env: dict[str, str], expected: tuple[str, bool]
) -> None:
    """FR-110: --config, then $AGENT_SANDBOX_CONFIG, then XDG (absolute), then ~/.config."""
    assert config_location(option, env) == expected


def test_fr110_tilde_and_relative_option(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """FR-110: ``--config=~/x`` and relative paths work."""
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.chdir(tmp_path)
    assert config_location("~/c.toml", {})[0] == "/home/someone/c.toml"
    assert config_location("c.toml", {})[0] == str(tmp_path / "c.toml")


# --- loading and validation (FR-111, FR-112) ---


def test_fr111_missing_default_file_means_no_profiles(tmp_path: Path) -> None:
    """FR-111: no file at the default location is not an error."""
    config = load_config(str(tmp_path / "missing.toml"), False, Log())
    assert config == Config(str(tmp_path / "missing.toml"), exists=False)


def test_fr111_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    """FR-111: a file named with --config or the variable must exist."""
    with pytest.raises(UsageError, match="configuration file .* does not exist"):
        load_config(str(tmp_path / "missing.toml"), True, Log())


def test_fr112_full_profile(tmp_path: Path) -> None:
    """FR-112: every key is read with its type."""
    config = load(
        tmp_path,
        """
[profiles.full]
description = "All keys"
extends = []
ro = ["~/a"]
rw = ["b"]
jail = ["."]
env = ["A", "B=1"]
env-file = ["e.env"]
network = false
chdir = "sub"
layout = "fhs"
state-dir = "~/state"
command = ["prog", "--flag"]
""",
    )
    assert config.profiles["full"] == Profile(
        name="full",
        description="All keys",
        ro=("~/a",),
        rw=("b",),
        jail=(".",),
        env=("A", "B=1"),
        env_files=("e.env",),
        network=False,
        chdir="sub",
        layout=LayoutChoice.FHS,
        state_dir="~/state",
        command=("prog", "--flag"),
    )


def test_fr112_empty_file_and_empty_profile(tmp_path: Path) -> None:
    """FR-112: an empty file and an empty profile are valid."""
    assert not load(tmp_path, "").profiles
    assert load(tmp_path, "[profiles.empty]\n").profiles["empty"] == Profile("empty")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[profiles.a\n", "invalid TOML"),
        ("version = 1\n", "unknown top-level key 'version'"),
        ("profiles = 1\n", "'profiles' must be a table"),
        ('[profiles."bad name"]\n', "invalid profile name 'bad name'"),
        ("[profiles]\na = 1\n", "profile a: must be a table"),
        ("[profiles.a]\nreadonly = []\n", "profile a: unknown key 'readonly'"),
        ('[profiles.a]\nro = "~/x"\n', "profile a: 'ro' must be a list of strings"),
        ("[profiles.a]\nenv = [1]\n", "profile a: 'env' must be a list of strings"),
        ('[profiles.a]\nenv = [""]\n', "profile a: 'env' entries must not be empty"),
        ('[profiles.a]\nnetwork = "no"\n', "profile a: 'network' must be true or false"),
        ("[profiles.a]\nchdir = 1\n", "profile a: 'chdir' must be a string"),
        ('[profiles.a]\nlayout = "guix"\n', "profile a: 'layout' must be"),
        ("[profiles.a]\ncommand = []\n", "profile a: 'command' must not be empty"),
        ('[profiles.a]\nextends = ["b"]\n', "profile a: 'extends' names unknown profile b"),
        (
            '[profiles.a]\nextends = ["b"]\n[profiles.b]\nextends = ["a"]\n',
            "'extends' forms a cycle: a -> b -> a",
        ),
        ('[profiles.a]\nextends = ["a"]\n', "'extends' forms a cycle: a -> a"),
    ],
)
def test_fr112_validation_errors(tmp_path: Path, text: str, message: str) -> None:
    """FR-112: every kind of error names the file, the profile and the problem (exit 2)."""
    error = config_error(tmp_path, text)
    assert error.startswith(str(tmp_path / "config.toml"))
    assert message in error


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
def test_fr111_unreadable_file(tmp_path: Path) -> None:
    """FR-111: an unreadable file is a usage error."""
    path = write_config(tmp_path, "", 0o000)
    with pytest.raises(UsageError, match="cannot read the configuration file .*Permission denied"):
        load_config(path, False, Log())


# --- selection and layering (FR-113 to FR-115) ---

LAYERS = """
[profiles.base]
jail = ["."]
ro = ["~/.gitconfig"]
env-file = ["base.env"]
env = ["A=base"]
network = false
layout = "fhs"
command = ["base-cmd"]

[profiles.tool]
extends = ["base"]
rw = ["~/.tool"]
env = ["A=tool", "B"]
chdir = "sub"
command = ["tool", "--x"]

[profiles.other]
extends = ["base"]
ro = ["/data"]
network = true
"""


def test_fr114_extends_first_and_each_profile_once(tmp_path: Path) -> None:
    """FR-114: depth-first, extends before the profile; duplicates only at first position."""
    config = load(tmp_path, LAYERS)
    assert [p.name for p in expand(config, ["tool"])] == ["base", "tool"]
    assert [p.name for p in expand(config, ["tool", "other"])] == ["base", "tool", "other"]
    assert [p.name for p in expand(config, ["base", "tool", "base"])] == ["base", "tool"]


@pytest.mark.parametrize(
    ("exists", "message"),
    [
        (True, "unknown profile nope; profiles in .*config.toml: base, other, tool"),
        (False, "unknown profile nope: there is no configuration file at .* profiles init"),
    ],
)
def test_fr114_unknown_profile(tmp_path: Path, exists: bool, message: str) -> None:
    """FR-114: unknown names list the defined profiles, or say how to create the file."""
    config = load(tmp_path, LAYERS) if exists else Config(str(tmp_path / "config.toml"), False)
    with pytest.raises(UsageError, match=message):
        expand(config, ["nope"])


def test_fr115_layering_profiles_then_command_line(tmp_path: Path) -> None:
    """FR-113, FR-115: lists add up in order, single values and command: last one wins."""
    config = load(tmp_path, LAYERS)
    resolved = apply_profiles(run_options(), expand(config, ["tool"]))
    assert resolved.mounts == (
        MountArg(RO, "~/.gitconfig", "profile base"),
        MountArg(JAIL, ".", "profile base"),
        MountArg(RW, "~/.tool", "profile tool"),
    )
    assert resolved.env == (
        EnvArg(EnvArgKind.FILE, "base.env", "profile base"),
        EnvArg(EnvArgKind.ENTRY, "A=base", "profile base"),
        EnvArg(EnvArgKind.ENTRY, "A=tool", "profile tool"),
        EnvArg(EnvArgKind.ENTRY, "B", "profile tool"),
    )
    assert (resolved.network, resolved.layout, resolved.chdir) == (False, LayoutChoice.FHS, "sub")
    assert (resolved.program, resolved.args) == ("tool", ("--x",))


def test_fr115_command_line_comes_last(tmp_path: Path) -> None:
    """FR-115: command-line options follow the profiles and override single values."""
    config = load(tmp_path, LAYERS)
    options = run_options(
        program="mine",
        args=("-v",),
        mounts=(MountArg(RW, "~/.gitconfig"),),
        env=(EnvArg(EnvArgKind.ENTRY, "A=cli"),),
        network=True,
        layout=LayoutChoice.NIXOS,
        state_dir="/state",
    )
    resolved = apply_profiles(options, expand(config, ["tool"]))
    assert resolved.mounts[-1] == MountArg(RW, "~/.gitconfig")
    assert resolved.env[-1] == EnvArg(EnvArgKind.ENTRY, "A=cli")
    assert (resolved.network, resolved.layout, resolved.chdir) == (True, LayoutChoice.NIXOS, "sub")
    assert resolved.state_dir == "/state"
    assert (resolved.program, resolved.args) == ("mine", ("-v",))


def test_fr115_later_profile_overrides_earlier(tmp_path: Path) -> None:
    """FR-115: of two profiles, the later one's values win; commands come from the last."""
    config = load(tmp_path, LAYERS)
    resolved = apply_profiles(run_options(), expand(config, ["tool", "other"]))
    assert resolved.network is True
    assert resolved.program == "tool"


def test_fr111_without_profiles_the_file_is_not_read(tmp_path: Path) -> None:
    """FR-111, AC-20: a broken configuration file does not matter without --profile."""
    broken = write_config(tmp_path, "this is not toml")
    options = run_options(program="true", config=broken)
    assert resolve_run(options, {}, Log(), Log()) == options


def test_fr115_resolve_run(tmp_path: Path) -> None:
    """FR-115: resolve_run applies the profiles and reports them with --debug."""
    path = write_config(tmp_path, LAYERS)
    debug = Log()
    resolved = resolve_run(run_options(profiles=("tool",), config=path), {}, Log(), debug)
    assert resolved.program == "tool"
    assert debug == [f"configuration file: {path}", "profiles applied in this order: base, tool"]


def test_fr8_no_program_and_no_command(tmp_path: Path) -> None:
    """FR-8: without PROGRAM, some profile must have a command."""
    path = write_config(tmp_path, "[profiles.a]\njail = ['.']\n")
    with pytest.raises(UsageError, match="no PROGRAM given, and none of the selected profiles"):
        resolve_run(run_options(profiles=("a",), config=path), {}, Log(), Log())


# --- secrets (FR-117) ---


@pytest.mark.parametrize(
    ("text", "mode", "warned"),
    [
        ("[profiles.a]\nenv = ['TOKEN=x']\n", 0o644, True),
        ("[profiles.a]\nenv = ['TOKEN=x']\n", 0o600, False),
        ("[profiles.a]\nenv = ['TOKEN']\n", 0o644, False),
    ],
)
def test_fr117_readable_file_with_values_warns(
    tmp_path: Path, text: str, mode: int, warned: bool
) -> None:
    """FR-117: only values readable by others are worth a warning; values are not shown."""
    warnings = Log()
    load_config(write_config(tmp_path, text, mode), True, warnings)
    assert bool(warnings) is warned
    assert all(
        "TOKEN=x" not in warning and "contains environment values" in warning
        for warning in warnings
    )


# --- profiles list and init (FR-118, FR-119) ---


def test_fr118_list(tmp_path: Path) -> None:
    """FR-118: one line per profile, sorted, NAME<TAB>DESCRIPTION."""
    path = write_config(tmp_path, '[profiles.b]\ndescription = "Bee"\n[profiles.a]\n')
    out = Log()
    options = ProfilesOptions(ProfilesAction.LIST, path, False)
    assert profiles_command(options, out, Log(), {}) == 0
    assert out == ["a\t\nb\tBee\n"]


def test_fr118_list_without_file(tmp_path: Path) -> None:
    """FR-118: no file prints nothing to stdout and explains on stderr, exit 0."""
    out, warnings = Log(), Log()
    options = ProfilesOptions(ProfilesAction.LIST, None, False)
    assert profiles_command(options, out, warnings, {"HOME": str(tmp_path)}) == 0
    assert not out
    assert warnings == [
        f"no configuration file at {tmp_path}/.config/agent-sandbox/config.toml; "
        "create one with 'agent-sandbox profiles init'"
    ]


def test_fr119_init_writes_a_valid_private_example(tmp_path: Path) -> None:
    """FR-119: init creates parents (0700) and the file (0600); the example is valid."""
    path = tmp_path / "new" / "dir" / "config.toml"
    out = Log()
    options = ProfilesOptions(ProfilesAction.INIT, str(path), False)
    assert profiles_command(options, out, Log(), {}) == 0
    assert out == [f"Created {path}\n"]
    assert path.read_text(encoding="utf-8") == EXAMPLE_CONFIG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for directory in (path.parent, path.parent.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    config = load_config(str(path), True, Log())
    example = config.profiles["example"]
    assert example.jail == (".",)
    assert example.command == ("bash", "-l")


def test_fr119_init_never_overwrites(tmp_path: Path) -> None:
    """FR-119: an existing file is kept and init fails with exit 1."""
    path = write_config(tmp_path, "# mine\n")
    with pytest.raises(AgentSandboxError, match="already exists; it was not changed") as info:
        init_config(path)
    assert info.value.exit_code is ExitCode.ERROR
    assert Path(path).read_text(encoding="utf-8") == "# mine\n"
