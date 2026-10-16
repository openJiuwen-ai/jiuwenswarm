"""Portable BPF evaluation and runtime admission regressions for Linux seccomp."""

import io
from pathlib import Path
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from jiuwenbox.models.policy import SecurityPolicy, SyscallPolicy
from jiuwenbox.supervisor import seccomp


ARCHES = [
    ("x86_64", 0xC000003E),
    ("amd64", 0xC000003E),
    ("aarch64", 0xC00000B7),
    ("arm64", 0xC00000B7),
]


def evaluate(program, nr, arch, *args):
    """Interpret the actual cBPF bytes, including jumps and argument reloads."""
    data = struct.pack("<iIQ6Q", nr, arch, 0, *(list(args) + [0] * (6 - len(args))))
    instructions = list(struct.iter_unpack("<HBBI", program))
    pc, acc = 0, 0
    for _ in range(len(instructions) + 1):
        code, jt, jf, k = instructions[pc]
        pc += 1
        if code == 0x20:  # LD W ABS
            acc = struct.unpack_from("<I", data, k)[0]
        elif code == 0x15:  # JMP JEQ K
            pc += jt if acc == k else jf
        elif code == 0x35:  # JMP JGE K
            pc += jt if acc >= k else jf
        elif code == 0x45:  # JMP JSET K
            pc += jt if acc & k else jf
        elif code == 0x05:  # JMP JA
            pc += k
        elif code == 0x06:  # RET K
            return k
        else:
            raise AssertionError(f"Unexpected BPF opcode {code}")
        assert 0 <= pc < len(instructions), "BPF jump outside program"
    raise AssertionError("BPF did not terminate")


@pytest.fixture(params=ARCHES, ids=[item[0] for item in ARCHES])
def abi(request, monkeypatch):
    machine, arch = request.param
    monkeypatch.setattr(seccomp, "_machine", lambda: machine)
    return arch, seccomp._get_syscall_numbers()


def test_empty_policy_keeps_every_baseline_block(abi):
    arch, table = abi
    for policy in (SyscallPolicy(), SyscallPolicy(x86_64={"blocked": []}, arm64={"blocked": []})):
        program = seccomp.build_seccomp_filter(policy)
        for name in seccomp.MINIMUM_BLOCKED_SYSCALLS:
            assert evaluate(program, table[name], arch) == 0x00050001, name


def test_policy_blocks_are_additive_and_arch_specific(abi):
    arch, table = abi
    key = "arm64" if arch == 0xC00000B7 else "x86_64"
    other = "x86_64" if key == "arm64" else "arm64"
    policy = SyscallPolicy(
        **{
            key: {"blocked": ["getppid", "getppid", "clone3"]},
            other: {"blocked": ["not_a_native_syscall"]},
        }
    )
    program = seccomp.build_seccomp_filter(policy)
    assert evaluate(program, table["getppid"], arch) == 0x00050001
    assert evaluate(program, table["clone3"], arch) == 0x00050026  # ENOSYS, libc fallback
    assert evaluate(program, table["ptrace"], arch) == 0x00050001


@pytest.mark.parametrize(
    "name",
    [
        "read",
        "write",
        "openat",
        "close",
        "execve",
        "clone",
        "clone3",
        "futex",
        "mmap",
        "mprotect",
        "socket",
        "wait4",
    ],
)
def test_normal_workload_syscalls_remain_allowed(abi, name):
    arch, table = abi
    assert evaluate(seccomp.build_seccomp_filter(SyscallPolicy()), table[name], arch) == 0x7FFF0000


@pytest.mark.parametrize(
    "name,args,denied",
    [
        ("kill", (0, 15), True),
        ("kill", (1, 15), True),
        ("kill", (2, 15), True),
        ("kill", (0xFFFFFFFF, 15), True),
        ("kill", (0xFFFFFFFE, 15), True),
        ("kill", (18, 15), False),
        ("kill", (0xFFFFFFED, 15), False),
        ("tkill", (2, 15), True),
        ("tkill", (18, 15), False),
        ("tgkill", (18, 2, 15), True),
        ("tgkill", (2, 18, 15), True),
        ("tgkill", (18, 19, 15), False),
    ],
)
def test_infrastructure_signal_guard_keeps_normal_process_signals(abi, name, args, denied):
    arch, table = abi
    result = evaluate(seccomp.build_seccomp_filter(SyscallPolicy()), table[name], arch, *args)
    assert result == (0x00050001 if denied else 0x7FFF0000)


@pytest.mark.parametrize("other_arch", [0x40000003, 0, 0xC000003E, 0xC00000B7])
def test_foreign_arch_is_killed(abi, other_arch):
    arch, _ = abi
    if arch != other_arch:
        assert evaluate(seccomp.build_seccomp_filter(SyscallPolicy()), 0, other_arch) == 0x80000000


