# Agent Sandbox: Specification

Status: Draft v0.1
Keywords **MUST**, **SHOULD**, **MAY** are used as in RFC 2119.
Requirements have stable IDs (e.g. `FR-12`) so tests and code can reference them.

---

## 1. Overview

AI agents sometimes misbehave: they write to files they should not touch or read
directories they should not see. **Agent Sandbox** (`agent-sandbox`) is a command-line
tool that runs an arbitrary program (typically an AI agent) inside a
[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) sandbox. The program
can only see the files and directories that the user explicitly allows.

Unlike Docker/VM-based approaches, Agent Sandbox does **not** need a second system with
its own set of installed tools. The host's own tools (`ls`, `cat`, `git`, compilers, ...)
are made available inside the sandbox by sharing the relevant system directories
read-only, plus the host environment (notably `$PATH`).

The central safety feature is **jailed writes**: a directory can be exposed through
`fuse-overlayfs`, so every change the program makes lands in a separate overlay layer.
From inside the sandbox this looks like normal writing to the original path. After the
session, the user can inspect the changes (`diff`) and then either apply them to the real
location (`merge`) or throw them away (`discard`).

## 2. Goals and Non-Goals

### Goals

- G1: Restrict filesystem visibility of a program to an explicit allowlist.
- G2: Reuse host tools without a container image or VM.
- G3: Support standard FHS Linux distributions and NixOS.
- G4: Provide transparent, reviewable write isolation via fuse-overlayfs.
- G5: Be a plain CLI with no daemon, no config file requirement, no interactive UI.
- G6: Work entirely as an unprivileged user. No root account, `sudo`, or other privilege
  escalation is needed to install configuration, run, review, merge, or discard (see 9.1).

### Non-Goals

- Complex interaction with the sandboxed program. The tool starts the program, connects
  stdio, and returns its exit status. It does not mediate, proxy, or script it.
- Resource limits (CPU, memory, disk quotas).
- Protection against a malicious program that exploits kernel or bubblewrap
  vulnerabilities. This is a guard against accidental misbehavior and overreach, not a
  hardened security boundary.
- Windows or macOS support.
- Any interface besides the command line (no GUI, no REST API, no config daemon).

## 3. Users and Use Cases

The primary user is a developer who runs AI coding agents on their own workstation.

- UC1: Run an agent on a project: the project is writable only via overlay, `~/.ssh` and
  all other home contents are invisible.
- UC2: Give an agent read-only access to reference material (`-r ~/docs`).
- UC3: Give an agent a scratch directory it may freely write to (`-w`).
- UC4: Review what the agent changed (`diff`), then apply (`merge`) or reject (`discard`).
- UC5: Run an arbitrary program, such as an interactive shell, in the same restricted
  environment for ad-hoc experiments.

## 4. Terminology

| Term | Meaning |
|------|---------|
| Sandbox | The bubblewrap environment a program runs in. |
| Session | One invocation of `agent-sandbox run`. |
| Mount spec | A path plus an access mode, given via `-r`, `-w`, or `-j`. |
| Read-only (`-r`) | Path is visible inside the sandbox and not writable. |
| Writable (`-w`) | Path is bind-mounted read-write with direct access to the host. |
| Jailed (`-j`) | Path is presented through fuse-overlayfs. Writes go to an upper layer and the lower layer (the real path) is untouched. |
| Lower layer | The real host directory underneath an overlay. |
| Upper layer | The directory that stores all changes made during sessions. |
| Pending changes | Content of an upper layer not yet merged or discarded. |
| State directory | Where Agent Sandbox stores upper layers and metadata (see 8.1). |
| System mounts | Host directories always shared read-only so system tools work (see 6.2). |
| Layout | The host system type: `fhs` or `nixos`. |

## 5. Command-Line Interface

The program is `agent-sandbox`. All functionality is exposed through subcommands and
flags.

```
agent-sandbox [--help] [--version] <subcommand> [options] [arguments]
```

| Subcommand | Purpose |
|------------|---------|
| `run` | Run a program inside the sandbox. |
| `diff <path>` | Show pending changes for a jailed path. |
| `listdiffs` | List all jailed paths that have pending changes. |
| `merge <path>` | Apply pending changes to the real path, then clear them. |
| `discard <path>` | Delete pending changes for a path. |

### 5.1 General requirements

- FR-1: `agent-sandbox --help` and `agent-sandbox <subcommand> --help` **MUST** print
  comprehensive help: purpose, all flags with descriptions, defaults, at least one
  example per subcommand, and exit codes. It **MUST** go to stdout and exit with 0.
- FR-2: `--version` **MUST** print the version and exit with 0.
- FR-3: Invoking without a subcommand, or with an unknown subcommand or flag, **MUST**
  print a short usage hint to stderr and exit with 2.
- FR-4: The tool itself **MUST** write only to stdout and stderr (plus state directory
  files, see 8). It **MUST NOT** create other output files, logs, or prompts, except for
  the explicit confirmation prompt of `merge`/`discard` (FR-40, FR-44).
- FR-5: Output intended for machine consumption (`listdiffs`, `diff`) goes to stdout.
  Diagnostics, warnings, and errors go to stderr.

Options common to all subcommands:

| Option | Meaning |
|--------|---------|
| `-h`, `--help` | Show help for the subcommand (FR-1). |
| `--debug` | Show detailed steps and tracebacks on stderr (FR-92, FR-93). Also accepted before the subcommand (`agent-sandbox --debug run ...`). |
| `--state-dir DIR` | Override the state directory (FR-80). |

### 5.2 `run`

```
agent-sandbox run [options] [--] PROGRAM [ARGS...]
```

#### Mount flags (all repeatable)

| Flag | Meaning |
|------|---------|
| `-r PATH`, `--ro PATH` | Make `PATH` visible read-only. |
| `-w PATH`, `--rw PATH` | Make `PATH` visible and writable directly on the host (no overlay). |
| `-j PATH`, `--jail PATH` | Make `PATH` visible with writes redirected to an overlay (jailed write). |

#### Other options

| Option | Meaning |
|--------|---------|
| `--env NAME` | Share only the named host variable (repeatable). See 6.4. |
| `--env NAME=VALUE` | Set `NAME` to `VALUE` inside the sandbox, regardless of the host (repeatable). See 6.4. |
| `--env-file FILE` | Read `NAME` and `NAME=VALUE` entries from `FILE` (repeatable). See 6.4. |
| `--no-network` | Disable network access in the sandbox. Network is shared by default. |
| `--chdir DIR` | Working directory inside the sandbox. Default: current directory if visible, else `/`. |
| `--layout {auto,fhs,nixos}` | Override host layout detection. Default: `auto`. |
| `--state-dir DIR` | Override state directory (see 8.1). |
| `--dry-run` | Print the full `bwrap` command line (and overlay setup) to stdout and exit without running anything. |

#### Command-line parsing

- FR-6: Mount flags and options **MUST** be parsed only up to the first argument that is
  not a recognized option (or its value). That argument and everything after it is the
  program and its arguments, passed verbatim. Example: in
  `agent-sandbox run -r ~/f -j ~/p /bin/bash -l`, `-l` belongs to `bash`. An argument
  before the program that starts with `-` but is not a known option is a usage error
  (exit 2), so a mistyped option is never executed as the program. A program whose
  name starts with `-` must follow `--`.
- FR-7: A literal `--` **MUST** end option parsing explicitly. Everything after it is
  the program and its arguments, even if it looks like a flag. Example:
  `agent-sandbox run -j ~/p -- /bin/prog -j -w -v -r`.
- FR-8: If no program is given, `run` **MUST** fail with exit code 2 and a clear message.
- FR-9: If `PROGRAM` contains no `/`, it **MUST** be resolved via `$PATH` as seen inside
  the sandbox. If resolution fails, `run` fails with a clear message (exit 127, see 5.6).
  "Seen inside the sandbox" means: the sandbox's `PATH` (after 6.4), only entries
  that lie inside a bind-mounted or symlinked system path, a mount spec, or a mount
  spec alias (FR-31), and not inside the hidden state directory (FR-39). A `PROGRAM`
  containing `/` is checked the same way, relative to the working directory. The
  check happens before `bwrap` starts; `PROGRAM` is passed to `bwrap` unchanged, so
  `argv[0]` stays as given. Inside a jailed path the check uses the merged view, so a
  program created in an earlier session (pending changes) is found.
- FR-10: Short flags **MUST** accept both `-r PATH` and `-rPATH`. Long flags **MUST**
  accept both `--ro PATH` and `--ro=PATH`. Further rules:
  - `-r=PATH` is treated like `-rPATH` with the `=` removed (argparse convention).
  - The argument after an option that takes a value is always that value, even if it
    starts with `-` (`-r -x` mounts the path `-x`; `-r -- prog` mounts the path `--`).
    Empty values (`--ro=`) are passed on and rejected later by validation.
  - Long options **MUST** be spelled out completely. Abbreviations (`--jai`) are
    unknown options (usage error).
  - These rules apply to the options of every subcommand, not only `run`.

### 5.3 `diff`

```
agent-sandbox diff [--stat] [--state-dir DIR] [--debug] PATH
```

| Option | Meaning |
|--------|---------|
| `--stat` | Print a summary instead of the full diff (FR-15). |

- FR-11: Prints pending changes for the jailed path `PATH` as a unified diff to stdout.
- FR-12: The diff **MUST** cover: modified text files, added files, deleted files,
  added/removed directories, permission changes, symlink changes, and (as a one-line
  note, not content) changed binary files.
