"""Exit codes and the errors that map to them (section 5.6)."""

from enum import IntEnum
from typing import ClassVar


class ExitCode(IntEnum):
    """Exit codes of agent-sandbox itself; ``run`` otherwise returns the program's code."""

    OK = 0
    ERROR = 1
    USAGE = 2
    SETUP_FAILED = 125
    NOT_FOUND = 127


class AgentSandboxError(Exception):
    """An expected error, reported as ``agent-sandbox: error: <message>`` (FR-92)."""

    exit_code: ClassVar[ExitCode] = ExitCode.ERROR


class UsageError(AgentSandboxError):
    """Invalid command-line arguments or input files given on the command line."""

    exit_code = ExitCode.USAGE
