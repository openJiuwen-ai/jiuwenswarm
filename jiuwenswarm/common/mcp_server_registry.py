# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""进程内 MCP Server 注册表：CRUD、工具缓存、周期扫描、全局 worker 池。

chat.send 的 ``mcp_server_list`` 只读这份缓存，不在对话路径上 ``list_tools``。
旧路径 ``office_claw_mcp`` / ``request_mcp_servers`` 不走本模块。
"""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from jiuwenswarm.common.mcp_config import (
    _MCP_CONNECTOR_DISCOVERY_TIMEOUT_S,
    _OFFICE_CLAW_MCP_ENV_KEYS,
    _PooledMcpWorker,
    _apply_tool_filter,
    _normalize_mcp_client_type,
    _run_mcp_worker,
    _validate_request_scoped_remote_mcp,
    create_mcp_tool,
    create_request_scoped_mcp_tool,
    list_request_mcp_server_tools,
    retire_pooled_mcp_worker,
    shutdown_pooled_mcp_worker,
    validate_office_claw_mcp_config,
)

logger = logging.getLogger(__name__)

# 周期扫描仅覆盖 remote HTTP MCP（§6.3 / D4）；playwright / openapi 不扫。
# 扫描超时固定 30s（方案 §6.3 / R2），与旧路径 invoke/发现默认 300s 分开。
_REMOTE_SCAN_CLIENT_TYPES = frozenset({"sse", "streamable-http"})
_MCP_SCAN_TIMEOUT_S = 30.0
_PER_REQUEST_ENV_MARKERS = (
    "OFFICE_CLAW_INVOCATION_ID",
    "OFFICE_CLAW_CALLBACK_TOKEN",
)
_REDACT_KEYS = frozenset(
    {
        "authorization",
        "token",
        "secret",
        "password",
        "api_key",
        "apikey",
        "auth",
        "signature",
    }
)


# 注册项里不属于连接配置的字段：计入指纹与 list 回显，但不交给准入和工具发现。
_REGISTRATION_ONLY_KEYS = frozenset(
    {"runtime_params", "scan_params", "connection_scope", "system", "version"}
)
_RUNTIME_PARAM_VIAS = ("meta", "header", "env")
_RUNTIME_PARAM_TRANSPORTS = {
    "meta": frozenset({"stdio"}),
    "env": frozenset({"stdio"}),
    "header": _REMOTE_SCAN_CLIENT_TYPES,
}
_CONNECTION_SCOPES = ("request",)
# 能力随实现逐项放开：未放开的声明在注册时拒绝，list 的 capabilities 只报已放开的部分。
_SUPPORTED_RUNTIME_PARAM_VIAS: frozenset[str] = frozenset({"meta", "header", "env"})
_SUPPORTED_CONNECTION_SCOPES: frozenset[str] = frozenset({"request"})
_SCAN_PARAMS_SUPPORTED = True
_TOOL_SCOPE_SUPPORTED = True
_RESCAN_SUPPORTED = True
_MAX_RUNTIME_PARAMS = 16
_MAX_RUNTIME_PARAM_NAME_LEN = 128
MAX_RUNTIME_PARAM_VALUE_LEN = 32768
MAX_RUNTIME_PARAMS_TOTAL_LEN = 65536
_MAX_VERSION_LEN = 256
# 由旧通道 office_claw_mcp 迁来的条目沿用启动身份校验，只多放行共享进程标记。
_OFFICE_CLAW_ENTRY_NAMES = frozenset({"office-claw", "office-claw-single-agent"})
_OFFICE_CLAW_ENTRY_EXTRA_ENV_KEYS = frozenset({"OFFICE_CLAW_MCP_SHARED"})

CODE_INVALID_RUNTIME_PARAMS = "INVALID_RUNTIME_PARAMS"
CODE_SYSTEM_ENTRY_OWNER_CONFLICT = "SYSTEM_ENTRY_OWNER_CONFLICT"
CODE_SYSTEM_ENTRY_IDENTITY_MISMATCH = "SYSTEM_ENTRY_IDENTITY_MISMATCH"
CODE_SCAN_FAILED = "SCAN_FAILED"


class McpRegistrationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class McpRegistryChatError(ValueError):
    """chat.send 新路径可预期失败（未知 server / 字段非法）。"""


class UnknownMcpServerError(McpRegistryChatError):
    def __init__(self, names: str | list[str]) -> None:
        if isinstance(names, str):
            names = [names]
        cleaned = [str(n).strip() for n in names if str(n).strip()]
        self.names = cleaned
        super().__init__("unknown mcp server: " + ", ".join(cleaned))


class DisabledMcpServerError(McpRegistryChatError):
    def __init__(self, names: str | list[str]) -> None:
        if isinstance(names, str):
            names = [names]
        cleaned = [str(n).strip() for n in names if str(n).strip()]
        self.names = cleaned
        self.name = cleaned[0] if cleaned else ""
        super().__init__("mcp server disabled: " + ", ".join(cleaned))


@dataclass(frozen=True)
class McpRegistrySettings:
    scan_interval_s: float = 600.0
    scan_concurrency: int = 3
    worker_idle_ttl_s: float = 600.0
    scan_fail_threshold: int = 3
    worker_group_size: int = 2


@dataclass(frozen=True)
class McpRuntimeParam:
    name: str
    via: str
    required: bool = True
    group_key: bool = False


@dataclass(frozen=True)
class McpServerDeclaration:
    runtime_params: tuple[McpRuntimeParam, ...] = ()
    connection_scope: str = ""
    system: bool = False


@dataclass
class McpServerEntry:
    name: str
    config: dict[str, Any]
    enabled: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0
    declaration: McpServerDeclaration = field(default_factory=McpServerDeclaration)
    owner_id: str = ""


@dataclass
class CachedServerRecord:
    name: str
    tools: list[dict[str, Any]]
    version: int
    last_scan_at: float
    last_scan_ok: bool
    last_error: str = ""
    connect_params: dict[str, Any] = field(default_factory=dict)
    scan_fail_count: int = 0


def get_mcp_registry_settings() -> McpRegistrySettings:
    yaml_vals: dict[str, Any] = {}
    try:
        from jiuwenswarm.common.config import get_config

        cfg = get_config() or {}
        mcp = cfg.get("mcp") if isinstance(cfg, dict) else None
        if isinstance(mcp, dict) and isinstance(mcp.get("registry"), dict):
            yaml_vals = dict(mcp["registry"])
    except Exception:
        yaml_vals = {}

    def _num(env_name: str, yaml_key: str, default: float, *, as_int: bool = False) -> float:
        raw = str(os.environ.get(env_name, "") or "").strip()
        if raw:
            try:
                value = int(raw) if as_int else float(raw)
                return float(value)
            except ValueError:
                pass
        yval = yaml_vals.get(yaml_key)
        if isinstance(yval, (int, float)):
            return float(int(yval) if as_int else yval)
        return default

    interval = max(1.0, _num("MCP_REGISTRY_SCAN_INTERVAL_S", "scan_interval_s", 600.0))
    concurrency = max(1, int(_num("MCP_REGISTRY_SCAN_CONCURRENCY", "scan_concurrency", 3.0, as_int=True)))
    ttl = max(1.0, _num("MCP_REGISTRY_WORKER_IDLE_TTL_S", "worker_idle_ttl_s", 600.0))
    fail_n = max(1, int(_num("MCP_REGISTRY_SCAN_FAIL_THRESHOLD", "scan_fail_threshold", 3.0, as_int=True)))
    group_size = max(1, int(_num("MCP_REGISTRY_WORKER_GROUP_SIZE", "worker_group_size", 2.0, as_int=True)))
    return McpRegistrySettings(
        worker_group_size=group_size,
        scan_interval_s=interval,
        scan_concurrency=concurrency,
        worker_idle_ttl_s=ttl,
        scan_fail_threshold=fail_n,
    )


def config_fingerprint(config: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(config or {}), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _changed_config_keys(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    old_map = dict(old or {})
    new_map = dict(new or {})
    keys = set(old_map) | set(new_map)
    return sorted(
        str(k)
        for k in keys
        if old_map.get(k) != new_map.get(k)
    )


def cached_tools_equal(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    """工具 schema 是否等价：按 name 排序后 deep-equal，忽略 list_tools 返回顺序。"""

    def _canon(tools: list[dict[str, Any]]) -> list[str]:
        ordered = sorted(tools, key=lambda item: str(item.get("name") or ""))
        return [
            json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
            for item in ordered
        ]

    return _canon(left) == _canon(right)


def is_remote_mcp_config(config: Mapping[str, Any]) -> bool:
    """True when this server participates in periodic remote scans."""

    client_type = _normalize_mcp_client_type(config.get("type"))
    return client_type in _REMOTE_SCAN_CLIENT_TYPES


def _admit_server_config(name: str, config: Mapping[str, Any]) -> None:
    """CRUD 准入：create_mcp_tool 白名单/危险参数；sse/http 再做 SSRF 主机屏蔽。

    个人版可通过本路径登记本地 stdio；企业版在 create_mcp_tool 内拒绝 stdio
    （handler 层也会整组拦截 mcp.server.*）。
    """

    transport = _transport_config(config)
    create_mcp_tool(json.dumps({**transport, "name": name}, ensure_ascii=False))
    if is_remote_mcp_config(transport):
        _validate_request_scoped_remote_mcp(name, transport)
    if name in _OFFICE_CLAW_ENTRY_NAMES:
        try:
            validate_office_claw_mcp_config(
                transport, extra_env_keys=_OFFICE_CLAW_ENTRY_EXTRA_ENV_KEYS
            )
        except ValueError as exc:
            raise McpRegistrationError(CODE_SYSTEM_ENTRY_IDENTITY_MISMATCH, str(exc)) from exc


def _transport_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in dict(config or {}).items() if k not in _REGISTRATION_ONLY_KEYS}


def _invalid_declaration(message: str) -> McpRegistrationError:
    return McpRegistrationError(CODE_INVALID_RUNTIME_PARAMS, message)


def _parse_runtime_params(raw: Any, client_type: str) -> tuple[McpRuntimeParam, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _invalid_declaration("runtime_params must be a list")
    if len(raw) > _MAX_RUNTIME_PARAMS:
        raise _invalid_declaration(f"runtime_params exceeds {_MAX_RUNTIME_PARAMS} entries")
    params: list[McpRuntimeParam] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise _invalid_declaration("runtime_params entry must be an object")
        name = item.get("name")
        via = item.get("via")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise _invalid_declaration("runtime_params name must be a non-empty trimmed string")
        if len(name) > _MAX_RUNTIME_PARAM_NAME_LEN or any(c in name for c in "\r\n\0"):
            raise _invalid_declaration(f"runtime_params name is invalid: {name[:32]!r}")
        if via not in _RUNTIME_PARAM_VIAS:
            raise _invalid_declaration(f"runtime_params via is invalid: {name}")
        if via not in _SUPPORTED_RUNTIME_PARAM_VIAS:
            raise _invalid_declaration(f"runtime_params via '{via}' is not supported")
        if client_type not in _RUNTIME_PARAM_TRANSPORTS[via]:
            raise _invalid_declaration(f"runtime_params via '{via}' does not fit type '{client_type}'")
        required = item.get("required", True)
        group_key = item.get("group_key", False)
        if not isinstance(required, bool) or not isinstance(group_key, bool):
            raise _invalid_declaration(f"runtime_params required/group_key must be boolean: {name}")
        if group_key and (via != "meta" or not required):
            raise _invalid_declaration(f"runtime_params group_key needs a required meta param: {name}")
        # HTTP 头名不区分大小写，按小写判重。
        key = name.lower() if via == "header" else name
        if key in seen:
            raise _invalid_declaration(f"runtime_params name is duplicated: {name}")
        seen.add(key)
        params.append(McpRuntimeParam(name=name, via=via, required=required, group_key=group_key))
    if len({p.via for p in params}) > 1:
        raise _invalid_declaration("runtime_params must use a single via")
    if sum(1 for p in params if p.group_key) > 1:
        raise _invalid_declaration("runtime_params allows at most one group_key")
    return tuple(params)


def _valid_runtime_value(value: Any, via: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > MAX_RUNTIME_PARAM_VALUE_LEN:
        return False
    # 请求头的值带换行会被拼成额外的头；进程环境变量的值不能含 NUL。
    return not any(c in value for c in ("\r\n\0" if via == "header" else "\0"))


def _parse_scan_params(raw: Any, params: tuple[McpRuntimeParam, ...]) -> dict[str, str]:
    if raw is None:
        return {}
    if not _SCAN_PARAMS_SUPPORTED:
        raise _invalid_declaration("scan_params is not supported")
    if not isinstance(raw, Mapping):
        raise _invalid_declaration("scan_params must be an object")
    # tools/list 不带调用元数据，meta 方式的参数无处可放。
    declared = {p.name: p.via for p in params if p.via != "meta"}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key not in declared:
            raise _invalid_declaration(f"scan_params name is not declared: {str(key)[:32]!r}")
        if not _valid_runtime_value(value, declared[key]):
            raise _invalid_declaration(f"scan_params value is invalid: {key}")
        out[key] = value
    if sum(len(v) for v in out.values()) > MAX_RUNTIME_PARAMS_TOTAL_LEN:
        raise _invalid_declaration("scan_params exceeds the total length limit")
    return out


def _parse_registration(
    name: str, item: Mapping[str, Any]
) -> tuple[dict[str, Any], McpServerDeclaration, dict[str, str]]:
    """拆出入库配置、声明与一次性扫描参数；扫描参数不入库、不计指纹。"""

    config = {k: copy.deepcopy(v) for k, v in item.items() if k not in {"name", "scan_params"}}
    client_type = _normalize_mcp_client_type(config.get("type")) or "stdio"
    params = _parse_runtime_params(config.get("runtime_params"), client_type)
    if name in _OFFICE_CLAW_ENTRY_NAMES:
        # 执行时 env 参数同样进子进程环境，须受启动身份的 env 白名单约束，与旧通道口径一致。
        allowed_env = _OFFICE_CLAW_MCP_ENV_KEYS | _OFFICE_CLAW_ENTRY_EXTRA_ENV_KEYS
        blocked = sorted(p.name for p in params if p.via == "env" and p.name not in allowed_env)
        if blocked:
            raise _invalid_declaration(f"runtime_params env name is not allowed for {name}: {blocked[0]}")
    scan_params = _parse_scan_params(item.get("scan_params"), params)
    scope = config.get("connection_scope")
    if scope is not None:
        if scope not in _CONNECTION_SCOPES or scope not in _SUPPORTED_CONNECTION_SCOPES:
            raise _invalid_declaration("connection_scope is not supported")
        if not any(p.via == "header" for p in params):
            raise _invalid_declaration("connection_scope needs header runtime_params")
    system = config.get("system", False)
    if not isinstance(system, bool):
        raise _invalid_declaration("system must be boolean")
    # 工具标识按点号分段，系统条目名含点会被拆错。
    if system and "." in name:
        raise _invalid_declaration("system entry name must not contain '.'")
    version = config.get("version")
    if version is not None and (not isinstance(version, str) or len(version) > _MAX_VERSION_LEN):
        raise _invalid_declaration("version must be a short string")
    declaration = McpServerDeclaration(
        runtime_params=params, connection_scope=str(scope or ""), system=system
    )
    return config, declaration, scan_params


def _failure(name: str, exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "ok": False, "error": str(exc)}
    code = getattr(exc, "code", "")
    if code:
        result["code"] = code
    return result


def _owner_conflict(entry: McpServerEntry, owner_id: str) -> bool:
    return entry.declaration.system and bool(entry.owner_id) and entry.owner_id != owner_id


def mcp_registry_capabilities() -> dict[str, Any]:
    return {
        "runtime_params": [v for v in _RUNTIME_PARAM_VIAS if v in _SUPPORTED_RUNTIME_PARAM_VIAS],
        "scan_params": _SCAN_PARAMS_SUPPORTED,
        "tool_scope": _TOOL_SCOPE_SUPPORTED,
        "connection_scope": [s for s in _CONNECTION_SCOPES if s in _SUPPORTED_CONNECTION_SCOPES],
        "rescan": _RESCAN_SUPPORTED,
        "system_entries": True,
    }


def is_office_claw_entry_name(name: str) -> bool:
    return name in _OFFICE_CLAW_ENTRY_NAMES


def resolve_runtime_params(
    declaration: McpServerDeclaration, raw: Any
) -> tuple[dict[str, str], str, list[str]]:
    """按声明筛出本轮参数，返回（参数值, 跳过原因, 被丢弃的未声明参数名）。

    跳过原因非空时该服务本轮不装载；原因与日志只含参数名，不含值。
    """

    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        return {}, "runtime params must be an object", []
    # 一个注册项只有一种传入方式；HTTP 头名不区分大小写。
    fold = any(p.via == "header" for p in declaration.runtime_params)
    declared = {(p.name.lower() if fold else p.name): p for p in declaration.runtime_params}
    values: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in raw.items():
        param = declared.get(str(key).lower() if fold else str(key))
        if param is None:
            dropped.append(str(key)[:_MAX_RUNTIME_PARAM_NAME_LEN])
            continue
        if not _valid_runtime_value(value, param.via):
            return {}, f"invalid value for runtime param: {param.name}", dropped
        values[param.name] = value
    if sum(len(v) for v in values.values()) > MAX_RUNTIME_PARAMS_TOTAL_LEN:
        return {}, "runtime params exceed the total length limit", dropped
    missing = [p.name for p in declaration.runtime_params if p.required and p.name not in values]
    if missing:
        return {}, "missing required runtime params: " + ", ".join(missing), dropped
    return values, "", dropped


def runtime_delivery(
    declaration: McpServerDeclaration, values: Mapping[str, str], request_key: str = ""
) -> tuple[str, str, bool]:
    """返回（传入方式, worker 池键后缀, 是否随请求关闭）；键只用参数值的摘要，不留原值。

    meta：后缀是分组键摘要，进程跨请求共享。header / env：后缀是全部参数值的指纹，
    值相同才共用连接或进程；声明了 ``connection_scope: request`` 时再混入请求标识，
    连接归本请求独占、请求结束即关。
    """

    if not declaration.runtime_params:
        return "", "", False
    via = declaration.runtime_params[0].via
    if via == "meta":
        group = next((p.name for p in declaration.runtime_params if p.group_key), "")
        digest = hashlib.sha256(values[group].encode("utf-8")).hexdigest()[:16] if group else ""
        return via, digest, False
    request_owned = declaration.connection_scope == "request"
    payload = json.dumps(
        [request_key if request_owned else "", sorted(values.items())], ensure_ascii=False
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return via, ("r" if request_owned else "v") + digest, request_owned


def apply_runtime_params(
    params: Mapping[str, Any], via: str, values: Mapping[str, str] | None
) -> dict[str, Any]:
    """把 header / env 方式的参数值并入连接参数的副本；同名时以执行时参数为准。"""

    merged = dict(params)
    if not values or via not in ("header", "env"):
        return merged
    if via == "env":
        env = params.get("env")
        merged["env"] = {**(dict(env) if isinstance(env, Mapping) else {}), **values}
        return merged
    names = {name.lower() for name in values}
    static = params.get("auth_headers")
    kept = {
        k: v for k, v in (static.items() if isinstance(static, Mapping) else ())
        if str(k).lower() not in names
    }
    merged["auth_headers"] = {**kept, **values}
    return merged


def _strip_scan_params(
    params: dict[str, Any], static: Mapping[str, Any], via: str, scan_params: Mapping[str, str]
) -> dict[str, Any]:
    """扫描用过的参数值不能留在工具缓存的连接参数里：去掉它们，同名的固定配置值放回。"""

    field_name = "auth_headers" if via == "header" else "env"
    current = params.get(field_name)
    if not isinstance(current, Mapping):
        return params
    names = {(n.lower() if via == "header" else n) for n in scan_params}

    def used(key: Any) -> bool:
        return (str(key).lower() if via == "header" else str(key)) in names

    cleaned = {k: v for k, v in current.items() if not used(k)}
    base = static.get(field_name)
    if isinstance(base, Mapping):
        cleaned.update({k: v for k, v in base.items() if used(k)})
    return {**params, field_name: cleaned}


def deepresearch_connect_params(
    params: Mapping[str, Any],
    declaration: McpServerDeclaration,
    values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """委托判定用的连接参数：声明的请求头全部计入，本轮没有值的用占位。

    判定按请求头名字识别来源，值只在装载当轮参与检查；复核时没有值，名字集合必须一致。
    """

    names = [p.name for p in declaration.runtime_params if p.via == "header"]
    if not names:
        return dict(params)
    provided = values or {}
    return apply_runtime_params(params, "header", {n: provided.get(n, "***") for n in names})


def _secret_name(name: str) -> bool:
    key_l = str(name).lower()
    if key_l in {"key", "access_key"} or key_l.endswith("_key"):
        return True
    return any(token in key_l for token in _REDACT_KEYS)


def _redact_url(url: str) -> str:
    """URL query 里 token/api_key/signature 等参数值打码（§10.2 延伸）。"""

    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    redacted: list[tuple[str, str]] = []
    changed = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _secret_name(key):
            redacted.append((key, "***"))
            changed = True
        else:
            redacted.append((key, value))
    if not changed:
        return url
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(redacted, quote_via=_quote_keep_star),
            parts.fragment,
        )
    )


def _quote_keep_star(value: str, safe: str = "", encoding=None, errors=None) -> str:
    return quote(value, safe=f"{safe}*", encoding=encoding, errors=errors)


def redact_mcp_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """list 接口脱敏：auth_* / 敏感 env / URL query 只回 key 不回 value。"""

    out: dict[str, Any] = {}
    for key, value in dict(config or {}).items():
        key_l = str(key).lower()
        if key_l in {"auth_headers", "auth_query_params"} and isinstance(value, dict):
            out[key] = {str(k): "***" for k in value}
            continue
        if key_l == "env" and isinstance(value, dict):
            redacted_env: dict[str, Any] = {}
            for env_k, env_v in value.items():
                if _secret_name(str(env_k)):
                    redacted_env[str(env_k)] = "***"
                else:
                    redacted_env[str(env_k)] = env_v
            out[key] = redacted_env
            continue
        if key_l == "url" and isinstance(value, str):
            out[key] = _redact_url(value)
            continue
        if _secret_name(key):
            out[key] = "***"
            continue
        out[key] = value
    return out


def _discover_timeout_s(config: Mapping[str, Any]) -> float:
    """周期/准入 remote 扫描 30s（§6.3 / R2）；stdio add/update 对齐旧路径 300s 下限。"""

    if is_remote_mcp_config(config):
        return _MCP_SCAN_TIMEOUT_S
    timeout = float(_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S)
    timeout_raw = config.get("timeout_s")
    if timeout_raw is None:
        nested = config.get("params")
        if isinstance(nested, Mapping):
            timeout_raw = nested.get("timeout_s")
    if (
        isinstance(timeout_raw, (int, float))
        and not isinstance(timeout_raw, bool)
        and float(timeout_raw) > timeout
    ):
        return float(timeout_raw)
    return timeout


def _worker_pool_key(server_name: str, params: Mapping[str, Any]) -> str:
    env = params.get("env")
    if not isinstance(env, Mapping):
        return server_name
    if not any(marker in env for marker in _PER_REQUEST_ENV_MARKERS):
        return server_name
    env_fp = hashlib.sha256(
        json.dumps({str(k): str(v) for k, v in env.items()}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"{server_name}#{env_fp}"


def _worker_matches_fingerprint(worker: _PooledMcpWorker | None, wanted_fp: str) -> bool:
    if worker is None or not worker.alive:
        return False
    return worker.params_fingerprint == wanted_fp


class GlobalMcpWorkerPool:
    """按 server_name（可附 env 指纹）跨请求复用 ``_PooledMcpWorker``。"""

    def __init__(self) -> None:
        self._workers: dict[str, _PooledMcpWorker] = {}
        self._lock = asyncio.Lock()
        self.on_worker_event: Callable[[str, _PooledMcpWorker, str], None] | None = None

    def _start(
        self, server_name: str, params: Mapping[str, Any], fingerprint: str, *, rebuilt: bool
    ) -> _PooledMcpWorker:
        worker = _PooledMcpWorker(server_name, fingerprint)
        notify = self.on_worker_event
        if notify is not None:
            worker.on_tools_changed = functools.partial(notify, server_name, worker, "list_changed")
        worker.task = asyncio.create_task(_run_mcp_worker(params, worker))
        worker.last_used = time.monotonic()
        if rebuilt and notify is not None:
            notify(server_name, worker, "rebuilt")
        return worker

    def active_worker(self, server_name: str) -> _PooledMcpWorker | None:
        prefix = f"{server_name}#"
        for key, worker in self._workers.items():
            if key != server_name and not key.startswith(prefix):
                continue
            if worker.alive and not worker.retiring:
                return worker
        return None

    async def acquire(
        self,
        server_name: str,
        params: Mapping[str, Any],
        *,
        force_rebuild: bool = False,
        variant: str = "",
    ) -> _PooledMcpWorker:
        key = f"{server_name}#{variant}" if variant else _worker_pool_key(server_name, params)
        copied = dict(params)
        env = copied.get("env")
        if isinstance(env, Mapping):
            copied["env"] = dict(env)
        wanted_fp = config_fingerprint(copied)
        stale = await self._evict_locked(key, wanted_fp, force_rebuild=force_rebuild)
        if stale is not None:
            await retire_pooled_mcp_worker(stale, reason="fingerprint_changed")
        extra = None
        async with self._lock:
            worker = self._workers.get(key)
            if worker is not None and _worker_matches_fingerprint(worker, wanted_fp):
                worker.last_used = time.monotonic()
                return worker
            if worker is not None and worker.alive:
                extra = self._workers.pop(key, None)
            worker = self._start(server_name, copied, wanted_fp, rebuilt=force_rebuild)
            self._workers[key] = worker
        if extra is not None:
            await retire_pooled_mcp_worker(extra, reason="worker_replaced")
        return worker

    async def _evict_locked(
        self,
        key: str,
        wanted_fp: str,
        *,
        force_rebuild: bool,
    ) -> _PooledMcpWorker | None:
        async with self._lock:
            worker = self._workers.get(key)
            if worker is None:
                return None
            if (not force_rebuild) and _worker_matches_fingerprint(worker, wanted_fp):
                return None
            if force_rebuild or worker.alive:
                return self._workers.pop(key, None)
            return None

    async def acquire_grouped(
        self,
        server_name: str,
        params: Mapping[str, Any],
        group: str,
        max_workers: int,
        *,
        replace: _PooledMcpWorker | None = None,
    ) -> _PooledMcpWorker:
        """分组池：同组最多 ``max_workers`` 个串行 worker，空闲优先，都忙时排到最闲的那个。

        返回的 worker 已替调用方占了一个 inflight 名额（选中与占位之间没有 await，
        并发调用不会挤到同一个空闲 worker 上），调用方必须用 ``reserved=True`` 调用，
        或在放弃时自行归还。``replace`` 是刚失败、需要换掉的 worker。
        """

        prefix = f"{server_name}#g{group}#"
        copied = dict(params)
        env = copied.get("env")
        if isinstance(env, Mapping):
            copied["env"] = dict(env)
        wanted_fp = config_fingerprint(copied)
        stale: list[_PooledMcpWorker] = []
        async with self._lock:
            slots = {k: w for k, w in self._workers.items() if k.startswith(prefix)}
            for key, worker in slots.items():
                if worker is replace or not _worker_matches_fingerprint(worker, wanted_fp):
                    self._workers.pop(key, None)
                    if worker.alive:
                        stale.append(worker)
            live = [w for k, w in slots.items() if self._workers.get(k) is w]
            chosen = next((w for w in live if not w.busy), None)
            if chosen is None and len(live) < max(1, max_workers):
                index = next(i for i in range(len(live) + 1) if f"{prefix}{i}" not in self._workers)
                chosen = self._start(server_name, copied, wanted_fp, rebuilt=replace is not None)
                self._workers[f"{prefix}{index}"] = chosen
            if chosen is None:
                chosen = min(live, key=lambda w: w.inflight)
            chosen.inflight += 1
            chosen.last_used = time.monotonic()
        try:
            for worker in stale:
                reason = "worker_replaced" if worker is replace else "fingerprint_changed"
                await retire_pooled_mcp_worker(worker, reason=reason)
        except BaseException:
            # 占位没交到调用方手里就必须归还，否则这个 worker 永远不算空闲、不会被回收。
            chosen.inflight -= 1
            raise
        return chosen

    async def retire_variant(self, server_name: str, variant: str, *, reason: str = "") -> None:
        """只退役一个参数指纹对应的 worker（随请求关闭的连接用）。"""

        async with self._lock:
            worker = self._workers.pop(f"{server_name}#{variant}", None)
        if worker is not None:
            await retire_pooled_mcp_worker(worker, reason=reason)

    async def close_server(self, server_name: str) -> None:
        name = str(server_name or "").strip()
        async with self._lock:
            keys = [k for k in self._workers if k == name or k.startswith(f"{name}#")]
            workers = [self._workers.pop(k) for k in keys]
        for worker in workers:
            await shutdown_pooled_mcp_worker(worker)

    async def retire_server(self, server_name: str, *, reason: str = "") -> None:
        """Detach workers so new calls rebuild, letting in-flight calls finish."""

        name = str(server_name or "").strip()
        async with self._lock:
            keys = [k for k in self._workers if k == name or k.startswith(f"{name}#")]
            workers = [self._workers.pop(k) for k in keys]
        for worker in workers:
            await retire_pooled_mcp_worker(worker, reason=reason)

    async def reap_idle(self, ttl_s: float) -> None:
        cutoff = time.monotonic() - max(1.0, float(ttl_s))
        async with self._lock:
            idle_keys = [
                key
                for key, worker in self._workers.items()
                if getattr(worker, "last_used", 0.0) <= cutoff and not worker.busy
            ]
            workers = [self._workers.pop(k) for k in idle_keys]
        for worker in workers:
            await shutdown_pooled_mcp_worker(worker, reason="idle_ttl")

    async def close_all(self) -> None:
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            await shutdown_pooled_mcp_worker(worker)


class McpServerRegistry:
    """进程内存注册表。registry 权威，tool_cache 为扫描产物。"""

    def __init__(self, settings: McpRegistrySettings | None = None) -> None:
        self.settings = settings or get_mcp_registry_settings()
        self._lock = asyncio.Lock()
        self._registry: dict[str, McpServerEntry] = {}
        self._cache: dict[str, CachedServerRecord] = {}
        self.worker_pool = GlobalMcpWorkerPool()
        self.worker_pool.on_worker_event = self._schedule_worker_rescan
        self._rescans: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._worker_rescans: dict[str, asyncio.Task[None]] = {}
        self._worker_rescan_again: dict[str, tuple[_PooledMcpWorker, str]] = {}
        self._scanner_task: asyncio.Task[None] | None = None
        self._scanner_stop = asyncio.Event()

    async def add_servers(
        self, servers: list[Mapping[str, Any]], *, owner_id: str = ""
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any] | None] = [None] * len(servers)
        to_scan: list[tuple[int, str, dict[str, Any], McpServerDeclaration, dict[str, str]]] = []
        pending: set[str] = set()
        async with self._lock:
            existing = set(self._registry)
        for index, item in enumerate(servers):
            if not isinstance(item, Mapping):
                results[index] = {"name": "", "ok": False, "error": "server entry must be an object"}
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                results[index] = {"name": "", "ok": False, "error": "name is required"}
                continue
            try:
                config, declaration, scan_params = _parse_registration(name, item)
                _admit_server_config(name, config)
            except ValueError as exc:
                results[index] = _failure(name, exc)
                continue
            if name in existing or name in pending:
                results[index] = {"name": name, "ok": False, "error": "already exists"}
                continue
            pending.add(name)
            to_scan.append((index, name, config, declaration, scan_params))

        sem = asyncio.Semaphore(self.settings.scan_concurrency)

        async def _scan_and_commit(
            name: str,
            config: dict[str, Any],
            declaration: McpServerDeclaration,
            scan_params: dict[str, str],
        ) -> dict[str, Any]:
            async with sem:
                tools, params, error = await self._discover(name, config, scan_params)
                if error:
                    return {"name": name, "ok": False, "error": error, "code": CODE_SCAN_FAILED}
                now = time.monotonic()
                entry = McpServerEntry(
                    name=name,
                    config=copy.deepcopy(config),
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                    declaration=declaration,
                    owner_id=owner_id if declaration.system else "",
                )
                record = CachedServerRecord(
                    name=name,
                    tools=copy.deepcopy(tools),
                    version=1,
                    last_scan_at=now,
                    last_scan_ok=True,
                    last_error="",
                    connect_params=copy.deepcopy(params),
                )
                async with self._lock:
                    if name in self._registry:
                        return {"name": name, "ok": False, "error": "already exists"}
                    self._registry[name] = entry
                    self._cache[name] = record
                tool_names = [str(t.get("name") or "") for t in tools]
                return {
                    "name": name,
                    "ok": True,
                    "tools": tool_names,
                    "tools_count": len(tool_names),
                }

        if to_scan:
            scanned = await asyncio.gather(
                *[_scan_and_commit(n, c, d, s) for _, n, c, d, s in to_scan],
                return_exceptions=True,
            )
            for (index, name, *_), item in zip(to_scan, scanned):
                if isinstance(item, dict):
                    results[index] = item
                else:
                    logger.warning(
                        "[McpServerRegistry] add_servers failed: name=%s error=%s",
                        name,
                        item,
                    )
                    results[index] = {"name": name, "ok": False, "error": str(item)}
        return [
            item if item is not None else {"name": "", "ok": False, "error": "internal error"}
            for item in results
        ]

    async def remove_servers(
        self, names: list[str], *, owner_id: str = ""
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for raw in names:
            name = str(raw or "").strip()
            if not name:
                results.append({"name": "", "ok": False, "error": "name is required"})
                continue
            async with self._lock:
                current = self._registry.get(name)
                conflict = current is not None and _owner_conflict(current, owner_id)
                existed = current is not None
                if existed and not conflict:
                    self._registry.pop(name, None)
                    self._cache.pop(name, None)
            if conflict:
                results.append(_failure(name, self._owner_conflict_error(name)))
                continue
            if not existed:
                results.append({"name": name, "ok": False, "error": "not found"})
                continue
            await self.worker_pool.retire_server(name, reason="server_removed")
            results.append({"name": name, "ok": True})
        return results

    @staticmethod
    def _owner_conflict_error(name: str) -> McpRegistrationError:
        return McpRegistrationError(
            CODE_SYSTEM_ENTRY_OWNER_CONFLICT,
            f"system entry '{name}' is owned by another live connection",
        )

    def release_owner(self, owner_id: str) -> None:
        owner = str(owner_id or "")
        if not owner:
            return
        for entry in self._registry.values():
            if entry.owner_id == owner:
                entry.owner_id = ""

    async def update_servers(
        self, servers: list[Mapping[str, Any]], *, owner_id: str = ""
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in servers:
            if not isinstance(item, Mapping):
                results.append({"name": "", "ok": False, "error": "server entry must be an object"})
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                results.append({"name": "", "ok": False, "error": "name is required"})
                continue
            try:
                config, declaration, scan_params = _parse_registration(name, item)
                _admit_server_config(name, config)
            except ValueError as exc:
                results.append(_failure(name, exc))
                continue
            expected_fp = ""
            async with self._lock:
                current = self._registry.get(name)
                if current is None:
                    results.append({"name": name, "ok": False, "error": "not found"})
                    continue
                expected_fp = config_fingerprint(current.config)
                unchanged = expected_fp == config_fingerprint(config)
                if not unchanged and _owner_conflict(current, owner_id):
                    results.append(_failure(name, self._owner_conflict_error(name)))
                    continue
                if unchanged:
                    if current.declaration.system and not current.owner_id:
                        current.owner_id = owner_id
                    current.updated_at = time.monotonic()
                    cached = self._cache.get(name)
                    tool_names = [str(t.get("name") or "") for t in (cached.tools if cached else [])]
                    results.append(
                        {
                            "name": name,
                            "ok": True,
                            "skipped": True,
                            "tools": tool_names,
                            "tools_count": len(tool_names),
                        }
                    )
                    continue
            tools, params, error = await self._discover(name, config, scan_params)
            if error:
                results.append({"name": name, "ok": False, "error": error, "code": CODE_SCAN_FAILED})
                continue
            now = time.monotonic()
            async with self._lock:
                current = self._registry.get(name)
                if current is None:
                    results.append({"name": name, "ok": False, "error": "not found"})
                    continue
                if _owner_conflict(current, owner_id):
                    results.append(_failure(name, self._owner_conflict_error(name)))
                    continue
                if config_fingerprint(current.config) != expected_fp:
                    results.append(
                        {
                            "name": name,
                            "ok": False,
                            "error": "conflict: config changed concurrently",
                        }
                    )
                    continue
                old = self._cache.get(name)
                self._registry[name] = dataclass_replace(
                    current,
                    config=copy.deepcopy(config),
                    updated_at=now,
                    declaration=declaration,
                    owner_id=owner_id if declaration.system else "",
                )
                self._cache[name] = CachedServerRecord(
                    name=name,
                    tools=copy.deepcopy(tools),
                    version=(old.version + 1) if old is not None else 1,
                    last_scan_at=now,
                    last_scan_ok=True,
                    last_error="",
                    connect_params=copy.deepcopy(params),
                )
            await self.worker_pool.retire_server(name, reason="config_update")
            logger.info(
                "[McpServerRegistry] config update name=%s old_fp=%s new_fp=%s changed=%s retire=True",
                name,
                expected_fp[:12],
                config_fingerprint(config)[:12],
                _changed_config_keys(current.config, config),
            )
            tool_names = [str(t.get("name") or "") for t in tools]
            results.append(
                {
                    "name": name,
                    "ok": True,
                    "tools": tool_names,
                    "tools_count": len(tool_names),
                }
            )
        return results

    async def list_servers(self) -> list[dict[str, Any]]:
        async with self._lock:
            items: list[dict[str, Any]] = []
            for name, entry in self._registry.items():
                cached = self._cache.get(name)
                items.append(
                    {
                        "name": name,
                        "config": redact_mcp_config(entry.config),
                        "enabled": entry.enabled,
                        "tools_count": len(cached.tools) if cached is not None else 0,
                        "version": cached.version if cached is not None else 0,
                        "last_scan_at": cached.last_scan_at if cached is not None else 0.0,
                        "last_scan_ok": cached.last_scan_ok if cached is not None else False,
                    }
                )
            return items

    async def get_server(self, name: str) -> dict[str, Any] | None:
        key = str(name or "").strip()
        async with self._lock:
            entry = self._registry.get(key)
            cached = self._cache.get(key)
            if entry is None:
                return None
            return {
                "name": key,
                "config": redact_mcp_config(entry.config),
                "enabled": entry.enabled,
                "tools": copy.deepcopy(cached.tools) if cached is not None else [],
                "version": cached.version if cached is not None else 0,
                "last_scan_at": cached.last_scan_at if cached is not None else 0.0,
                "last_scan_ok": cached.last_scan_ok if cached is not None else False,
                "last_error": cached.last_error if cached is not None else "",
            }

    def _invoke_connect_params_locked(self, name: str) -> dict[str, Any]:
        """Invoke 以 registry 为权威。必须持有 ``self._lock``。

        §6.2 的「在途不受影响」指工具快照 / allowlist 不在 CRUD 时被撕掉；
        不授权 remove/update 关掉 worker 之后再用登记时的旧 ``_params`` 把进程拉起来。
        """

        entry = self._registry.get(name)
        cached = self._cache.get(name)
        if entry is None or cached is None or not cached.connect_params:
            raise UnknownMcpServerError(name)
        if not entry.enabled:
            raise DisabledMcpServerError(name)
        return copy.deepcopy(cached.connect_params)

    async def acquire_worker(
        self,
        server_name: str,
        *,
        force_rebuild: bool = False,
        via: str = "",
        values: Mapping[str, str] | None = None,
        variant: str = "",
    ) -> _PooledMcpWorker:
        """全局池 acquire：server 须仍在册且 enabled，连接参数取当前缓存。

        header / env 方式传入本轮参数值与池键后缀：参数值并入连接参数的副本，
        只存在于这个 worker 的连接或进程上，不写回缓存。
        """

        name = str(server_name or "").strip()
        async with self._lock:
            params = apply_runtime_params(self._invoke_connect_params_locked(name), via, values)
        keyed = {"variant": variant} if variant else {}
        worker = await self.worker_pool.acquire(name, params, force_rebuild=force_rebuild, **keyed)
        async with self._lock:
            try:
                latest = apply_runtime_params(
                    self._invoke_connect_params_locked(name), via, values
                )
            except McpRegistryChatError as exc:
                stale_error: McpRegistryChatError | None = exc
                latest = None
            else:
                stale_error = None
        if stale_error is not None:
            await self.worker_pool.retire_server(name, reason="acquire_stale_disabled")
            raise stale_error
        if latest is not None and config_fingerprint(params) != config_fingerprint(latest):
            await self.worker_pool.retire_server(name, reason="acquire_fingerprint_changed")
            worker = await self.worker_pool.acquire(name, latest, force_rebuild=True, **keyed)
            async with self._lock:
                try:
                    self._invoke_connect_params_locked(name)
                except McpRegistryChatError as exc:
                    await self.worker_pool.retire_server(name, reason="acquire_stale")
                    raise exc
        return worker

    async def acquire_group_worker(
        self,
        server_name: str,
        group: str,
        *,
        replace: _PooledMcpWorker | None = None,
    ) -> _PooledMcpWorker:
        """meta 方式的服务取分组 worker；返回时已占位，须以 ``reserved=True`` 调用。"""

        name = str(server_name or "").strip()
        size = self.settings.worker_group_size
        async with self._lock:
            params = self._invoke_connect_params_locked(name)
        worker = await self.worker_pool.acquire_grouped(name, params, group, size, replace=replace)
        stale_error: McpRegistryChatError | None = None
        latest: dict[str, Any] = {}
        try:
            async with self._lock:
                try:
                    latest = self._invoke_connect_params_locked(name)
                except McpRegistryChatError as exc:
                    stale_error = exc
        except BaseException:
            worker.inflight -= 1
            raise
        if stale_error is None and config_fingerprint(params) == config_fingerprint(latest):
            return worker
        worker.inflight -= 1
        await self.worker_pool.retire_server(name, reason="acquire_stale")
        if stale_error is not None:
            raise stale_error
        return await self.worker_pool.acquire_grouped(name, latest, group, size)

    async def snapshot_for_deepresearch(self, name: str) -> tuple[dict, list, dict]:
        """Read one current delegation source atomically; do not start a worker."""
        async with self._lock:
            params = self._invoke_connect_params_locked(name)
            entry = self._registry[name]
            return (copy.deepcopy(entry.config), copy.deepcopy(self._cache[name].tools),
                    deepresearch_connect_params(params, entry.declaration))

    async def snapshot_for_chat(
        self, names: list[str]
    ) -> list[tuple[str, list[dict[str, Any]], dict[str, Any]]]:
        """按名称读取工具快照。缺省/停用抛错。无 IO。"""

        snapshots: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        unknown: list[str] = []
        disabled: list[str] = []
        async with self._lock:
            for raw in names:
                name = str(raw or "").strip()
                if not name:
                    continue
                entry = self._registry.get(name)
                cached = self._cache.get(name)
                if entry is None or cached is None:
                    unknown.append(name)
                    continue
                if not entry.enabled:
                    disabled.append(name)
                    continue
                snapshots.append(
                    (name, copy.deepcopy(cached.tools), copy.deepcopy(cached.connect_params))
                )
        if unknown:
            raise UnknownMcpServerError(unknown)
        if disabled:
            raise DisabledMcpServerError(disabled)
        return snapshots

    def _commit_scan_locked(
        self, name: str, tools: list[dict[str, Any]], params: Mapping[str, Any] | None
    ) -> tuple[bool, int]:
        """写入一次成功的扫描，返回（工具是否变化, 版本）。必须持有 ``self._lock``。"""

        current = self._cache.get(name)
        now = time.monotonic()
        if current is not None and cached_tools_equal(current.tools, tools):
            current.last_scan_at = now
            current.last_scan_ok = True
            current.last_error = ""
            current.scan_fail_count = 0
            if params:
                current.connect_params = copy.deepcopy(dict(params))
            return False, current.version
        version = (current.version + 1) if current is not None else 1
        kept = copy.deepcopy(current.connect_params) if current is not None else {}
        self._cache[name] = CachedServerRecord(
            name=name,
            tools=copy.deepcopy(tools),
            version=version,
            last_scan_at=now,
            last_scan_ok=True,
            last_error="",
            connect_params=copy.deepcopy(dict(params)) if params else kept,
        )
        logger.info(
            "[McpServerRegistry] tools changed: name=%s version=%s count=%s", name, version, len(tools)
        )
        return True, version

    async def rescan_servers(
        self, names: list[str], scan_params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """配置不变、重新列工具。失败保留原缓存；同一服务并发的复扫合并为一次。"""

        per_server = scan_params if isinstance(scan_params, Mapping) else {}
        results: list[dict[str, Any]] = []
        for raw in names:
            name = str(raw or "").strip()
            if not name:
                results.append({"name": "", "ok": False, "error": "name is required"})
                continue
            task = self._rescans.get(name)
            if task is None or task.done():
                task = asyncio.create_task(self._rescan_one(name, per_server.get(name)))
                self._rescans[name] = task
                task.add_done_callback(
                    lambda done, key=name: self._rescans.pop(key, None)
                    if self._rescans.get(key) is done else None
                )
            # shield：一个等待者被取消不能打断其它请求共用的这次扫描。
            results.append(await asyncio.shield(task))
        return results

    async def _rescan_one(self, name: str, raw_scan_params: Any) -> dict[str, Any]:
        async with self._lock:
            entry = self._registry.get(name)
        if entry is None:
            return {"name": name, "ok": False, "error": "not found"}
        try:
            scan_params = _parse_scan_params(raw_scan_params, entry.declaration.runtime_params)
        except McpRegistrationError as exc:
            return _failure(name, exc)
        tools, params, error = await self._discover(name, entry.config, scan_params)
        async with self._lock:
            if self._registry.get(name) is not entry:
                return {"name": name, "ok": False, "error": "conflict: config changed concurrently"}
            if error:
                return {"name": name, "ok": False, "error": error, "code": CODE_SCAN_FAILED}
            changed, version = self._commit_scan_locked(name, tools, params)
        return {
            "name": name, "ok": True, "changed": changed, "tools_count": len(tools), "version": version,
        }

    def _schedule_worker_rescan(self, name: str, worker: _PooledMcpWorker, reason: str) -> None:
        """worker 重建或服务通知工具有变：经这个 worker 复扫。多次触发只排一次补扫。"""

        running = self._worker_rescans.get(name)
        if running is not None and not running.done():
            # 记最新的那个 worker：在途复扫用的 worker 可能正是被换掉的旧进程。
            self._worker_rescan_again[name] = (worker, reason)
            return
        task = asyncio.create_task(self._worker_rescan_loop(name, worker, reason))
        self._worker_rescans[name] = task
        task.add_done_callback(
            lambda done, key=name: self._worker_rescans.pop(key, None)
            if self._worker_rescans.get(key) is done else None
        )

    async def _worker_rescan_loop(self, name: str, worker: _PooledMcpWorker, reason: str) -> None:
        while True:
            try:
                await self._rescan_via_worker(name, worker, reason)
            except Exception:
                # 不能让一次复扫异常结束本任务：排队的补扫还要继续处理。
                logger.warning(
                    "[McpServerRegistry] rescan via worker raised: name=%s reason=%s",
                    name, reason, exc_info=True,
                )
            queued = self._worker_rescan_again.pop(name, None)
            if queued is None:
                return
            worker, reason = queued

    async def _rescan_via_worker(self, name: str, worker: _PooledMcpWorker, reason: str) -> bool:
        """经活动 worker 列工具并更新缓存。失败只记日志：不计扫描失败，也不动缓存。"""

        async with self._lock:
            entry = self._registry.get(name)
        if entry is None or not entry.enabled:
            return False
        try:
            tools = await asyncio.wait_for(worker.list_tools(), timeout=_MCP_SCAN_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[McpServerRegistry] rescan via worker failed: name=%s reason=%s error=%s",
                name, reason, type(exc).__name__,
            )
            return False
        tools = _apply_tool_filter(list(tools or []), _transport_config(entry.config))
        async with self._lock:
            if self._registry.get(name) is not entry:
                return False
            self._commit_scan_locked(name, tools, None)
        return True

    def chat_declarations(self, names: list[str]) -> dict[str, McpServerDeclaration]:
        """读取条目声明（frozen，可直接共享）。同步、无 IO。"""

        out: dict[str, McpServerDeclaration] = {}
        for raw in names:
            entry = self._registry.get(str(raw or "").strip())
            if entry is not None:
                out[entry.name] = entry.declaration
        return out

    def start_scanner(self) -> None:
        if self._scanner_task is not None and not self._scanner_task.done():
            return
        self._scanner_stop = asyncio.Event()
        self._scanner_task = asyncio.create_task(self._scanner_loop(), name="mcp-registry-scanner")

    async def stop_scanner(self) -> None:
        self._scanner_stop.set()
        task = self._scanner_task
        self._scanner_task = None
        try:
            if task is not None:
                task.cancel()
                try:
                    # 等待已有 scanner 清理完成；调用 stop 的任务取消时，不再次打断它。
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass  # scanner 本身的取消，外部取消在下方继续传播
                        raise
                except Exception:
                    logger.exception("[McpServerRegistry] scanner shutdown failed")
        finally:
            for pending in [*self._worker_rescans.values(), *self._rescans.values()]:
                pending.cancel()
            # 即使 scanner 尚未启动，也可能已经创建了全局 worker。
            await self.worker_pool.close_all()

    async def scan_once(self) -> None:
        """单轮扫描（单测 / 扫描循环复用）。"""

        async with self._lock:
            remote = [
                entry
                for entry in list(self._registry.values())
                if entry.enabled and is_remote_mcp_config(entry.config)
            ]
        snapshot = [e for e in remote if not any(p.required for p in e.declaration.runtime_params)]
        for entry in remote:
            if entry in snapshot:
                continue
            worker = self.worker_pool.active_worker(entry.name)
            if worker is not None:
                await self._rescan_via_worker(entry.name, worker, "periodic")
        sem = asyncio.Semaphore(self.settings.scan_concurrency)

        async def _scan_one(entry: McpServerEntry) -> None:
            async with sem:
                async with self._lock:
                    if self._registry.get(entry.name) is not entry:
                        return
                tools, params, error = await self._discover(entry.name, entry.config)
                async with self._lock:
                    if self._registry.get(entry.name) is not entry:
                        return
                    current = self._cache.get(entry.name)
                    now = time.monotonic()
                    if error:
                        fail_count = (current.scan_fail_count + 1) if current is not None else 1
                        if current is not None:
                            current.last_scan_at = now
                            current.last_scan_ok = False
                            current.last_error = error
                            current.scan_fail_count = fail_count
                        if fail_count >= self.settings.scan_fail_threshold:
                            logger.error(
                                "[McpServerRegistry] remote scan failed %s times: name=%s error=%s",
                                fail_count,
                                entry.name,
                                error,
                            )
                        return
                    self._commit_scan_locked(entry.name, tools, params)

        if snapshot:
            scanned = await asyncio.gather(
                *[_scan_one(entry) for entry in snapshot],
                return_exceptions=True,
            )
            for entry, item in zip(snapshot, scanned):
                if item is not None:
                    logger.warning(
                        "[McpServerRegistry] scan_once failed: name=%s error=%s",
                        entry.name,
                        item,
                    )
        await self.worker_pool.reap_idle(self.settings.worker_idle_ttl_s)

    async def _scanner_loop(self) -> None:
        while not self._scanner_stop.is_set():
            try:
                await self.scan_once()
            except Exception:
                logger.exception("[McpServerRegistry] scanner cycle failed")
            jitter = 1.0 + random.uniform(0.0, 0.1)
            timeout = self.settings.scan_interval_s * jitter
            try:
                await asyncio.wait_for(self._scanner_stop.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                pass

    async def _discover(
        self, name: str, config: Mapping[str, Any], scan_params: Mapping[str, str] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        static = _transport_config(config)
        config = static
        via = ""
        if scan_params:
            via = "header" if is_remote_mcp_config(static) else "env"
            if via == "header" and not static.get("auth_headers"):
                # 配置里的 headers 是 auth_headers 的别名，合并前先归一，否则会被扫描参数整体顶掉。
                static = {**static, "auth_headers": static.get("headers") or {}}
            config = apply_runtime_params(static, via, scan_params)
        timeout_s = _discover_timeout_s(config)
        try:
            tools, params = await asyncio.wait_for(
                list_request_mcp_server_tools(name, config),
                timeout=timeout_s,
            )
            if scan_params and params:
                params = _strip_scan_params(dict(params), static, via, scan_params)
        except asyncio.TimeoutError:
            return [], {}, f"connect timeout after {int(timeout_s)}s"
        except Exception as exc:
            return [], {}, str(exc)
        if not params:
            return [], {}, "discovery failed"
        return list(tools or []), dict(params), ""


_REGISTRY: McpServerRegistry | None = None


def get_mcp_server_registry() -> McpServerRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = McpServerRegistry()
    return _REGISTRY


def reset_mcp_server_registry_for_tests() -> McpServerRegistry:
    global _REGISTRY
    _REGISTRY = McpServerRegistry()
    return _REGISTRY


async def start_mcp_registry_runtime() -> None:
    get_mcp_server_registry().start_scanner()


async def stop_mcp_registry_runtime() -> None:
    global _REGISTRY
    if _REGISTRY is None:
        return
    await _REGISTRY.stop_scanner()


async def build_registered_mcp_tools(
    request: Any,
    server_name: str,
    runtime_params: Mapping[str, Any] | None = None,
    tool_scope: list[str] | None = None,
) -> list[Any]:
    """为一个已注册服务构造本轮工具对象，口径与对话装载完全一致。只读缓存，不做 IO。

    供进程内扩展在对话中途按需装载（如懒打开）。工具对象未注册到任何 agent，由调用方
    安装；请求结束时调用方须把它们交给 ``retire_request_owned_mcp_workers``。
    服务未注册或已停用、参数缺失或不合法时抛 ``McpRegistryChatError``。
    """

    registry = get_mcp_server_registry()
    name = str(server_name or "").strip()
    snapshots = await registry.snapshot_for_chat([name])
    if not snapshots:
        raise UnknownMcpServerError(name)
    _, tool_defs, params = snapshots[0]
    declaration = registry.chat_declarations([name]).get(name) or McpServerDeclaration()
    values, skip_reason, _dropped = resolve_runtime_params(declaration, runtime_params)
    if skip_reason:
        raise McpRegistryChatError(f"mcp server '{name}' is unavailable: {skip_reason}")
    if tool_scope is not None:
        allowed = {str(item).strip() for item in tool_scope}
        tool_defs = [t for t in tool_defs if str(t.get("name") or "").strip() in allowed]
    delivery = runtime_delivery(declaration, values, f"{request.session_id}:{request.request_id}")
    return [
        create_request_scoped_mcp_tool(
            request.request_id, request.session_id, name, tool_def, params,
            use_global_pool=True, strict=is_office_claw_entry_name(name),
            runtime_params=values, runtime_delivery=delivery, catalog_host=declaration.system,
        )
        for tool_def in tool_defs
        if str(tool_def.get("name") or "").strip()
    ]


async def retire_request_owned_mcp_workers(tools: Any, *, request_id: str = "") -> None:
    """关闭这些工具对象名下随请求独占的连接；请求结束（含取消、报错、审批中断）时调用。"""

    owned: set[tuple[str, str]] = set()
    for tool in tools or ():
        variant = getattr(tool, "request_owned_worker", "")
        if variant:
            owned.add((tool.server_name, variant))
    for server_name, variant in owned:
        try:
            await get_mcp_server_registry().worker_pool.retire_variant(
                server_name, variant, reason="request_end",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[McpServerRegistry] request-owned connection close failed: "
                "request_id=%s server=%s error=%s", request_id, server_name, exc,
            )


def extract_mcp_server_list(params: Any) -> list[str] | None:
    """字段存在则返回名称列表（可空）；缺省返回 None（走旧路径）。"""

    if not isinstance(params, dict) or "mcp_server_list" not in params:
        return None
    raw = params.get("mcp_server_list")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise McpRegistryChatError("mcp_server_list must be a list of server names")
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def extract_mcp_runtime_params(params: Any) -> dict[str, Any]:
    """返回 {注册名: 原始参数对象}；按服务的校验留给 ``resolve_runtime_params``。"""

    raw = params.get("mcp_runtime_params") if isinstance(params, dict) else None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise McpRegistryChatError("mcp_runtime_params must be an object keyed by server name")
    return {str(name).strip(): value for name, value in raw.items()}


def extract_mcp_tool_scope(params: Any) -> dict[str, frozenset[str]]:
    """返回 {注册名: 本轮允许的原始工具名}；未出现的服务不收窄。"""

    raw = params.get("mcp_tool_scope") if isinstance(params, dict) else None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise McpRegistryChatError("mcp_tool_scope must be an object keyed by server name")
    scope: dict[str, frozenset[str]] = {}
    for name, tools in raw.items():
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise McpRegistryChatError("mcp_tool_scope values must be lists of tool names")
        scope[str(name).strip()] = frozenset(t.strip() for t in tools if t.strip())
    return scope