- FR-13: Output **SHOULD** be usable with `patch`/`git apply` for text changes where the
  format allows it. Paths in the diff header are relative to `PATH`. Format details
  (verified in Step 7: `git apply` and GNU `patch -p1` both apply the output to a copy of
  the original and reproduce the merged view, except for binary content and empty
  directories):
  - git's extended format: `diff --git a/P b/P`, `new file mode`, `deleted file mode`,
    `old mode`/`new mode`, and `index OLD..NEW [MODE]` with git blob hashes, then
    `---`/`+++` and hunks with 3 lines of context and `\ No newline at end of file`
    markers. For a jailed file (FR-37), `P` is the file's name.
  - Modes show the exact permission bits (`100600`, `100755`). Symlinks are mode
    `120000` with the target as content, as in git. GNU `patch` applies the exact
    permissions; `git apply` keeps only the executable bit (git's file model).
  - Paths are C-quoted like git (`"a/tab\tname"`, octal escapes for non-ASCII bytes);
    unlike git, paths with spaces are quoted too, because GNU `patch` cuts names at
    spaces otherwise.
  - Changes a patch cannot express are comment lines starting with `# `, which both
    tools ignore: `# added directory P/ (mode 0755)`, `# deleted directory P/`,
    `# changed mode of directory P/ from 0755 to 0700`, `# binary file P modified`
    (also `added`/`deleted`), `# added special file P`.
  - Text files are compared line by line on `\n` only. A file is binary if its first
    8000 bytes contain a NUL byte (git's rule). Bytes that are not UTF-8 are passed
    through unchanged.
  - Entries are sorted by path; a type change is a deletion followed by an addition.
- FR-14: If there are no pending changes, print nothing and exit with 0.
- FR-15: `--stat` **MAY** be supported to print a summary (files changed, added, deleted)
  instead of the full diff. Format: one line per change,
  `STATUS<TAB>ADDED<TAB>DELETED<TAB>PATH`, with status `A`, `M` or `D`, line counts (`-`
  for directories, binary and special files) and directories ending in `/`; then a
  summary line `N changes: A added, M modified, D deleted`.

### 5.4 `listdiffs`

```
agent-sandbox listdiffs [--state-dir DIR] [--debug]
```

- FR-16: Lists all jailed paths that currently have pending changes, one per line:
  absolute original path, number of changed entries, and last modification time of the
  upper layer, separated by tabs. The count is the number of changes as `diff --stat`
  reports them (directories included). The time is the latest modification time of
  any entry in the upper layer, as ISO 8601 local time with offset
  (`2026-11-08T14:03:12+01:00`). Paths are quoted like in FR-13 if they contain special
  characters, so tabs and newlines cannot break the format. Lines are sorted by path.
  Broken state (unreadable `meta.json`, or a jail directory that does not match its
  path) is skipped with a warning on stderr.
- FR-17: Paths whose state exists but has no changes are not listed.
- FR-18: Output is empty and exit code is 0 if nothing is pending.

### 5.5 `merge` and `discard`

```
agent-sandbox merge [-y] [--dry-run] [--force] [--state-dir DIR] [--debug] PATH
agent-sandbox discard [-y] [--state-dir DIR] [--debug] PATH
```

| Option | Meaning |
|--------|---------|
| `-y`, `--yes` | Do not ask for confirmation (FR-40, FR-44). |
| `--dry-run` | `merge` only: show what would change, modify nothing (FR-41). |
| `--force` | `merge` only: merge even if the host changed files after the sandbox did (FR-47). |

- FR-40: `merge` copies all pending changes for `PATH` to the real path: new and modified
  files are copied (preserving mode, mtime, and symlinks), deletions are applied,
  directories are created/removed. Afterwards the upper layer for `PATH` is emptied.
  Before acting it **MUST** show a summary and ask for confirmation unless `-y/--yes`
  is given.
- FR-40a: **Output of `merge`.** Without `--yes`, the summary (the `diff --stat` lines
  of FR-15 under a title line) goes to stdout and the question `Apply these N changes
  to PATH? [y/N]` to stderr; the answer is read from stdin. Only `y` or `yes` (any
  case) confirm; anything else, EOF or an unreadable stdin cancels with exit 1 and
  `merge cancelled; nothing was changed`. While applying, each applied change is
  printed as `STATUS<TAB>PATH` (directories end in `/`), and finally
  `Merged N changes into PATH.`
- FR-41: `merge --dry-run` **MUST** print what would change and modify nothing. It only
  reads, so it does not take the lock and also works while a session runs.
- FR-42: `merge` **MUST** be as atomic as practical: if it fails midway, the upper layer
  **MUST NOT** be cleared, and the error message states what has been applied. Repeating
  `merge` must be safe (idempotent). Implementation:
  - Changes that cannot be merged (special files) are rejected before anything is
    applied.
  - Files and symlinks are written to a temporary name in the target directory and
    renamed over the target, with the pending version's mode and times.
  - Directory modes are set last, deepest first, so a directory the program made
    read-only can still be filled. Directories the user cannot write to are opened
    with `u+rwx` for the merge and restored afterwards.
  - The changes are computed against the real path as it is now, so changes that
    were already applied are no longer changes; this makes a repeated merge safe.
  - On failure, the error names the change that failed; the `STATUS<TAB>PATH` lines
    printed before it are the applied changes.
  - After success, the jail's state is removed completely (`listdiffs` no longer
    lists it).
- FR-43: `merge` and `discard` **MUST** refuse (exit 1) while a session using `PATH` is
  still running (see 8.3).
- FR-44: `discard` removes the upper layer content for `PATH` after confirmation unless
  `-y/--yes` is given. The real path is never modified. Summary, question and answer
  work like FR-40a (`Discard these N changes? [y/N]`); afterwards the jail's state is
  removed completely and `Discarded N changes for PATH.` is printed.
- FR-45: `diff`, `merge`, and `discard` **MUST** work for any path with state, even if
  `PATH` no longer exists on the host. `merge` recreates it.
- FR-47: **Conflicts.** A conflict is a modified file or symlink whose version on the
  host is newer (modification time) than the pending version, i.e. the host changed it
  after the sandbox did. Without `--force`, `merge` lists the conflicts on stderr and
  refuses with exit 1 before asking or changing anything. With `--force`, the conflicts
  are listed as warnings and the pending versions replace the host's. `merge --dry-run`
  lists conflicts as warnings and says that `--force` would be needed. (Decision on
  former open question 1.)
- FR-46: For `PATH` with no state, these commands fail with exit code 1 and the message
  `no pending changes for PATH`. The exception is `diff`, which follows FR-14. State
  without changes (for example after a session that only read files) is removed by
  `merge` and `discard` without a question (`Merged 0 changes ...`).

### 5.6 Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. For `run`, the program exited with 0. |
| `1` | General error in `diff`/`listdiffs`/`merge`/`discard`. |
| `2` | Usage error (bad flags or arguments). |
| `125` | `run`: the sandbox could not be set up (bwrap or overlay failure, missing dependency, invalid mount spec). |
| `127` | `run`: program not found. |
| *N* | `run`: otherwise the exit code of the program. |
| `128+S` | `run`: the program was killed by signal `S`. |

## 6. Sandbox Behavior (`run`)

### 6.1 Isolation baseline

- FR-20: The sandbox **MUST** be created with bubblewrap using, at minimum: a new user,
  mount, PID, IPC, UTS, and cgroup namespace; `--die-with-parent`; `--proc /proc`;
  `--dev /dev`; and an empty tmpfs `/tmp`.
- FR-21: Network namespace is shared by default and unshared with `--no-network`.
  Sharing the network namespace also shares the host's abstract Unix sockets (for
  example D-Bus, X11, systemd; verified in Step 9), so a program can reach these
  services. The README's security section says so; `--no-network` isolates them too.
