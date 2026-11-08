"""Tests for bwrap status parsing and exit codes (FR-24, FR-24a)."""

import os
import signal
import sys
from pathlib import Path

import pytest

from agent_sandbox.errors import ExitCode, SetupError
from agent_sandbox.supervisor import BwrapStatus, exit_status, program_pid, supervise


def exited(code: int) -> int:
    """A wait status for a normal exit with ``code``."""
    return code << 8


def killed(signum: int) -> int:
    """A wait status for a process killed by ``signum``."""
    return signum


def test_status_parsing_in_chunks() -> None:
    """FR-24a: JSON lines may arrive in pieces; the first child-pid wins."""
    status = BwrapStatus()
    status.feed(b'{ "child-pid": 12, "mnt-names')
    assert status.init_pid is None
    status.feed(b'pace": 4026 }\n{ "child-pid": 99 }\nnot json\n[1]\n{ "exit-code": 3 }\n')
    assert status.init_pid == 12
    assert status.exit_code == 3


@pytest.mark.parametrize(
    ("wait_status", "status", "expected"),
    [
        (exited(3), BwrapStatus(12, 3), 3),
        (exited(1), BwrapStatus(12, 130), 130),
        (exited(0), BwrapStatus(12, 0), 0),
        (killed(signal.SIGINT), BwrapStatus(12, None), 130),
        (killed(signal.SIGTERM), BwrapStatus(None, None), 143),
        (exited(0), BwrapStatus(None, None), 0),
    ],
)
def test_fr24a_exit_status(wait_status: int, status: BwrapStatus, expected: int) -> None:
    """FR-24a: the reported exit code wins; else 128+S if bwrap was killed."""
    assert exit_status(wait_status, status) == expected


@pytest.mark.parametrize(
    ("status", "hint"),
    [(BwrapStatus(12, None), False), (BwrapStatus(None, None), True)],
)
def test_fr24a_setup_failure(status: BwrapStatus, hint: bool) -> None:
    """FR-24a, FR-91: bwrap exiting without a report is a setup failure (exit 125)."""
    with pytest.raises(SetupError) as info:
        exit_status(exited(1), status)
    assert info.value.exit_code is ExitCode.SETUP_FAILED
    assert str(info.value).startswith("bwrap could not set up the sandbox")
    assert ("user namespaces" in str(info.value)) is hint


def test_fr24_program_pid_is_first_child_of_init(tmp_path: Path) -> None:
    """FR-24: signals go to the first child of the sandbox's init process."""
    children = tmp_path / "42" / "task" / "42" / "children"
    children.parent.mkdir(parents=True)
    children.write_text("43 50 ")
    assert program_pid(42, str(tmp_path)) == 43
    children.write_text("")
    assert program_pid(42, str(tmp_path)) is None
    assert program_pid(7, str(tmp_path)) is None


def fake_bwrap(tmp_path: Path, body: str) -> str:
    """A Python script that stands in for bwrap: it gets ``--json-status-fd N ...``."""
    script = tmp_path / "fake-bwrap"
    script.write_text(
        f"#!{sys.executable}\nimport os, sys\nfd = int(sys.argv[2])\nargs = sys.argv[3:]\n{body}"
    )
    script.chmod(0o755)
    return str(script)


def test_fr24a_supervise_reports_exit_code(tmp_path: Path) -> None:
    """FR-24a: the exit code reported on the status fd is returned."""
    bwrap = fake_bwrap(
        tmp_path,
        'os.write(fd, b\'{ "child-pid": 1 }\\n{ "exit-code": 7 }\\n\')\nsys.exit(7)\n',
    )
    assert supervise([bwrap, "--", "prog"], os.environ) == 7


def test_fr24a_supervise_setup_failure(tmp_path: Path) -> None:
    """FR-24a: bwrap exiting without reporting is a setup failure."""
    bwrap = fake_bwrap(tmp_path, "sys.exit(1)\n")
    with pytest.raises(SetupError, match="user namespaces"):
        supervise([bwrap], os.environ)


def test_fr24_supervise_child_signal_mask_is_clean(tmp_path: Path) -> None:
    """FR-24: the child starts without blocked or ignored signals despite our mask."""
    out = tmp_path / "status"
    # A shell script, because Python itself ignores SIGPIPE at startup. The status fd is
    # written through /proc: dash (Debian's /bin/sh) only accepts one-digit fds in >&N.
    bwrap = tmp_path / "fake-bwrap.sh"
    bwrap.write_text(
        f"#!/bin/sh\ncat /proc/self/status > '{out}'\n"
        "printf '{ \"exit-code\": 0 }\\n' > /proc/self/fd/$2\n"
    )
    bwrap.chmod(0o755)
    assert supervise([str(bwrap)], os.environ) == 0
    fields = dict(line.split(":\t") for line in out.read_text().splitlines() if ":\t" in line)
    assert int(fields["SigBlk"], 16) == 0
    assert int(fields["SigIgn"], 16) == 0


def test_supervise_missing_executable(tmp_path: Path) -> None:
    """FR-90: an unstartable bwrap is a setup error."""
    with pytest.raises(SetupError, match="cannot start"):
        supervise([str(tmp_path / "missing")], os.environ)
