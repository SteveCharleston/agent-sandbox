"""Project requirements: privileges, prerequisites, quality gate (FR-95, FR-101, FR-102, AC-17)."""

import re
import tomllib
from pathlib import Path

import pytest

from agent_sandbox.deps import (
    check_user_namespaces,
    find_bwrap,
    find_fusermount,
    find_overlay_tools,
)
from agent_sandbox.errors import SetupError

ROOT = Path(__file__).resolve().parents[2]
ESCALATION_TOOLS = ("sudo", "su", "doas", "pkexec", "run0")
GATE = [
    "uv run black --check .",
    "uv run isort --check .",
    "uv run flake8",
    "uv run pylint src tests",
    "uv run mypy src tests",
    "uv run pytest",
]


def python_sources() -> list[Path]:
    """All Python files of the package and the tests."""
    return sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")])


def test_fr95_fr102_no_code_starts_privilege_escalation() -> None:
    """FR-95, FR-102: no source or test file uses sudo, su, doas, pkexec or run0 as a program."""
    names = "|".join(ESCALATION_TOOLS)
    # A program name is the first element of a command list: ["sudo", ...].
    as_program = re.compile(rf"""\[\s*["']({names})["']""")
    offenders = [
        f"{path.relative_to(ROOT)}: {match.group(0)}"
        for path in python_sources()
        if path.name != "test_project.py"
        for match in as_program.finditer(path.read_text(encoding="utf-8"))
    ]
    assert not offenders


def prerequisite_errors(tmp_path: Path) -> list[str]:
    """The messages for every missing prerequisite."""
    messages = []
    for check in (find_bwrap, find_fusermount, find_overlay_tools):
        with pytest.raises(SetupError) as info:
            check(str(tmp_path))
        messages.append(str(info.value))
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("#!/bin/sh\n", encoding="utf-8")
    bwrap.chmod(0o755)
    (tmp_path / "sys" / "user").mkdir(parents=True)
    (tmp_path / "sys" / "user" / "max_user_namespaces").write_text("0\n", encoding="utf-8")
    with pytest.raises(SetupError) as info:
        check_user_namespaces(str(bwrap), str(tmp_path / "sys"))
    messages.append(str(info.value))
    return messages


def test_fr101_prerequisite_errors_point_to_the_readme(tmp_path: Path) -> None:
    """FR-101: missing prerequisites point to the README and never suggest sudo."""
    messages = prerequisite_errors(tmp_path)
    assert len(messages) == 4
    for message in messages:
        assert "see README: requirements" in message
        assert "sudo" not in message
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Requirements" in readme


def test_ac17_quality_gate_is_configured() -> None:
    """AC-17, section 10.2: tool settings as specified."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tools = config["tool"]
    assert tools["black"]["line-length"] == 99
    assert tools["isort"]["profile"] == "black"
    assert tools["isort"]["line_length"] == 99
    assert tools["mypy"]["strict"] is True
    assert "max-line-length = 99" in (ROOT / ".flake8").read_text(encoding="utf-8")
    assert config["project"]["requires-python"] == ">=3.11"
    assert config["project"]["dependencies"] == []


def test_ac17_ci_runs_the_quality_gate_on_all_python_versions() -> None:
    """AC-17, FR-102: CI runs every gate command on Python 3.11 to 3.13, as a normal user."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for command in GATE:
        assert command in workflow, command
    assert '["3.11", "3.12", "3.13"]' in workflow
    test_steps = workflow[workflow.index("uv sync --locked") :]
    assert "sudo" not in test_steps
