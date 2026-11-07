"""Finding the program and checking ``$PATH`` as seen inside the sandbox (FR-9, FR-29)."""

import os
import posixpath

from agent_sandbox.bwrap import SandboxPlan


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_program(plan: SandboxPlan) -> str | None:
    """Return the path of the program as it will be found in the sandbox, or ``None``.

    A program with a ``/`` is taken relative to the working directory; otherwise the
    sandbox's ``PATH`` is searched. Only visible locations count (FR-9).
    """
    program = plan.argv[0]
    if "/" in program:
        candidates = [os.path.join(plan.chdir, program)]
    elif "PATH" in plan.env:
        candidates = [
            os.path.join(plan.chdir, entry or ".", program)
            for entry in plan.env["PATH"].split(":")
        ]
    else:
        candidates = []
    for candidate in map(posixpath.normpath, candidates):
        if plan.is_visible(candidate) and _executable(candidate):
            return candidate
    return None


def invisible_path_entries(plan: SandboxPlan) -> list[str]:
    """Return ``PATH`` directories that exist on the host but not in the sandbox (FR-29)."""
    result: list[str] = []
    for entry in plan.env.get("PATH", "").split(":"):
        if not os.path.isabs(entry):
            continue
        path = posixpath.normpath(entry)
        if path not in result and os.path.isdir(path) and not plan.is_visible(path):
            result.append(path)
    return result
