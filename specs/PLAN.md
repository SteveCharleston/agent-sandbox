# Agent Sandbox: Implementation Plan

Companion to [SPEC.md](SPEC.md). The spec says **what**, this plan says **in which order**.
Requirement IDs (`FR-n`, `NFR-n`, `AC-n`) refer to the spec.

## Working Rules

- One step at a time. Each step ends in a green state: all checks of the "Quality gate"
  pass, and the step's "Done when" holds. Commit after each step.
- Tests first, or at least together with the code. Every test names the FR it covers
  in its name or docstring (NFR-13).
- If the spec is silent or wrong, change the spec first, then the code.
- Keep logic in pure functions and put side effects (subprocess, mounts, filesystem
  changes) in thin layers. This keeps most of the code unit-testable without `bwrap`
  (NFR-11).
- No step introduces `sudo` or privileged operations (FR-95).

### Quality gate (run after every step)

```
uv run black --check .
uv run isort --check .
uv run flake8
uv run pylint src tests
uv run mypy src tests
uv run pytest
```

## Target Module Layout

```
src/agent_sandbox/
    __init__.py      # __version__
    __main__.py      # python -m agent_sandbox
    cli.py           # argparse setup, run-argument splitting, dispatch, exit codes
    options.py       # typed parse results: RunOptions, DiffOptions, ... (one per subcommand)
    errors.py        # SandboxError hierarchy mapped to exit codes
    envspec.py       # --env / --env-file parsing, environment building (6.4)
    layout.py        # host layout detection and system mount tables (6.2)
    mounts.py        # MountSpec model, path resolution, validation (6.3)
    bwrap.py         # sandbox plan and pure bwrap argument builder
    programs.py      # program lookup and PATH visibility (FR-9, FR-29)
    supervisor.py    # spawning bwrap, signal forwarding, exit status (FR-23, FR-24)
    deps.py          # dependency and user-namespace checks (9.2)
    state.py         # state dir, jail ids, meta.json, locking (8)
    overlay.py       # fuse-overlayfs mount/unmount (7)
    session.py       # `run` orchestration and lifecycle (6.5)
    upper.py         # reading upper layers: whiteouts, opaque dirs (FR-72)
    diffing.py       # diff, listdiffs, stat (5.3, 5.4)
    merging.py       # merge and discard (5.5)
tests/
    unit/            # pure tests, no bwrap needed
    integration/     # @pytest.mark.integration, skipped without bwrap/FUSE
```

---

## Step 0: Feasibility Spike (no code kept)

**Goal:** Prove the core design works unprivileged on this machine before building on it.

**Tasks**
1. Mount `fuse-overlayfs` manually with a temporary lower, upper, work and merged directory
   as a normal user.
2. Start `bwrap` with `--bind merged /some/path` and write files inside. Confirm the
   changes appear only in `upper`.
3. Observe how fuse-overlayfs represents deletions and replaced directories in `upper`
   (whiteout style, opaque markers). Write the findings down in `specs/NOTES.md`.
4. Try the single-file case (FR-37): bind one file from the merged view, and test the
   `rename()` over bind mount (`EBUSY`) limitation.
5. Verify `fusermount3 -u` cleanup, including after `kill -9` of the mounting process.
6. Check what `/etc`, `$HOME`, and NixOS paths look like inside a minimal `bwrap` command
   (`--ro-bind` set from 6.2).

**Done when:** `specs/NOTES.md` records working commands and the whiteout format. Any
spec change needed because of findings has been made.

---

## Step 1: Project Skeleton

**Spec:** NFR-1 to NFR-6, 10.2, FR-2

**Tasks**
- `uv init` with `src/` layout, `pyproject.toml` (project metadata, `requires-python
  >= 3.11`, console script `agent-sandbox = agent_sandbox.cli:main`).
- Dev dependency group: `black`, `isort`, `flake8`, `pylint`, `mypy`, `pytest`.
- Tool configuration: black and isort at line length 99, `.flake8`
  (`max-line-length = 99`, `extend-ignore = E203,W503`), pylint and mypy (`strict`) in
  `pyproject.toml`. `pytest` markers: register `integration`.
- Minimal `cli.main()` supporting `--version` and `--help`.
- `.gitignore` additions (`.venv`, caches), commit `uv.lock`.
- A trivial test for `--version`.

**Done when:** the quality gate passes on the skeleton, and `uv run agent-sandbox
--version` works.

---

## Step 2: CLI Parsing

