"""Tests for merge and discard (FR-40 to FR-47, FR-99)."""

import dataclasses
import io
import os
import stat
from pathlib import Path

import pytest

from agent_sandbox import merging
from agent_sandbox.cli import main, parse_command
from agent_sandbox.errors import AgentSandboxError, ExitCode
from agent_sandbox.merging import Io, apply_changes, check_mergeable, conflicts
from agent_sandbox.mounts import PathKind
from agent_sandbox.options import DiscardOptions, MergeOptions
from agent_sandbox.pending import Pending, find_pending
from agent_sandbox.state import locked, read_meta, remove_jail
from agent_sandbox.upper import compute_changes
from tests.unit.test_diffing import make_pending, tree_state
from tests.unit.test_upper import WHITEOUT, Link, Mode, Tree, build, opaque


class FakeIo:
    """Records output and warnings; answers questions from a list."""

    def __init__(self, *answers: bool) -> None:
        self.output: list[str] = []
        self.warnings: list[str] = []
        self.questions: list[str] = []
        self.answers = list(answers)

    def ask(self, question: str) -> bool:
        """Answer the next question."""
        self.questions.append(question)
        return self.answers.pop(0)

    @property
    def io(self) -> Io:
        """The ``Io`` for merge and discard."""
        return Io(out=self.output.append, ask=self.ask, warn=self.warnings.append)

    @property
    def text(self) -> str:
        """All output."""
        return "".join(self.output)


def merge_tree(tmp_path: Path, lower: Tree, upper: Tree) -> tuple[Path, list[str]]:
    """Apply ``upper`` to ``lower`` and return the lower directory and the report."""
    lower_dir = build(tmp_path / "lower", lower)
    upper_dir = build(tmp_path / "upper", upper)
    report: list[str] = []
    apply_changes(str(lower_dir), compute_changes(str(lower_dir), str(upper_dir)), report.append)
    return lower_dir, report


def expect(tmp_path: Path, tree: Tree) -> dict[str, tuple[int, bytes]]:
    """The state of ``tree`` built in a separate directory."""
    return tree_state(build(tmp_path / "expected", tree))


LOWER: Tree = {
    "file.txt": "original\n",
    "script.sh": Mode("echo\n", 0o644),
    "old.txt": "old\n",
    "link": Link("file.txt"),
    "dir": {"a.txt": "a\n", "sub": {"deep.txt": "deep\n"}},
    "keep": {"k.txt": "k\n"},
}


def test_fr40_all_kinds_of_changes(tmp_path: Path) -> None:
    """FR-40: new and modified files, symlinks, deletions, directories, modes."""
    upper: Tree = {
        "file.txt": "changed\n",
        "script.sh": Mode("echo\n", 0o755),
        "old.txt": WHITEOUT,
        "link": Link("other"),
        "dir": WHITEOUT,
        "new.txt": "new\n",
        "newlink": Link("new.txt"),
        "made": opaque({"inner": opaque({"f": "f\n"})}),
        "keep": {"k2.txt": "k2\n"},
    }
    lower, report = merge_tree(tmp_path, LOWER, upper)
    assert tree_state(lower) == expect(
        tmp_path,
        {
            "file.txt": "changed\n",
            "script.sh": Mode("echo\n", 0o755),
            "link": Link("other"),
            "new.txt": "new\n",
            "newlink": Link("new.txt"),
            "made": {"inner": {"f": "f\n"}},
            "keep": {"k.txt": "k\n", "k2.txt": "k2\n"},
        },
    )
    assert not (lower / "dir").exists()
    assert report == [
        "D\tdir/a.txt\n",
        "D\tdir/sub/deep.txt\n",
        "D\tdir/sub/\n",
        "D\tdir/\n",
        "M\tfile.txt\n",
        "A\tkeep/k2.txt\n",
        "M\tlink\n",
        "A\tmade/\n",
        "A\tmade/inner/\n",
        "A\tmade/inner/f\n",
        "A\tnew.txt\n",
        "A\tnewlink\n",
        "D\told.txt\n",
        "M\tscript.sh\n",
    ]