- FR-22: Nothing from the host filesystem is visible except system mounts (6.2) and
  explicit mount specs (6.3). In particular `$HOME` is an empty tmpfs unless a
  mount spec includes it or parts of it. `$HOME` as an environment variable is
  unaffected.
- FR-23: stdin, stdout, and stderr **MUST** be passed through unmodified, including TTY
  behavior, so that interactive programs (for example `bash -l`) work. The sandbox keeps
  the controlling terminal: `bwrap` is started in the same session and process group,
  and `--new-session` is not used. (Tested in Step 5: `--new-session` breaks job
  control and `/dev/tty`, and still does not protect `bwrap`'s monitor process, which
  stays in the terminal's process group.) Known limitation: a terminal-generated
  `SIGINT`/`SIGQUIT` (Ctrl-C, Ctrl-\\) also reaches `bwrap`'s monitor process if the
  program stays in `bwrap`'s process group. `bwrap` then exits at once and the program is
  killed (`--die-with-parent`); the exit code is `128+S`. Programs that put themselves
  into their own process group (interactive shells with job control) or that read the
  terminal in raw mode (full-screen programs, most agent TUIs) are not affected.
- FR-24: Signals (`SIGINT`, `SIGTERM`, `SIGHUP`) received by `agent-sandbox` **MUST** be
  forwarded to the sandboxed program. Cleanup (6.5) **MUST** happen afterwards.
  - Signals are sent to the program process itself, because neither `bwrap`'s monitor
    nor the sandbox's init process relays them (tested: `SIGTERM` to `bwrap` kills the
    program with `SIGKILL`, `SIGTERM` to the init process is ignored).
  - Only signals sent by a process (`kill`) are forwarded. Signals generated by the
    terminal reach the program's process group directly, so forwarding them would
    deliver them twice.
  - If the program has not started yet, `SIGINT`, `SIGTERM` and `SIGHUP` end `bwrap`.
- FR-24a: **Exit status of `run`:** `bwrap` reports the program's exit status on its
  `--json-status-fd`; that status is returned (`128+S` if the program was killed by
  signal `S`). If `bwrap` was killed by a signal before reporting, `128+S` is returned.
  Otherwise the sandbox could not be set up or the program could not be executed, and
  `run` fails with exit 125 (`bwrap` prints the details itself).
- FR-24b: **Terminal input injection:** the `TIOCSTI` ioctl lets a program with access
  to the terminal inject input into the user's shell. Linux 6.2 and later can block it
  (`dev.tty.legacy_tiocsti = 0`, the case on the test machine). `run` **MUST** always
  block `TIOCSTI` and `TIOCLINUX` with a seccomp filter (`bwrap --seccomp`), as Flatpak
  does, so the protection does not depend on the kernel setting. The `ioctl` fails with
  `EPERM`; all other system calls are unaffected. The filter covers the native and the
  32-bit compatibility system calls on x86_64 (including x32) and aarch64. On other
  architectures no filter is installed and `--debug` says so.

### 6.2 System mounts and host layout detection

Programs need system tools, libraries, and basic configuration, so a fixed set of host
directories is always shared **read-only**.

- FR-25: The tool **MUST** detect the host layout (`--layout auto`): `nixos` if
  `/etc/NIXOS` exists or `/run/current-system` exists, otherwise `fhs`.
- FR-26: **FHS layout** system mounts (each only if it exists on the host):
  `/usr`, `/bin`, `/sbin`, `/lib`, `/lib32`, `/lib64`, `/libx32`, `/opt`, and `/etc`.
  If one of `/bin`, `/sbin`, `/lib*` is a symlink to `usr/...` (merged-`/usr`), it
  **MUST** be recreated as the same symlink instead of being bind-mounted.
  General rule for all layouts: a system path that is a symlink is recreated as the same
  symlink if its target lies inside another system path that is bind-mounted. Otherwise
  the symlink's target is bind-mounted at the system path, or the path is skipped if the
  target does not exist.
- FR-27: **NixOS layout** system mounts: `/nix/store`, `/run/current-system`,
  `/etc` (including the symlink targets in `/etc/static`), `/run/wrappers`, and
  `/bin` and `/usr` (whichever exist; on NixOS they only hold symlinks such as
  `/bin/sh` and `/usr/bin/env` into the store). Symlinks into `/nix/store` or
  `/run/current-system` **MUST** resolve inside the sandbox. (Verified in
  [NOTES.md](NOTES.md).)
- FR-28: The system mount sets are data-driven (declared as tables in code), so adding a
  layout does not require changing control flow.
- FR-29: `$PATH` entries that are not visible in the sandbox (for example
  `~/.local/bin`) **SHOULD** produce a warning on stderr listing the missing entries,
  without duplicates (real `$PATH` values often repeat entries). The user can then add
  them with `-r`. Only entries that exist on the host as directories are reported;
  entries that exist nowhere are not worth a warning.
- FR-30: If a mount spec or `--chdir` targets a path inside a system mount, it is
  allowed. `-w` or `-j` on a path inside a system mount **MUST** be rejected
  (exit 125), because system mounts are read-only by design.

### 6.3 Mount specs

