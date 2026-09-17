# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Security policy data models (static only)."""

from __future__ import annotations

import enum
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


def _expand_path(value: str) -> str:
    """Expand shell-style path markers without requiring the path to exist."""
    return str(Path(os.path.expandvars(value)).expanduser())


def _contains_crlf_or_null(value: str) -> bool:
    """Check if string contains CRLF or null byte."""
    return "\r" in value or "\n" in value or "\x00" in value


def _contains_control_chars(value: str) -> bool:
    """Check if string contains control characters (excluding tab)."""
    for c in value:
        if ord(c) < 32 and c != "\t":
            return True
    return False


def _contains_path_traversal(value: str) -> bool:
    """Check if string contains path traversal sequence."""
    from urllib.parse import unquote
    decoded = unquote(value)
    return ".." in decoded or "/../" in decoded or decoded.endswith("/..")


def _normalize_octal_permissions(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        if 0 <= value <= 0o777:
            text = format(value, "o")
        else:
            text = str(value)
    else:
        text = str(value)
    if not text:
        raise ValueError(f"{label} cannot be empty")
    if not all(char in "01234567" for char in text):
        raise ValueError(f"{label} must be an octal value")
    if len(text) > 4:
        raise ValueError(f"{label} must be at most four octal digits")
    return text.zfill(4)


_MEMORY_UNIT_MULTIPLIERS: dict[str, int] = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024 ** 2,
    "MB": 1024 ** 2,
    "MIB": 1024 ** 2,
    "G": 1024 ** 3,
    "GB": 1024 ** 3,
    "GIB": 1024 ** 3,
    "T": 1024 ** 4,
    "TB": 1024 ** 4,
    "TIB": 1024 ** 4,
}

_CPU_DEFAULT_PERIOD_US = 100_000  # 100ms, matches cgroup v2 default
_CPU_MIN_PERIOD_US = 1_000
_CPU_MAX_PERIOD_US = 1_000_000
_CPU_MAX_CORES = 4096
_PIDS_MAX_VALUE = (1 << 31) - 1