**Spec:** FR-1, FR-3, FR-6 to FR-10, section 5 (all subcommand signatures)

**Tasks**
- Build the argparse structure for all five subcommands with full `--help` text,
  including examples and exit codes (FR-1).
- Implement the `run` argument splitter: parse options up to the first non-option
  argument or `--`, and pass everything after it verbatim (FR-6, FR-7). argparse alone
  does not do this correctly, so split the argv list by hand first, then give only the
  option part to argparse.
- Support `-rPATH`, `-r PATH`, `--ro=PATH`, `--ro PATH` (FR-10).
- Define a typed `RunOptions` dataclass (mount specs as unresolved strings, env entries
  in order, flags, program, args).
- Exit code 2 for usage errors, no program, unknown subcommand (FR-3, FR-8).
- The subcommands are stubs that report "not implemented" (exit 1).

**Tests (unit):** AC-1 and AC-2 parsing cases, `--` handling, program flags that look
like mount flags (`-j -w -v -r`), repeated flags, missing program, equals forms, unknown
flag before the program, and help output content.

**Done when:** all parsing tests pass and `--help` of every subcommand is complete.

---

## Step 3: Environment Handling

**Spec:** FR-50 to FR-58, AC-11, AC-11b

**Tasks**
- `envspec.py`: parse entries (`NAME`, `NAME=VALUE`) from `--env` and `--env-file` into
  an ordered list of entries. Validate names (`[A-Za-z_][A-Za-z0-9_]*`), report file
  and line number on errors (FR-53, FR-54).
- `build_environment(host_env, entries) -> dict[str, str]` as a pure function:
  full environment when there are no entries, otherwise baseline plus entries with
  last-wins semantics (FR-50 to FR-56).
- Masking helper for logging and `--dry-run` (FR-58). Warning for group/world-readable
  env files.
- Integrate with the CLI so that env entries keep their command-line order across
  `--env` and `--env-file` (FR-55). (Parsing in Step 2 must record the order, check
  and adjust.)

**Tests (unit):** literal values (`=` and `#` in the value, empty value, spaces), comments
and blank lines, bad names, last-wins, baseline override, missing host variable, masking,
restricted mode triggered by `NAME=VALUE` only.

**Done when:** AC-11 and AC-11b logic is covered at unit level.

---

## Step 4: Layout Detection and Mount Model

**Spec:** FR-25 to FR-28, FR-30 to FR-35, FR-38, FR-39 (error rule), FR-94 (file types)

**Tasks**
- `layout.py`: `detect_layout(root: Path) -> Layout` (`/etc/NIXOS`,
  `/run/current-system`), and data tables for FHS and NixOS system mounts. Functions take
  a `root` parameter, so tests can use a fake filesystem tree in `tmp_path`.
- Merged-`/usr` symlink handling: classify entries as `bind` or `symlink` (FR-26).
- `mounts.py`: `MountSpec(path, mode)`, resolution of paths (absolute, symlinks
  resolved, FR-31), existence checks (FR-32), duplicate and conflict rules (FR-34,
  FR-35), forbidding `-w`/`-j` inside system mounts (FR-30), ordering by depth (FR-38),
  forbidding mount specs on or inside the state directory (FR-39; the directory is a
  parameter here and gets resolved in Step 5).
- Detect unsupported file types (socket, device, FR-94).

**Tests (unit):** fake FHS and fake NixOS trees, merged-`/usr`, nested mounts, same path
different modes, `-j` in `-j`, `-w` in system mount, symlinked spec path, relative path,
nonexistent path, ordering regardless of the command line order.

**Done when:** given a fake root and a list of raw specs, a validated and ordered mount
plan is produced.

---

## Step 5: bwrap Command Builder and `run` Without `-j`

**Spec:** FR-9, FR-20 to FR-24, FR-29, FR-38, FR-39, FR-80, FR-90 to FR-93, FR-96, `--dry-run`, `--chdir`,
`--no-network`, exit codes (5.6)

**Tasks**
- `bwrap.py`: pure function `build_command(plan, env, options) -> list[str]`: namespaces,
  `--die-with-parent`, `--proc`, `--dev`, tmpfs `/tmp`, tmpfs `$HOME`, system mounts,
  `--ro-bind` / `--bind` for specs, `--clearenv` / `--setenv` (FR-57), `--chdir`,
  `--unshare-net`, then `--` and the program. System mounts and mount specs are sorted
  together by depth (FR-38). Recreate `MountSpec.aliases` as symlinks to the resolved
  path unless their location is already covered by another mount (FR-31).
