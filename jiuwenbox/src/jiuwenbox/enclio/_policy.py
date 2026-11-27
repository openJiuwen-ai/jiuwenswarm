# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Public inputs and bounded, pure policy compilation."""

import json
import math
import os
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from jiuwenbox.models.policy import BindMount, CgroupPolicy, FilesystemPolicy, ProcessPolicy, SyscallPolicy
from jiuwenbox.server.runtime.errors import PolicyValidationError
from jiuwenbox.supervisor.daemon_ipc import MAX_HEADER_BYTES, MAX_STDIN_BYTES
from jiuwenbox.supervisor.posix_id import resolve_posix_id

from ._address import endpoint

Kind = Literal["directory", "file"]
Mode = Literal["ro", "rw"]
RUNTIME_LINKS = {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": "usr/lib64"}
PROXY_URL = "http://127.0.0.1:18080"
PROXY_ENV = {
    "http_proxy": PROXY_URL, "https_proxy": PROXY_URL,
    "all_proxy": "socks5h://127.0.0.1:18080", "no_proxy": "",
}
_ENV_KEY = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]*")


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    kind: Kind = "directory"
    mode: Mode = "rw"


@dataclass(frozen=True)
class Endpoint:
    kind: Literal["dns", "ip"]
    address: str
    port: int


@dataclass(frozen=True)
class Network:
    mode: Literal["none", "host", "isolated"] = "none"
    endpoints: tuple[Endpoint, ...] = ()


@dataclass(frozen=True)
class Deployment:
    """Trusted locations and budgets. Runtime mounts must be read-only."""

    # Kept for source compatibility. Authorization is supplied in filesystem_policy.
    authorized_roots: tuple[str, ...] = ()
    runtime_mounts: tuple[Mount, ...] = ()
    bwrap: str = "/usr/bin/bwrap"
    python: str = "/usr/bin/python3"
    sandbox_python: str = "/usr/bin/python3"
    service_uid: int | str = field(default_factory=lambda: os.getuid())
    service_gid: int | str = field(default_factory=lambda: os.getgid())
    allow_host_network: bool = False
    max_concurrent: int = 8
    max_input_bytes: int = MAX_STDIN_BYTES
    max_output_bytes: int = 256 * 1024 * 1024
    startup_seconds: float = 15.0
    cleanup_seconds: float = 5.0
    backing_root: str | None = None


@dataclass(frozen=True)
class SandboxPolicy:
    mounts: tuple[Mount, ...] = ()
    default_cwd: str = "/tmp"
    network: Network = field(default_factory=Network)
    syscall: SyscallPolicy = field(default_factory=SyscallPolicy)
    limits: CgroupPolicy = field(default_factory=CgroupPolicy)
    version: str | None = None
    filesystem_policy: FilesystemPolicy | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    process: ProcessPolicy | None = None


@dataclass(frozen=True)
class CommandRequest:
    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    stdin: bytes = b""
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    exit_code: int | None = None
    term_signal: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    command_started: bool | None = False
    limit_reason: str | None = None
    cleanup_complete: bool = True
    error_stage: str | None = None
    error_code: str | None = None
    initial_status: str | None = None


@dataclass(frozen=True)
class Plan:
    cwd: str
    mode: str
    endpoints: tuple[tuple[str, str, int], ...]
    syscall_json: str
    limits_json: str
    filesystem_json: str
    environment: tuple[tuple[str, str], ...]
    identity: tuple[int, int]


def absolute(path: str) -> str:
    if not isinstance(path, str):
        raise ValueError("invalid_absolute_path")
    if not path.startswith("/") or path.startswith("//") or "\x00" in path:
        raise ValueError("invalid_absolute_path")
    if len(os.fsencode(path)) > 4095 or posixpath.normpath(path) != path:
        raise ValueError("invalid_absolute_path")
    return path


