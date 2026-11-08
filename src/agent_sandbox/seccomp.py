"""A seccomp filter against terminal input injection: TIOCSTI and TIOCLINUX (FR-24b).

The filter is a classic BPF program over ``struct seccomp_data`` (``nr`` at offset 0,
``arch`` at 4, ``args[i]`` at ``16 + 8 * i``), in the format ``bwrap --seccomp`` reads.
"""

import errno
import platform
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass

TIOCSTI = 0x5412
TIOCLINUX = 0x541C
BLOCKED_REQUESTS = (TIOCSTI, TIOCLINUX)

SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_I386 = 0x40000003
AUDIT_ARCH_AARCH64 = 0xC00000B7
AUDIT_ARCH_ARM = 0x40000028
X32_SYSCALL_BIT = 0x40000000

# ioctl's system call numbers per audit architecture, for each host machine. x32 uses
# the x86_64 architecture with X32_SYSCALL_BIT; its ioctl is 514 (16 is blocked too).
IOCTL_SYSCALLS: dict[str, tuple[tuple[int, tuple[int, ...]], ...]] = {
    "x86_64": (
        (AUDIT_ARCH_X86_64, (16, X32_SYSCALL_BIT | 16, X32_SYSCALL_BIT | 514)),
        (AUDIT_ARCH_I386, (54,)),
    ),
    "aarch64": (
        (AUDIT_ARCH_AARCH64, (29,)),
        (AUDIT_ARCH_ARM, (54,)),
    ),
}

OFFSET_NR = 0
OFFSET_ARCH = 4
OFFSET_ARG1_LOW = 24  # low 32 bits of args[1] on little-endian; the kernel uses only these

_LD_W_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ_K = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_RET_K = 0x06  # BPF_RET | BPF_K


@dataclass(frozen=True)
class Instruction:
    """A BPF instruction; jump targets are label names until assembled."""

    code: int
    k: int
    true: str | None = None
    false: str | None = None
    label: str | None = None


def _load(offset: int, label: str | None = None) -> Instruction:
    return Instruction(_LD_W_ABS, offset, label=label)


def _ret(value: int, label: str | None = None) -> Instruction:
    return Instruction(_RET_K, value, label=label)


def _match_any(values: Sequence[int], prefix: str, target: str) -> list[Instruction]:
    """Jump to ``target`` if the accumulator equals any value, else to ``allow``."""
    return [
        Instruction(
            _JEQ_K,
            value,
            true=target,
            false=f"{prefix}{index + 1}" if index + 1 < len(values) else "allow",
            label=f"{prefix}{index}",
        )
        for index, value in enumerate(values)
    ]


def _program(architectures: Sequence[tuple[int, tuple[int, ...]]]) -> list[Instruction]:
    """The filter: other architectures, other system calls and other requests are allowed."""
    program = [_load(OFFSET_ARCH)]
    for index, (arch, _) in enumerate(architectures):
        has_next = index + 1 < len(architectures)
        program.append(
            Instruction(
                _JEQ_K,
                arch,
                true=f"nr-of-arch{index}",
                false=f"arch{index + 1}" if has_next else "allow",
                label=f"arch{index}",
            )
        )
    for index, (_, numbers) in enumerate(architectures):
        program.append(_load(OFFSET_NR, label=f"nr-of-arch{index}"))
        program += _match_any(numbers, f"arch{index}-nr", "ioctl")
    program.append(_load(OFFSET_ARG1_LOW, label="ioctl"))
    program += _match_any(BLOCKED_REQUESTS, "request", "deny")
    program.append(_ret(SECCOMP_RET_ALLOW, label="allow"))
    program.append(_ret(SECCOMP_RET_ERRNO | errno.EPERM, label="deny"))
    return program


def assemble(program: Sequence[Instruction]) -> bytes:
    """Resolve labels to relative jumps and encode ``struct sock_filter`` entries."""
    positions = {insn.label: index for index, insn in enumerate(program) if insn.label}
    encoded = []
    for index, insn in enumerate(program):
        if insn.true is None or insn.false is None:
            encoded.append(struct.pack("=HBBI", insn.code, 0, 0, insn.k))
            continue
        true = positions[insn.true] - index - 1
        false = positions[insn.false] - index - 1
        if not (0 <= true <= 255 and 0 <= false <= 255):
            raise ValueError("BPF jump out of range")
        encoded.append(struct.pack("=HBBI", insn.code, true, false, insn.k))
    return b"".join(encoded)


def tty_injection_filter(machine: str | None = None) -> bytes | None:
    """The compiled filter for this machine, or ``None`` if the architecture is unsupported."""
    machine = platform.machine() if machine is None else machine
    architectures = IOCTL_SYSCALLS.get(machine)
    if architectures is None or sys.byteorder != "little":
        return None
    return assemble(_program(architectures))
