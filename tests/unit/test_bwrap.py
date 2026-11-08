"""Tests for the sandbox plan and the bwrap command line (FR-20 to FR-22, FR-31, FR-38, FR-39)."""

from collections.abc import Sequence

import pytest

from agent_sandbox.bwrap import (
    NAMESPACES,
    Bind,
    SandboxPlan,
    build_command,
    build_options,
    format_command,
    plan_sandbox,
)
from agent_sandbox.errors import SetupError
from agent_sandbox.layout import SystemMount
from agent_sandbox.mounts import MountSpec, PathKind
from agent_sandbox.options import MountMode

DIR = PathKind.DIRECTORY
FILE = PathKind.FILE
RO = MountMode.READ_ONLY
RW = MountMode.WRITABLE
JAIL = MountMode.JAILED

FHS_MERGED = (SystemMount("/usr"), SystemMount("/bin", "usr/bin"), SystemMount("/etc"))

HEAD = ["bwrap", *NAMESPACES, "--die-with-parent"]


# Test helper with the same keyword arguments as plan_sandbox.
def make_plan(  # pylint: disable=too-many-arguments
    mounts: Sequence[MountSpec] = (),
    *,
    env: dict[str, str] | None = None,
    cwd: str = "/somewhere",
    state_dir_exists: bool = True,
    jail_sources: dict[str, str] | None = None,
    logical_cwd: str | None = None,
    chdir: str | None = None,
    network: bool = True,
) -> SandboxPlan:
    """Plan a sandbox with merged-/usr system mounts and sensible defaults."""
    return plan_sandbox(
        system_mounts=FHS_MERGED,
        mounts=mounts,
        argv=("prog", "--flag"),
        env={"HOME": "/home/u", "PATH": "/usr/bin"} if env is None else env,
        cwd=cwd,
        state_dir="/home/u/.local/state/agent-sandbox",
        state_dir_exists=state_dir_exists,
        jail_sources=jail_sources,
        logical_cwd=logical_cwd,
        chdir=chdir,
        network=network,
    )


def filesystem_options(plan: SandboxPlan) -> list[list[str]]:
    """The mount-related option groups of a plan."""
    filesystem = {"--proc", "--dev", "--tmpfs", "--ro-bind", "--bind", "--symlink"}
    return [
        option
        for option in build_options(plan)
        if option[0] in filesystem or option[0] == "--remount-ro"
    ]


def test_fr20_fr22_minimal_command_snapshot() -> None:
    """FR-20 to FR-22, FR-57: the complete command for a plan without mount specs."""
    assert build_command(make_plan()) == [
        *HEAD,
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--symlink",
        "usr/bin",
        "/bin",
        "--ro-bind",
        "/etc",
        "/etc",
        "--ro-bind",
        "/usr",
        "/usr",
        "--tmpfs",
        "/home/u",
        "--clearenv",
        "--setenv",
        "HOME",
        "/home/u",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--chdir",
        "/",
        "--",
        "prog",
        "--flag",
    ]


def test_fr20_namespaces() -> None:
    """FR-20: user, mount (implicit in bwrap), PID, IPC, UTS and cgroup namespaces."""
    expected = {"user", "pid", "ipc", "uts", "cgroup"}
    assert {namespace.removeprefix("--unshare-") for namespace in NAMESPACES} == expected


def test_fr21_no_network_unshares_net() -> None:
    """FR-21: ``--no-network`` adds ``--unshare-net``; the default shares the network."""
    assert "--unshare-net" not in build_command(make_plan())
    assert ["--unshare-net"] in build_options(make_plan(network=False))


def test_fr26_bwrap_path_is_used() -> None:
    """FR-90: the command starts with the bwrap that was found."""
    assert build_command(make_plan(), "/usr/bin/bwrap")[0] == "/usr/bin/bwrap"