@pytest.mark.parametrize("nr", [0x40000000, 0x40000065, 0x40000000 | 521, 512, 521, 547])
def test_x32_and_legacy_x32_cannot_bypass_blocks(monkeypatch, nr):
    monkeypatch.setattr(seccomp, "_machine", lambda: "x86_64")
    assert evaluate(seccomp.build_seccomp_filter(SyscallPolicy()), nr, 0xC000003E) == 0x00050026


def test_syscall_tables_are_pinned_and_native(monkeypatch):
    monkeypatch.setattr(
        Path, "read_text", Mock(side_effect=AssertionError("must not read headers"))
    )
    for machine, expected in [("x86_64", (101, 165, 250, 202)), ("aarch64", (117, 40, 219, 98))]:
        monkeypatch.setattr(seccomp, "_machine", lambda: machine)
        table = seccomp._get_syscall_numbers()
        assert tuple(table[name] for name in ("ptrace", "mount", "keyctl", "futex")) == expected
        assert all(nr < 512 for nr in table.values())


@pytest.mark.parametrize("machine", ["i686", "armv7l", "riscv64", "unknown"])
def test_unsupported_arch_fails_closed(monkeypatch, machine):
    monkeypatch.setattr(seccomp, "_machine", lambda: machine)
    with pytest.raises(RuntimeError, match="Unsupported seccomp architecture"):
        seccomp.build_seccomp_filter(SyscallPolicy())


@pytest.mark.parametrize("byteorder,pointer_size", [("big", 8), ("little", 4)])
def test_unsupported_runtime_abi_fails_closed(monkeypatch, byteorder, pointer_size):
    monkeypatch.setattr(seccomp, "sys", SimpleNamespace(byteorder=byteorder))
    monkeypatch.setattr(seccomp.struct, "calcsize", lambda _: pointer_size)
    with pytest.raises(RuntimeError, match="64-bit runtime"):
        seccomp.build_seccomp_filter(SyscallPolicy())


def test_unknown_configured_syscall_fails_instead_of_silently_skipping(abi):
    policy = SyscallPolicy(x86_64={"blocked": ["ptrcae"]}, arm64={"blocked": ["ptrcae"]})
    with pytest.raises(ValueError, match="ptrcae"):
        seccomp.build_seccomp_filter(policy)


@pytest.mark.parametrize(
    "filename", ["default-policy.yaml", "enterprise-policy.yaml", "java-policy.yaml"]
)
def test_bundled_policies_build_for_both_native_arches(abi, filename):
    path = Path(seccomp.__file__).parents[1] / "configs" / filename
    policy = SecurityPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    arch, table = abi
    program = seccomp.build_seccomp_filter(policy.syscall)
    for name in seccomp.MINIMUM_BLOCKED_SYSCALLS:
        assert evaluate(program, table[name], arch) == 0x00050001


def test_empty_policy_has_same_filter_as_existing_linux_default(abi):
    path = Path(seccomp.__file__).parents[1] / "configs" / "default-policy.yaml"
    policy = SecurityPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert seccomp.build_seccomp_filter(policy.syscall) == seccomp.build_seccomp_filter(
        SyscallPolicy()
    )


