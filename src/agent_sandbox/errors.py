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


class SetupError(AgentSandboxError):
    """``run`` could not set up the sandbox, for example because of an invalid mount spec."""

    exit_code = ExitCode.SETUP_FAILED


class ProgramNotFoundError(AgentSandboxError):
    """The program to run does not exist inside the sandbox (FR-9)."""

    exit_code = ExitCode.NOT_FOUND


class Terminated(BaseException):
    """``SIGTERM`` or ``SIGHUP`` arrived while setting up or tearing down (FR-61).

    A ``BaseException`` like ``KeyboardInterrupt``, so that only cleanup code sees it.
    """

    def __init__(self, signum: int) -> None:
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum
