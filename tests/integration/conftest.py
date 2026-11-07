"""Fixtures for integration tests: they need bwrap and user namespaces (NFR-12)."""

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest


@pytest.fixture(name="sandbox_available", scope="session")
def fixture_sandbox_available() -> None:
    """Skip if bwrap is missing or cannot create a sandbox here."""
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bwrap is not installed")
    result = subprocess.run(
        [bwrap, "--unshare-user", "--ro-bind", "/", "/", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"bwrap cannot create a sandbox: {result.stderr.strip()}")


@pytest.fixture(autouse=True)
def require_sandbox(sandbox_available: None) -> None:
    """Apply the availability check to every integration test."""
    del sandbox_available


class Cli:
    """Runs ``agent-sandbox`` as a separate process with a private state directory."""

    def __init__(self, tmp_path: Path) -> None:
        self.state_dir = tmp_path / "state"
        self.env = {
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
            "AGENT_SANDBOX_STATE_DIR": str(self.state_dir),
        }

    def command(self, *args: str) -> list[str]:
        """The command line for ``agent-sandbox ARGS``."""
        return [sys.executable, "-m", "agent_sandbox", *args]

    def __call__(
        self,
        *args: str,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``agent-sandbox ARGS`` and capture its output."""
        return subprocess.run(
            self.command(*args),
            cwd=cwd,
            env={**self.env, **(env or {})},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


@pytest.fixture(name="cli")
def fixture_cli(tmp_path: Path) -> Cli:
    """Run the CLI with ``LC_ALL=C`` and a state directory in ``tmp_path``."""
    return Cli(tmp_path)
