# Step 0 Feasibility Notes

Spike run on 2026-11-06. All experiments ran as an unprivileged user with temporary
directories. No root or `sudo` was used, and nothing remains on disk or mounted.

**Environment:** NixOS (26.05), bubblewrap 0.12.0, fuse-overlayfs 1.17 (FUSE 3.17.4,
kernel interface 7.40), `fusermount3` via setuid wrapper in `/run/wrappers/bin`,
`/dev/fuse` mode 0666, `max_user_namespaces = 127977`, `/etc/fuse.conf`: `mount_max = 1000`.
Not tested: FHS distributions, older fuse-overlayfs or bwrap versions.

## Verdict

The core design works. An unprivileged user can mount fuse-overlayfs on the host and
bind the merged view into a `bwrap` sandbox. Writes show up only in the upper layer.
Cleanup, accumulation and the NixOS system mounts all behave as the spec assumes. Four
spec adjustments were made, listed at the end.

## 1. Overlay mount on host, bind into bwrap (FR-36, FR-73, FR-74, FR-97)

```
fuse-overlayfs -o lowerdir=LOWER,upperdir=UPPER,workdir=WORK MERGED      # background (default)
bwrap --unshare-user --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup \
      --die-with-parent --proc /proc --dev /dev --tmpfs /tmp \
      --ro-bind /nix/store /nix/store --ro-bind /run/current-system /run/current-system \
      --ro-bind /etc /etc --bind MERGED /original/path --setenv PATH ... -- PROGRAM
fusermount3 -u MERGED
```

- Works without any option such as `allow_other`. The bwrap process runs as the same
  user, so it can access the mount.
- `lowerdir` is never modified. Verified by listing it after a session
  (create, modify, delete, rename, chmod, symlink, `rm -r`, mkdir).
- Mounting without `-f` daemonizes, and the mount was usable immediately after the command
  returned, so no readiness polling was needed.
- Plain `-o lowerdir=... upperdir=... workdir=...` is enough. No `squash_to_uid` or
  `userxattr` options were required.

## 2. Upper layer representation (FR-72)

Observed in the upper directory after a session:

| Operation inside sandbox | Representation in upper |
|--------------------------|-------------------------|
| Create file / symlink | Normal file / symlink (`link -> a.txt`) |
| Modify file | Full copy-up, regular file with new content. Has xattr `user.fuseoverlayfs.origin=<path>`. |
| `chmod` / `touch` only | Full copy-up with unchanged content. **Identical copies can exist in upper.** |
| Delete file | **Character device 0:0** with the file's name (whiteout) |
| Delete dir tree, recreate dir | Whiteout is replaced by a directory with xattr `user.fuseoverlayfs.opaque=y` **and** a marker file `.wh..wh..opq` (mode 0700, empty) inside it |
| Create new directory (not in lower) | **Also** gets `user.fuseoverlayfs.opaque=y` and `.wh..wh..opq` |
| Rename directory `d` to `e` | Whiteout `d` (char dev 0:0) plus **full copy** of the contents as opaque dir `e` |
| Delete file, recreate it | The whiteout disappears and the upper holds the new regular file, so the **lower file is replaced** |
| Hard link (`ln g glink`) | Both names become independent copies with `origin` xattr, so the link relation is not visible |
| Replace file by `mv new old` | n/a for directories, see section 3 for the single-file case |

Consequences for `upper.py`, `diffing.py` and `merging.py` (Steps 7 and 8):

- Detect whiteouts via `stat.S_ISCHR(mode) and st_rdev == 0`.
- Detect opaque directories via the xattr `user.fuseoverlayfs.opaque` (primary) and the
  marker file `.wh..wh..opq` (fallback). The marker file **must never be reported or
  merged as a file**.
- **Opaque does not mean "existed in lower".** New directories are opaque too. Decide
  added versus replaced by looking at the lower layer, not at the opaque flag.
- **Never trust "present in upper" as "changed".** Compare content (and mode, symlink
  target, type) with the lower counterpart and drop identical entries. Otherwise `touch`
  or mode-only operations and unchanged copy-ups produce false diffs.
- An opaque directory means that the lower directory's contents are hidden. In
  `merge`, entries in lower that are missing in the upper opaque directory are deleted.
- Do not rely on or merge the `user.fuseoverlayfs.*` xattrs. Strip them when copying out.
- A directory rename appears as delete plus add of every file. Detecting renames is not
  needed for v0.1.

## 3. Single-file jail (FR-37)

Setup: overlay mounted on the parent directory, and only `MERGED/notes.txt` bound to
the original path.

- Sibling files are invisible. `ls` of the parent shows only `notes.txt`.
- Reading and writing (`>>`, `>`) the bound file works. The change lands in
  `upper/notes.txt`. The lower file is untouched.
- `mv other file` onto the bound file fails: `Device or resource busy` (EBUSY).
- **`rm` of the bound file also fails with EBUSY.**
- Creating **new** files next to it succeeds if the parent directory is writable in the
  sandbox (for example when the parent is on a tmpfs), but these files live in the
  sandbox's own filesystem. They are **silently lost** at exit and never reach the
  overlay or the host. This is an important caveat for programs that write temp files
  next to the target.

This is consistent with the spec's limitation, and the documented limitation was
extended (see changes below).

