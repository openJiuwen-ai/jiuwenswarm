# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Seccomp BPF filter generation for sandbox syscall restriction.

Generates binary seccomp-bpf programs that can be loaded via bwrap --seccomp.
The BPF program is written to an anonymous memfd whose fd is passed to bwrap.
"""

from __future__ import annotations

import logging
import platform
import struct
import sys
from collections.abc import Mapping
from types import MappingProxyType

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import SyscallPolicy
from jiuwenbox.supervisor.syscall_numbers import AARCH64_SYSCALLS, X86_64_SYSCALLS

configure_logging()
logger = logging.getLogger(__name__)

# ── BPF constants (linux/bpf_common.h, linux/seccomp.h) ──

BPF_LD = 0x00
BPF_W = 0x00
BPF_ABS = 0x20
BPF_JMP = 0x05
BPF_JEQ = 0x10
BPF_JGE = 0x30
BPF_JSET = 0x40
BPF_K = 0x00
BPF_RET = 0x06

SECCOMP_RET_ALLOW = 0x7FFF_0000
SECCOMP_RET_ERRNO = 0x0005_0000
SECCOMP_RET_KILL_PROCESS = 0x8000_0000

ERRNO_EPERM = 1
ERRNO_ENOSYS = 38

AUDIT_ARCH_X86_64 = 0xC000_003E
AUDIT_ARCH_AARCH64 = 0xC000_00B7

SUPPORTED_AUDIT_ARCHES = {
    "amd64": AUDIT_ARCH_X86_64,
    "x86_64": AUDIT_ARCH_X86_64,
    "aarch64": AUDIT_ARCH_AARCH64,
    "arm64": AUDIT_ARCH_AARCH64,
}

ARCH_ALIASES = {
    "amd64": ("x86_64", "amd64"),
    "x86_64": ("x86_64", "amd64"),
    "aarch64": ("arm64", "aarch64"),
    "arm64": ("arm64", "aarch64"),
}

# offsetof(struct seccomp_data, nr)
SECCOMP_DATA_NR_OFFSET = 0
# offsetof(struct seccomp_data, arch)
SECCOMP_DATA_ARCH_OFFSET = 4
# offsetof(struct seccomp_data, args[0]) / args[1]
SECCOMP_DATA_ARG0_OFFSET = 16
SECCOMP_DATA_ARG1_OFFSET = 24

# kill(2) pid arguments compared via BPF_W (lower 32 bits of seccomp args[0]).
_DAEMON_PROTECTED_KILL_TARGETS = (
    0,
    1,
    2,
    0xFFFFFFFE,  # -2, kill process group 2
    0xFFFFFFFF,  # -1, broadcast to same-UID processes
)
_DAEMON_PROTECTED_TGKILL_TARGETS = (1, 2)


def _machine() -> str:
    """Return the normalized machine name used for syscall/seccomp tables."""
    return platform.machine().lower()


def _audit_arch() -> int:
    """Return the Linux audit architecture constant for this process."""
    machine = _machine()
    try:
        return SUPPORTED_AUDIT_ARCHES[machine]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported seccomp architecture: {machine}") from exc


def _arch_aliases() -> tuple[str, ...]:
    """Return policy key aliases for the current architecture."""
    return ARCH_ALIASES.get(_machine(), (_machine(),))


# The same baseline as the bundled Linux default policy. Configured blocks
# are additive; even an empty model or an unreadable YAML cannot remove it.
MINIMUM_BLOCKED_SYSCALLS = frozenset(
    {
        "kexec_load",
        "kexec_file_load",
        "reboot",
        "pivot_root",
        "swapon",
        "swapoff",
        "nfsservctl",
        "lookup_dcookie",
        "ptrace",
        "mount",
        "umount2",
        "unshare",
        "setns",
        "userfaultfd",
        "perf_event_open",
        "bpf",
        "add_key",
        "request_key",
        "keyctl",
        "io_uring_setup",
        "io_uring_enter",
        "io_uring_register",
        "open_by_handle_at",
        "name_to_handle_at",
        "init_module",
        "finit_module",
        "delete_module",
        "acct",
    }
)

_NATIVE_SYSCALL_TABLES = {
    AUDIT_ARCH_X86_64: MappingProxyType(X86_64_SYSCALLS),
    AUDIT_ARCH_AARCH64: MappingProxyType(AARCH64_SYSCALLS),
}


def _get_syscall_numbers() -> Mapping[str, int]:
    """Return a pinned native ABI table, independent of installed headers."""
    if sys.byteorder != "little" or struct.calcsize("P") != 8:
        raise RuntimeError("Seccomp requires a supported little-endian 64-bit runtime")
    return _NATIVE_SYSCALL_TABLES[_audit_arch()]


def _blocked_syscalls_for_current_arch(policy: SyscallPolicy) -> list[str]:
    """Return the syscall block list for the current architecture."""
    for key in _arch_aliases():
        arch_policy = getattr(policy, key, None)
        if arch_policy is not None:
            return list(dict.fromkeys(arch_policy.blocked))

    return []


def _bpf_stmt(code: int, k: int) -> bytes:
    """Encode a single BPF instruction (8 bytes)."""
    return struct.pack("<HBBI", code, 0, 0, k)


def _bpf_jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def _append_daemon_signal_guard(prog: bytearray, syscall_table: Mapping[str, int]) -> None:
    """Deny kill-family syscalls that target sandbox infrastructure PIDs.

    Appended after policy-driven syscall blocks and before the default
    ALLOW.  Each guarded syscall uses an inline EPERM return so this
    section does not share tail-jump offsets with the policy blocklist.
    """
    guarded = (
        ("kill", _DAEMON_PROTECTED_KILL_TARGETS, ()),
        ("tkill", _DAEMON_PROTECTED_KILL_TARGETS, ()),
        ("tgkill", _DAEMON_PROTECTED_KILL_TARGETS, _DAEMON_PROTECTED_TGKILL_TARGETS),
    )
    for name, arg0_targets, arg1_targets in guarded:
        nr = syscall_table.get(name)
        if nr is None:
            continue

        body = bytearray()
        body += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, SECCOMP_DATA_ARG0_OFFSET)
        eperm_index = 1 + len(arg0_targets) + (1 if arg1_targets else 0) + len(arg1_targets) + 1
        for index, value in enumerate(arg0_targets):
            remaining = eperm_index - (1 + index + 1)
            body += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, value, remaining, 0)
        if arg1_targets:
            body += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, SECCOMP_DATA_ARG1_OFFSET)
            arg1_start = 1 + len(arg0_targets) + 1
            for index, value in enumerate(arg1_targets):
                remaining = eperm_index - (arg1_start + index + 1)
                body += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, value, remaining, 0)
        # ``BPF_JMP|BPF_K`` uses the immediate ``k`` field as the jump
        # offset.  When no protected PID matched above, skip the EPERM RET
        # below (``k=1``).  ``k=0`` incorrectly falls through to EPERM and
        # blocks every other kill/tkill/tgkill target (e.g. user pid 18).
        body += _bpf_jump(BPF_JMP | BPF_K, 1, 0, 0)
        body += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ERRNO_EPERM)

        skip_body = len(body) // 8
        # Re-load nr before each guard: prior LD arg0/arg1 clobbers the accumulator.
        prog += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, SECCOMP_DATA_NR_OFFSET)
        prog += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, nr, 0, skip_body)
        prog.extend(body)


def build_seccomp_filter(policy: SyscallPolicy) -> bytes:
    """Build a seccomp-bpf binary filter from a SyscallPolicy.

    Returns raw bytes suitable for writing to a memfd and passing to
    bwrap via --seccomp <fd>.
    """
    blocked_names = MINIMUM_BLOCKED_SYSCALLS | set(_blocked_syscalls_for_current_arch(policy))

    syscall_table = _get_syscall_numbers()
    unknown = blocked_names - syscall_table.keys()
    if unknown:
        raise ValueError(f"Unknown syscall(s) for {_machine()}: {', '.join(sorted(unknown))}")
    blocked_nrs = sorted({syscall_table[name] for name in blocked_names})
    return _assemble_bpf(blocked_nrs)


def _assemble_bpf(blocked_nrs: list[int]) -> bytes:
    """Assemble a BPF program that blocks the given syscall numbers.

    Structure:
      1. Validate arch == current process architecture
      2. Load syscall number
      3. For each blocked nr: if match -> inline return errno
      4. Deny kill/tkill/tgkill that target sandbox infrastructure PIDs
      5. Default: ALLOW

    Policy blocks use inline RET actions instead of tail jumps so adding
    the daemon signal guard cannot perturb blocked-syscall offsets.
    """
    syscall_table = _get_syscall_numbers()
    clone3_nr = syscall_table.get("clone3")
    prog = bytearray()

    # Load arch
    prog += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, SECCOMP_DATA_ARCH_OFFSET)
    # If arch does not match, kill; jump over the kill instruction when it does.
    prog += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, _audit_arch(), 1, 0)
    prog += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS)

    # Load syscall number
    prog += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, SECCOMP_DATA_NR_OFFSET)

    if _audit_arch() == AUDIT_ARCH_X86_64:
        # x32 shares AUDIT_ARCH_X86_64 but tags nr with bit 30. Checking arch
        # alone would allow the tagged variants of every denied syscall.
        prog += _bpf_jump(BPF_JMP | BPF_JSET | BPF_K, 0x40000000, 0, 1)
        prog += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ERRNO_ENOSYS)
        # Linux < 5.4 also accepted untagged legacy x32 numbers 512..547.
        prog += _bpf_jump(BPF_JMP | BPF_JGE | BPF_K, 512, 0, 2)
        prog += _bpf_jump(BPF_JMP | BPF_JGE | BPF_K, 548, 1, 0)
        prog += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ERRNO_ENOSYS)

    # For each blocked syscall: inline deny on match.
    # On miss, skip only the inline RET (``jf=1``). A larger skip would
    # jump over the next syscall's JEQ as well and leave the filter in an
    # invalid state that makes bwrap crash with SIGSEGV during startup.
    for nr in blocked_nrs:
        prog += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, nr, 0, 1)
        errno = ERRNO_ENOSYS if nr == clone3_nr else ERRNO_EPERM
        prog += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | errno)

    _append_daemon_signal_guard(prog, syscall_table)

    # Default: allow
    prog += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)

    return bytes(prog)
