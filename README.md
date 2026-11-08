# agent-sandbox

AI agents sometimes misbehave: they write to files they should not touch or read
directories they should not see. **agent-sandbox** runs an agent, or any other program,
in a [bubblewrap](https://github.com/containers/bubblewrap) sandbox that only sees the
paths you allow.

- **No second system to maintain.** Unlike a container or VM, the sandbox uses the
  host's own tools (`git`, compilers, language runtimes, ...): the system directories
  are shared read-only and the environment, including `$PATH`, is passed on.
- **Reviewable writes.** With `-j` ("jailed"), every change the program makes lands in
  an overlay ([fuse-overlayfs](https://github.com/containers/fuse-overlayfs)); the real
  directory stays untouched. Afterwards you look at the changes with `diff` and apply
  them with `merge` or throw them away with `discard`. Inside the sandbox, writing
  looks completely normal.
- **No root.** Everything works as a normal user; nothing ever calls `sudo`.

```console
$ agent-sandbox run -j ~/git/myproject -- my-agent     # the agent works in its jail
$ agent-sandbox listdiffs                               # which paths have changes?
/home/me/git/myproject	3	2026-11-08T14:03:12+01:00
$ agent-sandbox diff ~/git/myproject                    # review them as a patch
$ agent-sandbox merge ~/git/myproject                   # apply them (or: discard)
```

## Requirements

agent-sandbox needs Linux, Python 3.11 or newer, and a few host programs. Installing
them and enabling the kernel features is a one-time job for the administrator;
agent-sandbox itself never needs root.

| Needed | For | Check |
|--------|-----|-------|
| `bwrap` (bubblewrap) | every `run` | `bwrap --version` |
| unprivileged user namespaces | every `run` (unless `bwrap` is setuid) | `bwrap --unshare-user --ro-bind / / true` |
| `fuse-overlayfs` | `run -j` | `fuse-overlayfs --version` |
| `fusermount3` (setuid, from fuse3) | `run -j`, cleanup in `merge`/`discard` | `fusermount3 --version` |

Installation per distribution:

- **Debian, Ubuntu:** `sudo apt install bubblewrap fuse-overlayfs fuse3`
- **Fedora:** `sudo dnf install bubblewrap fuse-overlayfs fuse3`
- **Arch Linux:** `sudo pacman -S bubblewrap fuse-overlayfs fuse3`
- **NixOS:** add `bubblewrap` and `fuse-overlayfs` to `environment.systemPackages`;
  NixOS provides `/run/wrappers/bin/fusermount3` by default.

User namespaces are enabled on most distributions. If `bwrap` reports that it cannot
create a namespace, check these settings (as root):

- `sysctl user.max_user_namespaces` must not be `0`.
- Older Debian kernels: `sysctl kernel.unprivileged_userns_clone=1`.
- Ubuntu 24.04 and later restrict them with AppArmor:
  `sysctl kernel.apparmor_restrict_unprivileged_userns=0`, or an AppArmor profile that
  allows `bwrap` to use user namespaces.

Not needed: `/etc/fuse.conf` changes (`allow_other` is not used; the default
`mount_max` is plenty) and `/etc/subuid` entries (the sandbox maps only your own user).

## Installation

```sh
uv tool install git+https://github.com/SteveCharleston/agent-sandbox
# or
pipx install git+https://github.com/SteveCharleston/agent-sandbox
```

From a checkout: `uv sync`, then `uv run agent-sandbox --help`.

## What the program sees

Inside the sandbox the program sees:

- the host's system directories, read-only, so installed tools work
  (FHS: `/usr`, `/bin`, `/lib*`, `/opt`, `/etc`; NixOS: `/nix/store`,
  `/run/current-system`, `/run/wrappers`, `/etc`, `/bin`, `/usr`),
- the paths you give with `-r`, `-w` and `-j`, at their original locations,
- an empty, writable home directory, an empty `/tmp`, and its own `/proc` and `/dev`.

Everything else is invisible: your other files, `~/.ssh`, other projects, the
agent-sandbox state directory. The host layout (FHS or NixOS) is detected
automatically; `--layout` overrides it.

| Option | Meaning |
|--------|---------|
| `-r PATH` | read-only |
| `-w PATH` | writable; writes go directly to the host |
| `-j PATH` | jailed: writes go to an overlay for review (directory or single file) |
| `--env NAME`, `--env NAME=VALUE` | pass only these variables (plus `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `LANG`, `LC_ALL`, `TZ`); `NAME=VALUE` sets a value |
| `--env-file FILE` | the same, one entry per line; `#` starts a comment line |
| `--no-network` | no network (only loopback) |
| `--chdir DIR` | working directory (default: the current one if visible, else `/`) |
| `--dry-run` | print the `bwrap` command (values masked) and exit |
| `--debug` | show each step on stderr |

Without `--env` or `--env-file` the whole environment is passed on. Options end at the
first argument that is not an option; it and everything after it is the program and
its arguments. `--` ends the options explicitly.

`agent-sandbox --help` and `agent-sandbox <subcommand> --help` describe everything in
detail.

## Examples

```sh
# Read-only file, jailed project, writable scratch directory, interactive shell:
agent-sandbox run -r ~/important_file -j ~/git/myproject -w ~/Documents/scratchspace /bin/bash -l

# Arguments after -- are passed on as they are, even if they look like options:
agent-sandbox run -j ~/git/myproject -- /bin/some_program -j -w -v -r

# Only one API key and one setting in the environment, and no network:
agent-sandbox run -j ~/git/myproject --env API_KEY --env DEBUG=1 --no-network -- my-agent

# Secrets from a file (chmod 600 it; agent-sandbox warns otherwise):
agent-sandbox run -j ~/git/myproject --env-file ~/.config/agent.env -- my-agent

# A single jailed file:
agent-sandbox run -j ~/notes.txt -- my-editor ~/notes.txt

# What would run, without running it:
agent-sandbox run --dry-run -r ~/docs -- ls ~/docs
```

## Reviewing jailed changes

Changes from several runs accumulate until you merge or discard them.

- `agent-sandbox listdiffs` lists every jailed path with pending changes: path, number
  of changes and time of the last change, separated by tabs.
- `agent-sandbox diff PATH` prints a git-style patch. `git apply` and `patch -p1` can
  apply it (GNU `patch` keeps exact permissions, `git apply` only the executable bit).
  Directories and binary files are shown as `#` comment lines. `diff --stat` prints a
  summary.
- `agent-sandbox merge PATH` shows a summary, asks, and applies the changes to the real
  path. `-y` skips the question, `--dry-run` only shows the plan. If the host changed a
  file after the program changed it, `merge` lists these conflicts and refuses unless
  you add `--force`. A merge that fails halfway can simply be run again.
- `agent-sandbox discard PATH` deletes the pending changes; the real path is not
  touched.

While a session uses a jailed path, a second session, `merge` and `discard` on it are
refused. Pending changes live in `$AGENT_SANDBOX_STATE_DIR`, else
`$XDG_STATE_HOME/agent-sandbox`, else `~/.local/state/agent-sandbox`, or wherever
`--state-dir` points.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | success |
| `1` | error in `diff`, `listdiffs`, `merge` or `discard` (including a refused merge) |
| `2` | usage error |
| `125` | `run`: the sandbox could not be set up |
| `127` | `run`: the program does not exist in the sandbox |
| *N* | `run`: the program's exit code |
| `128+S` | `run`: the program was killed by signal *S* |

## Security model

agent-sandbox protects against an agent that **accidentally or carelessly** reads or
writes outside what you gave it. It is not a hardened boundary against a determined
attacker who exploits kernel or bubblewrap bugs.

What it does:

- Only the system directories (read-only) and your mount paths are visible.
- Jailed writes never touch the real path until you merge them; the state directory
  with the pending changes is hidden from the sandbox, so a program cannot change them
  behind your back.
- The program runs in its own user, PID, IPC, UTS, cgroup and mount namespaces, so it
  cannot see or signal your other processes, and has no capabilities.
- Terminal input injection (`TIOCSTI`, `TIOCLINUX`) is blocked with a seccomp filter.
- Environment values are never printed by agent-sandbox, not even with `--debug`.

What it does not do:

- **The network is shared by default.** This includes the host's *abstract* Unix
  sockets, for example D-Bus or X11, so a program can talk to those services. Use
  `--no-network` when the program does not need the network.
- **The environment is passed on by default**, including tokens and keys in it. Use
  `--env`/`--env-file` to pass only what the program needs.
- **All of `/etc` is readable**, so the program can read host configuration (but not
  files your user cannot read, such as `/etc/shadow`).
- No limits on CPU, memory or disk usage.
- `-w` paths are written directly; only `-j` paths are reviewable.

## Known limitations

- **Single-file jails:** replacing or deleting the jailed file fails with "Device or
  resource busy" (editors that save via rename are affected), and new files next to
  it are lost when the sandbox exits. Jail the directory instead if that matters.
- **Read-only directories:** inside a directory that is read-only in the real tree
  (for example Go's module cache), nothing can be changed in a jail. This is a
  fuse-overlayfs limitation.
- **Ctrl-C on a non-interactive program** started directly (for example
  `run -- python script.py`) ends the sandbox at once; the program gets no chance to
  clean up. Interactive shells and full-screen programs (most agent TUIs) are not
  affected.
- **`$PATH` entries** outside the sandbox (for example `~/.local/bin`) are not
  visible; `run` warns about them. Add them with `-r` if needed.
- **NixOS:** tools installed only through `nix profile` or `/nix/var/nix/profiles`
  are not visible unless you add that directory with `-r`.
- Special files (FIFOs, sockets) created in a jail cannot be merged.

## Development

The project is developed spec-first: [specs/SPEC.md](specs/SPEC.md) is the
specification, [specs/PLAN.md](specs/PLAN.md) the implementation plan, and
[specs/NOTES.md](specs/NOTES.md) records what experiments on real systems showed.
Tests name the requirements (`FR-n`, `AC-n`) they check, and a test enforces that
every requirement has one.

```sh
uv sync
uv run black --check . && uv run isort --check . && uv run flake8 \
  && uv run pylint src tests && uv run mypy src tests && uv run pytest
```

Integration tests need `bwrap`, `fuse-overlayfs` and user namespaces; they are skipped
automatically where these are not available. Nothing in the test suite needs root.

## License

MIT, see [LICENSE](LICENSE).
