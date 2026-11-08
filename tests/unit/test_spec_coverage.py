"""Every requirement and acceptance criterion is referenced by a test (NFR-13)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "specs" / "SPEC.md").read_text(encoding="utf-8")
TESTS = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted((ROOT / "tests").rglob("test_*.py"))
)


def _referenced(identifier: str) -> bool:
    """Referenced as ``FR-12`` in a docstring or as ``fr12`` in a test name."""
    plain = re.compile(rf"(?<![\w-]){re.escape(identifier)}(?![\w])")
    name = re.compile(rf"(?<![a-z0-9]){identifier.lower().replace('-', '')}(?![0-9a-z])")
    return bool(plain.search(TESTS) or name.search(TESTS))


def _section(start: str, end: str) -> str:
    return SPEC[SPEC.index(start) : SPEC.index(end)]


def test_nfr13_every_functional_requirement_has_a_test() -> None:
    """NFR-13: each FR of sections 5 to 9 is named by at least one test."""
    section = _section("## 5. Command-Line Interface", "## 10. Non-Functional")
    identifiers = set(re.findall(r"^- (FR-\d+[a-z]?):", section, re.M))
    assert len(identifiers) > 80
    assert sorted(identifier for identifier in identifiers if not _referenced(identifier)) == []


def test_nfr13_every_acceptance_criterion_has_a_test() -> None:
    """NFR-13: each acceptance criterion of section 11 is named by at least one test."""
    section = _section("## 11. Acceptance Criteria", "## 12.")
    identifiers = set(re.findall(r"^\*\*(AC-\d+[a-z]?):", section, re.M))
    assert len(identifiers) >= 17
    assert sorted(identifier for identifier in identifiers if not _referenced(identifier)) == []