- FR-31: Every mount spec path is made absolute and symlinks are resolved. The path
  appears inside the sandbox at the same absolute path as on the host
  ("transparent paths"). A leading `~` or `~user` is expanded, because the shell does
  not expand it in forms like `--ro=~/x`. Relative paths are relative to the current
  directory. The mount appears at the resolved path. If the given (absolute, unresolved)
  path differs because it contains symlinks, it is recreated inside the sandbox as a
  symlink to the resolved path, unless that location is already covered by another
  mount. This keeps both spellings working, for example when `~/project` is a symlink
  and also the working directory. Error messages show the given path and the resolved
  path.
- FR-32: A mount spec path that does not exist is an error (exit 125). Directories
  and files are both valid for `-r` and `-w`.
- FR-33: Parent directories of mount points are created as empty directories in the
  sandbox as needed.
- FR-34: The same resolved path given twice with different modes is an error. Given twice
  with the same mode it is deduplicated.
- FR-35: Nested mount specs are allowed (for example `-r /a -w /a/b`). More specific
  (deeper) paths take precedence over their parents. `-j` nested inside another `-j`
  is an error. A `-j` or `-w` nested inside an `-r` is allowed.
- FR-36: **Jailed (`-j`) on a directory:** a fuse-overlayfs is mounted with the real
  directory as lower layer and the state directory's upper layer, and the merged
  view is bind-mounted into the sandbox at the original path (see 7).
- FR-37: **Jailed (`-j`) on a single file:** fuse-overlayfs is mounted on the **parent
  directory** of the file, and only that file from the merged view is bind-mounted into
  the sandbox at the original path. Sibling files are not exposed. The state is keyed
  by the file's own path, so `diff`/`merge`/`discard` take the file path. Known
  limitations (document in `--help`): (a) programs that replace or remove the file
  (write temp file then `rename()` over it, or `unlink()`) fail on the bind-mounted file
  with `EBUSY`; (b) new files that a program creates next to the jailed file are not part
  of the overlay and are lost when the sandbox exits (or fail if the parent directory is
  not writable in the sandbox).
- FR-38: Mount specs are applied in order of path depth (shallow first), so nested
  specs overlay their parents deterministically regardless of the order on the command
  line. System mounts and mount specs are ordered together by the same rule.
- FR-39: The state directory (8.1) **MUST NEVER** be visible inside the sandbox, because
  writing to it directly would bypass review: the program could change any jail's upper
  layer, and those changes would later be merged into paths it was never given. A mount
  spec on or inside the state directory is an error (exit 125). If a mount spec contains
  the state directory (for example `-w ~`), the state directory is hidden with an empty
  read-only tmpfs.

### 6.4 Environment

- FR-50: **Default:** the full environment of `agent-sandbox` is passed to the program
  unchanged.
- FR-51: If at least one `--env` or `--env-file` is given, the environment is
  **restricted**: only the **baseline set**, the variables named without a value
  (taken from the host, if set there), and the variables given with an explicit value
  are passed. All other host variables are discarded. This applies even if all entries
  are `NAME=VALUE` entries.
- FR-52: **Baseline set**: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `LANG`,
  `LC_ALL`, `TZ`. Baseline variables are always passed if set, in both modes.
- FR-53: **Entry syntax**, for both `--env` and each line of an `--env-file`:
  - `NAME`: pass the host value of `NAME`. If `NAME` is not set on the host, nothing is
    set in the sandbox (no error).
  - `NAME=VALUE`: set `NAME` to `VALUE` in the sandbox. The host value is ignored.
  - Only the first `=` separates name and value. `VALUE` is taken **literally**: no
    quote removal, no variable expansion, no escape processing, and it may contain `=`,
    spaces, or `#`. `NAME=` sets `NAME` to the empty string.
  - `NAME` must match `[A-Za-z_][A-Za-z0-9_]*`. Invalid entries are an error (exit 2),
    with the file name and line number for `--env-file`.
  - A `VALUE` containing a NUL character cannot be passed to a process and is an
    error (exit 2).
- FR-54: `--env-file` format: one entry per line. Blank lines and lines whose first
  non-whitespace character is `#` are ignored. Leading whitespace before `NAME` and
  trailing whitespace after a bare `NAME` are trimmed. For `NAME=VALUE`, whitespace in
  `VALUE` is preserved. Lines end with LF; a CRLF line ending is accepted and the CR
  is not part of the value. The file is decoded like other file system data
  (`os.fsdecode`, UTF-8 with `surrogateescape`), so arbitrary bytes in values are
  passed on unchanged. A file that cannot be read is an error (exit 2).
- FR-55: `--env` and `--env-file` can be combined and repeated. They are processed in
  command-line order. If a name occurs more than once, the **last** occurrence wins
  (so a later `NAME=VALUE` overrides an earlier `NAME`, and the other way round).
- FR-56: Explicit values may override baseline variables (for example `--env PATH=...`
  or `--env HOME=/home/sandbox`). Overriding `PATH` is the user's responsibility and is
  not validated, but the warning of FR-29 applies to the effective `PATH`.
- FR-57: Environment variables are applied with bubblewrap's `--clearenv`/`--setenv`
  (or equivalent), not by modifying the tool's own environment.
- FR-58: Because `--env` values and `--env-file` contents may be secrets, the tool
  **MUST NOT** print values in normal output, error messages, or `--debug` output (names
  only; values are shown as `***`). `--dry-run` also masks values. An `--env-file`
  that is readable by group or others **SHOULD** trigger a warning on stderr.
  Error messages identify an invalid entry by its source (`--env #N` for the N-th
  `--env` option, or file name and line number) and show the name only if the entry
  contains `=`. Values and invalid entries without `=` are never echoed, because they
  may be secrets pasted by mistake.

### 6.5 Lifecycle and cleanup

- FR-60: Order for `run`: validate arguments, check dependencies, acquire locks (8.3),
  mount overlays, start bwrap, wait, unmount overlays, release locks.