@pytest.mark.parametrize("source", ["missing", "corrupt", "empty"])
def test_base_policy_fallback_cannot_remove_baseline(abi, monkeypatch, tmp_path, source):
    from jiuwenbox.server import policy_reader

    base = tmp_path / "base.yaml"
    if source != "missing":
        base.write_text("syscall: [invalid yaml" if source == "corrupt" else "{}", encoding="utf-8")
    monkeypatch.setattr(policy_reader, "base_policy_path", lambda: base)
    monkeypatch.setattr(policy_reader, "_resolve_tool_paths", lambda value: value)
    policy = policy_reader.PolicyReader(policy_path=base).load_policy()
    arch, table = abi
    program = seccomp.build_seccomp_filter(policy.syscall)
    assert evaluate(program, table["ptrace"], arch) == 0x00050001
    assert evaluate(program, table["mount"], arch) == 0x00050001
    assert evaluate(program, table["keyctl"], arch) == 0x00050001


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["build", "memfd"])
async def test_linux_create_never_spawns_without_seccomp(monkeypatch, tmp_path, failure):
    from jiuwenbox.server.runtime import process

    runtime = process.ProcessRuntime()
    monkeypatch.setattr(process, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runtime, "_load_policy", lambda _: SecurityPolicy())
    for name in (
        "_policy_binds_for_sandbox",
        "_install_sandbox_host_firewall_rules",
        "_ensure_launcher_dir",
        "_ensure_landlock_payload",
    ):
        monkeypatch.setattr(runtime, name, Mock())
    monkeypatch.setattr(runtime, "_ensure_named_netns", lambda *a: "test-netns")
    runtime._netns_names["sb"] = "test-netns"
    listener = Mock()
    listener.fileno.return_value = 42
    control = tmp_path / "control"
    control.mkdir()
    runtime._control_dirs["sb"] = control
    runtime._daemon_control_tokens[control / process.SANDBOX_CONTROL_SOCKET_NAME] = b"token"
    monkeypatch.setattr(runtime, "_create_daemon_listener", lambda *a: listener)
    monkeypatch.setattr(process.network_module, "namespace_exists", lambda _: True)
    delete_namespace = Mock()
    monkeypatch.setattr(process.network_module, "delete_named_namespace", delete_namespace)
    monkeypatch.setattr(runtime, "_remove_sandbox_host_firewall_rules", Mock())
    popen = Mock(side_effect=AssertionError("must not spawn without seccomp"))
    monkeypatch.setattr(process.subprocess, "Popen", popen)
    if failure == "build":
        monkeypatch.setattr(
            process, "build_seccomp_filter", Mock(side_effect=ValueError("bad ABI"))
        )
    else:
        monkeypatch.setattr(runtime, "_ensure_seccomp_bpf", lambda *a: b"filter")
        monkeypatch.setattr(
            runtime, "_open_seccomp_fd_from_bytes", Mock(side_effect=OSError("memfd failed"))
        )
    with pytest.raises(RuntimeError, match="Seccomp is required.*refusing to start"):
        await runtime.create("sb", tmp_path / "policy.yaml")
    popen.assert_not_called()
    listener.close.assert_called_once()
    delete_namespace.assert_called_once_with("test-netns")
    assert not control.exists()
    for cache in (
        runtime._runtime_policies,
        runtime._policy_paths,
        runtime._network_modes,
        runtime._netns_names,
        runtime._daemon_socket_ready,
        runtime._seccomp_bpf,
        runtime._control_dirs,
        runtime._daemon_control_tokens,
    ):
        assert not cache


@pytest.mark.asyncio
async def test_windows_create_keeps_existing_desktop_path(monkeypatch, tmp_path):
    from jiuwenbox.server.runtime import process

    runtime = process.ProcessRuntime()
    monkeypatch.setattr(process, "sys", SimpleNamespace(platform="win32"))
    windows = AsyncMock(return_value=123)
    monkeypatch.setattr(runtime, "_create_windows", windows)
    monkeypatch.setattr(
        process, "build_seccomp_filter", Mock(side_effect=AssertionError("Linux only"))
    )
    assert await runtime.create("desktop", tmp_path / "policy.yaml", {"TOOL": "value"}) == 123
    windows.assert_awaited_once_with("desktop", tmp_path / "policy.yaml", {"TOOL": "value"})


@pytest.mark.parametrize(
    "program", [b"", b"bad", b"x" * (4097 * 8)], ids=["empty", "unaligned", "too-large"]
)
def test_invalid_bpf_cannot_create_memfd(program):
    from jiuwenbox.server.runtime.process import ProcessRuntime

    with pytest.raises(ValueError, match="BPF program size"):
        ProcessRuntime._open_seccomp_fd_from_bytes(program)


@pytest.mark.parametrize("failure", ["write", "zero-write", "seek"])
def test_memfd_io_failure_closes_descriptor(monkeypatch, failure):
    from jiuwenbox.server.runtime import process

    close = Mock()
    monkeypatch.setattr(
        process,
        "os",
        SimpleNamespace(
            memfd_create=lambda *a: 42,
            write=Mock(side_effect=OSError("write failed"))
            if failure == "write"
            else lambda *a: 0 if failure == "zero-write" else 8,
            lseek=Mock(side_effect=OSError("seek failed")) if failure == "seek" else Mock(),
            SEEK_SET=0,
            close=close,
        ),
    )
    with pytest.raises(OSError):
        process.ProcessRuntime._open_seccomp_fd_from_bytes(b"x" * 8)
    close.assert_called_once_with(42)


def test_kernel_filter_install_failure_is_not_marked_ready(monkeypatch):
    from jiuwenbox.server.runtime import process

    runtime = process.ProcessRuntime()
    proc = Mock(returncode=1)
    proc.poll.return_value = 1
    runtime._processes["sb"] = proc
    cleanup = Mock()
    monkeypatch.setattr(runtime, "_cleanup_sandbox_artifacts", cleanup)
    with pytest.raises(RuntimeError, match="Failed to load seccomp"):
        runtime._verify_daemon_alive("sb", proc, None, io.BytesIO(b"bwrap: Failed to load seccomp"))
    assert "sb" not in runtime._processes
    assert not runtime._daemon_socket_ready.get("sb")
    cleanup.assert_called_once_with("sb", None)
