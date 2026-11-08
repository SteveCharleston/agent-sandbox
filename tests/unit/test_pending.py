"""Tests for finding pending changes and the diff/listdiffs commands (FR-14 to FR-18, FR-84)."""

import os
from pathlib import Path

import pytest

from agent_sandbox.cli import main
from agent_sandbox.errors import AgentSandboxError, ExitCode
from agent_sandbox.mounts import PathKind
from agent_sandbox.pending import all_pending, find_pending, normalize_path
from agent_sandbox.state import jail_dir, make_jail_root
from tests.unit.test_diffing import make_pending
from tests.unit.test_upper import WHITEOUT, build


class Warnings(list[str]):
    """Collects warnings."""

    def __call__(self, message: str) -> None:
        self.append(message)


@pytest.fixture(name="setup")
def fixture_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A project ``proj`` with pending changes, as the current directory; returns state, proj."""
    state = tmp_path / "state"
    proj = build(tmp_path / "proj", {"a.txt": "a\n", "old.txt": "old\n"})
    make_pending(state, proj, {"a.txt": "changed\n", "old.txt": WHITEOUT, "new.txt": "n\n"})
    monkeypatch.chdir(proj)
    monkeypatch.setenv("AGENT_SANDBOX_STATE_DIR", str(state))
    return state, proj


def test_fr84_normalize_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-84: relative, ``..`` and symlinked spellings resolve to one path; it need not exist."""
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    root = tmp_path.resolve()
    cwd = str(root / "real")
    assert normalize_path(".", cwd) == str(root / "real")
    assert normalize_path("../link", cwd) == str(root / "real")
    assert normalize_path(str(tmp_path / "link" / "gone"), "/") == str(root / "real" / "gone")
    monkeypatch.setenv("HOME", str(root))
    assert normalize_path("~/real", "/") == str(root / "real")


def test_find_pending(setup: tuple[Path, Path]) -> None:
    """FR-46: state is found by path; no state is ``None``."""
    state, proj = setup
    pending = find_pending(str(state), str(proj))
    assert pending is not None
    assert pending.lower == str(proj)
    assert pending.only is None
    assert find_pending(str(state), str(proj / "other")) is None


def test_fr37_pending_for_a_file(tmp_path: Path) -> None:
    """FR-37: a jailed file compares its parent directory, restricted to its name."""
    proj = build(tmp_path / "proj", {"notes.txt": "n\n"})
    pending = make_pending(
        tmp_path / "state", proj / "notes.txt", {"notes.txt": "m\n"}, PathKind.FILE
    )
    assert pending.lower == str(proj)
    assert pending.only == "notes.txt"
    assert [change.path for change in pending.changes()] == ["notes.txt"]


def test_find_pending_corrupt_is_an_error(setup: tuple[Path, Path], tmp_path: Path) -> None:
    """FR-46: state that belongs to another path is an error with exit 1."""
    state, proj = setup
    other = tmp_path / "other"
    os.rename(jail_dir(str(state), str(proj)).root, jail_dir(str(state), str(other)).root)
    with pytest.raises(AgentSandboxError, match="corrupt state") as info:
        find_pending(str(state), str(other))
    assert info.value.exit_code is ExitCode.ERROR


def test_fr16_all_pending_skips_broken_state(setup: tuple[Path, Path], tmp_path: Path) -> None:
    """FR-16: broken jails are skipped with a warning; no meta.json is skipped silently."""
    state, proj = setup
    second = build(tmp_path / "second", {})
    make_pending(state, second, {"x": "x\n"})
    broken = jail_dir(str(state), "/broken")
    make_jail_root(str(state), broken)
    Path(broken.meta).write_text("not json", encoding="utf-8")
    make_jail_root(str(state), jail_dir(str(state), "/empty"))
    misplaced = Path(state, "jails", "misplaced")
    os.rename(jail_dir(str(state), str(second)).root, misplaced)
    make_pending(state, second, {})
    warnings = Warnings()
    paths = [pending.meta.path for pending in all_pending(str(state), warnings)]
    assert paths == sorted([str(proj), str(second)])
    assert len(warnings) == 2
    assert all(warning.startswith("corrupt state in ") for warning in warnings)


def test_all_pending_without_state_dir(tmp_path: Path) -> None:
    """FR-18: no state directory means nothing pending."""
    assert not all_pending(str(tmp_path / "missing"), Warnings())


def test_fr11_diff_command(setup: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """FR-11: ``diff PATH`` prints the diff to stdout and exits 0."""
    del setup
    assert main(["diff", "."]) == 0
    out = capsys.readouterr().out
    assert "diff --git a/a.txt b/a.txt\n" in out
    assert "deleted file mode 100644\n" in out
    assert "+++ b/new.txt\n" in out


def test_fr15_diff_stat_command(
    setup: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """FR-15: ``diff --stat``."""
    del setup
    assert main(["diff", "--stat", "."]) == 0
    assert capsys.readouterr().out == (
        "M\t1\t1\ta.txt\nA\t1\t0\tnew.txt\nD\t0\t1\told.txt\n"
        "3 changes: 1 added, 1 modified, 1 deleted\n"
    )


def test_fr14_diff_without_state(
    setup: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """FR-14, FR-46: no state for the path prints nothing and exits 0."""
    del setup
    assert main(["diff", "/nonexistent-4711"]) == 0
    assert capsys.readouterr() == ("", "")


def test_fr45_diff_after_the_path_was_deleted(
    setup: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """FR-45: without the real directory, everything pending is shown as added."""
    _, proj = setup
    for name in ["a.txt", "old.txt"]:
        (proj / name).unlink()
    os.chdir(proj.parent)
    proj.rmdir()
    assert main(["diff", "--stat", str(proj)]) == 0
    assert capsys.readouterr().out == (
        "A\t1\t0\ta.txt\nA\t1\t0\tnew.txt\n2 changes: 2 added, 0 modified, 0 deleted\n"
    )


def test_fr16_listdiffs_command(
    setup: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """FR-16: ``listdiffs`` prints one tab-separated line per jail with changes."""
    _, proj = setup
    assert main(["listdiffs"]) == 0
    path, count, _ = capsys.readouterr().out.rstrip("\n").split("\t")
    assert (path, count) == (str(proj), "3")


def test_fr18_listdiffs_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """FR-18: nothing pending prints nothing and exits 0."""
    assert main(["listdiffs", "--state-dir", str(tmp_path)]) == 0
    assert capsys.readouterr() == ("", "")


def test_fr5_diff_output_keeps_bytes(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """FR-5, FR-13: undecodable bytes in content reach stdout unchanged."""
    proj = build(tmp_path / "proj", {"f": b"caf\xe9\n"})
    make_pending(tmp_path / "state", proj, {"f": b"caf\xe8\n"})
    assert main(["diff", "--state-dir", str(tmp_path / "state"), str(proj)]) == 0
    assert b"+caf\xe8\n" in capsysbinary.readouterr().out
