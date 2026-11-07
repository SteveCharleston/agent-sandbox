"""Tests for environment handling (section 6.4, AC-11, AC-11b)."""

import os
from pathlib import Path

import pytest

from agent_sandbox.cli import main
from agent_sandbox.envspec import (
    BASELINE,
    EnvEntry,
    EnvSpec,
    build_environment,
    mask_values,
    parse_entry,
    parse_env_file_text,
    read_env_file,
    resolve_env_args,
)
from agent_sandbox.errors import ExitCode, UsageError
from agent_sandbox.options import EnvArg, EnvArgKind

HOST_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/u",
    "USER": "u",
    "TERM": "xterm",
    "LANG": "C.UTF-8",
    "FOO": "host",
    "BAR": "2",
    "SECRET": "s3cr3t",
}

HOST_BASELINE = {name: HOST_ENV[name] for name in BASELINE if name in HOST_ENV}


class Warnings(list[str]):
    """Collects warnings passed to the ``warn`` callback."""

    def __call__(self, message: str) -> None:
        self.append(message)


def env_arg(value: str) -> EnvArg:
    """Return an ``--env`` argument."""
    return EnvArg(EnvArgKind.ENTRY, value)


def env_file_arg(path: Path) -> EnvArg:
    """Return an ``--env-file`` argument."""
    return EnvArg(EnvArgKind.FILE, str(path))


def write_env_file(path: Path, content: str | bytes, mode: int = 0o600) -> Path:
    """Write an env file with the given permissions."""
    if isinstance(content, str):
        content = content.encode()
    path.write_bytes(content)
    path.chmod(mode)
    return path


# --- entry syntax (FR-53) ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FOO", EnvEntry("FOO")),
        ("_foo_1", EnvEntry("_foo_1")),
        ("FOO=bar", EnvEntry("FOO", "bar")),
        ("FOO=", EnvEntry("FOO", "")),
        ("FOO=a=b=c", EnvEntry("FOO", "a=b=c")),
        ("FOO=x # not a comment", EnvEntry("FOO", "x # not a comment")),
        ("FOO= a b ", EnvEntry("FOO", " a b ")),
        ("FOO='quoted'", EnvEntry("FOO", "'quoted'")),
        ('FOO="$HOME\\n"', EnvEntry("FOO", '"$HOME\\n"')),
    ],
)
def test_fr53_entry_syntax_values_are_literal(text: str, expected: EnvEntry) -> None:
    """FR-53: ``NAME`` or ``NAME=VALUE``; only the first ``=`` splits, values are literal."""
    assert parse_entry(text, "--env #1") == expected


@pytest.mark.parametrize("name", ["1BAD", "A-B", "A B", "ÄB", "", "FOO "])
def test_fr53_invalid_name_with_value_shows_name_not_value(name: str) -> None:
    """FR-53, FR-58: invalid names are errors; the name is shown, the value never."""
    with pytest.raises(UsageError) as info:
        parse_entry(f"{name}=s3cr3t", "--env #3")
    assert str(info.value) == (
        f"--env #3: invalid variable name '{name}'; names must match [A-Za-z_][A-Za-z0-9_]*"
    )


@pytest.mark.parametrize("text", ["", "1BAD", "sk-live-0123456789", "A B"])
def test_fr58_invalid_entry_without_equals_is_not_echoed(text: str) -> None:
    """FR-58: a bare invalid entry might be a pasted secret, so it is not echoed."""
    with pytest.raises(UsageError) as info:
        parse_entry(text, "--env #1")
    assert str(info.value) == (
        "--env #1: invalid variable name; names must match [A-Za-z_][A-Za-z0-9_]*"
    )


def test_fr53_nul_in_value_is_an_error() -> None:
    """FR-53: a value with a NUL character cannot be passed on; the value is not shown."""
    with pytest.raises(UsageError) as info:
        parse_entry("FOO=a\0secret", "--env #1")
    assert str(info.value) == "--env #1: the value of FOO contains a NUL character"


def test_usage_error_exit_code() -> None:
    """Section 5.6: invalid entries are usage errors (exit 2)."""
    assert UsageError.exit_code is ExitCode.USAGE
    assert int(ExitCode.USAGE) == 2


# --- env file format (FR-54) ---


def test_fr54_file_format() -> None:
    """FR-54: comments, blank lines and whitespace rules of ``--env-file``."""
    text = (
        "# comment\n"
        "\n"
        "   \t\n"
        "  # indented comment\n"
        "A=from file\n"
        "  B=x=y # kept\n"
        "C=\n"
        "  FOO  \n"
        "D= leading and trailing \n"
        "E=#not a comment"
    )
    assert parse_env_file_text(text, "env.txt") == [
        EnvEntry("A", "from file"),
        EnvEntry("B", "x=y # kept"),
        EnvEntry("C", ""),
        EnvEntry("FOO"),
        EnvEntry("D", " leading and trailing "),
        EnvEntry("E", "#not a comment"),
    ]