- State directory location (FR-80) as a pure function (moved here from Step 6, because
  FR-39 needs it): pass it to the mount plan and hide it with a read-only tmpfs when a
  mount spec contains it (FR-39).
- `deps.py`: check for `bwrap` (and later fuse tools), user-namespace failure
  detection with a helpful message (FR-90, FR-91).
- `session.py`: run `bwrap` with passthrough stdio, forward `SIGINT`/`SIGTERM`/`SIGHUP`,
  map the exit status (exit code, `128+S`, 127, 125).
- Program resolution and `PATH` warning for entries not visible in the sandbox
  (FR-9, FR-29).
- `--dry-run` and `--debug` output with masked env values (FR-58, FR-93).
- Standard error format `agent-sandbox: error: ...` (FR-92).

**Tests**
- Unit: exact command lines for typical plans (snapshot style), `--no-network`,
  `--chdir` fallback, env handling, special characters in paths (NFR-6), exit code mapping.
- Integration (AC-3, AC-4, AC-5, AC-11, AC-12, AC-13, AC-15, AC-16b without `-j`): hidden
  files, read-only enforcement, writable paths, env restriction, network, layouts,
  exit codes.

**Milestone:** `agent-sandbox run -r ... -w ... -- bash -l` works. This is already useful
without overlay.

---

## Step 6: State, Locking, and Overlay Mounting (`-j`)

**Spec:** FR-36, FR-37, FR-60 to FR-63, FR-70 to FR-76, FR-80 to FR-85, FR-97, FR-98

**Tasks**
- `state.py`: state directory layout (location from Step 5), jail id from the SHA-256
  of the resolved path, `meta.json` with version (FR-81, FR-83), `0700` permissions
  (FR-82), path normalization (FR-84).
- Locking with `flock` and the owner PID, stale lock detection, and a clear message for
  concurrent sessions (FR-76, FR-85).
- `overlay.py`: mount `fuse-overlayfs` (lower, upper, work, merged) as an unprivileged
  user, wait for readiness, unmount via `fusermount3 -u` with retries (FR-73, FR-74,
  FR-97).
- Single-file jails: overlay on the parent directory, bind just the file (FR-37).
- Extend `session.py` and `bwrap.py` so that jail specs bind the merged view at the
  original path. Use a context manager or `ExitStack` so overlays are unmounted in all
  cases (FR-61, FR-63): normal exit, signals, setup failure halfway.
- Stale mount and stale lock recovery (FR-62), which never deletes upper data.
- Checks for `fuse-overlayfs` and `fusermount3` in `deps.py` when `-j` is used (FR-90).

**Tests**
- Unit: state paths, id stability, precedence of state dir overrides, lock behavior with
  fake PIDs, order of setup and teardown with a faked overlay layer (including failure
  injection).
- Integration: AC-8 (transparency), AC-9 (accumulation), AC-10 (concurrency
  refusal), AC-14 (single file), AC-16 (cleanup after `SIGTERM`), checksum test that the
  lower layer is untouched (FR-71).

**Milestone:** jailed writes work, changes accumulate in the state directory.

---

## Step 7: Reading Upper Layers, `diff`, `listdiffs`

**Spec:** FR-11 to FR-18, FR-72

**Tasks**
- `upper.py`: iterate an upper layer and classify entries as added, modified, deleted,
  replaced (opaque dir), mode-changed, symlink or type change, using the whiteout
  representation recorded in `specs/NOTES.md` (Step 0). Work purely on directory paths,
  so it can be tested with hand-built upper trees as well as real overlay output.
- `diffing.py`: unified diff generation (via `difflib`, no external `diff` needed),
  binary file notes, header paths relative to `PATH`, optional `--stat` (FR-12, FR-13,
  FR-15). No output for no changes (FR-14).
- `listdiffs`: tab-separated output (FR-16 to FR-18). Ignore entries without changes.
- Both work even if the original path no longer exists (FR-45), with the exception that
  `diff` against a missing lower treats everything as added.

**Tests (unit):** hand-built upper trees for every change kind, binary detection, empty
state, unusual filenames (spaces, newlines, unicode), `patch` compatibility check
for text changes.

**Integration:** AC-6 up to the diff stage.

---

## Step 8: `merge` and `discard`

**Spec:** FR-40 to FR-46, FR-99, open question 1

