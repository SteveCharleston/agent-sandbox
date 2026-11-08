"""Applying or dropping pending changes: ``merge`` and ``discard`` (section 5.5)."""

import filecmp
import os
import secrets
import shutil
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from agent_sandbox.deps import find_fusermount
from agent_sandbox.diffing import format_stat, quote_path
from agent_sandbox.errors import AgentSandboxError
from agent_sandbox.options import DiscardOptions, MergeOptions
from agent_sandbox.overlay import is_mounted, unmount_overlay
from agent_sandbox.pending import Pending, find_pending, normalize_path
from agent_sandbox.state import locked, remove_jail, state_dir_location
from agent_sandbox.upper import Change, ChangeKind, EntryType, readable_tree

Output = Callable[[str], None]


@dataclass(frozen=True)
class Io:
    """How merge and discard talk to the user: output, confirmation, warnings."""

    out: Output
    ask: Callable[[str], bool]
    warn: Output


_STATUS = {ChangeKind.ADDED: "A", ChangeKind.DELETED: "D", ChangeKind.MODIFIED: "M"}
_TEMPORARY_PREFIX = ".agent-sandbox-"


def check_mergeable(changes: Iterable[Change]) -> None:
    """Refuse before anything is applied if a change cannot be merged (FR-42).

    Special files (FIFOs, sockets, devices) created in the sandbox are not merged.
    """
    for change in changes:
        if change.new is not None and change.new.type is EntryType.OTHER:
            raise AgentSandboxError(
                f"cannot merge the special file {quote_path(change.path)}; remove it in a "
                "new session or discard the changes"
            )


def conflicts(changes: Iterable[Change]) -> list[str]:
    """Files changed on the host after the sandbox changed them (FR-47).

    A modified file or symlink whose host version is newer than the pending version.
    """
    result = []
    for change in changes:
        old, new = change.old, change.new
        if change.kind is not ChangeKind.MODIFIED or old is None or new is None:
            continue
        if old.type is EntryType.DIRECTORY:
            continue
        if os.lstat(old.path).st_mtime_ns > os.lstat(new.path).st_mtime_ns:
            result.append(
                f"{quote_path(change.path)} changed on the host after the sandbox changed it"
            )
    return result


def _temporary_name(directory: str) -> str:
    return os.path.join(directory, _TEMPORARY_PREFIX + secrets.token_hex(8))


def _replace_file(source: str, target: str, mode: int) -> None:
    """Copy ``source`` over ``target`` atomically, with ``mode`` and its times (FR-40, FR-99).

    ``mode`` is the pending version's mode as recorded, not the (possibly temporarily
    opened) mode of ``source``.
    """
    temporary = _temporary_name(os.path.dirname(target))
    try:
        with open(source, "rb") as reader, open(temporary, "xb") as writer:
            shutil.copyfileobj(reader, writer)
        info = os.lstat(source)
        os.chmod(temporary, mode)
        os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.replace(temporary, target)
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def _replace_symlink(source: str, target: str) -> None:
    """Create or replace the symlink ``target`` with the one at ``source``."""
    temporary = _temporary_name(os.path.dirname(target))
    os.symlink(os.readlink(source), temporary)
    try:
        os.replace(temporary, target)
    except BaseException:
        os.unlink(temporary)
        raise


def _report_line(change: Change) -> str:
    """``STATUS<TAB>PATH``; directories end in ``/`` as in ``diff --stat``."""
    entry = change.new or change.old
    suffix = "/" if entry is not None and entry.type is EntryType.DIRECTORY else ""
    return f"{_STATUS[change.kind]}\t{quote_path(change.path + suffix)}\n"


@dataclass
class _Merger:
    """Applies changes to the lower directory; directory modes are set at the end."""

    lower: str
    report: Output
    final_modes: dict[str, int] = field(default_factory=dict)
    temporary_modes: dict[str, int] = field(default_factory=dict)
    mode_reports: dict[str, str] = field(default_factory=dict)

    def _target(self, change: Change) -> str:
        return os.path.join(self.lower, change.path)

    def _make_writable(self, directory: str) -> None:
        """Give the owner access to a directory to change its contents; restore it later."""
        if os.path.isdir(directory) and not os.access(directory, os.W_OK | os.X_OK):
            mode = stat.S_IMODE(os.lstat(directory).st_mode)
            os.chmod(directory, mode | 0o700)
            self.temporary_modes.setdefault(directory, mode)

    def apply(self, change: Change) -> None:
        """Apply one change and report it."""
        target = self._target(change)
        self._make_writable(os.path.dirname(target))
        old, new = change.old, change.new
        if change.kind is ChangeKind.DELETED and old is not None:
            if old.type is EntryType.DIRECTORY:
                os.rmdir(target)
                for modes in (self.final_modes, self.temporary_modes, self.mode_reports):
                    modes.pop(target, None)
            else:
                os.unlink(target)
        elif new is not None and new.type is EntryType.DIRECTORY:
            if change.kind is ChangeKind.ADDED:
                os.mkdir(target, 0o700)
            self.final_modes[target] = new.mode
            if change.kind is ChangeKind.MODIFIED:
                self.mode_reports[target] = _report_line(change)
                return  # reported when the mode is set in finish()
        elif new is not None and new.type is EntryType.SYMLINK:
            _replace_symlink(new.path, target)
        elif new is not None and old is not None and filecmp.cmp(old.path, new.path, False):
            os.chmod(target, new.mode)
        elif new is not None:
            _replace_file(new.path, target, new.mode)
        self.report(_report_line(change))

    def finish(self, success: bool) -> None:
        """Set directory modes, deepest first; after a failure only restore what was opened."""
        modes = dict(self.temporary_modes)
        if success:
            modes.update(self.final_modes)
        for path in sorted(modes, key=lambda path: path.count("/"), reverse=True):
            if os.path.isdir(path) and not os.path.islink(path):
                os.chmod(path, modes[path])
                if success and path in self.mode_reports:
                    self.report(self.mode_reports[path])