- FR-61: Overlays **MUST** be unmounted and temporary directories removed after the
  program exits, whether it ends normally, by signal, or because of an error. `SIGTERM`
  and `SIGHUP` received while overlays are being set up or torn down also lead to a
  clean unmount (exit `128+S`).
- FR-62: If the tool crashes or is killed hard, the next `run`, `merge`, or `discard`
  **SHOULD** detect stale locks and stale mounts (dead PID) and clean them up. Pending
  changes in upper layers **MUST NEVER** be lost during this cleanup. Cleanup is done
  by the commands that take the jail's lock; `diff` and `listdiffs` only read the upper
  layer, which a stale mount does not affect. A stale mount is a mount on a jail's
  `merged/` directory (according to `/proc/self/mountinfo`) while nobody holds the
  jail's lock. It is unmounted with `fusermount3 -u`, falling back to a lazy unmount
  (`-u -z`) if it is busy.
- FR-63: Failure to set up one overlay aborts the entire `run` with exit 125, and
  already-mounted overlays are unmounted before exit.

## 7. Jailed Writes (fuse-overlayfs)

- FR-70: Inside the sandbox, a jailed path behaves like a normal read/write path:
  reading shows lower content merged with prior pending changes, writes, creates,
  deletes, renames, chmods, and symlinks all succeed as on a normal filesystem. The
  program cannot tell it is an overlay (no path rewriting).
