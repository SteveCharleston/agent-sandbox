"""Tests for the state directory location (FR-80)."""

from pathlib import Path

import pytest

from agent_sandbox.state import state_dir_location

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
