"""Integration tests for ``run`` without ``-j`` (AC-3 to AC-5, AC-11 to AC-13, AC-15)."""

import os
import pty
import select
import shutil
import signal
import time
from pathlib import Path

import pytest

from tests.integration.conftest import Cli

pytestmark = pytest.mark.integration


@pytest.fixture(name="tree")
def fixture_tree(tmp_path: Path) -> Path:
    """``proj/`` with a file, a ``secret.txt`` next to it, and an empty ``home/``."""
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "file.txt").write_text("content\n")
    (tmp_path / "secret.txt").write_text("secret\n")
    (tmp_path / "home").mkdir()
    return tmp_path


def interfaces(proc_net_dev: str) -> set[str]:
    """Interface names from the contents of ``/proc/net/dev``."""
    return {line.split(":")[0].strip() for line in proc_net_dev.splitlines()[2:]}


def test_ac3_fr22_only_mounted_paths_are_visible(cli: Cli, tree: Path) -> None:
    """AC-3, FR-22: unmounted files and the real home directory are invisible."""
    result = cli(
        "run",
        "-r",
        str(tree / "proj"),
        "--",
        "sh",
        "-c",
        f'ls -A "$HOME"; cat {tree}/proj/file.txt; test -e {tree}/secret.txt || echo hidden;'
        f" test -e {Path.home()}/.ssh || echo no-ssh",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "content\nhidden\nno-ssh\n"


def test_ac4_read_only_mount(cli: Cli, tree: Path) -> None:
    """AC-4: writing to a ``-r`` path fails with "Read-only file system"."""
    target = tree / "proj" / "file.txt"
    result = cli("run", "-r", str(target), "--", "sh", "-c", f"echo x >> {target}")
    assert result.returncode != 0
    assert "Read-only file system" in result.stderr
    assert target.read_text() == "content\n"


def test_ac5_writable_mount(cli: Cli, tree: Path) -> None:
    """AC-5: files created in a ``-w`` path exist on the host afterwards."""
    result = cli("run", "-w", str(tree / "proj"), "--", "touch", str(tree / "proj" / "new"))
    assert result.returncode == 0, result.stderr
    assert (tree / "proj" / "new").exists()


def test_fr22_home_is_an_empty_writable_tmpfs(cli: Cli, tree: Path) -> None:
    """FR-22: ``$HOME`` is empty and writable; nothing reaches the host."""
    home = tree / "home"
    (home / ".bashrc").write_text("real\n")
    result = cli(
        "run",
        "--",
        "sh",
        "-c",
        'ls -A "$HOME"; echo hi > "$HOME/x" && cat "$HOME/x"',
        env={"HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "hi\n"
    assert sorted(path.name for path in home.iterdir()) == [".bashrc"]


def test_ac11_environment_restriction(cli: Cli) -> None:
    """AC-11: ``--env FOO`` passes FOO and the baseline, not BAR; without it both pass."""
    env = {"FOO": "1", "BAR": "2"}
    restricted = cli("run", "--env", "FOO", "--", "env", env=env).stdout.splitlines()
    assert "FOO=1" in restricted
    assert not any(line.startswith("BAR=") for line in restricted)
    assert any(line.startswith("PATH=") for line in restricted)
    full = cli("run", "--", "env", env=env).stdout.splitlines()
    assert "FOO=1" in full
    assert "BAR=2" in full


def test_ac11b_explicit_values(cli: Cli, tmp_path: Path) -> None:
    """AC-11b: entries from a file and the command line; the last one wins."""
    env_file = tmp_path / "env.txt"
    env_file.write_text("# comment\nA=from file\nB=x=y # kept\nC=\nFOO\n")
    env_file.chmod(0o600)
    result = cli(
        "run",
        "--env-file",
        str(env_file),
        "--env",
        "A=cli",
        "--env",
        "X=a b",
        "--",
        "env",
        env={"FOO": "host", "BAR": "2"},
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    for expected in ["A=cli", "B=x=y # kept", "C=", "FOO=host", "X=a b"]:
        assert expected in lines
    assert not any(line.startswith("BAR=") for line in lines)


def test_ac12_network(cli: Cli) -> None:
    """AC-12: ``--no-network`` leaves only loopback; by default the host network is shared."""
    isolated = cli("run", "--no-network", "--", "cat", "/proc/net/dev")
    assert isolated.returncode == 0, isolated.stderr
    assert interfaces(isolated.stdout) == {"lo"}
    shared = cli("run", "--", "cat", "/proc/net/dev")
    assert interfaces(shared.stdout) == interfaces(
        Path("/proc/net/dev").read_text(encoding="ascii")
    )


def test_ac13_system_tools_work(cli: Cli) -> None:
    """AC-13: system tools and configuration are available on this host's layout."""
    result = cli(
        "run", "--", "sh", "-c", "ls / >/dev/null && cat /etc/os-release && command -v ls"
    )
    assert result.returncode == 0, result.stderr
    assert "NAME=" in result.stdout


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["--", "sh", "-c", "exit 7"], 7),
        (["--", "true"], 0),
        (["--", "sh", "-c", "kill -TERM $$"], 128 + signal.SIGTERM),
        (["--", "nonexistent_prog_4711"], 127),
        (["-w", "/nonexistent-4711", "--", "true"], 125),
        (["--chdir", "/nonexistent-4711", "--", "true"], 125),
    ],
)
def test_ac15_exit_codes(cli: Cli, args: list[str], code: int) -> None:
    """AC-15, FR-24a: the program's exit code, 128+S, 127 and 125."""
    assert cli("run", *args).returncode == code


def test_fr24a_setup_failure_message(cli: Cli) -> None:
    """FR-24a, FR-92: a bwrap failure ends with one agent-sandbox error line."""
    result = cli("run", "--chdir", "/nonexistent-4711", "--", "true")
    assert result.stderr.splitlines()[-1].startswith(
        "agent-sandbox: error: bwrap could not set up the sandbox"
    )


def test_fr23_arguments_and_stdin_are_passed_through(cli: Cli) -> None:
    """FR-6, FR-23: arguments arrive verbatim; stdin and stdout are passed through."""
    printed = cli("run", "--", "printf", "%s|", "a b", "-j", "$HOME", "")
    assert printed.stdout == "a b|-j|$HOME||"
    assert cli("run", "--", "cat", stdin="hello\n").stdout == "hello\n"


def test_fr24_sigterm_is_forwarded_to_the_program(cli: Cli) -> None:
    """FR-24: SIGTERM sent to agent-sandbox reaches the program, which can clean up."""
    script = 'trap "echo got-TERM; exit 42" TERM; echo started; sleep 30 & wait'
    with cli.start("run", "--", "sh", "-c", script) as process:
        assert process.stdout is not None
        assert process.stdout.readline() == "started\n"
        process.send_signal(signal.SIGTERM)
        assert process.stdout.read() == "got-TERM\n"
        assert process.wait(timeout=10) == 42


def read_pty(fd: int, seconds: float) -> bytes:
    """Read everything that arrives on ``fd`` within ``seconds``."""
    data = b""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                data += os.read(fd, 4096)
            except OSError:
                break
    return data


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_fr23_interactive_shell_with_ctrl_c(cli: Cli) -> None:
    """FR-23: an interactive shell works on a terminal; Ctrl-C interrupts only its job."""
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        os.execve(cli.command()[0], cli.command("run", "--", "bash", "--norc", "-i"), cli.env)
    output = read_pty(fd, 2)
    for data in [b"sleep 10; echo after\n", b"\x03", b"echo alive; exit 7\n"]:
        os.write(fd, data)
        output += read_pty(fd, 1.5)
    _, wait_status = os.waitpid(pid, 0)
    text = output.decode(errors="replace")
    assert "alive" in text
    assert "after" not in text.replace("echo after", "")
    assert os.waitstatus_to_exitcode(wait_status) == 7


def test_fr31_symlinked_mount_keeps_given_path(cli: Cli, tmp_path: Path) -> None:
    """FR-31: a mount given through a symlink works under both spellings."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f").write_text("via link\n")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    result = cli(
        "run",
        "-r",
        str(tmp_path / "link"),
        "--",
        "sh",
        "-c",
        f"cat {tmp_path}/link/f {tmp_path}/real/f",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "via link\nvia link\n"


def test_fr39_state_dir_is_hidden(cli: Cli, tree: Path) -> None:
    """FR-39: a mount that contains the state directory does not show it."""
    home = tree / "home"
    state = home / ".local" / "state" / "agent-sandbox"
    state.mkdir(parents=True)
    (state / "pending").write_text("upper layer\n")
    result = cli(
        "run",
        "-w",
        str(home),
        "--state-dir",
        str(state),
        "--",
        "sh",
        "-c",
        f"ls -A {state}; touch {state}/x || echo cannot-write; touch {home}/ok",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "cannot-write\n"
    assert sorted(path.name for path in state.iterdir()) == ["pending"]
    assert (home / "ok").exists()


@pytest.mark.parametrize(("mounted", "expected"), [(True, "proj"), (False, "/")])
def test_chdir_default(cli: Cli, tree: Path, mounted: bool, expected: str) -> None:
    """Section 5.2: the current directory if visible, else ``/``."""
    args = ["-r", str(tree / "proj")] if mounted else []
    result = cli("run", *args, "--", "pwd", cwd=tree / "proj")
    assert result.stdout.strip() == (str(tree / "proj") if mounted else expected)


def test_nfr6_special_characters_in_paths(cli: Cli, tmp_path: Path) -> None:
    """NFR-6: paths with spaces and quotes work."""
    directory = tmp_path / 'it\'s a "dir"'
    directory.mkdir()
    result = cli("run", "-w", str(directory), "--", "touch", str(directory / "new file"))
    assert result.returncode == 0, result.stderr
    assert (directory / "new file").exists()


TTY_PROBE = """
import errno, fcntl, os, termios
master, slave = os.openpty()
for name, request, argument in (
    ("TIOCSTI", termios.TIOCSTI, b"x"),
    ("TIOCLINUX", 0x541C, b"\\x06"),
    ("TIOCGWINSZ", termios.TIOCGWINSZ, bytes(8)),
):
    try:
        fcntl.ioctl(slave, request, argument)
        print(name, "allowed")
    except OSError as exc:
        print(name, errno.errorcode[exc.errno])
"""


@pytest.mark.skipif(shutil.which("python3") is None, reason="needs python3 on the host")
def test_fr24b_terminal_injection_is_blocked(cli: Cli) -> None:
    """FR-24b: TIOCSTI and TIOCLINUX fail with EPERM inside the sandbox; others work."""
    result = cli("run", "--", "python3", "-c", TTY_PROBE)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "TIOCSTI EPERM\nTIOCLINUX EPERM\nTIOCGWINSZ allowed\n"


def test_fr21_no_network_also_hides_abstract_sockets(cli: Cli) -> None:
    """FR-21: --no-network isolates the host's abstract Unix sockets too."""
    script = "grep -c '@' /proc/net/unix || true"
    assert cli("run", "--no-network", "--", "sh", "-c", script).stdout == "0\n"