def test_fr38_mounts_ordered_by_depth_with_system_mounts() -> None:
    """FR-38: system mounts, tmpfs mounts and mount specs are ordered together by depth."""
    plan = make_plan(
        [
            MountSpec("/home/u/proj/docs", RO, DIR),
            MountSpec("/tmp/scratch", RW, DIR),
            MountSpec("/home/u/proj", RW, DIR),
            MountSpec("/data", RO, DIR),
        ]
    )
    assert filesystem_options(plan) == [
        ["--dev", "/dev"],
        ["--proc", "/proc"],
        ["--tmpfs", "/tmp"],
        ["--symlink", "usr/bin", "/bin"],
        ["--ro-bind", "/etc", "/etc"],
        ["--ro-bind", "/usr", "/usr"],
        ["--ro-bind", "/data", "/data"],
        ["--tmpfs", "/home/u"],
        ["--bind", "/tmp/scratch", "/tmp/scratch"],
        ["--bind", "/home/u/proj", "/home/u/proj"],
        ["--ro-bind", "/home/u/proj/docs", "/home/u/proj/docs"],
    ]


def test_fr38_mount_spec_on_root_comes_first() -> None:
    """FR-38: ``-r /`` is the shallowest mount, so everything else is mounted on top."""
    assert filesystem_options(make_plan([MountSpec("/", RO, DIR)]))[0] == ["--ro-bind", "/", "/"]


def test_fr38_mount_on_tmp_comes_after_tmpfs() -> None:
    """FR-38: ``-w /tmp`` replaces the sandbox's own tmpfs, so it comes after it."""
    options = filesystem_options(make_plan([MountSpec("/tmp", RW, DIR)]))
    assert options.index(["--tmpfs", "/tmp"]) < options.index(["--bind", "/tmp", "/tmp"])


def test_fr32_file_mounts() -> None:
    """FR-32: files are bound like directories; read-only and writable."""
    plan = make_plan([MountSpec("/data/a.txt", RO, FILE), MountSpec("/data/b.txt", RW, FILE)])
    assert ["--ro-bind", "/data/a.txt", "/data/a.txt"] in filesystem_options(plan)
    assert ["--bind", "/data/b.txt", "/data/b.txt"] in filesystem_options(plan)


# --- home (FR-22) ---


@pytest.mark.parametrize("mount", ["/home/u", "/home", "/"])
def test_fr22_no_home_tmpfs_if_a_mount_covers_home(mount: str) -> None:
    """FR-22: no tmpfs on ``$HOME`` if a mount spec includes it."""
    plan = make_plan([MountSpec(mount, RO, DIR)])
    assert plan.home_tmpfs is None
    assert ["--tmpfs", "/home/u"] not in build_options(plan)


def test_fr22_home_tmpfs_if_only_parts_are_mounted() -> None:
    """FR-22: parts of ``$HOME`` are mounted on top of the tmpfs."""
    assert make_plan([MountSpec("/home/u/proj", RW, DIR)]).home_tmpfs == "/home/u"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"HOME": "/home/sandbox"}, "/home/sandbox"),
        ({"HOME": "/home/u/"}, "/home/u"),
        ({}, None),
        ({"HOME": ""}, None),
        ({"HOME": "relative"}, None),
        ({"HOME": "/"}, None),
        ({"HOME": "/usr/share/home"}, None),
    ],
)
def test_fr22_home_tmpfs_follows_sandbox_home(env: dict[str, str], expected: str | None) -> None:
    """FR-22, FR-56: the tmpfs goes to the sandbox's ``$HOME``; none if unusable or covered."""
    assert make_plan(env=env).home_tmpfs == expected


# --- aliases (FR-31) ---


def test_fr31_alias_becomes_symlink() -> None:
    """FR-31: a symlinked mount path is also available under its given spelling."""
    plan = make_plan([MountSpec("/data/proj", RW, DIR, aliases=("/home/u/proj",))])
    assert plan.aliases == (("/home/u/proj", "/data/proj"),)
    options = filesystem_options(plan)
    assert options[-1] == ["--symlink", "/data/proj", "/home/u/proj"]
    assert options.index(["--tmpfs", "/home/u"]) < options.index(options[-1])