- FR-70a: **Known limitation (fuse-overlayfs, tested in Step 8):** inside a directory
  that is read-only in the real tree (no write permission for the owner, for example
  Go's module cache), nothing can be changed in a jail: no new files, no `chmod` of
  the directory or of files in it ("Permission denied"). This happens with
  `fuse-overlayfs` alone, without the sandbox.
- FR-71: The lower layer (the real path) **MUST NOT** be modified during a session.
  Verified by tests (checksum of lower tree before/after).
- FR-72: Every change is stored in the upper layer in the state directory. Deletions and
  replaced directories are represented the way fuse-overlayfs does it (whiteouts and
  opaque directories). `diff`, `listdiffs`, `merge`, and `discard` **MUST** interpret
  that representation correctly. Observed representation (see [NOTES.md](NOTES.md)):
  whiteouts are character devices 0:0 (or, where those cannot be created, files named
  `.wh.NAME`), opaque directories carry the xattr
  `user.fuseoverlayfs.opaque` and a marker file `.wh..wh..opq` that is never reported
  or merged. Newly created directories are opaque too, so "opaque" does not mean
  "existed in lower". The upper layer may also contain unchanged copy-ups (for
  example after `touch`), so a change **MUST** be determined by comparing with the
  lower layer (content, mode, type, symlink target), not by presence in the upper layer.
  `user.fuseoverlayfs.*` xattrs are never copied by `merge`.
- FR-73: Overlays are mounted by the unprivileged user. No root, no `sudo` (see 9.1).
- FR-74: The overlay mount happens outside the sandbox on the host, in a private
  directory inside the state directory with mode `0700`, and only the merged view
  is bind-mounted into the sandbox. `fuse-overlayfs` is started in the jail directory
  with the fixed relative names `lowerdir=lower,upperdir=upper,workdir=work`, where
  `lower` is a symlink to the real directory. This way, paths containing `,` or `:`
  (which are separators in the mount options) work.
- FR-75: Pending changes **accumulate** across sessions: a later `run -j PATH` sees the
  earlier pending changes of `PATH` as part of the overlay. They persist until `merge`
  or `discard`.
- FR-76: Concurrent `run` sessions on the same jailed path are **refused** (exit 125)
  with a message naming the PID of the other session. Sessions on different jailed
  paths are independent.

## 8. State

### 8.1 State directory

- FR-80: The state directory is, in order of precedence: `--state-dir` if given, else
  `$AGENT_SANDBOX_STATE_DIR` if set, else `$XDG_STATE_HOME/agent-sandbox` if
  `$XDG_STATE_HOME` is set, else `~/.local/state/agent-sandbox`.
- FR-81: Layout per jailed path (identified by the SHA-256 of its absolute resolved path
  as directory name `<id>`):

  ```
  <state-dir>/jails/<id>/
      meta.json    # original path, kind (dir|file), created time, tool version
      upper/       # upper layer
      work/        # overlayfs workdir (same filesystem as upper/)
      lock         # lock file with owner PID while a session runs
      lower        # symlink to the lower directory (FR-74)
      merged/      # mount point of the merged view while a session runs
  ```

  For a jailed **directory**, the lower directory is the path itself. For a jailed
  **file** (FR-37), the lower directory is the file's parent directory, and the upper
  layer holds at most the file itself, under its name.
- FR-81a: If `meta.json` of a path records a different kind (directory versus file)
  than the path has now, `run -j` fails (exit 125) and asks to merge or discard the
  pending changes first. `meta.json` must record the same path; anything else is
  reported as corrupt state and never deleted automatically.

- FR-82: The state directory and everything below it **MUST** be created with mode
  `0700`.
- FR-83: `meta.json` has a version field, so format changes can be migrated.

### 8.2 Path identity

- FR-84: Two spellings of the same path (relative, symlinked, with `..`) map to the
  same state. `diff`, `merge`, and `discard` apply the same normalization as `run`.

### 8.3 Locking

- FR-85: A lock (`flock` or PID file) protects each jailed path while a session runs.
  `merge` and `discard` take the same lock. Stale locks of dead processes are ignored.
  Implementation: an exclusive, non-blocking `flock` on the jail's `lock` file, held
  by `agent-sandbox` for the whole session. The kernel releases it when the process
  dies, so locks can never become stale. The holder writes its PID into the file
  only so that FR-76 can name it.

## 9. Unprivileged Operation, Host Requirements and Error Handling

### 9.1 Unprivileged operation

Every function of the tool **MUST** work for a normal, unprivileged user.

- FR-95: No subcommand (`run`, `diff`, `listdiffs`, `merge`, `discard`) requires root,
  `sudo`, `su`, `doas`, polkit, or any other privilege escalation, and the tool **MUST
  NOT** invoke any of them.
- FR-96: The sandbox relies on unprivileged user namespaces. `bwrap` **MUST** work
  without being installed setuid-root (a setuid `bwrap` is allowed but not required).
- FR-97: fuse-overlayfs is mounted and unmounted by the unprivileged user via FUSE
  (`fuse-overlayfs` and `fusermount3`). Kernel overlayfs mounts via `mount(8)` as root
  are never used. A setuid `fusermount3` as shipped by distributions is accepted,
  because the tool invokes it as the normal user.
- FR-98: All files the tool creates (state, locks, temporary mount points) live in
  directories owned by the invoking user: the state directory (see 8.1) and the private
  mount directory inside it (FR-74). No system directories are modified, and nothing is
  installed system-wide at runtime.
- FR-99: Files created by `merge` are owned by the invoking user and written with the
  mode recorded in the upper layer, restricted to what an unprivileged user can
  set. Ownership changes (`chown`) to other users are never attempted.
- FR-100: If the tool is started as root (uid 0), it **MAY** run, but behavior is
  not guaranteed and a warning is printed to stderr. Support for root is not a
  goal.
- FR-101: Setup that needs administrator rights (installing `bwrap`/`fuse-overlayfs`,
  enabling user namespaces via sysctl, `/etc/fuse.conf` settings, `/etc/subuid`) is
  a prerequisite documented in the README. It is outside the tool. Error messages
  for missing prerequisites **MUST** point to that documentation instead of
  suggesting `sudo` commands to run automatically.
- FR-102: The test suite and the code quality tools run without root. Integration
  tests that need user namespaces are skipped if unavailable (NFR-12) and never
  attempt to escalate privileges.

### 9.2 Host requirements and error handling

- FR-90: `run` **MUST** check at startup that `bwrap` is available (and, if any `-j` is
  used, also `fuse-overlayfs` and `fusermount3`/`fusermount`). A missing dependency
  is an error (exit 125) naming the missing binary.
- FR-91: If unprivileged user namespaces are unavailable, the error message states the
  likely cause and does not print a raw traceback.
- FR-92: All errors are one-line messages of the form `agent-sandbox: error: <message>`
  on stderr. Python tracebacks are shown only with `--debug`.
- FR-93: A global `--debug` flag prints the bwrap command line and detailed steps to
  stderr.
- FR-94: Edge cases that **MUST** be handled with a clear error: PATH not readable,
  mount spec on a non-directory/non-file (socket, device), filesystem that does not
  support the overlay upper layer, state directory not writable, symlink loop.

## 10. Non-Functional Requirements

### 10.1 Implementation

- NFR-1: Python `>= 3.11`. Linux only.
- NFR-2: **No third-party runtime dependencies.** Only the standard library
  (`argparse`, `subprocess`, `pathlib`, `json`, ...). External programs: `bwrap`,
  `fuse-overlayfs`, `fusermount3`.
- NFR-3: The project uses a `src/` layout with the package `agent_sandbox`, a
  `pyproject.toml`, and a console script `agent-sandbox`.
- NFR-4: Dependency and environment management with **uv**. `uv.lock` is committed.
- NFR-5: Code is fully type annotated.
- NFR-6: Subprocess calls always use argument lists (never `shell=True`), so
  paths with spaces or special characters are safe.

### 10.2 Code quality

All of the following **MUST** pass with zero warnings in CI and locally before merging:

| Tool | Configuration |
|------|---------------|
| `black` | `line-length = 99` |
| `isort` | `profile = "black"`, `line_length = 99` |
| `flake8` | `max-line-length = 99`, `extend-ignore = E203,W503` (black compatible) |
| `pylint` | Default rules; justified suppressions only, with a comment |
| `mypy` | `strict = true` |

Tool configuration lives in `pyproject.toml` (flake8 in `.flake8` or `setup.cfg`, since
flake8 does not read `pyproject.toml`).

### 10.3 Testing

- NFR-10: Unit tests use **pytest**. Run via `uv run pytest`.
- NFR-11: The core logic is built from pure functions that are unit-testable without
  bwrap: argument parsing (FR-6 to FR-10), layout detection and mount tables,
  bwrap command construction, env filtering, state paths, diff/merge algorithms on
  temporary directories.
- NFR-12: Integration tests that need `bwrap`/`fuse-overlayfs` are marked
  (`@pytest.mark.integration`) and skipped automatically if the dependencies are missing
  or user namespaces are unavailable.
- NFR-13: Every FR in sections 5 to 9 has at least one test that references its ID
  (in the test name or docstring).

### 10.4 Documentation

- NFR-20: `README.md` with install, quickstart, and the same examples as in section 11.
- NFR-21: `--help` output is the primary reference (FR-1).

### 10.5 Performance

- NFR-30: Sandbox startup overhead (excluding the program) **SHOULD** be below 300 ms
  without `-j`, and below 1 s with a `-j` on a directory of 10 000 files.

## 11. Acceptance Criteria and Examples

Each example is an acceptance test (integration test where bwrap is needed).

**AC-1: Argument parsing, trailing program flags (FR-6)**
```
agent-sandbox run -r ~/important_file -j ~/git/myproject -w ~/Documents/scratchspace /bin/bash -l
```
`bash` is started with `-l`. `~/important_file` is read-only, `~/git/myproject` is
jailed, `~/Documents/scratchspace` is writable.

**AC-2: Explicit `--` (FR-7)**
```
agent-sandbox run -j ~/git/myproject -- /bin/some_program -j -w -v -r
```
`some_program` receives `-j -w -v -r` unchanged.

**AC-3: Invisible files**
```
agent-sandbox run -r ~/proj -- ls ~
```
Only `proj` (and parents) appear, and `~/.ssh` does not exist inside the sandbox.

**AC-4: Read-only enforcement**
```
agent-sandbox run -r ~/f -- sh -c 'echo x >> ~/f'
```
Fails with "Read-only file system" and the exit code is non-zero. The file is unchanged.

**AC-5: Writable**
```
agent-sandbox run -w ~/scratch -- touch ~/scratch/new
```
`~/scratch/new` exists on the host afterwards.

**AC-6: Jailed write, full review cycle**
```
agent-sandbox run -j ~/proj -- sh -c 'echo hi > ~/proj/a.txt; rm ~/proj/old.txt'
agent-sandbox listdiffs     # -> /home/u/proj   2   <time>
agent-sandbox diff ~/proj   # shows a.txt added, old.txt deleted
ls ~/proj                   # host: a.txt absent, old.txt still present
agent-sandbox merge -y ~/proj
ls ~/proj                   # host: a.txt present, old.txt gone
agent-sandbox listdiffs     # -> (empty)
```

**AC-7: Discard**
After AC-6's first command, `agent-sandbox discard -y ~/proj` leaves the host
unchanged and `listdiffs` is empty.

**AC-8: Transparency (FR-70)**
Inside a jail, a program that writes, reads back, renames, and deletes files sees
consistent results and no paths other than the original ones.

**AC-9: Accumulation (FR-75)**
Run A creates `x`. Run B (`-j` on the same path) sees `x`. `diff` shows `x` as added.

**AC-10: Concurrency refusal (FR-76)**
While one session on `~/proj` is running, a second `run -j ~/proj` exits with 125.
`merge ~/proj` exits with 1.

**AC-11: Environment restriction (FR-51 to FR-55)**
```
FOO=1 BAR=2 agent-sandbox run --env FOO -- env
```
Output contains `FOO` and the baseline variables (`PATH`, ...), and does not contain
`BAR`. Without `--env`, both are present.

**AC-11b: Explicit values (FR-53 to FR-58)**
```
printf '# comment\nA=from file\nB=x=y # kept\nC=\nFOO\n' > env.txt
FOO=host BAR=2 agent-sandbox run --env-file env.txt --env A=cli -- env
```
Output contains `A=cli` (later entry wins), `B=x=y # kept`, `C=` (empty), `FOO=host`,
the baseline variables, and not `BAR`. `--env 'X=a b'` yields `X=a b` inside the
sandbox. `--env 1BAD=x` exits with 2. `--dry-run` shows `***` instead of values.

**AC-12: Network (FR-21)**
`run --no-network -- <program that connects out>` fails to connect. Without the flag it
connects.

**AC-13: Layouts (FR-25 to FR-27)**
On an FHS host and on a NixOS host (or on mocked layout inputs for unit tests),
`run -- ls /` and `run -- cat /etc/os-release` work, and `PATH` entries resolve
inside the sandbox.

**AC-14: Single-file jail (FR-37)**
`run -j ~/proj/notes.txt -- sh -c 'echo x >> ~/proj/notes.txt'`: siblings of
`notes.txt` are invisible, `diff ~/proj/notes.txt` shows the change, `merge` applies it.

**AC-15: Exit codes (5.6)**
`run -- sh -c 'exit 7'` returns 7. `run -- nonexistent_prog` returns 127.
`run -w /nonexistent -- true` returns 125.

**AC-16: Cleanup (FR-61)**
After any `run` finishes (including `kill -TERM` of the tool), `mount | grep fuse-overlayfs`
shows no leftover mounts for it, and the upper layers still hold their changes.

**AC-16b: Unprivileged operation (FR-95 to FR-102)**
As a regular user in a fresh session (no `sudo` credentials cached, no `CAP_SYS_ADMIN`),
every example above (AC-1 to AC-16) works. An audit of every program `agent-sandbox`
starts during a full `run -j` / `listdiffs` / `diff` / `merge` cycle (Python audit hooks
on `subprocess`, `posix_spawn` and `exec`) shows `bwrap`, `fuse-overlayfs` and
`fusermount3`, and no `sudo`, `su`, `doas`, `pkexec` or `run0`. (`strace -f` cannot be
used for this: tracing disables the setuid bit of `fusermount3`, so mounting fails.)
With the default state directory, nothing besides the state directory and the explicit
mount spec paths is created or modified in `$HOME`.

**AC-17: Code quality (10.2, 10.3)**
`uv run black --check . && uv run isort --check . && uv run flake8 && uv run pylint src tests && uv run mypy src tests && uv run pytest`
all succeed.

## 12. Decisions on Former Open Questions

Decided for v0.1 (2026-11-08); they can be revisited later.

1. **Merge conflict detection:** `merge` refuses when the host changed a file after the
   sandbox did, unless `--force` is given (FR-47). Detection uses modification times;
   recording base hashes for exact detection is a possible later improvement.
2. **`/etc` exposure:** all of `/etc` stays shared read-only, so tools find their
   configuration. The README's security section says that the program can read host
   configuration there.
3. **Garbage collection:** no extra command. `listdiffs` shows state of paths that no
   longer exist, and `discard PATH` removes it (FR-45).
4. **Other layouts:** Guix, Gentoo Prefix and similar are out of scope for v0.1; the
   layout table (FR-28) makes adding them a local change.
5. **Terminal input injection:** the seccomp filter of FR-24b is always applied.