## 4. Cleanup and crash recovery (FR-61, FR-62)

- After normal `fusermount3 -u MERGED`, no mount remains, and re-mounting with the same
  upper and work directories works. Content and whiteouts persist, which confirms
  **accumulation** (FR-75).
- After `kill -9` of a foreground `fuse-overlayfs`: the mount point shows
  `Transport endpoint is not connected` (ENOTCONN) and appears in `mount`. A plain
  `fusermount3 -u MERGED` as the user cleans it up (exit 0). Re-mounting afterwards with
  the existing upper and work directories works. The stale workdir caused no problem.
- Recovery routine for FR-62: if `stat(MERGED)` fails with ENOTCONN, or the mount is
  listed in `/proc/self/mountinfo` while no live owner holds the lock, call
  `fusermount3 -u` and continue.

## 5. NixOS system mounts inside bwrap (FR-25 to FR-29)

Detected: `/etc/NIXOS` exists. Facts on this system:

- `/bin` contains only the symlink `sh -> /nix/store/...-bash-interactive/bin/sh`.
- `/usr/bin` contains only `env -> /nix/store/...-coreutils/bin/env`.
- `/etc` is a normal directory (with many symlinks to `/etc/static`, which points to
  `/nix/store/...-etc/etc`). `/etc/os-release` and `/etc/resolv.conf` resolve inside.
- `/run/current-system` is a symlink into `/nix/store`. `/run/current-system/sw/bin`
  holds the user-visible tools.
- `/run/wrappers/bin` contains setuid wrappers (`sudo`, `fusermount3`, ...).

Minimal set that worked (all `--ro-bind`):
`/nix/store`, `/run/current-system`, `/run/wrappers`, `/etc`, `/bin`, `/usr`.

Verified inside the sandbox with the real host `$PATH`:

- `ls`, `cat`, `git`, `python3`, `uv`, `rg`, `env`, `sh` all resolve to
  `/run/current-system/sw/bin/...` and run. `#!/usr/bin/env sh` scripts work.
- DNS resolution works (network shared by default, FR-21).
- `$HOME` as tmpfs is empty, `~/.ssh` does not exist (FR-22).
- `sudo` is visible through `/run/wrappers`, but its setuid bit is ineffective inside the
  user namespace (file owner appears as `nobody`). Not a problem, but worth
  knowing: `/run/wrappers` is not needed for ordinary tools and could be dropped.
- 16 of the entries in the user's `$PATH` were **not visible** (paths below `$HOME`,
  `/opt/android/sdk`, `/var/lib/flatpak`, `/nix/var/nix/profiles/default/bin`,
  `/nix/profile/bin`). The `$PATH` also contains duplicates. This confirms the FR-29
  warning is needed, and it must deduplicate entries to be readable.
- `/nix/var` is not part of the minimal set. `nix` profile directories under
  `/nix/var/nix/profiles` are therefore missing, and tools installed only through them are
  unavailable until the user adds `-r /nix/var/nix/profiles` (or similar).

## Spec changes made because of these findings

1. **FR-27:** NixOS system mounts bind `/bin` and `/usr` as directories (they only hold
   symlinks into the store) instead of individual symlink files.
2. **FR-29:** the `PATH` warning deduplicates entries.
3. **FR-37:** limitation text extended: `rm` of the file also fails with `EBUSY`, and new
   files created next to the target are lost with the sandbox.
4. **FR-72:** clarified how fuse-overlayfs represents changes and that unchanged
   copy-ups must not be reported as changes.

## Open items for later steps

- Test on an FHS distribution (Debian/Ubuntu or Fedora) for the layout tables, ideally in CI.
- Check older fuse-overlayfs versions for different whiteout or opaque handling, and
  record minimum versions in the README (tested here: bwrap 0.12.0, fuse-overlayfs 1.17).
- TTY behavior (`--new-session`, job control) was not tested because the spike had no
  terminal. Decide this together with open question 3 when Step 5 is implemented.
- Startup latency of overlay mounts (NFR-30) was not measured.

## Step 6 findings (2026-11-07)

- `fuse-overlayfs` accepts a **symlink as `lowerdir`** and relative option values when
  started in the jail directory (`-o lowerdir=lower,upperdir=upper,workdir=work merged`).
  A lower path containing a space, `,` and `:` works this way (FR-74).
- Without `-f`, `fuse-overlayfs` returns after about 0.02 s with the mount ready. Its
  daemon does not keep captured stdout/stderr pipes open, so `subprocess.run(...,
  capture_output=True)` does not hang.
- Mounting twice on the same mount point fails (`fusermount3: failed to access
  mountpoint ... Permission denied`), so stale mounts must be removed first (FR-62).
- An upper directory that is not a directory fails with `cannot read upper dir: Not a
  directory`, which is passed on as the setup error (FR-63).
- After `kill -9` of `agent-sandbox`, `bwrap` and the program die (`--die-with-parent`),
  but the `fuse-overlayfs` daemon and its mount stay. The next `run` unmounts it with
  `fusermount3 -u`, and the upper layer is intact (integration test).
- **`-j ~` with the default state directory** puts the upper layer inside the lower
  directory. `fuse-overlayfs` handles this. The state directory must be hidden in the
  sandbox even when it is only created by this run (bug found and fixed: the hiding
  decision used to depend on whether the directory existed before the run).