def test_fr54_crlf_line_endings() -> None:
    """FR-54: CRLF line endings are accepted; the CR is not part of the value."""
    assert parse_env_file_text("A=1\r\nB\r\n", "env.txt") == [EnvEntry("A", "1"), EnvEntry("B")]


def test_fr54_empty_file() -> None:
    """FR-54: an empty file yields no entries."""
    assert not parse_env_file_text("", "env.txt")


def test_fr53_file_error_reports_file_and_line() -> None:
    """FR-53: errors in an env file name the file and the line number."""
    with pytest.raises(UsageError) as info:
        parse_env_file_text("# comment\n\nA=1\n2BAD=secret\n", "my.env")
    assert str(info.value).startswith("my.env, line 4: invalid variable name '2BAD'")
    assert "secret" not in str(info.value)


def test_fr54_read_file(tmp_path: Path) -> None:
    """FR-54: an env file is read and parsed without warnings if only the owner can read it."""
    path = write_env_file(tmp_path / "env", "A=1\nB\n")
    warnings = Warnings()
    assert read_env_file(str(path), warnings) == [EnvEntry("A", "1"), EnvEntry("B")]
    assert not warnings


def test_fr54_non_utf8_bytes_round_trip(tmp_path: Path) -> None:
    """FR-54: arbitrary bytes in values are passed on unchanged."""
    path = write_env_file(tmp_path / "env", b"A=caf\xe9\n")
    entries = read_env_file(str(path), Warnings())
    assert len(entries) == 1
    assert entries[0].value is not None
    assert os.fsencode(entries[0].value) == b"caf\xe9"


@pytest.mark.parametrize("name", ["missing", "."])
def test_fr54_unreadable_file_is_usage_error(tmp_path: Path, name: str) -> None:
    """FR-54: a file that cannot be read (missing, a directory) is an error (exit 2)."""
    path = tmp_path / name
    with pytest.raises(UsageError) as info:
        read_env_file(str(path), Warnings())
    assert str(info.value).startswith(f"cannot read env file {path}: ")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_fr58_warns_if_file_readable_by_others(tmp_path: Path, mode: int) -> None:
    """FR-58: a group- or world-readable env file triggers a warning without its contents."""
    path = write_env_file(tmp_path / "env", "TOKEN=s3cr3t\n", mode)
    warnings = Warnings()
    read_env_file(str(path), warnings)
    assert warnings == [
        f"env file {path} is readable by group or others "
        f"(mode {mode:04o}); consider 'chmod 600 {path}'"
    ]


# --- resolving arguments in order (FR-51, FR-55) ---


def test_fr55_entries_keep_command_line_order(tmp_path: Path) -> None:
    """FR-55: ``--env`` and ``--env-file`` entries are combined in command-line order."""
    first = write_env_file(tmp_path / "first", "A=1\nB\n")
    second = write_env_file(tmp_path / "second", "C=3\n")
    spec = resolve_env_args(
        [env_arg("X"), env_file_arg(first), env_arg("Y=2"), env_file_arg(second)], Warnings()
    )
    assert spec == EnvSpec(
        restricted=True,
        entries=(
            EnvEntry("X"),
            EnvEntry("A", "1"),
            EnvEntry("B"),
            EnvEntry("Y", "2"),
            EnvEntry("C", "3"),
        ),
    )


def test_fr51_no_arguments_is_unrestricted() -> None:
    """FR-50: without ``--env``/``--env-file`` the environment is not restricted."""
    assert resolve_env_args([], Warnings()) == EnvSpec(restricted=False)


def test_fr51_empty_env_file_still_restricts(tmp_path: Path) -> None:
    """FR-51: an ``--env-file`` restricts the environment even if it has no entries."""
    path = write_env_file(tmp_path / "env", "# nothing\n")
    assert resolve_env_args([env_file_arg(path)], Warnings()) == EnvSpec(restricted=True)


def test_fr58_env_errors_are_numbered_per_env_option(tmp_path: Path) -> None:
    """FR-58: ``--env #N`` counts only ``--env`` options."""
    path = write_env_file(tmp_path / "env", "A=1\n")
    with pytest.raises(UsageError, match=r"^--env #2: "):
        resolve_env_args([env_arg("OK"), env_file_arg(path), env_arg("1BAD")], Warnings())


# --- building the environment (FR-50 to FR-56) ---


def test_fr50_unrestricted_passes_full_environment() -> None:
    """FR-50, AC-11: without entries the full environment is passed as a copy."""
    env = build_environment(HOST_ENV, EnvSpec(restricted=False))
    assert env == HOST_ENV
    assert env is not HOST_ENV