**Tasks**
- `merging.py`: plan then apply. Build an ordered list of operations from the upper
  layer (create dir, copy file, set mode and mtime, symlink, delete, replace directory),
  show a summary, and ask for confirmation unless `-y` (FR-40). `--dry-run` prints the
  plan only (FR-41).
- Apply operations idempotently, in a safe order (deletions of replaced dirs before
  creation, parents before children), and clear the upper layer only after full success
  (FR-42). On failure, report what was applied.
- Conflict warning when the real file is newer than the upper copy (open question 1
  default).
- `discard` with confirmation, and refusal while a session holds the lock (FR-43,
  FR-44). Unknown path error (FR-46).
- No `chown`, modes limited to what an unprivileged user may set (FR-99).

**Tests (unit):** each operation kind, idempotence (merge twice), simulated failure midway
(upper layer kept), confirmation handling (`-y`, `n`, non-interactive stdin), permission
preservation, symlinks, directory replacement.

**Integration:** AC-6 (full cycle), AC-7 (discard), AC-10 (merge refused during session).

**Milestone:** the complete workflow from the spec works.

---

## Step 9: Hardening, Documentation, Final Acceptance

**Spec:** FR-4, FR-5, FR-62, FR-92, FR-94, FR-100 to FR-102, NFR-13, NFR-20, NFR-30,
AC-1 to AC-17

**Tasks**
- Walk through section 11 and make sure every AC has an automated test (unit or
  integration), and every FR has at least one test referencing it (NFR-13). Add a
  small script or test that fails if an FR ID from the spec is not referenced in `tests/`.
- Seccomp filter against `TIOCSTI`/`TIOCLINUX` on hosts that still allow them (FR-24b).
- Edge cases from FR-94: unreadable paths, sockets and devices, filesystems that don't
  support overlay upper layers, unwritable state directory, symlink loops. Warn when
  started as root (FR-100).
- Verify that errors never print tracebacks without `--debug` (FR-92) and that secrets
  never appear in output (FR-58).
- `README.md`: purpose, install (uv/pipx), prerequisites with distro-specific setup for
  `bwrap`, `fuse-overlayfs`, user namespaces and `/etc/fuse.conf` (FR-101), quickstart,
  the examples from section 11, the NixOS notes, the single-file jail limitation, and
  a "Security model and non-goals" section.
- Measure start-up overhead (NFR-30). Optimize only if the target is missed.
- Run the `strace` check for AC-16b.
- Resolve or explicitly accept the remaining open questions (spec section 12) and update
  the spec.
- Tag `v0.1.0`.

**Done when:** all of AC-1 to AC-17 pass on at least one FHS machine. NixOS behavior is
verified on a real NixOS system or documented as covered by unit tests only.

---

## Summary

| Step | Deliverable | Needs bwrap/FUSE to test |
|------|-------------|--------------------------|
| 0 | Feasibility notes (`NOTES.md`) | yes (manual) |
| 1 | Project skeleton, tooling, CI-ready gate | no |
| 2 | CLI parsing | no |
| 3 | Environment handling | no |
| 4 | Layout detection, mount model | no |
| 5 | bwrap builder and `run` without `-j` | partly |
| 6 | State, locks, overlay, `-j` | yes |
| 7 | `diff`, `listdiffs` | partly |
| 8 | `merge`, `discard` | partly |
| 9 | Hardening, README, final acceptance | yes |

Steps 1 to 4 need no sandbox tooling. They can be done and verified anywhere, and they
cover the largest share of the logic.

## Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| fuse-overlayfs cannot be mounted unprivileged on some hosts (sysctl, `/etc/fuse.conf`) | `-j` unusable | Step 0 check, clear error messages (FR-91), documented prerequisites (FR-101) |
| Whiteout/opaque representation differs between fuse-overlayfs versions | `diff`/`merge` wrong | Record the format in `NOTES.md`, test with real overlay output in integration tests, pin the minimum version in the README |
| Single-file jail breaks programs that replace files via `rename()` | Surprising failures | Documented limitation (FR-37), covered in `--help` |
| Stale FUSE mounts after a crash | Disk state confusing, locked data | Recovery routine (FR-62) and tests with `kill -9` |
| NixOS cannot be tested locally | Layout bugs go unnoticed | Table-driven layout with fake root trees, a real NixOS check in step 9 (VM or a friend's machine) |
| bwrap behavior differs between versions | Flaky integration tests | Record the minimum bwrap version in the README, and skip tests with a clear reason |
