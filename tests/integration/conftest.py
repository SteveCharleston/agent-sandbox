"""Fixtures for integration tests: they need bwrap and user namespaces (NFR-12)."""

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_sandbox.overlay import unmount_overlay


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


@pytest.fixture(name="overlay_available", scope="session")
def fixture_overlay_available(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Skip if fuse-overlayfs cannot be mounted here as an unprivileged user."""
    tools = [shutil.which(name) for name in ("fuse-overlayfs", "fusermount3")]
    if None in tools:
        pytest.skip("fuse-overlayfs or fusermount3 is not installed")
    root = tmp_path_factory.mktemp("overlay-check")
    for name in ("lower", "upper", "work", "merged"):
        (root / name).mkdir()
    result = subprocess.run(
        [str(tools[0]), "-o", "lowerdir=lower,upperdir=upper,workdir=work", "merged"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"fuse-overlayfs cannot mount: {result.stderr.strip()}")
    # Right after mounting, fusermount3 -u is sometimes busy; unmount_overlay retries.
    unmount_overlay(str(root / "merged"), str(tools[1]))


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

    def start(self, *args: str) -> "subprocess.Popen[str]":
        """Start ``agent-sandbox ARGS`` with stdout as a pipe and stderr discarded."""
        return subprocess.Popen(
            self.command(*args),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def run_raw(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        """Run any command with the CLI's environment and capture its output."""
        return subprocess.run(
            command, env=self.env, capture_output=True, text=True, timeout=120, check=False
        )

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