def test_ac11_fr51_restricted_to_named_and_baseline() -> None:
    """AC-11, FR-51, FR-52: ``--env FOO`` passes FOO and the baseline, not BAR."""
    env = build_environment(HOST_ENV, EnvSpec(True, (EnvEntry("FOO"),)))
    assert env == {**HOST_BASELINE, "FOO": "host"}


def test_ac11b_explicit_values() -> None:
    """AC-11b: entries from the file and the command line, last one wins."""
    entries = parse_env_file_text("# comment\nA=from file\nB=x=y # kept\nC=\nFOO\n", "env.txt")
    entries.append(parse_entry("A=cli", "--env #1"))
    env = build_environment(HOST_ENV, EnvSpec(True, tuple(entries)))
    assert env == {**HOST_BASELINE, "A": "cli", "B": "x=y # kept", "C": "", "FOO": "host"}
    assert "BAR" not in env


def test_fr51_restricted_by_explicit_values_only() -> None:
    """FR-51: ``NAME=VALUE`` entries alone also restrict the environment."""
    env = build_environment(HOST_ENV, EnvSpec(True, (EnvEntry("X", "a b"),)))
    assert env == {**HOST_BASELINE, "X": "a b"}


def test_fr53_missing_host_variable_is_not_set() -> None:
    """FR-53: a bare NAME that is unset on the host sets nothing."""
    env = build_environment(HOST_ENV, EnvSpec(True, (EnvEntry("NOPE"),)))
    assert env == HOST_BASELINE


def test_fr52_missing_baseline_variable_is_not_set() -> None:
    """FR-52: baseline variables are passed only if they are set on the host."""
    env = build_environment({"PATH": "/bin", "OTHER": "x"}, EnvSpec(True))
    assert env == {"PATH": "/bin"}


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ((EnvEntry("FOO"), EnvEntry("FOO", "cli")), "cli"),
        ((EnvEntry("FOO", "cli"), EnvEntry("FOO")), "host"),
        ((EnvEntry("FOO", "1"), EnvEntry("FOO", "2")), "2"),
    ],
)
def test_fr55_last_entry_wins(entries: tuple[EnvEntry, ...], expected: str) -> None:
    """FR-55: the last entry for a name wins, in both directions."""
    assert build_environment(HOST_ENV, EnvSpec(True, entries))["FOO"] == expected


def test_fr55_later_bare_name_unset_on_host_removes_value() -> None:
    """FR-53, FR-55: a later bare NAME that is unset on the host leaves NAME unset."""
    entries = (EnvEntry("NOPE", "x"), EnvEntry("NOPE"))
    assert "NOPE" not in build_environment(HOST_ENV, EnvSpec(True, entries))


def test_fr56_explicit_value_overrides_baseline() -> None:
    """FR-56: explicit values may override baseline variables."""
    entries = (EnvEntry("HOME", "/home/sandbox"), EnvEntry("PATH", "/opt/bin"))
    env = build_environment(HOST_ENV, EnvSpec(True, entries))
    assert env["HOME"] == "/home/sandbox"
    assert env["PATH"] == "/opt/bin"


def test_fr57_host_environment_is_not_modified() -> None:
    """FR-57: building the environment never modifies the host mapping."""
    host = dict(HOST_ENV)
    build_environment(host, EnvSpec(True, (EnvEntry("FOO", "changed"), EnvEntry("PATH", "x"))))
    assert host == HOST_ENV


def test_fr58_mask_values() -> None:
    """FR-58: values are replaced by ``***`` for display; names are kept."""
    assert mask_values({"A": "secret", "B": ""}) == {"A": "***", "B": "***"}


# --- command-line integration ---


def test_ac11b_invalid_env_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """AC-11b: ``--env 1BAD=x`` exits with 2 and does not show the value."""
    assert main(["run", "--env", "1BAD=hunter2", "prog"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("agent-sandbox: error: --env #1: invalid variable name '1BAD'")
    assert "hunter2" not in captured.err


def test_fr54_missing_env_file_exits_2(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """FR-54: an unreadable ``--env-file`` exits with 2."""
    path = tmp_path / "missing"
    assert main(["run", "--env-file", str(path), "prog"]) == 2
    assert capsys.readouterr().err == (
        f"agent-sandbox: error: cannot read env file {path}: No such file or directory\n"
    )


def test_fr58_cli_warns_about_readable_env_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """FR-58: the warning goes to stderr with the ``agent-sandbox: warning:`` prefix."""
    path = write_env_file(tmp_path / "env", "A=1\n", 0o644)
    assert main(["run", "--env-file", str(path), "prog"]) == 1
    err = capsys.readouterr().err
    assert f"agent-sandbox: warning: env file {path} is readable by group or others" in err