def _parse_memory_max(value: object) -> int | None:
    """Parse user-facing ``memory_max`` literal into bytes.

    Accepts:
      - ``None`` -> no limit.
      - positive ``int`` -> raw bytes.
      - ``str`` of the form ``<int><unit?>`` where unit is one of
        ``B / K / KB / KiB / M / MB / MiB / G / GB / GiB / T / TB / TiB``
        (binary multipliers, case-insensitive). Decimal magnitudes
        (e.g. ``"1.5G"``) are explicitly rejected.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int``; reject it explicitly so
        # ``cgroup: {memory_max: true}`` never silently becomes 1 byte.
        raise ValueError(
            "memory_max must be an integer byte count or a '<N><K|M|G|T>' "
            "suffixed string, not a boolean"
        )
    if isinstance(value, int):
        bytes_value = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("memory_max cannot be an empty string")
        # Split numeric prefix from suffix.
        idx = 0
        while idx < len(text) and (text[idx].isdigit() or (idx == 0 and text[idx] == "+")):
            idx += 1
        number_part = text[:idx]
        suffix_part = text[idx:].strip().upper()
        if not number_part:
            raise ValueError(
                f"memory_max must start with a non-negative integer, got {value!r}"
            )
        try:
            magnitude = int(number_part)
        except ValueError as exc:
            raise ValueError(
                f"memory_max numeric prefix is not an integer: {value!r}"
            ) from exc
        multiplier = _MEMORY_UNIT_MULTIPLIERS.get(suffix_part)
        if multiplier is None:
            raise ValueError(
                f"memory_max has unknown unit suffix {suffix_part!r}; "
                "expected one of B/K/KB/KiB/M/MB/MiB/G/GB/GiB/T/TB/TiB"
            )
        bytes_value = magnitude * multiplier
    else:
        raise ValueError(
            f"memory_max must be int or str, got {type(value).__name__}"
        )
    if bytes_value <= 0:
        raise ValueError("memory_max must be > 0")
    return bytes_value


def _parse_cpu_max(value: object) -> tuple[int, int] | None:
    """Parse user-facing ``cpu_max`` literal into ``(quota_us, period_us)``.

    Accepts:
      - ``None`` or string ``"max"`` -> no limit.
      - ``int`` / ``float`` -> fractional cores (period fixed at
        ``_CPU_DEFAULT_PERIOD_US``, quota = ``round(cores * period)``).
      - ``str`` numeric (e.g. ``"0.5"``) -> same as float.
      - ``str`` ``"<quota> <period>"`` (two positive integers, microseconds)
        -> directly passed through.
      - ``tuple`` / ``list`` of exactly two positive ints -> passed through
        as ``(quota_us, period_us)``. This case exists so the value
        survives a round-trip through ``model_dump(mode="json")`` (which
        turns the stored tuple into a list) and back through
        ``SecurityPolicy.model_validate`` (e.g. when the sandbox policy is
        reloaded from disk by the supervisor).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(
            "cpu_max must be a number (fractional cores) or '<quota> <period>' "
            "string, not a boolean"
        )
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValueError(
                f"cpu_max sequence form must have exactly 2 elements "
                f"(quota_us, period_us), got {len(value)}"
            )
        quota_raw, period_raw = value
        if isinstance(quota_raw, bool) or isinstance(period_raw, bool):
            raise ValueError(
                "cpu_max (quota_us, period_us) must be ints, not bool"
            )
        if not (isinstance(quota_raw, int) and isinstance(period_raw, int)):
            raise ValueError(
                f"cpu_max (quota_us, period_us) must be ints, "
                f"got ({type(quota_raw).__name__}, {type(period_raw).__name__})"
            )
        quota = int(quota_raw)
        period = int(period_raw)
        if quota <= 0:
            raise ValueError("cpu_max quota must be > 0")
        if not (_CPU_MIN_PERIOD_US <= period <= _CPU_MAX_PERIOD_US):
            raise ValueError(
                f"cpu_max period must be in [{_CPU_MIN_PERIOD_US}, "
                f"{_CPU_MAX_PERIOD_US}] microseconds"
            )
        if quota > period * _CPU_MAX_CORES:
            raise ValueError(
                f"cpu_max quota {quota} implies more than {_CPU_MAX_CORES} cores"
            )
        return (quota, period)
    if isinstance(value, (int, float)):
        cores = float(value)
        if cores <= 0:
            raise ValueError("cpu_max must be > 0 cores")
        if cores > _CPU_MAX_CORES:
            raise ValueError(
                f"cpu_max cores {cores} exceeds sanity limit {_CPU_MAX_CORES}"
            )
        quota = max(1, int(round(cores * _CPU_DEFAULT_PERIOD_US)))
        return (quota, _CPU_DEFAULT_PERIOD_US)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("cpu_max cannot be an empty string")
        if text.lower() == "max":
            return None
        parts = text.split()
        if len(parts) == 2:
            try:
                quota = int(parts[0])
                period = int(parts[1])
            except ValueError as exc:
                raise ValueError(
                    f"cpu_max '<quota> <period>' must be two integers, got {value!r}"
                ) from exc
            if quota <= 0:
                raise ValueError("cpu_max quota must be > 0")
            if not (_CPU_MIN_PERIOD_US <= period <= _CPU_MAX_PERIOD_US):
                raise ValueError(
                    f"cpu_max period must be in [{_CPU_MIN_PERIOD_US}, "
                    f"{_CPU_MAX_PERIOD_US}] microseconds"
                )
            if quota > period * _CPU_MAX_CORES:
                raise ValueError(
                    f"cpu_max quota {quota} implies more than {_CPU_MAX_CORES} cores"
                )
            return (quota, period)
        if len(parts) == 1:
            try:
                cores = float(parts[0])
            except ValueError as exc:
                raise ValueError(
                    f"cpu_max string must parse as cores or '<quota> <period>': {value!r}"
                ) from exc
            return _parse_cpu_max(cores)
        raise ValueError(
            f"cpu_max string must be cores or '<quota> <period>', got {value!r}"
        )
    raise ValueError(
        f"cpu_max must be number or string, got {type(value).__name__}"
    )