def apply_changes(lower: str, changes: list[Change], report: Output) -> None:
    """Apply ``changes`` to ``lower``, creating it if needed (FR-40, FR-42, FR-45).

    On failure, the error names the change that failed; the reported lines before it
    were applied. Running again is safe: applied changes are no longer changes.
    """
    os.makedirs(lower, exist_ok=True)
    merger = _Merger(lower, report)
    success = False
    try:
        for change in changes:
            try:
                merger.apply(change)
            except OSError as exc:
                raise AgentSandboxError(
                    f"merge stopped at {quote_path(change.path)}: {exc.strerror or exc}; the "
                    "changes listed above were applied, the others are still pending"
                ) from exc
        success = True
    finally:
        merger.finish(success)


def _cleanup_stale_mount(pending: Pending, host_env: Mapping[str, str]) -> None:
    """Remove a mount left by a crashed session (FR-62); needed before removing state."""
    if is_mounted(pending.jail.merged):
        unmount_overlay(pending.jail.merged, find_fusermount(host_env.get("PATH")))


def _find(state_option: str | None, path: str, host_env: Mapping[str, str], cwd: str) -> Pending:
    state_dir = state_dir_location(state_option, host_env)
    resolved = normalize_path(path, cwd)
    pending = find_pending(state_dir, resolved)
    if pending is None:
        raise AgentSandboxError(f"no pending changes for {resolved}")
    return pending


def _summary(title: str, changes: list[Change]) -> str:
    return f"{title}\n{format_stat(changes)}"


def _check_conflicts(changes: list[Change], io: Io, *, force: bool, dry_run: bool) -> None:
    """Warn about conflicts; without ``--force`` a real merge refuses (FR-47)."""
    found = conflicts(changes)
    for conflict in found:
        io.warn(f"conflict: {conflict}")
    if not found or force:
        return
    if dry_run:
        io.warn(f"merge would refuse because of {len(found)} conflicts; --force overrides this")
        return
    raise AgentSandboxError(
        f"{len(found)} conflicts: the host changed these files after the sandbox did; "
        "nothing was changed. Use --force to replace the host's versions"
    )


def merge(options: MergeOptions, io: Io, *, host_env: Mapping[str, str], cwd: str) -> int:
    """``agent-sandbox merge`` (FR-40 to FR-43, FR-45, FR-46)."""
    pending = _find(options.state_dir, options.path, host_env, cwd)
    path = pending.meta.path
    if options.dry_run:
        with pending.reading() as modes:
            changes = pending.changes(modes)
        check_mergeable(changes)
        _check_conflicts(changes, io, force=options.force, dry_run=True)
        if changes:
            io.out(_summary(f"Changes that merge would apply to {path}:", changes))
        return 0
    with locked(pending.jail.lock, path, AgentSandboxError):
        _cleanup_stale_mount(pending, host_env)
        with readable_tree(pending.jail.upper) as modes:
            changes = pending.changes(modes)
            check_mergeable(changes)
            _check_conflicts(changes, io, force=options.force, dry_run=False)
            if changes and not options.yes:
                io.out(_summary(f"Changes to apply to {path}:", changes))
                if not io.ask(f"Apply these {len(changes)} changes to {path}? [y/N] "):
                    raise AgentSandboxError("merge cancelled; nothing was changed")
            apply_changes(pending.lower, changes, io.out)
        _remove_state(pending, f"merged into {path}")
    io.out(f"Merged {len(changes)} changes into {path}.\n")
    return 0


def _remove_state(pending: Pending, done: str) -> None:
    """Remove the jail's state; a failure says what was already done."""
    try:
        remove_jail(pending.jail)
    except OSError as exc:
        raise AgentSandboxError(
            f"the changes were {done}, but the state in {pending.jail.root} could not be "
            f"removed: {exc.strerror}: {exc.filename}"
        ) from exc


def discard(options: DiscardOptions, io: Io, *, host_env: Mapping[str, str], cwd: str) -> int:
    """``agent-sandbox discard``: delete the pending changes, never the real path (FR-44)."""
    pending = _find(options.state_dir, options.path, host_env, cwd)
    path = pending.meta.path
    with locked(pending.jail.lock, path, AgentSandboxError):
        _cleanup_stale_mount(pending, host_env)
        with readable_tree(pending.jail.upper) as modes:
            changes = pending.changes(modes)
        if changes and not options.yes:
            io.out(_summary(f"Changes to discard for {path}:", changes))
            if not io.ask(f"Discard these {len(changes)} changes? [y/N] "):
                raise AgentSandboxError("discard cancelled; nothing was changed")
        _remove_state(pending, f"discarded for {path}")
    io.out(f"Discarded {len(changes)} changes for {path}.\n")
    return 0
