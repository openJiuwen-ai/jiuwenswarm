# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Seccomp BPF filter generation for sandbox syscall restriction.

Generates binary seccomp-bpf programs that can be loaded via bwrap --seccomp.
The BPF program is written to an anonymous memfd whose fd is passed to bwrap.
"""

from __future__ import annotations

import logging
import platform
import struct
from functools import lru_cache
from pathlib import Path

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import SyscallPolicy

configure_logging()
logger = logging.getLogger(__name__)

# ── BPF constants (linux/bpf_common.h, linux/seccomp.h) ──

BPF_LD = 0x00
BPF_W = 0x00
BPF_ABS = 0x20
BPF_JMP = 0x05
BPF_JEQ = 0x10
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


def _fallback_syscall_numbers() -> dict[str, int]:
    """Return a small syscall table for the current architecture."""
    if _audit_arch() == AUDIT_ARCH_AARCH64:
        return {
            "lookup_dcookie": 18,
            "umount2": 39,
            "mount": 40,
            "pivot_root": 41,
            "nfsservctl": 42,
            "acct": 89,
            "exit": 93,
            "unshare": 97,
            "kexec_load": 104,
            "init_module": 105,
            "delete_module": 106,
            "ptrace": 117,
            "reboot": 142,
            "kill": 129,
            "tkill": 130,
            "tgkill": 131,
            "getpid": 172,
            "getppid": 173,
            "add_key": 217,
            "request_key": 218,
            "keyctl": 219,
            "swapon": 224,
            "swapoff": 225,
            "perf_event_open": 241,
            "name_to_handle_at": 264,
            "open_by_handle_at": 265,
            "setns": 268,
            "finit_module": 273,
            "bpf": 280,
            "userfaultfd": 282,
            "kexec_file_load": 294,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "clone3": 435,
        }

    return {
        "read": 0, "write": 1, "open": 2, "close": 3, "stat": 4,
        "fstat": 5, "lstat": 6, "poll": 7, "lseek": 8, "mmap": 9,
        "mprotect": 10, "munmap": 11, "brk": 12, "ioctl": 16,
        "access": 21, "pipe": 22, "select": 23, "sched_yield": 24,
        "dup": 32, "dup2": 33, "pause": 34, "nanosleep": 35,
        "getpid": 39, "socket": 41, "connect": 42, "accept": 43,
        "sendto": 44, "recvfrom": 45, "bind": 49, "listen": 50,
        "clone": 56, "fork": 57, "vfork": 58, "execve": 59, "exit": 60,
        "wait4": 61, "kill": 62, "uname": 63, "fcntl": 72,
        "flock": 73, "fsync": 74, "getcwd": 79, "chdir": 80,
        "mkdir": 83, "rmdir": 84, "link": 86, "unlink": 87,
        "chmod": 90, "chown": 92, "getuid": 102, "getgid": 104,
        "geteuid": 107, "getegid": 108,
        "ptrace": 101, "mount": 165, "umount2": 166, "reboot": 169,
        "swapon": 167, "swapoff": 168, "pivot_root": 155,
        "kexec_load": 246, "kexec_file_load": 320,
        "unshare": 272, "setns": 308, "clone3": 435,
        "userfaultfd": 323, "perf_event_open": 298, "bpf": 321,
        "add_key": 248, "request_key": 249, "keyctl": 250,
        "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
        "open_by_handle_at": 304, "name_to_handle_at": 303,
        "init_module": 175, "finit_module": 313, "delete_module": 176,
        "acct": 163, "nfsservctl": 180, "lookup_dcookie": 212,
    }


@lru_cache(maxsize=1)
def _get_syscall_numbers() -> dict[str, int]:
    """Load the syscall number table for the current architecture.

    Falls back to a small architecture-specific subset if /usr/include headers
    are absent.
    """
    table: dict[str, int] = {}

    machine = _machine()
    if machine in {"aarch64", "arm64"}:
        headers = [
            Path("/usr/include/asm/unistd.h"),
            Path("/usr/include/aarch64-linux-gnu/asm/unistd.h"),
            Path("/usr/include/asm-generic/unistd.h"),
        ]
    else:
        headers = [
            Path("/usr/include/asm/unistd_64.h"),
            Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h"),
            Path("/usr/include/asm-generic/unistd.h"),
        ]

    for header in headers:
        if header.exists():
            try:
                for line in header.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("#define __NR_"):
                        parts = line.split()
                        if len(parts) >= 3:
                            name = parts[1].removeprefix("__NR_")
                            try:
                                table[name] = int(parts[2])
                            except ValueError:
                                continue
                if table:
                    return table
            except OSError:
                continue

    return _fallback_syscall_numbers()


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


def _append_daemon_signal_guard(prog: bytearray, syscall_table: dict[str, int]) -> None:
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
        eperm_index = (
            1
            + len(arg0_targets)
            + (1 if arg1_targets else 0)
            + len(arg1_targets)
            + 1
        )
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
    blocked_names = set(_blocked_syscalls_for_current_arch(policy))

    syscall_table = _get_syscall_numbers()
    blocked_nrs: list[int] = []
    for name in blocked_names:
        if name in syscall_table:
            blocked_nrs.append(syscall_table[name])
        else:
            logger.warning("Unknown syscall '%s', skipping", name)

    blocked_nrs.sort()
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


