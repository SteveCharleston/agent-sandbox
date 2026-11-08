"""Tests for the seccomp filter against terminal input injection (FR-24b)."""

import errno
import struct

import pytest

from agent_sandbox.seccomp import (
    AUDIT_ARCH_AARCH64,
    AUDIT_ARCH_ARM,
    AUDIT_ARCH_I386,
    AUDIT_ARCH_X86_64,
    SECCOMP_RET_ALLOW,
    SECCOMP_RET_ERRNO,
    TIOCLINUX,
    TIOCSTI,
    X32_SYSCALL_BIT,
    Instruction,
    assemble,
    tty_injection_filter,
)

DENY = SECCOMP_RET_ERRNO | errno.EPERM
TIOCGWINSZ = 0x5413


def seccomp_data(nr: int, arch: int, *args: int) -> bytes:
    """``struct seccomp_data`` on a little-endian machine."""
    padded = (list(args) + [0] * 6)[:6]
    return struct.pack("<iIQ6Q", nr, arch, 0, *padded)


def run_filter(program: bytes, data: bytes) -> int:
    """A minimal classic-BPF interpreter for the instructions the filter uses."""
    pc, accumulator = 0, 0
    while True:
        code, true, false, k = (
            int(value) for value in struct.unpack_from("=HBBI", program, pc * 8)
        )
        if code == 0x20:  # ld word at absolute offset
            accumulator = int(struct.unpack_from("<I", data, k)[0])
            pc += 1
        elif code == 0x15:  # jeq k
            pc += 1 + (true if accumulator == k else false)
        elif code == 0x06:  # ret k
            return k
        else:
            raise AssertionError(f"unexpected BPF opcode {code:#x}")


X86_64 = tty_injection_filter("x86_64")
AARCH64 = tty_injection_filter("aarch64")


@pytest.mark.parametrize(
    ("program", "arch", "nr"),
    [
        (X86_64, AUDIT_ARCH_X86_64, 16),
        (X86_64, AUDIT_ARCH_X86_64, X32_SYSCALL_BIT | 514),
        (X86_64, AUDIT_ARCH_X86_64, X32_SYSCALL_BIT | 16),
        (X86_64, AUDIT_ARCH_I386, 54),
        (AARCH64, AUDIT_ARCH_AARCH64, 29),
        (AARCH64, AUDIT_ARCH_ARM, 54),
    ],
)
@pytest.mark.parametrize("request_value", [TIOCSTI, TIOCLINUX, 0xFFFFFFFF_00000000 | TIOCSTI])
def test_fr24b_injection_ioctls_are_denied(
    program: bytes | None, arch: int, nr: int, request_value: int
) -> None:
    """FR-24b: TIOCSTI and TIOCLINUX fail with EPERM for native and compat ioctl calls."""
    assert program is not None
    assert run_filter(program, seccomp_data(nr, arch, 0, request_value)) == DENY


@pytest.mark.parametrize(
    ("arch", "nr", "request_value"),
    [
        (AUDIT_ARCH_X86_64, 16, TIOCGWINSZ),
        (AUDIT_ARCH_X86_64, 0, TIOCSTI),
        (AUDIT_ARCH_X86_64, 54, TIOCSTI),
        (AUDIT_ARCH_I386, 16, TIOCSTI),
        (AUDIT_ARCH_I386, 54, TIOCGWINSZ),
        (AUDIT_ARCH_AARCH64, 16, TIOCSTI),
        (0x12345678, 16, TIOCSTI),
    ],
)
def test_fr24b_everything_else_is_allowed(arch: int, nr: int, request_value: int) -> None:
    """FR-24b: other requests, other system calls and other architectures are untouched."""
    assert X86_64 is not None
    assert run_filter(X86_64, seccomp_data(nr, arch, 0, request_value)) == SECCOMP_RET_ALLOW


def test_fr24b_unsupported_architecture() -> None:
    """FR-24b: no filter where the system call numbers are unknown."""
    assert tty_injection_filter("riscv64") is None
    assert tty_injection_filter("s390x") is None


def test_fr24b_host_filter_exists() -> None:
    """FR-24b: the test machines (x86_64, aarch64) get a filter."""
    assert tty_injection_filter() is not None


def test_assemble_rejects_backward_jumps() -> None:
    """BPF only jumps forward; the assembler refuses anything else."""
    program = [
        Instruction(0x06, 0, label="start"),
        Instruction(0x15, 0, true="start", false="start"),
    ]
    with pytest.raises(ValueError, match="out of range"):
        assemble(program)