def _parse_pids_max(value: object) -> int | None:
    """Parse ``pids_max`` literal into a positive int (or None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("pids_max must be a positive integer, not a boolean")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("pids_max cannot be an empty string")
        if text.lower() == "max":
            return None
        try:
            int_value = int(text)
        except ValueError as exc:
            raise ValueError(
                f"pids_max must be 'max' or a positive integer, got {value!r}"
            ) from exc
    elif isinstance(value, int):
        int_value = value
    else:
        raise ValueError(
            f"pids_max must be int or str, got {type(value).__name__}"
        )
    if int_value < 1:
        raise ValueError("pids_max must be >= 1")
    if int_value > _PIDS_MAX_VALUE:
        raise ValueError(
            f"pids_max must be <= {_PIDS_MAX_VALUE}"
        )
    return int_value


class BindMount(BaseModel):
    host_path: str
    sandbox_path: str
    mode: Literal["ro", "rw"] = "ro"

    @field_validator("host_path", mode="before")
    @classmethod
    def reject_wildcard_host_path(cls, value: object) -> object:
        # Historically some policy yamls used ``host_path: "*"`` as a
        # "mount everything under root" placeholder, but bwrap has no such
        # semantics and the entry was silently a no-op. We now reject this
        # explicitly so misconfigurations surface at policy load time and
        # point users at ``bind_root_entries`` which supports that intent.
        if isinstance(value, str) and value.strip() == "*":
            raise ValueError(
                "bind_mounts.host_path cannot be literal '*'; "
                "use filesystem_policy.bind_root_entries to mount all "
                "immediate entries under a host directory"
            )
        return value

    @field_validator("host_path", "sandbox_path", mode="before")
    @classmethod
    def expand_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return _expand_path(value)
        return value


class BindRootEntries(BaseModel):
    """Mount every immediate child entry of ``host_root`` into the sandbox.

    For each file or directory directly under ``host_root`` (no recursion past
    the first level), a bind mount is added at
    ``f"{sandbox_path}/{child_name}"`` with the configured ``mode``. Hidden
    entries (names starting with ``.``) are excluded by default; pass
    ``include_hidden: true`` to keep them. ``exclude`` accepts fnmatch globs
    applied against the child basename.
    """

    host_root: str
    sandbox_path: str
    mode: Literal["ro", "rw"] = "ro"
    include_hidden: bool = False
    exclude: list[str] = Field(default_factory=list)

    @field_validator("host_root", "sandbox_path", mode="before")
    @classmethod
    def expand_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return _expand_path(value)
        return value


class DeviceMount(BaseModel):
    host_path: str
    sandbox_path: str

    @field_validator("host_path", "sandbox_path", mode="before")
    @classmethod
    def expand_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return _expand_path(value)
        return value


class DirectoryMount(BaseModel):
    path: str
    permissions: str | int | None = None

    @field_validator("path", mode="before")
    @classmethod
    def expand_path(cls, value: object) -> object:
        if isinstance(value, str):
            return _expand_path(value)
        return value

    @field_validator("permissions", mode="before")
    @classmethod
    def permissions_must_be_octal(cls, value: object) -> str | None:
        return _normalize_octal_permissions(value, label="directory permissions")


class FileMount(BaseModel):
    path: str
    permissions: str | int | None = None

    @field_validator("path", mode="before")
    @classmethod
    def expand_path(cls, value: object) -> object:
        if isinstance(value, str):
            return _expand_path(value)
        return value

    @field_validator("permissions", mode="before")
    @classmethod
    def permissions_must_be_octal(cls, value: object) -> str | None:
        return _normalize_octal_permissions(value, label="file permissions")


class FilesystemPolicy(BaseModel):
    directories: list[str | DirectoryMount] = Field(default_factory=list)
    files: list[str | FileMount] = Field(default_factory=list)
    read_only: list[str] = Field(default_factory=list)
    read_write: list[str] = Field(default_factory=list)
    bind_mounts: list[BindMount] = Field(default_factory=list)
    bind_root_entries: list[BindRootEntries] = Field(default_factory=list)
    device: list[DeviceMount] = Field(default_factory=list)

    @field_validator("directories", mode="before")
    @classmethod
    def expand_directory_paths(cls, value: object) -> object:
        if isinstance(value, list):
            return [_expand_path(item) if isinstance(item, str) else item for item in value]
        return value

    @field_validator("files", mode="before")
    @classmethod
    def expand_file_paths(cls, value: object) -> object:
        if isinstance(value, list):
            return [_expand_path(item) if isinstance(item, str) else item for item in value]
        return value

    @field_validator("read_only", "read_write", mode="before")
    @classmethod
    def expand_path_lists(cls, value: object) -> object:
        if isinstance(value, list):
            return [_expand_path(item) if isinstance(item, str) else item for item in value]
        return value


class ProcessPolicy(BaseModel):
    run_as_user: str = "sandbox"
    run_as_group: str = "sandbox"


class NamespacePolicy(BaseModel):
    user: bool = True
    pid: bool = True
    ipc: bool = True
    cgroup: bool = True
    uts: bool = True


class CapabilityPolicy(BaseModel):
    add: list[str] = Field(default_factory=list)
    drop: list[str] = Field(default_factory=list)


class LandlockPolicy(BaseModel):
    compatibility: Literal["disabled", "best_effort", "hard_requirement"] = "best_effort"


class ArchitectureSyscallPolicy(BaseModel):
    blocked: list[str] = Field(default_factory=list)


class SyscallPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x86_64: ArchitectureSyscallPolicy = Field(default_factory=ArchitectureSyscallPolicy)
    arm64: ArchitectureSyscallPolicy = Field(default_factory=ArchitectureSyscallPolicy)


class NetworkMode(str, enum.Enum):
    ISOLATED = "isolated"
    HOST = "host"


class NetworkRulePolicy(BaseModel):
    default: Literal["deny", "allow"] = "deny"
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    allowed_ips: list[str] = Field(default_factory=list)
    blocked_ips: list[str] = Field(default_factory=list)
    allowed_ports: list[int] = Field(default_factory=list)
    blocked_ports: list[int] = Field(default_factory=list)


class ProxyBasicAuth(BaseModel):
    """HTTP Basic credentials for a proxy route.

    ``password`` is inline (dev/test only; like ``api_key`` it is stored in the
    policy YAML in plaintext). ``password_file`` is the production-recommended
    source: a server-side readable file (Kubernetes Secret / Docker Secret /
    Linux restricted file) whose content is read at route-assembly time. The
    resolved password lives only in the in-memory runtime route and is never
    serialized back to YAML or returned by any API.

    Fields are intentionally all optional with defaults and no raising
    validators live here: every structural check (non-empty username, exactly
    one password source, file existence/readability, CR/LF/NUL injection
    prevention) is performed at route-assembly time in
    :func:`build_proxy_route`, which raises ``ValueError`` with a generic
    message. This keeps the password out of Pydantic ``ValidationError`` reprs
    (which echo raw input) and therefore out of API 400 responses and startup
    logs.
    """

    username: str = ""
    password: str = ""
    password_file: str = ""

    @field_validator("password_file", mode="before")
    @classmethod
    def expand_password_file_path(cls, value: object) -> object:
        if isinstance(value, str) and value:
            return _expand_path(value)
        return value

    def __repr__(self) -> str:
        # Never expose the password in reprs / tracebacks.
        return (
            f"ProxyBasicAuth(username={self.username!r}, password=***, "
            f"password_file={self.password_file!r})"
        )


class ProxyRouteEntry(BaseModel):
    path_prefix: str  # Required - no default (must be single-level, non-root)
    target_endpoint: str = "https://api.openai.com"
    api_key: str = ""
    basic_auth: ProxyBasicAuth | None = None
    skip_cert_verify: bool = False

    @field_validator("path_prefix", mode="after")
    @classmethod
    def validate_path_prefix(cls, value: str) -> str:
        from urllib.parse import unquote
        
        if not value or not value.strip():
            raise ValueError("path_prefix cannot be empty")
        
        normalized = value.strip()
        
        if _contains_crlf_or_null(normalized):
            raise ValueError("path_prefix contains invalid characters")
        
        decoded = unquote(normalized)
        if _contains_crlf_or_null(decoded):
            raise ValueError("path_prefix contains invalid characters")
        
        if _contains_control_chars(normalized) or _contains_control_chars(decoded):
            raise ValueError("path_prefix contains invalid characters")
        
        if _contains_path_traversal(normalized):
            raise ValueError("path_prefix cannot contain path traversal")
        
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        
        # Check for root path BEFORE stripping trailing slash
        # "/" normalized becomes "" after rstrip, so check original normalized
        if normalized.rstrip("/") == "":
            raise ValueError(
                "path_prefix cannot be root path '/'. "
                "Root path would match all requests and make other routes unreachable. "
                "Use a specific prefix like '/api' or '/llm-proxy'."
            )
        
        normalized = normalized.rstrip("/")
        
        # Ban internal slashes (single-level only)
        stripped = normalized.lstrip("/")
        if "/" in stripped:
            raise ValueError(
                f"path_prefix must be single-level (no internal slashes). "
                f"Got '{value}' -> '{normalized}'. "
                f"Use '/api' not '/api/v1'. Each route handles one path level only."
            )
        
        return normalized

    @field_validator("api_key", mode="after")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        from urllib.parse import unquote
        
        if not value:
            return value
        
        if _contains_crlf_or_null(value):
            raise ValueError("api_key contains invalid characters")
        
        decoded = unquote(value)
        if _contains_crlf_or_null(decoded):
            raise ValueError("api_key contains invalid characters")
        
        if _contains_control_chars(value) or _contains_control_chars(decoded):
            raise ValueError("api_key contains invalid characters")
        
        return value

    @field_validator("target_endpoint", mode="after")
    @classmethod
    def validate_target_endpoint(cls, value: str) -> str:
        from urllib.parse import urlparse
        
        if not value or not value.strip():
            raise ValueError("target_endpoint cannot be empty")
        
        normalized = value.strip()
        
        if _contains_crlf_or_null(normalized):
            raise ValueError("target_endpoint contains invalid characters")
        
        try:
            parsed = urlparse(normalized)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("target_endpoint must be a valid URL")
            if parsed.scheme not in ("http", "https"):
                raise ValueError("target_endpoint must use http or https scheme")
        except Exception as e:
            raise ValueError(f"target_endpoint must be a valid URL: {e}") from e
        
        return normalized


class InferencePrivacyProxyPolicy(BaseModel):
    listen_port: int = 0
    listen_host: str | None = None
    routes: list[ProxyRouteEntry] = Field(default_factory=list)

    @field_validator("listen_port", mode="after")
    @classmethod
    def validate_listen_port(cls, value: int) -> int:
        if value < 0 or value > 65535:
            raise ValueError("listen_port must be between 0 and 65535")
        return value

    @field_validator("listen_host", mode="after")
    @classmethod
    def validate_listen_host(cls, value: str | None, info) -> str | None:
        import ipaddress
        listen_port = info.data.get("listen_port", 0)
        if listen_port <= 0:
            return value
        if not value or not value.strip():
            raise ValueError("listen_host required when listen_port > 0")
        try:
            ipaddress.ip_address(value.strip())
        except ValueError as e:
            raise ValueError(f"listen_host must be valid IP address: {value}") from e
        return value.strip()


class NetworkUplinkPolicy(BaseModel):
    subnet: str = ""
    nat: bool = True
    interface: str = ""

    @field_validator("subnet", mode="after")
    @classmethod
    def validate_subnet(cls, value: str) -> str:
        import ipaddress

        if not value.strip():
            return ""
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError(f"uplink.subnet must be a valid CIDR: {value}") from exc
        if network.version != 4:
            raise ValueError("uplink.subnet must be an IPv4 CIDR")
        if network.prefixlen > 24:
            raise ValueError("uplink.subnet prefix must be /24 or shorter")
        return str(network)


class NetworkPolicy(BaseModel):
    mode: NetworkMode = NetworkMode.ISOLATED
    uplink: NetworkUplinkPolicy = Field(default_factory=NetworkUplinkPolicy)
    egress: NetworkRulePolicy = Field(default_factory=NetworkRulePolicy)
    ingress: NetworkRulePolicy = Field(default_factory=NetworkRulePolicy)


class CgroupPolicy(BaseModel):
    """Per-sandbox cgroup resource limits.

    All three fields default to ``None`` which means *no limit applied*. If
    every field is ``None`` (also true when the user omits ``cgroup``
    entirely or passes ``cgroup: {}``), ``ProcessRuntime`` skips cgroup
    setup completely, so this field is safe to leave alone on hosts that
    don't expose a writable cgroup hierarchy.
    """

    model_config = ConfigDict(extra="forbid")

    memory_max: int | None = None
    cpu_max: tuple[int, int] | None = None
    pids_max: int | None = None

    @field_validator("memory_max", mode="before")
    @classmethod
    def normalize_memory_max(cls, value: object) -> int | None:
        return _parse_memory_max(value)

    @field_validator("cpu_max", mode="before")
    @classmethod
    def normalize_cpu_max(cls, value: object) -> tuple[int, int] | None:
        return _parse_cpu_max(value)

    @field_validator("pids_max", mode="before")
    @classmethod
    def normalize_pids_max(cls, value: object) -> int | None:
        return _parse_pids_max(value)

    def is_empty(self) -> bool:
        """Return True when no field is set, i.e. cgroup setup should be skipped."""
        return (
            self.memory_max is None
            and self.cpu_max is None
            and self.pids_max is None
        )


class TimeoutPolicy(BaseModel):
    """jiuwenbox 服务端的空闲沙箱淘汰配置.

    本字段仅在 server 启动时加载的根 policy (``SandboxManager.policy``) 上生效,
    用于驱动后台 reaper task; per-sandbox policy 上的同名字段不会影响沙箱隔离
    (不下传到 bwrap / landlock / cgroup), 仅用于配置回显。

    Fields:
        idle_timeout: 沙箱最大空闲时长 (秒). ``None`` 或 ``<= 0`` 表示禁用淘汰
            (默认禁用, 与未配置 timeout 字段时行为完全一致)。
        idle_check_interval: reaper 轮询间隔 (秒). 必须 ``> 0``。
    """

    model_config = ConfigDict(extra="forbid")

    idle_timeout: float | None = None
    idle_check_interval: float = 60.0

    @field_validator("idle_timeout", mode="before")
    @classmethod
    def normalize_idle_timeout(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("idle_timeout must be a number, not a boolean")
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                number = float(text)
            except ValueError as exc:
                raise ValueError(
                    f"idle_timeout must parse as a number of seconds, got {value!r}"
                ) from exc
        else:
            raise ValueError(
                f"idle_timeout must be number or string, got {type(value).__name__}"
            )
        if number <= 0:
            # Treat 0 / negative as "disabled" so users can flip the feature
            # off without removing the whole key from their YAML.
            return None
        return number

    @field_validator("idle_check_interval", mode="before")
    @classmethod
    def normalize_idle_check_interval(cls, value: object) -> float:
        if value is None:
            return 60.0
        if isinstance(value, bool):
            raise ValueError(
                "idle_check_interval must be a positive number, not a boolean"
            )
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return 60.0
            try:
                number = float(text)
            except ValueError as exc:
                raise ValueError(
                    f"idle_check_interval must parse as a number of seconds, "
                    f"got {value!r}"
                ) from exc
        else:
            raise ValueError(
                f"idle_check_interval must be number or string, "
                f"got {type(value).__name__}"
            )
        if number <= 0:
            raise ValueError("idle_check_interval must be > 0")
        return number


class SecurityPolicy(BaseModel):
    """Complete static security policy for a sandbox."""

    version: int = 1
    name: str = "default"
    environment: dict[str, str] = Field(default_factory=dict)
    filesystem_policy: FilesystemPolicy = Field(default_factory=FilesystemPolicy)
    process: ProcessPolicy = Field(default_factory=ProcessPolicy)
    namespace: NamespacePolicy = Field(default_factory=NamespacePolicy)
    capabilities: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    landlock: LandlockPolicy = Field(default_factory=LandlockPolicy)
    syscall: SyscallPolicy = Field(default_factory=SyscallPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    cgroup: CgroupPolicy = Field(default_factory=CgroupPolicy)
    timeout: TimeoutPolicy = Field(default_factory=TimeoutPolicy)
    inference_privacy_proxies: InferencePrivacyProxyPolicy = Field(default_factory=InferencePrivacyProxyPolicy)

    def tostring(self) -> str:
        """Serialize the policy to a YAML string."""
        return yaml.safe_dump(
            self.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        )

    def __str__(self) -> str:
        return self.tostring()
