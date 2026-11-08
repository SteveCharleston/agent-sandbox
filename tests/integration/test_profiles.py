"""Integration tests for profiles (AC-18 to AC-20, FR-110 to FR-119)."""

from pathlib import Path

import pytest

from tests.integration.conftest import Cli

pytestmark = pytest.mark.integration


@pytest.fixture(name="home")
def fixture_home(tmp_path: Path) -> Path:
    """An empty home directory with a project and a scratch directory in it."""
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    (home / "scratch").mkdir()
    (home / "proj" / "a.txt").write_text("a\n", encoding="utf-8")
    return home


def home_env(home: Path) -> dict[str, str]:
    """The environment for a user whose home is ``home`` (default config location)."""
    return {"HOME": str(home), "XDG_CONFIG_HOME": "", "AGENT_SANDBOX_CONFIG": ""}


def config_file(home: Path) -> Path:
    """The default configuration file of ``home``."""
    return home / ".config" / "agent-sandbox" / "config.toml"


BASE = """
[profiles.base]
description = "project jailed, scratch read-only, no network"
jail = ["."]
ro = ["../scratch"]
network = false
command = ["sh", "-c", "echo from-profile; wc -l < /proc/net/dev"]
"""


@pytest.mark.usefixtures("overlay_available")
def test_ac18_init_list_and_run_the_example(cli: Cli, home: Path) -> None:
    """AC-18, FR-118, FR-119: init, list, and run the example profile."""
    env = home_env(home)
    created = cli("profiles", "init", env=env)
    assert created.stdout == f"Created {config_file(home)}\n"
    listing = cli("profiles", env=env)
    assert listing.stdout.startswith("example\tExample: the current directory jailed")
    shell = cli("run", "--profile", "example", cwd=home / "proj", env=env, stdin="pwd\nexit 3\n")
    assert shell.returncode == 3, shell.stderr
    assert f"\n{home / 'proj'}\n" in f"\n{shell.stdout}"  # a login shell may print more


@pytest.mark.usefixtures("overlay_available")
def test_ac18_profile_command_and_settings(cli: Cli, home: Path) -> None:
    """AC-18, FR-115: the profile's command runs with its mounts and network = false."""
    config_file(home).parent.mkdir(parents=True)
    config_file(home).write_text(BASE, encoding="utf-8")
    result = cli("run", "-p", "base", cwd=home / "proj", env=home_env(home))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "from-profile\n3\n"  # two header lines plus loopback


@pytest.mark.usefixtures("overlay_available")
def test_ac18_command_line_overrides_the_profile(cli: Cli, home: Path) -> None:
    """AC-18, FR-115: -w replaces the profile's -r, --network wins, PROGRAM replaces command."""
    config_file(home).parent.mkdir(parents=True)
    config_file(home).write_text(BASE, encoding="utf-8")
    script = "touch ../scratch/written && echo wrote; wc -l < /proc/net/dev"
    result = cli(
        "run",
        "-p",
        "base",
        "-w",
        "../scratch",
        "--network",
        "--",
        "sh",
        "-c",
        script,
        cwd=home / "proj",
        env=home_env(home),
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "wrote"
    assert int(lines[1]) == len(Path("/proc/net/dev").read_text(encoding="ascii").splitlines())
    assert (home / "scratch" / "written").exists()


@pytest.mark.parametrize("mode", ["-w", "-j"])
def test_ac19_configuration_file_is_protected(
    cli: Cli, home: Path, mode: str, request: pytest.FixtureRequest
) -> None:
    """AC-19, FR-116: inside the sandbox the configuration file can't be read or written."""
    if mode == "-j":
        request.getfixturevalue("overlay_available")
    config_file(home).parent.mkdir(parents=True)
    config_file(home).write_text(BASE, encoding="utf-8")
    path = config_file(home)
    script = f"cat {path} || echo no-read; echo x >> {path} || echo no-write"
    result = cli("run", mode, str(home), "--", "sh", "-c", script, env=home_env(home))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "no-read\nno-write\n"
    assert "Permission denied" in result.stderr
    assert path.read_text(encoding="utf-8") == BASE


def test_ac20_broken_configuration_does_not_affect_runs_without_profiles(
    cli: Cli, home: Path
) -> None:
    """AC-20, FR-111: without --profile the configuration file is not read."""
    config_file(home).parent.mkdir(parents=True)
    config_file(home).write_text("this is [not toml", encoding="utf-8")
    result = cli(
        "run", "-r", str(home / "proj"), "--", "cat", f"{home}/proj/a.txt", env=home_env(home)
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "a\n"
    broken = cli("profiles", env=home_env(home))
    assert broken.returncode == 2
    assert "invalid TOML" in broken.stderr