def test_fr40_mode_only_change_keeps_the_file(tmp_path: Path) -> None:
    """FR-40: a pure permission change is a chmod; the file (inode) stays."""
    lower = build(tmp_path / "lower", {"s": Mode("x\n", 0o644)})
    inode = (lower / "s").stat().st_ino
    upper = build(tmp_path / "upper", {"s": Mode("x\n", 0o700)})
    apply_changes(str(lower), compute_changes(str(lower), str(upper)), lambda _: None)
    assert (lower / "s").stat().st_ino == inode
    assert stat.S_IMODE((lower / "s").stat().st_mode) == 0o700


def test_fr40_times_are_preserved(tmp_path: Path) -> None:
    """FR-40: copied files keep the modification time of the pending version."""
    upper = build(tmp_path / "upper", {"f": "new\n"})
    os.utime(upper / "f", ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    lower = build(tmp_path / "lower", {"f": "old\n"})
    apply_changes(str(lower), compute_changes(str(lower), str(upper)), lambda _: None)
    assert (lower / "f").stat().st_mtime_ns == 1_600_000_000_000_000_000


@pytest.mark.parametrize(
    ("lower", "upper", "expected"),
    [
        ({"x": "file\n"}, {"x": opaque({"in": "in\n"})}, {"x": {"in": "in\n"}}),
        ({"x": {"in": "in\n"}}, {"x": "file\n"}, {"x": "file\n"}),
        ({"x": "file\n"}, {"x": Link("target")}, {"x": Link("target")}),
        ({"x": Link("target")}, {"x": "file\n"}, {"x": "file\n"}),
    ],
    ids=["file-to-dir", "dir-to-file", "file-to-symlink", "symlink-to-file"],
)
def test_fr40_type_changes(tmp_path: Path, lower: Tree, upper: Tree, expected: Tree) -> None:
    """FR-40: a changed type replaces the entry."""
    result, _ = merge_tree(tmp_path, lower, upper)
    assert tree_state(result) == expect(tmp_path, expected)


def test_fr40_opaque_directory_replaces_contents(tmp_path: Path) -> None:
    """FR-40: ``rm -r dir; mkdir dir`` removes the old contents."""
    lower, _ = merge_tree(tmp_path, LOWER, {"dir": opaque({"new.txt": "n\n"})})
    assert sorted(path.name for path in (lower / "dir").iterdir()) == ["new.txt"]


def test_fr42_read_only_directories_are_filled_then_closed(tmp_path: Path) -> None:
    """FR-42: a new directory made read-only in the sandbox is created, filled, then closed."""
    upper = build(tmp_path / "upper", {"ro": opaque({"f": "f\n", "sub": opaque({"g": "g\n"})})})
    (upper / "ro" / "sub").chmod(0o555)
    (upper / "ro").chmod(0o555)
    lower = build(tmp_path / "lower", {})
    report: list[str] = []
    try:
        apply_changes(str(lower), compute_changes(str(lower), str(upper)), report.append)
        assert (lower / "ro" / "sub" / "g").read_text(encoding="utf-8") == "g\n"
        assert stat.S_IMODE((lower / "ro").stat().st_mode) == 0o555
        assert stat.S_IMODE((lower / "ro" / "sub").stat().st_mode) == 0o555
    finally:
        for path in (upper / "ro", upper / "ro" / "sub", lower / "ro", lower / "ro" / "sub"):
            if path.exists():
                path.chmod(0o755)


def test_fr42_directory_mode_change_is_reported_last(tmp_path: Path) -> None:
    """FR-42: directory modes are set (and reported) after the contents."""
    upper = build(tmp_path / "upper", {"keep": {"new": "n\n"}})
    (upper / "keep").chmod(0o700)
    lower = build(tmp_path / "lower", LOWER)
    report: list[str] = []
    apply_changes(str(lower), compute_changes(str(lower), str(upper)), report.append)
    assert report == ["A\tkeep/new\n", "M\tkeep/\n"]
    assert stat.S_IMODE((lower / "keep").stat().st_mode) == 0o700


def test_fr42_read_only_host_directory_is_restored(tmp_path: Path) -> None:
    """FR-42: a host directory without write permission is opened and restored."""
    lower = build(tmp_path / "lower", {"ro": {"f": "f\n"}})
    upper = build(tmp_path / "upper", {"ro": {"f": "changed\n"}})
    (lower / "ro").chmod(0o555)
    (upper / "ro").chmod(0o555)  # a copy-up keeps the mode
    try:
        apply_changes(str(lower), compute_changes(str(lower), str(upper)), lambda _: None)
        assert (lower / "ro" / "f").read_text(encoding="utf-8") == "changed\n"
        assert stat.S_IMODE((lower / "ro").stat().st_mode) == 0o555
    finally:
        (lower / "ro").chmod(0o755)
        (upper / "ro").chmod(0o755)


def test_fr45_missing_path_is_recreated(tmp_path: Path) -> None:
    """FR-45: merge recreates a path that no longer exists."""
    upper = build(tmp_path / "upper", {"f": "f\n", "d": {"g": "g\n"}})
    lower = tmp_path / "gone" / "proj"
    apply_changes(str(lower), compute_changes(str(lower), str(upper)), lambda _: None)
    assert tree_state(lower) == expect(tmp_path, {"f": "f\n", "d": {"g": "g\n"}})


def test_fr42_merge_is_idempotent(tmp_path: Path) -> None:
    """FR-42: after a merge, the same upper layer has no changes left."""
    upper: Tree = {"file.txt": "changed\n", "old.txt": WHITEOUT, "dir": opaque({"x": "x\n"})}
    lower, _ = merge_tree(tmp_path, LOWER, upper)
    assert not compute_changes(str(lower), str(tmp_path / "upper"))


def test_fr42_failure_midway_names_the_change_and_can_be_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-42: the error names the failed change; a second merge finishes the job."""
    lower = build(tmp_path / "lower", LOWER)
    upper = build(tmp_path / "upper", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
    real_replace = merging._replace_file  # pylint: disable=protected-access

    def fail_on_b(source: str, target: str, mode: int) -> None:
        if target.endswith("b.txt"):
            raise PermissionError(13, "Permission denied")
        real_replace(source, target, mode)

    monkeypatch.setattr(merging, "_replace_file", fail_on_b)
    report: list[str] = []
    with pytest.raises(AgentSandboxError) as info:
        apply_changes(str(lower), compute_changes(str(lower), str(upper)), report.append)
    assert str(info.value) == (
        "merge stopped at b.txt: Permission denied; the changes listed above were applied, "
        "the others are still pending"
    )
    assert report == ["A\ta.txt\n"]
    monkeypatch.setattr(merging, "_replace_file", real_replace)
    report.clear()
    apply_changes(str(lower), compute_changes(str(lower), str(upper)), report.append)
    assert report == ["A\tb.txt\n", "A\tc.txt\n"]


def test_fr42_special_files_are_rejected_before_anything_happens(tmp_path: Path) -> None:
    """FR-42: a FIFO cannot be merged; nothing is applied."""
    upper = build(tmp_path / "upper", {"a.txt": "a\n"})
    os.mkfifo(upper / "fifo")
    lower = build(tmp_path / "lower", {})
    with pytest.raises(AgentSandboxError, match="cannot merge the special file fifo"):
        check_mergeable(compute_changes(str(lower), str(upper)))


def test_fr47_conflict_detection(tmp_path: Path) -> None:
    """FR-47: a file the host changed after the sandbox did is a conflict."""
    lower = build(tmp_path / "lower", {"f": "host\n", "g": "host\n"})
    upper = build(tmp_path / "upper", {"f": "sandbox\n", "g": "sandbox\n"})
    os.utime(upper / "f", (1, 1))
    os.utime(lower / "g", (1, 1))
    assert conflicts(compute_changes(str(lower), str(upper))) == [
        "f changed on the host after the sandbox changed it"
    ]


def test_fr99_files_belong_to_the_user(tmp_path: Path) -> None:
    """FR-99: merged files are owned by the invoking user."""
    lower, _ = merge_tree(tmp_path, {}, {"f": "f\n"})
    assert (lower / "f").stat().st_uid == os.getuid()


# --- the merge and discard commands ---


@pytest.fixture(name="pending")
def fixture_pending(tmp_path: Path) -> Pending:
    """A project with pending changes in ``tmp_path/state``."""
    proj = build(tmp_path / "proj", {"a.txt": "a\n", "old.txt": "old\n"})
    return make_pending(
        tmp_path / "state", proj, {"a.txt": "changed\n", "old.txt": WHITEOUT, "n.txt": "n\n"}
    )


def merge_options(pending: Pending, *, yes: bool = False, dry_run: bool = False) -> MergeOptions:
    """Merge options for the pending jail."""
    state = str(Path(pending.jail.root).parents[1])
    return MergeOptions(
        path=pending.meta.path, yes=yes, dry_run=dry_run, state_dir=state, debug=False
    )


def discard_options(pending: Pending, *, yes: bool = False) -> DiscardOptions:
    """Discard options for the pending jail."""
    state = str(Path(pending.jail.root).parents[1])
    return DiscardOptions(path=pending.meta.path, yes=yes, state_dir=state, debug=False)


def run_merge(pending: Pending, fake: FakeIo, **options: bool) -> int:
    """Run the merge command."""
    return merging.merge(merge_options(pending, **options), fake.io, host_env={}, cwd="/")


def run_discard(pending: Pending, fake: FakeIo, **options: bool) -> int:
    """Run the discard command."""
    return merging.discard(discard_options(pending, **options), fake.io, host_env={}, cwd="/")


def test_fr40_merge_asks_then_applies_and_removes_state(pending: Pending) -> None:
    """FR-40, FR-40a: summary, question, applied changes, state removed."""
    fake = FakeIo(True)
    assert run_merge(pending, fake) == 0
    proj = Path(pending.meta.path)
    assert fake.questions == [f"Apply these 3 changes to {proj}? [y/N] "]
    assert fake.text == (
        f"Changes to apply to {proj}:\n"
        "M\t1\t1\ta.txt\nA\t1\t0\tn.txt\nD\t0\t1\told.txt\n"
        "3 changes: 1 added, 1 modified, 1 deleted\n"
        "M\ta.txt\nA\tn.txt\nD\told.txt\n"
        f"Merged 3 changes into {proj}.\n"
    )
    assert sorted(path.name for path in proj.iterdir()) == ["a.txt", "n.txt"]
    assert not os.path.exists(pending.jail.root)


def test_fr40_merge_declined_changes_nothing(pending: Pending) -> None:
    """FR-40a: declining cancels with exit 1; nothing changes."""
    with pytest.raises(AgentSandboxError, match="^merge cancelled; nothing was changed$"):
        run_merge(pending, FakeIo(False))
    assert (Path(pending.meta.path) / "old.txt").exists()
    assert read_meta(pending.jail) is not None


def test_fr40_merge_yes_does_not_ask(pending: Pending) -> None:
    """FR-40: ``--yes`` skips summary and question."""
    fake = FakeIo()
    assert run_merge(pending, fake, yes=True) == 0
    assert not fake.questions
    assert fake.output[0] == "M\ta.txt\n"


def test_fr41_dry_run_changes_nothing(pending: Pending) -> None:
    """FR-41: ``--dry-run`` shows the changes and modifies nothing."""
    fake = FakeIo()
    assert run_merge(pending, fake, dry_run=True) == 0
    assert fake.text.startswith(f"Changes that merge would apply to {pending.meta.path}:\n")
    assert (Path(pending.meta.path) / "old.txt").exists()
    assert find_pending(str(Path(pending.jail.root).parents[1]), pending.meta.path) is not None


@pytest.mark.parametrize("command", [run_merge, run_discard])
def test_fr43_refused_while_a_session_runs(pending: Pending, command: object) -> None:
    """FR-43: merge and discard exit 1 while the jail is locked."""
    assert callable(command)
    with locked(pending.jail.lock, "-j"):
        with pytest.raises(AgentSandboxError, match="is in use by another") as info:
            command(pending, FakeIo(True), yes=True)
    assert info.value.exit_code is ExitCode.ERROR
    assert (Path(pending.meta.path) / "old.txt").exists()


def test_fr41_dry_run_works_while_a_session_runs(pending: Pending) -> None:
    """FR-41: the dry run only reads and does not need the lock."""
    with locked(pending.jail.lock, "-j"):
        assert run_merge(pending, FakeIo(), dry_run=True) == 0


def test_fr44_discard_asks_and_never_touches_the_real_path(pending: Pending) -> None:
    """FR-44: discard deletes the pending changes only."""
    proj = Path(pending.meta.path)
    before = tree_state(proj)
    fake = FakeIo(True)
    assert run_discard(pending, fake) == 0
    assert fake.questions == ["Discard these 3 changes? [y/N] "]
    assert fake.output[-1] == f"Discarded 3 changes for {proj}.\n"
    assert tree_state(proj) == before
    assert not os.path.exists(pending.jail.root)


def test_fr44_discard_declined(pending: Pending) -> None:
    """FR-44: declining keeps the pending changes."""
    with pytest.raises(AgentSandboxError, match="^discard cancelled; nothing was changed$"):
        run_discard(pending, FakeIo(False))
    assert read_meta(pending.jail) is not None


@pytest.mark.parametrize("command", ["merge", "discard"])
def test_fr46_no_state(tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]) -> None:
    """FR-46: no state for the path is an error with exit 1."""
    assert main([command, "--state-dir", str(tmp_path), "-y", "/nonexistent-4711"]) == 1
    assert capsys.readouterr().err == (
        "agent-sandbox: error: no pending changes for /nonexistent-4711\n"
    )


@pytest.mark.parametrize("command", [run_merge, run_discard])
def test_fr46_state_without_changes_is_removed(tmp_path: Path, command: object) -> None:
    """FR-46: state without changes is removed without a question."""
    assert callable(command)
    proj = build(tmp_path / "proj", {"a": "a\n"})
    pending = make_pending(tmp_path / "state", proj, {"a": "a\n"})
    fake = FakeIo()
    assert command(pending, fake) == 0
    assert not fake.questions
    assert not os.path.exists(pending.jail.root)


def test_fr37_merge_of_a_jailed_file(tmp_path: Path) -> None:
    """FR-37, FR-40: a jailed file is merged into its parent directory."""
    proj = build(tmp_path / "proj", {"notes.txt": "n\n", "other.txt": "o\n"})
    pending = make_pending(
        tmp_path / "state", proj / "notes.txt", {"notes.txt": "m\n"}, PathKind.FILE
    )
    assert run_merge(pending, FakeIo(), yes=True) == 0
    assert (proj / "notes.txt").read_text(encoding="utf-8") == "m\n"
    assert (proj / "other.txt").read_text(encoding="utf-8") == "o\n"


def test_fr62_merge_removes_a_stale_mount_first(
    pending: Pending, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-62: a mount left by a crashed session is removed before the state."""
    mounted = {pending.jail.merged}
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(merging, "is_mounted", lambda path: path in mounted)
    monkeypatch.setattr(merging, "find_fusermount", lambda path: "/bin/fusermount3")

    def unmount(merged: str, fusermount: str) -> None:
        calls.append((merged, fusermount))
        mounted.discard(merged)

    monkeypatch.setattr(merging, "unmount_overlay", unmount)
    assert run_merge(pending, FakeIo(), yes=True) == 0
    assert calls == [(pending.jail.merged, "/bin/fusermount3")]


def test_remove_jail_handles_read_only_and_whiteouts(tmp_path: Path) -> None:
    """FR-44: state with read-only directories and whiteouts is removed completely."""
    proj = build(tmp_path / "proj", {"x": "x\n"})
    pending = make_pending(tmp_path / "state", proj, {"ro": {"f": "f\n"}, "x": WHITEOUT})
    (Path(pending.jail.upper) / "ro").chmod(0o500)
    os.makedirs(os.path.join(pending.jail.work, "work"), mode=0)
    remove_jail(pending.jail)
    assert not os.path.exists(pending.jail.root)


def test_fr40_cli_prompt_on_stderr_answer_from_stdin(
    pending: Pending, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-40a: the question goes to stderr and the answer comes from stdin."""
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))
    state = str(Path(pending.jail.root).parents[1])
    assert main(["merge", "--state-dir", state, pending.meta.path]) == 0
    captured = capsys.readouterr()
    assert captured.err == f"Apply these 3 changes to {pending.meta.path}? [y/N] "
    assert captured.out.endswith(f"Merged 3 changes into {pending.meta.path}.\n")


@pytest.mark.parametrize("answer", ["", "n\n", "no\n", "maybe\n", "Y"])
def test_fr40_cli_anything_but_yes_cancels(
    pending: Pending,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    """FR-40a: EOF and other answers cancel; ``Y`` without newline still counts."""
    monkeypatch.setattr("sys.stdin", io.StringIO(answer))
    state = str(Path(pending.jail.root).parents[1])
    code = main(["merge", "--state-dir", state, pending.meta.path])
    err = capsys.readouterr().err
    if answer == "Y":
        assert code == 0
    else:
        assert code == 1
        assert err.endswith("agent-sandbox: error: merge cancelled; nothing was changed\n")


@pytest.fixture(name="conflicting")
def fixture_conflicting(pending: Pending) -> Pending:
    """The pending jail, with ``a.txt`` changed on the host after the sandbox changed it."""
    os.utime(Path(pending.jail.upper) / "a.txt", (1, 1))
    (Path(pending.meta.path) / "a.txt").write_text("host edit\n", encoding="utf-8")
    return pending


def test_fr47_conflicts_refuse_without_force(conflicting: Pending) -> None:
    """FR-47: conflicts stop the merge before anything is asked or changed."""
    fake = FakeIo(True)
    with pytest.raises(AgentSandboxError) as info:
        run_merge(conflicting, fake)
    assert str(info.value) == (
        "1 conflicts: the host changed these files after the sandbox did; nothing was "
        "changed. Use --force to replace the host's versions"
    )
    assert fake.warnings == ["conflict: a.txt changed on the host after the sandbox changed it"]
    assert not fake.questions
    proj = Path(conflicting.meta.path)
    assert (proj / "a.txt").read_text(encoding="utf-8") == "host edit\n"
    assert (proj / "old.txt").exists()


def test_fr47_force_replaces_the_host_version(conflicting: Pending) -> None:
    """FR-47: ``--force`` merges and still names the conflicts."""
    fake = FakeIo()
    options = dataclasses.replace(merge_options(conflicting, yes=True), force=True)
    assert merging.merge(options, fake.io, host_env={}, cwd="/") == 0
    assert fake.warnings == ["conflict: a.txt changed on the host after the sandbox changed it"]
    assert (Path(conflicting.meta.path) / "a.txt").read_text(encoding="utf-8") == "changed\n"


def test_fr47_dry_run_reports_conflicts(conflicting: Pending) -> None:
    """FR-47: the dry run lists conflicts and says that --force would be needed."""
    fake = FakeIo()
    assert run_merge(conflicting, fake, dry_run=True) == 0
    assert fake.warnings == [
        "conflict: a.txt changed on the host after the sandbox changed it",
        "merge would refuse because of 1 conflicts; --force overrides this",
    ]


def test_fr47_cli_force_option(capsys: pytest.CaptureFixture[str]) -> None:
    """FR-47: ``merge --force`` is parsed."""
    assert parse_command(["merge", "--force", "/p"]) == MergeOptions(
        path="/p", yes=False, dry_run=False, state_dir=None, debug=False, force=True
    )
    assert main(["merge", "--help"]) == 0
    assert "--force" in capsys.readouterr().out
