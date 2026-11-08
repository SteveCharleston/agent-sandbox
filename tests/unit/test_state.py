"""Tests for the state directory: location, jails, meta.json and locks (section 8)."""

import dataclasses
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from agent_sandbox import __version__
from agent_sandbox.errors import AgentSandboxError, ExitCode, SetupError
from agent_sandbox.mounts import MountSpec, PathKind
from agent_sandbox.options import MountMode
from agent_sandbox.state import (
    META_VERSION,
    JailDir,
    bind_source,
    check_or_write_meta,
    jail_dir,
    jail_id,
    locked,
    lower_dir,
    make_jail_root,
    read_meta,
    state_dir_location,
    write_meta,
)

HOME = "/home/u"


@pytest.mark.parametrize(
    ("option", "env", "expected"),
    [
        (
            "/opt/state",
            {"AGENT_SANDBOX_STATE_DIR": "/env", "XDG_STATE_HOME": "/xdg"},
            "/opt/state",
        ),
        (None, {"AGENT_SANDBOX_STATE_DIR": "/env", "XDG_STATE_HOME": "/xdg"}, "/env"),
        (None, {"AGENT_SANDBOX_STATE_DIR": "", "XDG_STATE_HOME": "/xdg"}, "/xdg/agent-sandbox"),
        (None, {"XDG_STATE_HOME": "relative", "HOME": HOME}, f"{HOME}/.local/state/agent-sandbox"),
        (None, {"XDG_STATE_HOME": "", "HOME": HOME}, f"{HOME}/.local/state/agent-sandbox"),
        (None, {"HOME": HOME}, f"{HOME}/.local/state/agent-sandbox"),
    ],
)
def test_fr80_precedence(option: str | None, env: dict[str, str], expected: str) -> None:
    """FR-80: ``--state-dir``, then the variable, then XDG (absolute only), then the default."""
    assert state_dir_location(option, env) == expected


def test_fr80_relative_option_and_symlinks_are_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-80, FR-39: the location is absolute and resolved, so it compares with mount paths."""
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    monkeypatch.chdir(tmp_path)
    assert state_dir_location("link/state", {}) == str(tmp_path.resolve() / "real" / "state")


def test_fr80_tilde_in_option_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-80: ``--state-dir=~/x`` works although the shell does not expand it."""
    monkeypatch.setenv("HOME", "/home/someone")
    assert state_dir_location("~/state", {}) == "/home/someone/state"


# --- jail directories (FR-81, FR-82, FR-84) ---


def spec(path: str, kind: PathKind = PathKind.DIRECTORY) -> MountSpec:
    """A jailed mount spec."""
    return MountSpec(path, MountMode.JAILED, kind)


def test_fr81_jail_id_is_sha256_of_path() -> None:
    """FR-81: the jail directory name is the SHA-256 of the resolved path."""
    assert jail_id("/home/u/proj") == hashlib.sha256(b"/home/u/proj").hexdigest()
    assert jail_id("/home/u/proj") != jail_id("/home/u/proj2")


def test_fr84_non_utf8_paths_have_an_id() -> None:
    """FR-84: paths are hashed as bytes, so undecodable names work."""
    assert len(jail_id(os.fsdecode(b"/data/caf\xe9"))) == 64


def test_fr81_jail_layout() -> None:
    """FR-81: the files of a jail below ``<state>/jails/<id>/``."""
    jail = jail_dir("/state", "/home/u/proj")
    root = f"/state/jails/{jail_id('/home/u/proj')}"
    assert jail.root == root
    assert (jail.meta, jail.upper, jail.work, jail.lock, jail.lower_link, jail.merged) == (
        f"{root}/meta.json",
        f"{root}/upper",
        f"{root}/work",
        f"{root}/lock",
        f"{root}/lower",
        f"{root}/merged",
    )


def test_fr37_lower_and_source_of_directory_and_file() -> None:
    """FR-36, FR-37: a directory is its own lower; a file uses its parent directory."""
    jail = JailDir("/j")
    assert lower_dir(spec("/data/proj")) == "/data/proj"
    assert bind_source(jail, spec("/data/proj")) == "/j/merged"
    notes = spec("/data/proj/notes.txt", PathKind.FILE)
    assert lower_dir(notes) == "/data/proj"
    assert bind_source(jail, notes) == "/j/merged/notes.txt"


def test_fr82_directories_are_private(tmp_path: Path) -> None:
    """FR-82: the state directory and the jail directories have mode 0700."""
    state = str(tmp_path / "parent" / "state")
    jail = jail_dir(state, "/data/proj")
    make_jail_root(state, jail)
    for path in [state, os.path.dirname(jail.root), jail.root]:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700, path


# --- meta.json (FR-81a, FR-83) ---