def test_fr31_covered_alias_is_not_recreated() -> None:
    """FR-31: an alias inside another mount exists there already."""
    plan = make_plan(
        [
            MountSpec("/data/proj", RW, DIR, aliases=("/home/u/proj",)),
            MountSpec("/home/u", RO, DIR),
        ]
    )
    assert not plan.aliases


# --- state directory (FR-39) ---


def test_fr39_state_dir_hidden_if_mount_contains_it() -> None:
    """FR-39: ``-w ~`` would show the state directory, so it is hidden read-only."""
    plan = make_plan([MountSpec("/home/u", RW, DIR)])
    assert plan.hidden == ("/home/u/.local/state/agent-sandbox",)
    options = filesystem_options(plan)
    assert options[-2:] == [
        ["--tmpfs", "/home/u/.local/state/agent-sandbox"],
        ["--remount-ro", "/home/u/.local/state/agent-sandbox"],
    ]
    assert not plan.is_visible("/home/u/.local/state/agent-sandbox/jails")
    assert plan.is_visible("/home/u/.local/share")


@pytest.mark.parametrize(
    ("mounts", "exists"),
    [
        ([], True),
        ([MountSpec("/home/u/proj", RW, DIR)], True),
        ([MountSpec("/home/u", RW, DIR)], False),
    ],
)
def test_fr39_state_dir_not_hidden_if_invisible_or_missing(
    mounts: list[MountSpec], exists: bool
) -> None:
    """FR-39: nothing to hide if no mount shows the state directory, or it does not exist."""
    assert not make_plan(mounts, state_dir_exists=exists).hidden


def test_fr39_alias_inside_hidden_state_dir_is_skipped() -> None:
    """FR-39: no alias symlink is created inside the hidden (read-only) state directory."""
    alias = "/home/u/.local/state/agent-sandbox/link"
    plan = make_plan(
        [MountSpec("/home/u", RW, DIR), MountSpec("/data", RO, DIR, aliases=(alias,))]
    )
    assert not plan.aliases


# --- environment (FR-57, FR-58) ---


def test_fr57_environment_is_cleared_and_set() -> None:
    """FR-57: ``--clearenv`` first, then one ``--setenv`` per variable, sorted by name."""
    options = build_options(make_plan(env={"B": "2", "A": "x y", "HOME": "/home/u"}))
    start = options.index(["--clearenv"])
    assert options[start : start + 4] == [
        ["--clearenv"],
        ["--setenv", "A", "x y"],
        ["--setenv", "B", "2"],
        ["--setenv", "HOME", "/home/u"],
    ]


def test_fr58_masked_options_and_display() -> None:
    """FR-58: the displayed command masks every value; the real command does not."""
    plan = make_plan(env={"TOKEN": "s3cr3t"})
    assert ["--setenv", "TOKEN", "***"] in build_options(plan, mask_env=True)
    assert "s3cr3t" in build_command(plan)
    text = format_command(plan, "/usr/bin/bwrap")
    assert "s3cr3t" not in text
    assert "    --setenv TOKEN '***' \\\n" in text
    assert text.startswith("/usr/bin/bwrap \\\n    --unshare-user \\\n")
    assert text.endswith("    -- prog --flag")


def test_nfr6_special_characters_are_passed_verbatim() -> None:
    """NFR-6: paths with spaces and quotes are single arguments, quoted only for display."""
    path = "/data/it's a dir"
    plan = make_plan([MountSpec(path, RW, DIR)])
    assert ["--bind", path, path] in build_options(plan)
    assert "--bind '/data/it'\"'\"'s a dir' '/data/it'\"'\"'s a dir'" in format_command(plan)


# --- working directory (section 5.2) ---


