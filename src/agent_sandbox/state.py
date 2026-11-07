"""The state directory that holds jailed changes (section 8)."""

import os
from collections.abc import Mapping

STATE_DIR_VARIABLE = "AGENT_SANDBOX_STATE_DIR"


def state_dir_location(option: str | None, env: Mapping[str, str]) -> str:
    """Return the absolute, resolved state directory (FR-80).

    Precedence: ``--state-dir``, ``$AGENT_SANDBOX_STATE_DIR``,
    ``$XDG_STATE_HOME/agent-sandbox`` (only if absolute, as the XDG specification
    requires), ``~/.local/state/agent-sandbox``. The directory need not exist.
    """
    xdg_state_home = env.get("XDG_STATE_HOME", "")
    if option:
        location = option
    elif env.get(STATE_DIR_VARIABLE):
        location = env[STATE_DIR_VARIABLE]
    elif os.path.isabs(xdg_state_home):
        location = os.path.join(xdg_state_home, "agent-sandbox")
    else:
        home = env.get("HOME") or os.path.expanduser("~")
        location = os.path.join(home, ".local", "state", "agent-sandbox")
    return os.path.realpath(os.path.expanduser(location))