def test_fr83_meta_round_trip(tmp_path: Path) -> None:
    """FR-83: meta.json has a version and is written with mode 0600."""
    jail = JailDir(str(tmp_path))
    meta = check_or_write_meta(jail, spec("/data/proj"))
    assert meta.version == META_VERSION
    assert meta.tool_version == __version__
    assert read_meta(jail) == meta
    assert stat.S_IMODE(os.stat(jail.meta).st_mode) == 0o600
    data = json.loads(Path(jail.meta).read_text(encoding="utf-8"))
    assert data["path"] == "/data/proj"
    assert data["kind"] == "directory"
    assert data["version"] == META_VERSION


def test_fr81a_existing_meta_is_kept(tmp_path: Path) -> None:
    """FR-75: a later session keeps the original metadata."""
    jail = JailDir(str(tmp_path))
    first = check_or_write_meta(jail, spec("/data/proj"))
    assert check_or_write_meta(jail, spec("/data/proj")) == first


def test_fr81a_kind_change_is_rejected(tmp_path: Path) -> None:
    """FR-81a: pending changes for a directory cannot be used for a file."""
    jail = JailDir(str(tmp_path))
    check_or_write_meta(jail, spec("/data/proj"))
    with pytest.raises(SetupError, match="recorded for a directory, but the path is now a file"):
        check_or_write_meta(jail, spec("/data/proj", PathKind.FILE))


def test_fr81a_other_path_is_corrupt_state(tmp_path: Path) -> None:
    """FR-81a: meta.json of another path is reported, never deleted."""
    jail = JailDir(str(tmp_path))
    check_or_write_meta(jail, spec("/data/other"))
    with pytest.raises(SetupError, match="corrupt state .* belongs to /data/other"):
        check_or_write_meta(jail, spec("/data/proj"))
    assert Path(jail.meta).exists()


@pytest.mark.parametrize("content", ["not json", "{}", '{"path": 1}', "[]"])
def test_fr83_unreadable_meta_is_corrupt_state(tmp_path: Path, content: str) -> None:
    """FR-83: a damaged meta.json is reported clearly."""
    jail = JailDir(str(tmp_path))
    Path(jail.meta).write_text(content, encoding="utf-8")
    with pytest.raises(SetupError, match="corrupt state"):
        check_or_write_meta(jail, spec("/data/proj"))


def test_fr83_newer_meta_version_is_rejected(tmp_path: Path) -> None:
    """FR-83: state written by a newer version is not touched."""
    jail = JailDir(str(tmp_path))
    meta = check_or_write_meta(jail, spec("/data/proj"))
    write_meta(jail, dataclasses.replace(meta, version=META_VERSION + 1, tool_version="9.9"))
    with pytest.raises(SetupError, match="newer agent-sandbox"):
        check_or_write_meta(jail, spec("/data/proj"))


# --- locking (FR-76, FR-85) ---


def test_fr85_lock_records_pid_and_excludes_others(tmp_path: Path) -> None:
    """FR-76, FR-85: a held lock names its PID and refuses a second holder."""
    lock = str(tmp_path / "lock")
    with locked(lock, "-j /data/proj"):
        assert Path(lock).read_text(encoding="ascii") == f"{os.getpid()}\n"
        assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600
        with pytest.raises(SetupError) as info:
            with locked(lock, "-j /data/proj"):
                pass
        assert str(info.value) == (
            f"-j /data/proj is in use by another agent-sandbox session (PID {os.getpid()})"
        )
        assert info.value.exit_code is ExitCode.SETUP_FAILED
    with locked(lock, "-j /data/proj"):
        pass


def test_fr43_lock_error_type_can_be_chosen(tmp_path: Path) -> None:
    """FR-43: merge and discard report a held lock as a general error (exit 1)."""
    lock = str(tmp_path / "lock")
    with locked(lock, "x"):
        with pytest.raises(AgentSandboxError) as info:
            with locked(lock, "x", AgentSandboxError):
                pass
    assert not isinstance(info.value, SetupError)
    assert info.value.exit_code is ExitCode.ERROR


def test_fr85_stale_pid_without_lock_is_ignored(tmp_path: Path) -> None:
    """FR-85: a PID left in the file by a dead process does not block."""
    lock = tmp_path / "lock"
    lock.write_text("999999\n", encoding="ascii")
    with locked(str(lock), "x"):
        assert lock.read_text(encoding="ascii") == f"{os.getpid()}\n"


def test_fr85_lock_is_released_on_error(tmp_path: Path) -> None:
    """FR-61: the lock is released when the session fails."""
    lock = str(tmp_path / "lock")
    with pytest.raises(RuntimeError):
        with locked(lock, "x"):
            raise RuntimeError("boom")
    with locked(lock, "x"):
        pass