@pytest.mark.parametrize(
    ("cwd", "logical", "chdir", "expected"),
    [
        ("/data/proj/src", None, None, "/data/proj/src"),
        ("/data/proj", None, None, "/data/proj"),
        ("/elsewhere", None, None, "/"),
        ("/home/u", None, None, "/"),
        ("/data/proj", "/home/u/proj", None, "/home/u/proj"),
        ("/data/proj", "/home/u/other", None, "/data/proj"),
        ("/data/proj", None, "/work", "/work"),
        ("/data/proj", None, "sub/../src", "/data/proj/src"),
        ("/usr/share", None, None, "/usr/share"),
    ],
)
def test_chdir_default_and_option(
    cwd: str, logical: str | None, chdir: str | None, expected: str
) -> None:
    """Section 5.2, FR-30: ``--chdir``, else the visible cwd (logical first), else ``/``."""
    plan = make_plan(
        [MountSpec("/data/proj", RW, DIR, aliases=("/home/u/proj",))],
        cwd=cwd,
        logical_cwd=logical,
        chdir=chdir,
    )
    assert plan.chdir == expected


# --- jails (Step 6) ---


def test_fr36_jail_without_overlay_source_is_rejected() -> None:
    """FR-36: a jailed spec needs an overlay source, which Step 6 provides."""
    with pytest.raises(SetupError, match="jailed writes are not available yet"):
        make_plan([MountSpec("/data/proj", JAIL, DIR)])


def test_fr36_jail_binds_overlay_source_writable() -> None:
    """FR-36: the merged view is bound writable at the original path."""
    plan = make_plan(
        [MountSpec("/data/proj", JAIL, DIR)], jail_sources={"/data/proj": "/state/merged"}
    )
    assert plan.binds == (Bind("/state/merged", "/data/proj", writable=True),)
    assert ["--bind", "/state/merged", "/data/proj"] in build_options(plan)


# --- visibility ---


@pytest.mark.parametrize(
    ("path", "visible"),
    [
        ("/usr/bin/ls", True),
        ("/bin/ls", True),
        ("/etc/hosts", True),
        ("/data/proj/x", True),
        ("/home/u/proj/x", True),
        ("/home/u/.bashrc", False),
        ("/tmp/x", False),
        ("/opt/x", False),
    ],
)
def test_fr9_visibility(path: str, visible: bool) -> None:
    """FR-9: host content is visible below system mounts, mount specs and aliases only."""
    plan = make_plan([MountSpec("/data/proj", RO, DIR, aliases=("/home/u/proj",))])
    assert plan.is_visible(path) is visible


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/data/proj", "/state/merged"),
        ("/data/proj/src/main.c", "/state/merged/src/main.c"),
        ("/data/proj/docs/x", "/data/proj/docs/x"),
        ("/data/notes.txt", "/state/n/merged/notes.txt"),
        ("/data/project2", "/data/project2"),
        ("/usr/bin/ls", "/usr/bin/ls"),
    ],
)
def test_fr9_host_path_follows_deepest_bind(path: str, expected: str) -> None:
    """FR-9: sandbox paths map to their host source through the deepest bind."""
    plan = make_plan(
        [
            MountSpec("/data/proj", JAIL, DIR),
            MountSpec("/data/proj/docs", RO, DIR),
            MountSpec("/data/notes.txt", JAIL, FILE),
        ],
        jail_sources={
            "/data/proj": "/state/merged",
            "/data/notes.txt": "/state/n/merged/notes.txt",
        },
    )
    assert plan.host_path(path) == expected


def test_fr9_host_path_below_root_bind() -> None:
    """FR-9: a bind on ``/`` maps paths unchanged."""
    assert make_plan([MountSpec("/", RO, DIR)]).host_path("/x/y") == "/x/y"


def test_fr116_configuration_file_is_covered_when_visible() -> None:
    """FR-116: the configuration file gets /dev/null read-only on top, and is not visible."""
    config = "/home/u/.config/agent-sandbox/config.toml"
    covered = plan_sandbox(
        system_mounts=FHS_MERGED,
        mounts=[MountSpec("/home/u", RW, DIR)],
        argv=("prog",),
        env={},
        cwd="/",
        state_dir="/state",
        state_dir_exists=False,
        protected_files=[config],
    )
    assert covered.hidden_files == (config,)
    assert filesystem_options(covered)[-1] == ["--ro-bind", "/dev/null", config]
    assert not covered.is_visible(config)
    assert covered.is_visible("/home/u/.config/agent-sandbox/other")
    invisible = make_plan([MountSpec("/data", RO, DIR)])
    assert not invisible.hidden_files