def under(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def _mount(mount: Mount) -> Mount:
    if not isinstance(mount, Mount) or mount.kind not in ("file", "directory") or mount.mode not in ("ro", "rw"):
        raise ValueError("invalid_mount")
    return mount


def _bounded_seconds(value: float) -> float:
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        raise ValueError("invalid_timeout")
    if not 0 < value <= 2**31 or not math.isfinite(value):
        raise ValueError("invalid_timeout")
    return float(value)


def snapshot_deployment(config: Deployment, package_root: str) -> dict:
    absolute(config.python)
    absolute(config.bwrap)
    runtime = tuple(_mount(mount) for mount in config.runtime_mounts)
    if len(runtime) > 64:
        raise ValueError("deployment_mount_budget")
    for mount in runtime:
        absolute(mount.source)
        absolute(mount.target)
        if mount.mode != "ro":
            raise ValueError("invalid_runtime_mount")
    if config.backing_root is not None:
        absolute(config.backing_root)
    sandbox_python = absolute(config.sandbox_python)
    resolved = sandbox_python
    for link, target in RUNTIME_LINKS.items():
        if under(resolved, link):
            resolved = "/" + target + resolved[len(link):]
            break
    if not any(under(resolved, mount.target) for mount in runtime):
        raise ValueError("python_outside_runtime")
    if not isinstance(config.allow_host_network, bool):
        raise ValueError("invalid_deployment_budget")
    for value, minimum, maximum in (
        (config.max_concurrent, 1, 256),
        (config.max_input_bytes, 0, MAX_STDIN_BYTES),
        (config.max_output_bytes, 0, 256 * 1024 * 1024),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("invalid_deployment_budget")
        if not minimum <= value <= maximum:
            raise ValueError("invalid_deployment_budget")
    startup = _bounded_seconds(config.startup_seconds)
    cleanup = _bounded_seconds(config.cleanup_seconds)
    if startup > 300 or cleanup > 60:
        raise ValueError("invalid_deployment_window")
    return {
        "runtime": runtime, "package_root": package_root,
        "bwrap": config.bwrap, "python": config.python, "sandbox_python": sandbox_python,
        "uid": resolve_posix_id(str(config.service_uid), kind="user"),
        "gid": resolve_posix_id(str(config.service_gid), kind="group"),
        "allow_host_network": config.allow_host_network,
        "max_concurrent": config.max_concurrent, "max_input": config.max_input_bytes,
        "max_output": config.max_output_bytes, "startup": startup, "cleanup": cleanup,
        "backing_root": config.backing_root,
    }


def validate_filesystem(filesystem: FilesystemPolicy) -> None:
    # Service-managed storage paths belong to the caller's environment.
    # Import lazily so policy lookup is only performed when validation is needed.
    from jiuwenbox.server.policy_engine import PolicyEngine

    # Treat bulk declarations as binds for validation only. Keep the extra
    # Lite checks here so native ProcessRuntime policy semantics stay unchanged.
    bulk_binds = [
        BindMount(host_path=row.host_root, sandbox_path=row.sandbox_path, mode=row.mode)
        for row in filesystem.bind_root_entries
    ]
    PolicyEngine.validate_filesystem_policy(filesystem.model_copy(update={
        "bind_mounts": [*filesystem.bind_mounts, *bulk_binds],
    }))
    targets = [
        *filesystem.read_only, *filesystem.read_write,
        *(row if isinstance(row, str) else row.path for row in (*filesystem.directories, *filesystem.files)),
        *(row.sandbox_path for row in (*filesystem.bind_mounts, *bulk_binds, *filesystem.device)),
    ]
    for target in targets:
        # Lexical aliases must not bypass Lite's trusted launcher boundary.
        normalized = posixpath.normpath("/" + target.lstrip("/"))
        if under(normalized, "/jiuwenbox"):
            raise ValueError("reserved_sandbox_path")


def compile_plan(policy: SandboxPolicy, deployment: dict) -> Plan:
    mode = policy.network.mode
    if mode not in ("none", "host", "isolated") or (mode == "host" and not deployment["allow_host_network"]):
        raise ValueError("network_mode_denied")
    if len(policy.network.endpoints) > 256 or bool(policy.network.endpoints) != (mode == "isolated"):
        raise ValueError("invalid_network_rules")
    endpoints = tuple(sorted({
        endpoint(row.kind, row.address, row.port) for row in policy.network.endpoints
    }))
    if len(policy.mounts) > 128:
        raise ValueError("mount_budget")
    mounts = tuple(_mount(entry) for entry in policy.mounts)
    filesystem = FilesystemPolicy.model_validate(
        policy.filesystem_policy.model_dump() if policy.filesystem_policy is not None else {"read_write": ["/tmp"]},
    )
    filesystem.bind_mounts.extend(
        BindMount(host_path=row.source, sandbox_path=row.target, mode=row.mode) for row in mounts
    )
    try:
        validate_filesystem(filesystem)
    except PolicyValidationError as exc:
        raise ValueError("invalid_filesystem_policy") from exc
    if (filesystem.directories or filesystem.files) and deployment["backing_root"] is None:
        raise ValueError("backing_root_required")
    environment = dict(policy.environment)
    validate_environment(environment)
    syscall = SyscallPolicy.model_validate(policy.syscall.model_dump())
    for architecture in (syscall.arm64, syscall.x86_64):
        if len(architecture.blocked) > 1024 or any(len(name) > 128 for name in architecture.blocked):
            raise ValueError("syscall_budget")
    identity = (deployment["uid"], deployment["gid"])
    if policy.process is not None:
        process = ProcessPolicy.model_validate(policy.process.model_dump())
        resolved = []
        for value, kind in ((process.run_as_user, "user"), (process.run_as_group, "group")):
            try:
                resolved.append(resolve_posix_id(value, kind=kind))
            except ValueError:
                # Match ProcessRuntime's unresolved identity fallback.
                resolved.append(65534)
        identity = (resolved[0], resolved[1])
    return Plan(
        absolute(policy.default_cwd),
        mode, endpoints,
        syscall.model_dump_json(),
        CgroupPolicy.model_validate(policy.limits.model_dump()).model_dump_json(),
        filesystem.model_dump_json(),
        tuple(sorted(environment.items())),
        identity,
    )


def validate_environment(env: Mapping[str, str]) -> None:
    for key, value in env.items():
        if not isinstance(key, str) or not _ENV_KEY.fullmatch(key):
            raise ValueError("invalid_environment")
        if key.lower() in PROXY_ENV or key.upper().startswith(("ENCLIO_", "JIUWENBOX_")):
            raise ValueError("invalid_environment")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("invalid_environment")


def snapshot_command(command: CommandRequest, plan: Plan, max_input: int) -> dict:
    """在命令进入沙箱前校验并固化请求，返回可直接序列化传给 supervisor 的快照字典"""
    # argv：必须是元组、非空，且首元素（可执行文件）非空。
    if not isinstance(command.argv, tuple) or not command.argv or not command.argv[0]:
        raise ValueError("invalid_argv")
    # 数量预算：参数最多 4096 个、环境变量最多 1024 个。
    if len(command.argv) > 4096 or len(command.env) > 1024:
        raise ValueError("request_budget")
    env = dict(command.env)
    # 环境变量：键必须是合法 shell 标识符，不得与 supervisor 注入的代理变量
    # （大小写不敏感）冲突，不得使用 ENCLIO_/JIUWENBOX_ 保留前缀，值不得含 NUL 字节。
    validate_environment(env)
    # 每个参数必须是字符串且不含 NUL（NUL 会在 execve 传参时截断字符串）。
    if any(not isinstance(arg, str) or "\x00" in arg for arg in command.argv):
        raise ValueError("invalid_argv")
    # stdin：必须是字节串，且不超过部署配置的 max_input 上限。
    if not isinstance(command.stdin, bytes) or len(command.stdin) > max_input:
        raise ValueError("stdin_budget")
    # timeout_seconds：可选；设置时必须是有限的正数且不超过 2**31 秒。
    if command.timeout_seconds is not None:
        _bounded_seconds(command.timeout_seconds)
    # cwd：未指定时使用执行计划的默认工作目录，指定时必须为合法绝对路径。
    cwd = plan.cwd if command.cwd is None else absolute(command.cwd)
    request = {"argv": command.argv, "cwd": cwd, "env": env}
    # 单串预算：每个参数及 "key=value" 形式的环境变量按文件系统编码后须小于 128 KiB。
    strings = (*command.argv, *(f"{key}={value}" for key, value in env.items()))
    if any(len(os.fsencode(value)) >= 128 * 1024 for value in strings):
        raise ValueError("argument_budget")
    # 总量预算：整个请求 JSON 序列化后不得超过 MAX_HEADER_BYTES（1 MiB 的 IPC 头部上限）。
    if len(json.dumps(request).encode()) > MAX_HEADER_BYTES:
        raise ValueError("request_budget")
    return request
