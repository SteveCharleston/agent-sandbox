"""Tests for program lookup and PATH visibility (FR-9, FR-29)."""

from pathlib import Path

import pytest

from agent_sandbox.bwrap import SandboxPlan, plan_sandbox
from agent_sandbox.layout import SystemMount
from agent_sandbox.mounts import MountSpec, PathKind
from agent_sandbox.options import MountMode
from agent_sandbox.programs import find_program, invisible_path_entries


@pytest.fixture(name="root")
def fixture_root(tmp_path: Path) -> Path:
    """Host directories: ``sys/bin`` (system), ``proj/bin`` (mounted), ``home/bin`` (not)."""
    for directory in ["sys/bin", "proj/bin", "home/bin", "state"]:
        (tmp_path / directory).mkdir(parents=True)
    for program in ["sys/bin/tool", "proj/bin/projtool", "home/bin/hometool", "sys/bin/both"]:
        (tmp_path / program).write_text("#!/bin/sh\n")
        (tmp_path / program).chmod(0o755)
    (tmp_path / "home/bin/both").write_text("")
    (tmp_path / "sys/bin/data").write_text("not executable")
    return tmp_path


def make_plan(
    root: Path, program: str, path: str | None, *, cwd: str | None = None
) -> SandboxPlan:
    """A plan with ``root/sys`` as system mount and ``root/proj`` mounted read-only."""
    env = {} if path is None else {"PATH": path.replace("ROOT", str(root))}
    return plan_sandbox(
        system_mounts=(SystemMount(str(root / "sys")),),
        mounts=(MountSpec(str(root / "proj"), MountMode.READ_ONLY, PathKind.DIRECTORY),),
        argv=(program,),
        env=env,
        cwd=cwd or str(root / "proj"),
        state_dir=str(root / "state"),
        state_dir_exists=True,
    )


@pytest.mark.parametrize(
    ("program", "expected"),
    [
        ("tool", "sys/bin/tool"),
        ("projtool", "proj/bin/projtool"),
        ("hometool", None),
        ("both", "sys/bin/both"),
        ("data", None),
        ("missing", None),
    ],
)
def test_fr9_program_found_via_visible_path_entries(
    root: Path, program: str, expected: str | None
) -> None:
    """FR-9: only visible PATH entries and executable files count."""
    plan = make_plan(root, program, "ROOT/home/bin:ROOT/sys/bin:ROOT/proj/bin")
    assert find_program(plan) == (None if expected is None else str(root / expected))


def test_fr9_invisible_entry_does_not_shadow(root: Path) -> None:
    """FR-9: an invisible entry earlier in PATH is skipped, as it does not exist inside."""
    plan = make_plan(root, "both", "ROOT/home/bin:ROOT/sys/bin")
    assert find_program(plan) == str(root / "sys/bin/both")


@pytest.mark.parametrize(
    ("program", "found"),
    [("bin/projtool", True), ("./bin/projtool", True), ("ROOT/sys/bin/tool", True)],
)
def test_fr9_program_with_slash(root: Path, program: str, found: bool) -> None:
    """FR-9: a program with ``/`` is checked relative to the working directory."""
    plan = make_plan(root, program.replace("ROOT", str(root)), None)
    assert (find_program(plan) is not None) is found


@pytest.mark.parametrize("program", ["ROOT/home/bin/hometool", "../home/bin/hometool"])
def test_fr9_program_with_slash_outside_sandbox(root: Path, program: str) -> None:
    """FR-9: a program path outside the visible locations is not found."""
    assert find_program(make_plan(root, program.replace("ROOT", str(root)), None)) is None


def test_fr9_no_path_variable(root: Path) -> None:
    """FR-9: without PATH in the sandbox, only programs with ``/`` can be found."""
    assert find_program(make_plan(root, "tool", None)) is None


def test_fr9_empty_path_entry_means_working_directory(root: Path) -> None:
    """FR-9: an empty PATH entry is the working directory, as for execvp."""
    plan = make_plan(root, "projtool", "ROOT/sys/bin:", cwd=str(root / "proj/bin"))
    assert find_program(plan) == str(root / "proj/bin/projtool")


def test_fr29_invisible_entries_deduplicated_existing_only(root: Path) -> None:
    """FR-29: existing, invisible PATH directories are reported once, in PATH order."""
    plan = make_plan(
        root,
        "tool",
        "ROOT/home/bin:ROOT/sys/bin:ROOT/home/bin/:ROOT/missing/bin:relative:"
        ":ROOT/proj/bin:ROOT/home",
    )
    assert invisible_path_entries(plan) == [str(root / "home/bin"), str(root / "home")]


def test_fr29_no_path_no_warning(root: Path) -> None:
    """FR-29: nothing to report without PATH."""
    assert not invisible_path_entries(make_plan(root, "tool", None))


def test_fr9_host_path_is_used_for_the_check(root: Path) -> None:
    """FR-9: with overlays mounted, the merged view decides whether the program exists."""
    plan = make_plan(root, "projtool", "ROOT/proj/bin")
    hidden = str(root / "home" / "bin")
    assert find_program(plan, lambda path: path) == str(root / "proj/bin/projtool")
    assert find_program(plan, lambda path: path.replace(str(root / "proj/bin"), hidden)) is None
