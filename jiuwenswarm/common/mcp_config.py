# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for converting ``config.yaml`` MCP entries to runtime configs."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import os
import re
import sys
import threading
import time
import weakref
from contextlib import AsyncExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import urlparse
import anyio

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import McpServerConfig, Tool, ToolCard

try:
    from openjiuwen.core.foundation.tool.mcp.client import (
        sse_client as _sse_client,  # noqa: F401
        stdio_client as _stdio_client,  # noqa: F401
        streamable_http_client as _streamable_http_client,  # noqa: F401
    )
except ImportError:
    pass

_HTTP_MCP_TRANSPORTS = frozenset({"sse", "http", "streamable-http", "streamable_http"})


def extract_enabled_mcp_server_entries(
    config_base: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return enabled ``mcp.servers`` entries from a resolved config mapping."""
    if not isinstance(config_base, dict):
        return []
    mcp_cfg = config_base.get("mcp", {})
    if not isinstance(mcp_cfg, dict):
        return []
    servers = mcp_cfg.get("servers", [])
    if not isinstance(servers, list):
        return []

    result: list[dict[str, Any]] = []
    for item in servers:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled", True)):
            continue
        result.append(item)
    return result


def build_mcp_server_config(
    entry: dict[str, Any],
    *,
    server_id_scope: str | None = None,
) -> McpServerConfig | None:
    """Build a ``McpServerConfig`` from one ``mcp.servers`` entry.

    Args:
        entry: One config entry under ``mcp.servers``.
        server_id_scope: Optional scope used to derive a stable ``server_id``.
            When omitted, openjiuwen's default random id behavior is preserved.
    """
    name = str(entry.get("name", "")).strip()
    if not name:
        return None
    transport = str(entry.get("transport", "")).strip().lower()
    if transport not in {"stdio", "sse", "http", "streamable-http", "streamable_http"}:
        return None

    payload: dict[str, Any] = {
        "server_name": name,
        "client_type": transport,
    }
    explicit_server_id = str(entry.get("server_id", "") or "").strip()
    if explicit_server_id:
        payload["server_id"] = explicit_server_id

    if transport == "stdio":
        command = str(entry.get("command", "")).strip()
        if not command:
            return None
        params: dict[str, Any] = {"command": command}
        args = entry.get("args")
        if isinstance(args, list):
            params["args"] = [str(item) for item in args]
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            params["cwd"] = cwd.strip()
        env = entry.get("env")
        if isinstance(env, dict):
            params["env"] = {str(k): str(v) for k, v in env.items()}
        timeout_s = entry.get("timeout_s")
        if isinstance(timeout_s, (int, float)) and int(timeout_s) > 0:
            params["timeout_s"] = int(timeout_s)
        payload["server_path"] = f"stdio://{name}"
        payload["params"] = params
    else:
        url = str(entry.get("url", "")).strip()
        if not url:
            return None
        payload["server_path"] = url
        params: dict[str, Any] = {}
        headers = entry.get("headers")
        if isinstance(headers, dict):
            params["headers"] = {str(k): str(v) for k, v in headers.items()}
        timeout_s = entry.get("timeout_s")
        if isinstance(timeout_s, (int, float)) and int(timeout_s) > 0:
            params["timeout_s"] = int(timeout_s)
        if params:
            payload["params"] = params

    if server_id_scope and "server_id" not in payload:
        payload["server_id"] = _stable_mcp_server_id(server_id_scope, name, payload)

    return McpServerConfig(**payload)


def build_enabled_mcp_server_configs(
    config_base: dict[str, Any],
    *,
    server_id_scope: str | None = None,
) -> list[McpServerConfig]:
    """Build all enabled MCP server configs, skipping invalid entries."""
    configs: list[McpServerConfig] = []
    for entry in extract_enabled_mcp_server_entries(config_base):
        cfg = build_mcp_server_config(entry, server_id_scope=server_id_scope)
        if cfg is not None:
            configs.append(cfg)
    return configs


async def preflight_mcp_server_reachable(
    cfg: McpServerConfig, *, timeout: float = 3.0
) -> tuple[bool, str]:
    """Cheap reachability probe for HTTP-based MCP servers.

    Why this exists: when an HTTP MCP server is unreachable, openjiuwen still
    enters the mcp ``streamablehttp_client`` async context (which spins up an
    anyio task group with background request tasks) before failing on
    ``session.initialize()``. Tearing that context back down leaks orphaned
    background tasks and raises noisy ``aclose(): asynchronous generator is
    already running`` / ``Attempted to exit cancel scope in a different task``
    errors. Probing the host:port first lets us skip registration cleanly.

    Returns ``(reachable, reason)``. Non-HTTP transports (stdio/playwright/…)
    report reachable — they are spawned locally and have no cheap probe.
    """
    transport = (getattr(cfg, "client_type", "") or "").strip().lower()
    if transport not in _HTTP_MCP_TRANSPORTS:
        return True, ""

    url = (getattr(cfg, "server_path", "") or "").strip()
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False, f"invalid url: {url!r}"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except asyncio.TimeoutError:
        return False, f"tcp connect to {host}:{port} timed out after {timeout}s"
    except Exception as exc:
        # Connection refused / DNS failure / etc. — also defensive: the probe
        # itself must never break startup with an unexpected exception type.
        return (
            False,
            f"tcp connect to {host}:{port} failed: {type(exc).__name__}: {exc}",
        )

    writer.close()
    try:
        await writer.wait_closed()
    except Exception as exc:
        logger.debug(
            "[mcp-preflight] reachability probe socket close failed for %s:%s: %r",
            host,
            port,
            exc,
        )
    return True, ""


def is_asyncio_outer_cancellation() -> bool:
    """Return True when the current task has a pending outer cancel request.

    Used to distinguish real interrupt / WebSocket disconnect cancels from
    anyio TaskGroup connect-failure paths that cancel()+uncancel() the host
    task (leaving ``cancelling()`` at 0 when ``CancelledError`` reaches the
    caller). Requires Python 3.11+ (``Task.cancelling()``).
    """
    current = asyncio.current_task()
    return bool(current is not None and current.cancelling())


def _stable_mcp_server_id(scope: str, name: str, payload: dict[str, Any]) -> str:
    stable_payload = {
        key: value for key, value in payload.items() if key != "server_id"
    }
    raw = json.dumps(
        {"scope": scope, "payload": stable_payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    safe_scope = _safe_id_part(scope, default="scope")
    safe_name = _safe_id_part(name, default="server")
    return f"mcp_{safe_scope}_{safe_name}_{digest}"


def _safe_id_part(value: str, *, default: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    return (normalized or default)[:48]


def _normalize_stdio_command_kind(command: str) -> str:
    """将 command 归一化为 'node'、'python'、'npx' 或 'uvx'。

    支持绝对路径如 /usr/local/bin/node、C:\\Program Files\\node.exe 等。
    npx/uvx为包运行器，参数为包名而非本地脚本路径，安全模型与 node/python 不同。
    """
    raw = str(command or "").strip()
    if not raw:
        raise ValueError("工具配置缺少 'command' 字段")

    normalized = raw.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if normalized in ("node", "node.exe"):
        return "node"
    if normalized.startswith("python"):
        return "python"
    if normalized in ("npx", "npx.exe", "npx.cmd", "npx.bat"):
        return "npx"
    if normalized in ("uvx", "uvx.exe"):
        return "uvx"
    raise ValueError(
        f"不支持的 command 类型: '{command}'，目前仅支持 node/python/npx/uvx 及其绝对路径"
    )


def _normalize_mcp_client_type(raw_type: object) -> str:
    if raw_type is None:
        return "stdio"
    s = str(raw_type).strip().lower().replace("_", "-")
    if "streamable" in s:
        return "streamable-http"
    if s == "sse":
        return "sse"
    if s == "stdio":
        return "stdio"
    return s if s else "stdio"


def _pick_mcp_url(tool_config: dict) -> str:
    v = tool_config.get("url")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return ""


def _optional_auth_dict(tool_config: dict, key: str) -> dict | None:
    raw = tool_config.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"字段 {key!r} 必须是 JSON 对象")
    return dict(raw)


_DANGEROUS_ARGS_PATTERN = frozenset(
    {
        "-e",
        "--eval",
        "-c",
        "--command",
        "-i",
    }
)


def _check_dangerous_args(tool_name: str, args: list) -> None:
    if not isinstance(args, list):
        return
    for arg in args:
        arg_str = str(arg).strip()
        if arg_str in _DANGEROUS_ARGS_PATTERN:
            raise ValueError(
                f"安全拦截阻断：工具 '{tool_name}' 的 args 包含危险标志 '{arg_str}'，"
                "禁止通过参数注入执行任意代码。"
            )
        for dangerous_prefix in ("-e=", "--eval=", "-c=", "--command="):
            if arg_str.lower().startswith(dangerous_prefix):
                raise ValueError(
                    f"安全拦截阻断：工具 '{tool_name}' 的 args 包含危险标志 '{arg_str}'，"
                    "禁止通过参数注入执行任意代码。"
                )


def _trusted_cat_cafe_stdio_roots() -> list[Path]:
    roots: list[Path] = []
    raw = (os.getenv("CAT_CAFE_MCP_CWD") or "").strip()
    if raw:
        try:
            roots.append(Path(raw).expanduser().resolve())
        except OSError:
            pass
    try:
        roots.append((Path.home() / ".office-claw").resolve())
    except OSError:
        pass
    try:
        roots.append(Path(sys.executable).resolve().parent)
    except OSError:
        pass
    lp = (os.getenv("LOCALAPPDATA") or "").strip()
    if lp:
        inst = Path(lp) / "Programs" / "OfficeClaw"
        try:
            if inst.exists():
                roots.append(inst.resolve())
        except OSError:
            pass
    for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.getenv(env_key, "").strip()
        if not base:
            continue
        inst = Path(base) / "OfficeClaw"
        try:
            if inst.exists():
                roots.append(inst.resolve())
        except OSError:
            pass
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = os.path.normcase(str(r))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _path_is_under_trusted_root(path: Path, roots: list[Path]) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _validate_cat_cafe_request_scoped_stdio(params: dict[str, Any]) -> None:
    """限制请求级 stdio：禁止内联代码执行面，脚本路径须在受信根目录下。

    npx/uvx 为包运行器，参数为包名及其参数，不适用本地脚本路径受信根校验
    （代码来源为包仓库而非本地文件，信任决策在于包名而非路径）。
    """
    cmd = str(params.get("command") or "").strip()
    args = params.get("args") or []
    if not isinstance(args, list):
        raise ValueError("stdio MCP 的 args 须为列表")

    kind = _normalize_stdio_command_kind(cmd)
    flat = [str(a) for a in args]
    lowered = [a.strip().lower() for a in flat]
    if any(x == "-c" or x == "--command" for x in lowered):
        raise ValueError("请求级 cat_cafe_mcp 禁止使用 python -c / --command")
    if kind == "node" and any(x in ("-e", "--eval") for x in lowered):
        raise ValueError("请求级 cat_cafe_mcp 禁止使用 node -e / --eval")

    if kind in ("npx", "uvx"):
        return

    cwd_path: Path | None = None
    cwd_raw = params.get("cwd")
    if isinstance(cwd_raw, str) and cwd_raw.strip():
        try:
            cwd_path = Path(cwd_raw).expanduser().resolve()
        except OSError as exc:
            raise ValueError(f"请求级 cat_cafe_mcp cwd 无效: {cwd_raw}") from exc
        if not _path_is_under_trusted_root(cwd_path, _trusted_cat_cafe_stdio_roots()):
            raise ValueError(f"请求级 cat_cafe_mcp cwd 不在受信根目录下: {cwd_path}")

    roots = _trusted_cat_cafe_stdio_roots()
    for a in flat:
        s = a.strip()
        if not s or s.startswith("-"):
            continue
        path_like_suffix = s.lower().endswith((".js", ".mjs", ".cjs", ".py"))
        if "/" not in s and "\\" not in s and not path_like_suffix:
            continue
        candidate = Path(s).expanduser()
        if not candidate.is_absolute() and cwd_path is None:
            raise ValueError(
                "请求级 cat_cafe_mcp 使用相对脚本路径时必须提供位于受信根下的 cwd"
            )
        try:
            if cwd_path is not None and not candidate.is_absolute():
                resolved = (cwd_path / candidate).resolve()
            else:
                resolved = candidate.resolve()
        except OSError:
            continue
        if not _path_is_under_trusted_root(resolved, roots):
            raise ValueError(
                f"请求级 cat_cafe_mcp 参数路径不在受信根目录下: {resolved}"
            )


_REQUEST_REMOTE_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata",
        "metadata.azure.com",
    }
)

_REQUEST_REMOTE_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "metadata.azure.com",
    }
)


def _loopback_mcp_allowed() -> bool:
    return (os.getenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _is_blocked_host(host: str) -> bool:
    if not host:
        return False
    host = host.lower().strip()
    allow_loopback = _loopback_mcp_allowed()
    if allow_loopback and host == "localhost":
        return False
    if host in _REQUEST_REMOTE_METADATA_HOSTS:
        return True
    if not allow_loopback and host in _REQUEST_REMOTE_BLOCKED_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if allow_loopback and ip.is_loopback:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_request_scoped_remote_mcp(tool_name: str, cfg: dict) -> None:
    if not isinstance(cfg, dict):
        return
    if _loopback_mcp_allowed():
        logger.warning(
            "[mcp-config][WARN] JIUWENSWARM_ALLOW_LOOPBACK_MCP 已启用，"
            "loopback/localhost 地址放行（仅开发测试用，生产环境勿设此变量）"
        )
    url = cfg.get("url")
    if isinstance(url, str) and url:
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            host = ""
        if _is_blocked_host(host):
            raise ValueError(
                f"安全拦截阻断：请求级 sse/http MCP ({tool_name!r}) 的 url 指向"
                f"内网/元数据地址 ({host})，疑似 SSRF 攻击。"
            )
    for field in ("auth_headers", "auth_query_params"):
        val = cfg.get(field)
        if not isinstance(val, dict):
            continue
        for k, v in val.items():
            if not isinstance(v, str):
                continue
            try:
                host = (urlparse(v).hostname or "") if "://" in v else v.strip()
            except Exception:
                host = v.strip()
            if _is_blocked_host(host):
                raise ValueError(
                    f"安全拦截阻断：请求级 sse/http MCP ({tool_name!r}) 的 "
                    f"{field}[{k!r}] 值指向内网/元数据地址，疑似凭证外泄/SSRF。"
                )


def create_mcp_tool(config_str: str) -> McpServerConfig:
    """从 JSON 字符串解析并构造 ``McpServerConfig``。

    Args:
        1.stdio类型:
        config_str: JSON 格式配置字符串，格式为：
            {
                "name": "tool_name",
                "command": "node" | "python" | "npx" | "uvx",
                "args": ["xxx.js"] | ["xxx.py"] | ["-y", "@scope/pkg"] | ["pkg"]
            }

        2.streamable-http类型:
            {
                "type": "streamableHttp",
                "url": "http://127.0.0.1:3002/mcp",
                "env": {},
                "auth_headers": {
                    "Authorization": "Bearer xxx"
                },
                "auth_query_params": {
                    "token": "yyy"
                }
            }

        3.sse类型:
            {
                "name": "my-sse-mcp",
                "type": "sse",
                "url": "http://127.0.0.1:3001/sse",
                "env": {},
                "auth_headers": {
                    "Authorization": "Bearer xxx"
                },
                "auth_query_params": {
                    "token": "yyy"
                }
            }
        4.playwright类型:
            {
                "name": "my-playwright-mcp",
                "description": "可选说明",
                "type": "playwright",
                "url": "http://127.0.0.1:3003/sse",
                "env": {}
            }

    Returns:
        ``McpServerConfig``，由调用方通过 ``Runner.resource_mgr.add_mcp_server(..., tag=...)`` 注册。

    Raises:
        ValueError: JSON 解析失败或配置不合法时
    """
    try:
        config = json.loads(config_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"无效的 JSON 配置") from e

    if isinstance(config, list):
        if len(config) == 0:
            raise ValueError("工具配置数组不能为空")
        tool_config = config[0]
    elif isinstance(config, dict):
        tool_config = config
    else:
        raise ValueError("配置必须是字典或数组类型")

    if not isinstance(tool_config, dict):
        raise ValueError("工具配置必须是字典类型")

    tool_name = tool_config.get("name")
    server_id = str(tool_config.get("server_id") or tool_name or "").strip()
    command = tool_config.get("command")
    args = tool_config.get("args", [])
    env = tool_config.get("env")
    cwd = tool_config.get("cwd")

    if not tool_name:
        raise ValueError("工具配置缺少 'name' 字段")

    url = _pick_mcp_url(tool_config)
    client_type = _normalize_mcp_client_type(tool_config.get("type"))
    params = {}
    if isinstance(env, dict) and env:
        params["env"] = {
            str(k): str(v) for k, v in env.items() if k is not None and v is not None
        }
    if client_type == "sse":
        if not url:
            raise ValueError(f"工具 '{tool_name}'（'{client_type}'）需要 url")
        headers = _optional_auth_dict(tool_config, "auth_headers")
        query = _optional_auth_dict(tool_config, "auth_query_params")
        return McpServerConfig(
            server_id=server_id or tool_name,
            server_name=tool_name,
            server_path=url,
            client_type="sse",
            auth_headers=headers,
            auth_query_params=query,
            params=params,
        )

    if client_type == "streamable-http":
        if not url:
            raise ValueError(f"工具 '{tool_name}'（'{client_type}'）需要 url")
        headers = _optional_auth_dict(tool_config, "auth_headers")
        query = _optional_auth_dict(tool_config, "auth_query_params")
        return McpServerConfig(
            server_id=server_id or tool_name,
            server_name=tool_name,
            server_path=url,
            client_type="streamable-http",
            auth_headers=headers,
            auth_query_params=query,
            params=params,
        )

    if client_type == "playwright":
        if not url:
            raise ValueError(f"工具 '{tool_name}'（'{client_type}'）需要 url")
        return McpServerConfig(
            server_id=server_id or tool_name,
            server_name=tool_name,
            server_path=url,
            client_type="playwright",
            params=params,
        )

    if client_type == "openapi":
        if not url:
            raise ValueError(f"工具 '{tool_name}'（'{client_type}'）需要 url")
        return McpServerConfig(
            server_id=server_id or tool_name,
            server_name=tool_name,
            server_path=url,
            client_type="openapi",
            params=params,
        )

    if not isinstance(args, list):
        raise ValueError(f"工具 '{tool_name}' 的 args 必须是列表类型")

    _check_dangerous_args(tool_name, args)

    normalized_command = str(command or "").strip()
    _normalize_stdio_command_kind(normalized_command)
    params["command"] = normalized_command
    params["args"] = args
    if isinstance(cwd, str) and cwd.strip():
        params["cwd"] = cwd.strip()
    return McpServerConfig(
        server_id=server_id or tool_name,
        server_name=tool_name,
        server_path=f"stdio://{tool_name}",
        client_type="stdio",
        params=params,
    )


_OFFICE_CLAW_MCP_ENV_KEYS = frozenset(
    {
        "OFFICE_CLAW_API_URL",
        "OFFICE_CLAW_INVOCATION_ID",
        "OFFICE_CLAW_CALLBACK_TOKEN",
        "OFFICE_CLAW_USER_ID",
        "OFFICE_CLAW_AGENT_ID",
        "OFFICE_CLAW_SIGNAL_USER",
        "OFFICE_CLAW_MCP_EXCLUDED_TOOLS",
        "NODE_EXTRA_CA_CERTS",
    }
)

_OFFICE_CLAW_MCP_SCHEMA_CACHE_ENV = "JIUWENSWARM_MCP_SCHEMA_CACHE"
_OFFICE_CLAW_MCP_SCHEMA_CACHE_OFF = frozenset({"0", "false", "no", "off"})
_office_claw_mcp_schema_cache: dict[str, list[dict[str, Any]]] = {}
_office_claw_mcp_schema_inflight: dict[
    tuple[int, int, str], asyncio.Task[list[dict[str, Any]]]
] = {}
_office_claw_mcp_schema_cache_lock = threading.Lock()
_office_claw_mcp_schema_generation = 0


def _office_claw_mcp_schema_cache_enabled() -> bool:
    return (
        str(os.environ.get(_OFFICE_CLAW_MCP_SCHEMA_CACHE_ENV, "") or "").strip().lower()
        not in _OFFICE_CLAW_MCP_SCHEMA_CACHE_OFF
    )


def _office_claw_mcp_build_fingerprint(
    params: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Fingerprint the MCP bundle file(s) so a rebuild rotates the cache key.

    Relative script paths in ``command``/``args`` are resolved against
    ``params["cwd"]`` first — the stdio MCP process is spawned with that cwd,
    so it is the directory that actually owns the bundle — and only fall back
    to the sidecar process cwd. Resolving against the process cwd alone would
    either miss the real bundle or stat the wrong file, so a bundle rebuild
    would not change the fingerprint and the cache would serve a stale schema.
    """
    fingerprints: list[dict[str, Any]] = []
    base_dirs: list[Path] = []
    cwd_raw = str(params.get("cwd") or "").strip()
    if cwd_raw:
        base_dirs.append(Path(cwd_raw).expanduser())
    base_dirs.append(Path.cwd())
    candidates = [params.get("command"), *(params.get("args") or [])]
    seen: set[str] = set()
    for candidate in candidates:
        raw = str(candidate or "").strip()
        if not raw:
            continue
        probe = Path(raw).expanduser()
        resolved: Path | None = None
        if probe.is_absolute():
            resolved = probe
        else:
            for base in base_dirs:
                candidate_path = base / probe
                if candidate_path.is_file():
                    resolved = candidate_path
                    break
        if resolved is None:
            try:
                resolved = probe.resolve(strict=False)
            except OSError:
                continue
        resolved_str = str(resolved)
        if not resolved.is_file() or resolved_str in seen:
            continue
        seen.add(resolved_str)
        try:
            stat = resolved.stat()
        except OSError:
            continue
        fingerprints.append(
            {
                "path": _normalized_path(resolved_str),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return fingerprints


def _office_claw_mcp_schema_cache_key(params: Mapping[str, Any]) -> str:
    raw_excluded = str(
        (params.get("env") or {}).get("OFFICE_CLAW_MCP_EXCLUDED_TOOLS") or ""
    )
    payload = {
        "command": _normalized_path(str(params.get("command") or "")),
        "args": [str(value) for value in params.get("args") or []],
        "cwd": _normalized_path(str(params.get("cwd") or "")),
        "excluded_tools": sorted(
            {item.strip() for item in raw_excluded.split(",") if item.strip()}
        ),
        "build_files": _office_claw_mcp_build_fingerprint(params),
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def invalidate_office_claw_mcp_schema_cache() -> None:
    """Drop the entire OfficeClaw MCP schema cache and abort coalesced discovery.

    Called when the agent catalog revision changes (Relay resync) so that a
    rebuilt MCP bundle or changed excluded-tool set is re-discovered instead
    of serving a stale schema. Bumping ``_office_claw_mcp_schema_generation``
    also prevents any in-flight discovery task from writing back into a newer
    generation.
    """
    global _office_claw_mcp_schema_generation
    with _office_claw_mcp_schema_cache_lock:
        _office_claw_mcp_schema_generation += 1
        _office_claw_mcp_schema_cache.clear()
        _office_claw_mcp_schema_inflight.clear()


def _clear_office_claw_mcp_schema_cache_for_tests() -> None:
    invalidate_office_claw_mcp_schema_cache()


@dataclass(frozen=True)
class OfficeClawMcpRegistration:
    """Tools installed on one session agent for one Relay request."""

    request_id: str
    tool_ids: tuple[str, ...]
    tool_names: tuple[str, ...]
    tool_instances: tuple[RequestScopedOfficeClawMcpTool, ...] = ()
    # Relay callback invocation id pinned into the MCP subprocess env for this
    # request (OFFICE_CLAW_INVOCATION_ID). Empty when unknown / not office-claw.
    invocation_id: str = ""


# Request-scoped long-lived MCP worker pool
# ----------------------------------------------------------------------------
# 旧实现每次 call_tool 都 fork+kill 一个 stdio 进程，对 chrome-devtools-mcp 这类
# 有状态连接器会反复开关浏览器（"flash-open"）。这里按 (request_id, server_name)
# 池化一个长生命周期 session。
# stdio 的 anyio cancel scope 是 task-local，跨 task 调 call_tool 会报"Attempted
# to exit a cancel scope that isn't the current tasks's current cancel scope"。
# 故 session 必须驻留在专用 owner task：调用方把请求投队列并 await Future，
# owner task 排干队列在自身上下文里跑 session.call_tool。请求清理时销毁 owner task
# （cancel 关闭 stdio 进程/浏览器）。

# stdio MCP 无 apply_mcp_call_timeout_patch 兜底（仅覆盖 HTTP），故此处对 call_tool/discovery 各加超时。
_MCP_CALL_TOOL_TIMEOUT_S = 30.0
_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S = 30.0


class _McpCallRequest:
    """One call_tool request handed from an invoke task to the owner task."""

    __slots__ = ("tool_name", "arguments", "future")

    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()


class _PooledMcpWorker:
    """Owns one stdio MCP process+session in a dedicated asyncio task.

    Exposes ``call_tool`` so callers can treat it like a ``ClientSession``.
    """

    __slots__ = ("queue", "task", "server_name")

    def __init__(self, server_name: str) -> None:
        self.queue: asyncio.Queue[_McpCallRequest | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.server_name = server_name

    @property
    def alive(self) -> bool:
        return self.task is not None and not self.task.done()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Submit a call_tool to the owner task and await its result."""
        if not self.alive:
            raise RuntimeError(
                f"request-scoped MCP worker for '{self.server_name}' is not running"
            )
        req = _McpCallRequest(name, arguments)
        await self.queue.put(req)
        return await req.future


# 按 (request_id, server_name) 池化：同 key 并发 invoke 共享一个进程/浏览器，跨请求不共享。
_request_scoped_mcp_sessions: dict[tuple[str, str], _PooledMcpWorker] = {}
# 串行化 (re)build：同 key 并发首调只起一个 owner task，避免竞态泄漏失败者。
_request_scoped_mcp_build_lock = asyncio.Lock()


async def _run_mcp_worker(
    params: Mapping[str, Any],
    worker: _PooledMcpWorker,
) -> None:
    """Owner task：持有 MCP client 句柄，排干调用队列。

    按 ``params["_mcp_client_type"]`` 分派：
    - stdio（默认/缺省）：起 ``stdio_client`` + ``ClientSession`` 进程。
    - sse / streamable-http：复用 openjiuwen 的 ``SseClient`` / ``StreamableHttpClient``，
      connect 后长连接复用（自带 owner-task/cancel-scope/超时/重连 + auth 注入）。

    全程在本 task 内执行，使 stdio 的 anyio task group / cancel scope 不逃逸到
    外来 task。task 被 cancel（清理）或队列收到 ``None`` 哨兵时退出（关闭 stdio 进程 / 远端连接）。
    """

    client_type = str(params.get("_mcp_client_type") or "").lower() or "stdio"

    async with AsyncExitStack() as stack:
        try:
            if client_type in ("sse", "streamable-http"):
                session = await _enter_remote_mcp_session(stack, params, client_type)
            else:
                session = await _enter_stdio_mcp_session(stack, params)
        except Exception as exc:
            # 初始化失败：失败已排队的 caller，退出以便下次 invoke 时 acquire 重建。
            logger.warning(
                "request-scoped MCP worker init failed: server=%s transport=%s error=%s",
                worker.server_name,
                client_type,
                exc,
            )
            await _drain_queue_with_error(worker, exc)
            return
        while True:
            req = await worker.queue.get()
            if req is None:
                break
            try:
                # 超时分派：
                # - stdio：必须用 anyio.fail_after（cancel scope）。MCP stdio transport 的
                #   anyio task group / cancel scope 要求进入与退出在同一 task；asyncio.wait_for
                #   把协程放到新 Task 跑，会破坏该不变量。
                # - remote（sse/streamable-http）：相反，必须用 asyncio.wait_for。remote
                #   transport（尤其 streamable-http）内部自带后台 task + anyio cancel scope；
                #   若用 anyio.fail_after 在本 task 套一层 scope，外层 scope 与 transport 后台
                #   task 的 scope 跨 task 退出会抛 "Attempted to exit a cancel scope that isn't
                #   the current tasks's current cancel scope"（企查查 streamable-http 实测故障）。
                #   asyncio.wait_for 在新 Task 里跑协程，scope 隔离，与 SseClient 自身超时
                #   机制（同样 asyncio.wait_for）一致，已由百度网盘 SSE 实证可行。
                try:
                    if client_type in ("sse", "streamable-http"):
                        result = await asyncio.wait_for(
                            session.call_tool(req.tool_name, arguments=req.arguments),
                            timeout=_MCP_CALL_TOOL_TIMEOUT_S,
                        )
                    else:
                        with anyio.fail_after(_MCP_CALL_TOOL_TIMEOUT_S):
                            result = await session.call_tool(
                                req.tool_name, arguments=req.arguments
                            )
                except TimeoutError as exc:
                    if not req.future.done():
                        req.future.set_exception(exc)
                    worker.queue.task_done()
                    logger.warning(
                        "request-scoped MCP worker call_tool timed out: "
                        "server=%s tool=%s timeout=%.0fs",
                        worker.server_name,
                        req.tool_name,
                        _MCP_CALL_TOOL_TIMEOUT_S,
                    )
                    # remote transport（sse/streamable-http）：SseClient/StreamableHttpClient
                    # 的 _submit 把调用转发到其内部 owner_task 串行执行；外层 cancel 只取消
                    # 等待方的 future，不会取消 owner_task 上正在跑的命令。若 continue 复用
                    # worker，下次 invoke 投的新命令会排在未完成的旧命令后面，最长阻塞 60s
                    # （SseClient 内部 asyncio.wait_for 上限）。故超时即退出 worker：AsyncExitStack
                    # 关闭触发 disconnect（SseClient 内部 cancel owner_task 清掉未完成命令），
                    # 下次 invoke 的 acquire 发现 worker 已死便重建。
                    if client_type in ("sse", "streamable-http"):
                        await _drain_queue_with_error(worker, exc)
                        break
                    continue
            except BaseException as exc:
                # 即使 cancel/keyboard 也要解除 caller，避免 invoke 永挂死 worker。
                if not req.future.done():
                    req.future.set_exception(exc)
                worker.queue.task_done()
                if not isinstance(exc, Exception):
                    # Cancelled/KeyboardInterrupt/SystemExit：worker 被销毁，先排干队列再 raise。
                    await _drain_queue_with_error(worker, exc)
                    raise
                # 普通调用失败：保留循环，让 invoke 的 force_rebuild 重建 worker。
                continue
            if not req.future.done():
                req.future.set_result(result)
            worker.queue.task_done()


async def _enter_stdio_mcp_session(stack: AsyncExitStack, params: Mapping[str, Any]) -> Any:
    """stdio transport：起子进程 ClientSession，生命周期交由 stack 管理。"""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    read, write = await stack.enter_async_context(
        stdio_client(_stdio_server_parameters(params))
    )
    session = await stack.enter_async_context(
        ClientSession(read, write, sampling_callback=None)
    )
    await session.initialize()
    return session


def _build_remote_mcp_config(
    server_name: str,
    params: Mapping[str, Any],
    client_type: str,
) -> Any:
    """从 worker params（或 connect_params）重建 ``McpServerConfig``。

    discovery（``_list_remote_mcp_connector_tools``）与 worker（``_enter_remote_mcp_session``）
    都要 new 一个 remote client，配置字段手工重复易漂移，集中在此。字段口径：
    server_id 缺省回退 server_name，再回退传入的 server_name，避免空 id。
    """

    return McpServerConfig(
        server_id=str(params.get("server_id") or params.get("server_name") or server_name),
        server_name=str(params.get("server_name") or server_name),
        server_path=str(params.get("server_path") or ""),
        client_type=client_type,
        params=dict(params.get("params") or {}),
        auth_headers=dict(params.get("auth_headers") or {}),
        auth_query_params=dict(params.get("auth_query_params") or {}),
    )


async def _enter_remote_mcp_session(
    stack: AsyncExitStack,
    params: Mapping[str, Any],
    client_type: str,
) -> Any:
    """sse/streamable-http transport：复用 openjiuwen 高层 client，长连接复用。

    connect 时 ``Runner.callback_framework.trigger(TOOL_AUTH)`` 注入 auth_headers
    （relay 下发的 ``Authorization: Bearer xxx`` 经 HeaderQueryAuthStrategy 加到请求头）。
    把 disconnect 注册进 stack，使 worker 退出时统一关连接。
    """
    client_cls = _remote_mcp_client_cls(client_type)
    if client_cls is None:
        raise ValueError(f"unsupported remote MCP transport: {client_type}")

    rebuild_cfg = _build_remote_mcp_config(params.get("server_name") or "", params, client_type)
    client = client_cls(rebuild_cfg)
    connected = await client.connect(timeout=_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S)
    if not connected:
        raise RuntimeError(
            f"remote MCP client connect returned false: {rebuild_cfg.server_path}"
        )
    # 把 disconnect 注册进 stack，worker 退出时统一关连接。

    async def _disconnect() -> None:
        try:
            await client.disconnect(timeout=10.0)
        except Exception as exc:
            logger.debug("remote MCP client disconnect failed: %s", exc)

    stack.push_async_callback(_disconnect)
    return _RemoteMcpCallAdapter(client)


class _RemoteMcpCallAdapter:
    """让 remote MCP client 的 call_tool 返回形状对齐 stdio ClientSession。

    openjiuwen 的 ``SseClient`` / ``StreamableHttpClient`` 已经把
    ``CallToolResult`` 抽成裸文本/值返回（见 ``extract_mcp_tool_result_content``）；
    而 ``_run_mcp_worker`` 的消费端（``RequestScopedOfficeClawMcpTool.invoke``）
    按 stdio ``ClientSession.call_tool`` 的契约读 ``result.content[-1].text``。
    本适配器把 remote 的裸返回值重新包成 ``CallToolResult(content=[TextContent])``，
    使两条路径在 invoke 处统一，避免 ``'str' object has no attribute 'content'``。
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        from mcp.types import CallToolResult, TextContent

        raw = await self._client.call_tool(name, arguments)
        # extract_mcp_tool_result_content 对空结果返回 None。
        if raw is None:
            return CallToolResult(content=[])
        # 按 extract_mcp_tool_result_content 的实际返回形状分类型装箱，
        # 避免 str(dict) 产出 Python repr 让 LLM 难解析。
        text = _remote_mcp_result_to_text(raw)
        return CallToolResult(content=[TextContent(type="text", text=text)])


def _remote_mcp_result_to_text(raw: Any) -> str:
    """把 extract_mcp_tool_result_content 的裸返回值规整成可读文本。

    返回形状：str（文本/image 占位串）/ dict（model_dump）/ 原始 data（base64 等）/ 其它。
    - str：原样用。
    - dict：JSON 序列化（ensure_ascii=False 保留中文），比 str() 的 repr 可读。
    - 其它：str() 兜底。
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        try:
            return json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(raw)
    return str(raw)


async def _drain_queue_with_error(worker: _PooledMcpWorker, exc: BaseException) -> None:
    """Fail any already-queued callers when the worker cannot start."""
    while not worker.queue.empty():
        req = worker.queue.get_nowait()
        if req is None:
            continue
        if not req.future.done():
            req.future.set_exception(exc)
        worker.queue.task_done()


async def acquire_request_scoped_mcp_session(
    request_id: str,
    server_name: str,
    params: Mapping[str, Any],
    *,
    force_rebuild: bool = False,
) -> Any:
    """Return a long-lived ``_PooledMcpWorker`` for this request+connector.

    owner task 首次用时起、后续 invoke 复用；死掉时透明重建。
    force_rebuild=True 丢弃缓存 worker 起新的（invoke 重试路径在死进程后用）。
    """

    key = (str(request_id or ""), str(server_name or ""))
    worker = _request_scoped_mcp_sessions.get(key)
    if force_rebuild and worker is not None:
        await _close_pooled_worker(worker, key)
        worker = None
    if worker is not None and worker.alive:
        return worker
    # (re)build：旧 worker 缺失或已死。串行化 build 以免同 key 并发首调起多个 owner task。
    async with _request_scoped_mcp_build_lock:
        worker = _request_scoped_mcp_sessions.get(key)
        if worker is not None and worker.alive:
            return worker
        worker = _PooledMcpWorker(server_name)
        _request_scoped_mcp_sessions[key] = worker
        # create_task 会复制当前 context，但 stdio 的 task group 在 owner task 内进入，
        # cancel scope 属于 owner task，跨 invoke task 安全。
        worker.task = asyncio.create_task(_run_mcp_worker(params, worker))
    return worker


async def _close_pooled_worker(
    worker: _PooledMcpWorker,
    key: tuple[str, str],
) -> None:
    """Best-effort stop+remove one pooled worker (kills the stdio process)."""

    _request_scoped_mcp_sessions.pop(key, None)
    task = worker.task
    if task is None:
        return
    # Ask the owner loop to exit gracefully, then await (cancel as fallback).
    # 用 anyio.fail_after 而非 asyncio.wait_for：后者在新 Task 里跑协程，会破坏
    # MCP transport（stdio 的 task group / remote 的 SseClient owner-task）的
    # anyio cancel-scope 不变量，与 _run_mcp_worker 内部的超时机制保持一致。
    try:
        worker.queue.put_nowait(None)
    except Exception as exc:
        logger.warning(
            "request-scoped MCP worker close: put_nowait(None) failed "
            "server=%s key=%s error=%s (will fall back to task.cancel)",
            worker.server_name, key, exc,
        )
    try:
        with anyio.fail_after(5.0):
            await task
    except TimeoutError:
        # anyio.fail_after 抛内置 TimeoutError（asyncio.TimeoutError 的父类）。
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    except asyncio.CancelledError:
        # 清理路径自身被 cancel：仍 cancel worker 以免 stdio 进程/浏览器泄漏。
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        raise
    except Exception as exc:
        # owner task 抛了非超时/非 cancel 的异常（多数是已结束），记录一下根因。
        logger.warning(
            "request-scoped MCP worker owner task exited with error "
            "server=%s key=%s error=%s",
            worker.server_name, key, exc,
        )


async def release_request_scoped_mcp_sessions(request_id: str) -> None:
    """Stop every long-lived MCP worker for one request (cleanup hook)."""

    rid = str(request_id or "")
    keys = [k for k in _request_scoped_mcp_sessions if k[0] == rid]
    for key in keys:
        worker = _request_scoped_mcp_sessions.get(key)
        if worker is None:
            continue
        await _close_pooled_worker(worker, key)


OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX = "office-claw-request-"
OFFICE_CLAW_EXPECTED_TOOL_IDS_KWARG = "_office_claw_expected_tool_ids"

# Attribute name used to store the request-scoped OfficeClaw tool id allowlist.
# The allowlist must cross the asyncio task boundary between the caller's task
# (where ``bind_active_office_claw_mcp_tools`` sets a ContextVar) and the
# DeepAgent's supervisor / round task (created at session startup, before the
# ContextVar is set).  Storing the ids on the shared ability_manager gives the
# round task a reliable source to re-bind the ContextVar before invoking an
# OfficeClaw MCP tool.
_OFFICE_CLAW_TOOL_IDS_ATTR = "_active_office_claw_tool_ids"

# Task-local allowlist for request-scoped OfficeClaw tool ids. Set for the
# duration of one Relay chat.send so a concurrent session cannot invoke this
# request's MCP tools (or vice versa) via a polluted short-name AbilityManager.
_active_office_claw_tool_ids: ContextVar[frozenset[str] | None] = ContextVar(
    "active_office_claw_tool_ids",
    default=None,
)

# Process-local live registrations. DeepAgent runs tools on an interaction
# round task created by the supervisor, which does not inherit the facade
# task's ContextVar. ProgressiveToolRail / Tool.invoke re-bind from here.
_live_office_claw_allowlists_by_tool_id: dict[str, frozenset[str]] = {}
_live_office_claw_registration_count_by_tool_name: dict[str, int] = {}
_live_office_claw_tool_instances: weakref.WeakKeyDictionary[Any, frozenset[str]] = (
    weakref.WeakKeyDictionary()
)
_live_office_claw_allowlist_lock = threading.Lock()


def _office_claw_tool_name_from_tool_id(tool_id: str) -> str:
    normalized = str(tool_id or "").strip()
    marker = ".office-claw."
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    return normalized.rsplit(".", 1)[-1]


def publish_live_office_claw_allowlist(tool_ids: Iterable[str]) -> None:
    """Publish a request's OfficeClaw tool ids for interaction-round re-bind."""

    ids = frozenset(str(tool_id).strip() for tool_id in tool_ids if str(tool_id).strip())
    seen_names: set[str] = set()
    with _live_office_claw_allowlist_lock:
        for tool_id in ids:
            _live_office_claw_allowlists_by_tool_id[tool_id] = ids
            tool_name = _office_claw_tool_name_from_tool_id(tool_id)
            if tool_name in seen_names:
                continue
            seen_names.add(tool_name)
            _live_office_claw_registration_count_by_tool_name[tool_name] = (
                _live_office_claw_registration_count_by_tool_name.get(tool_name, 0) + 1
            )


def revoke_live_office_claw_allowlist(tool_ids: Iterable[str]) -> None:
    """Drop live allowlist entries after request-scoped MCP cleanup."""

    seen_names: set[str] = set()
    with _live_office_claw_allowlist_lock:
        for tool_id in tool_ids:
            normalized = str(tool_id or "").strip()
            if normalized:
                _live_office_claw_allowlists_by_tool_id.pop(normalized, None)
                tool_name = _office_claw_tool_name_from_tool_id(normalized)
                if tool_name in seen_names:
                    continue
                seen_names.add(tool_name)
                count = _live_office_claw_registration_count_by_tool_name.get(tool_name, 0)
                if count <= 1:
                    _live_office_claw_registration_count_by_tool_name.pop(tool_name, None)
                else:
                    _live_office_claw_registration_count_by_tool_name[tool_name] = count - 1


def get_live_office_claw_allowlist_for_tool_id(tool_id: str) -> frozenset[str] | None:
    """Return the live allowlist that currently owns ``tool_id``, if any."""

    normalized = str(tool_id or "").strip()
    if not normalized:
        return None
    with _live_office_claw_allowlist_lock:
        return _live_office_claw_allowlists_by_tool_id.get(normalized)


def is_office_claw_tool_name_live_concurrent(tool_name: str) -> bool:
    """True when more than one live request-scoped registration owns ``tool_name``."""

    normalized = str(tool_name or "").strip()
    if not normalized:
        return False
    with _live_office_claw_allowlist_lock:
        return _live_office_claw_registration_count_by_tool_name.get(normalized, 0) > 1


def register_live_office_claw_tool_instance(
    tool: RequestScopedOfficeClawMcpTool,
    tool_ids: Iterable[str],
) -> None:
    """Track one request-scoped tool object for safe unbound re-bind."""

    ids = frozenset(str(tool_id).strip() for tool_id in tool_ids if str(tool_id).strip())
    if ids:
        with _live_office_claw_allowlist_lock:
            _live_office_claw_tool_instances[tool] = ids


def unregister_live_office_claw_tool_instance(tool: RequestScopedOfficeClawMcpTool) -> None:
    """Drop a request-scoped tool object from the live instance registry."""

    with _live_office_claw_allowlist_lock:
        _live_office_claw_tool_instances.pop(tool, None)


def get_live_office_claw_allowlist_for_tool_instance(
    tool: RequestScopedOfficeClawMcpTool,
) -> frozenset[str] | None:
    """Return the allowlist registered for this tool object, if still live."""

    with _live_office_claw_allowlist_lock:
        return _live_office_claw_tool_instances.get(tool)


def _office_claw_invocation_id_from_params(params: Mapping[str, Any] | None) -> str:
    """Extract OFFICE_CLAW_INVOCATION_ID from MCP connect params, if present."""

    if not isinstance(params, Mapping):
        return ""
    env = params.get("env")
    if not isinstance(env, Mapping):
        return ""
    return str(env.get("OFFICE_CLAW_INVOCATION_ID") or "").strip()


def resolve_active_office_claw_invocation_id() -> str | None:
    """Return the callback invocation id owned by the active allowlist, if known.

    Looks up live request-scoped tool instances whose registered allowlist matches
    the ContextVar binding. Used to refuse invokes whose subprocess env was pinned
    to a different Relay invocation (cross-session credential mix-ups).
    """

    allowed = get_active_office_claw_mcp_tool_ids()
    if not allowed:
        return None
    allowed_ids = frozenset(allowed)
    exact_match: str | None = None
    card_match: str | None = None
    with _live_office_claw_allowlist_lock:
        for tool, ids in _live_office_claw_tool_instances.items():
            inv = _office_claw_invocation_id_from_params(getattr(tool, "_params", None))
            if not inv:
                continue
            if ids == allowed_ids:
                exact_match = inv
                break
            tool_id = str(getattr(getattr(tool, "_card", None), "id", "") or "")
            if card_match is None and tool_id in allowed_ids:
                card_match = inv
    return exact_match or card_match


def _clear_live_office_claw_allowlists_for_tests() -> None:
    with _live_office_claw_allowlist_lock:
        _live_office_claw_allowlists_by_tool_id.clear()
        _live_office_claw_registration_count_by_tool_name.clear()
        _live_office_claw_tool_instances.clear()


def _office_claw_tool_ids_carrier(agent: Any) -> Any:
    """Return the object that carries the request-scoped allowlist.

    The DeepAgent and its inner ReActAgent share the same ``ability_manager``
    (openjiuwen wires ``agent.ability_manager = deep_agent.ability_manager``),
    so storing the allowlist on the ability_manager makes it visible to
    whichever agent object runs the round (``ctx.agent`` may be either the
    DeepAgent or the ReActAgent).
    """

    if agent is None:
        return None
    ability_manager = getattr(agent, "ability_manager", None)
    if ability_manager is not None:
        return ability_manager
    return agent


@contextmanager
def bind_active_office_claw_mcp_tools(
    tool_ids: Iterable[str] | None,
) -> Iterator[None]:
    """Bind the request-scoped OfficeClaw tool id allowlist for this task."""

    allowed = frozenset(str(tool_id) for tool_id in (tool_ids or ()) if str(tool_id))
    token = _active_office_claw_tool_ids.set(allowed)
    try:
        yield
    finally:
        _active_office_claw_tool_ids.reset(token)


def set_agent_office_claw_tool_ids(agent: Any, tool_ids: Iterable[str] | None) -> None:
    """Store the request-scoped OfficeClaw tool id allowlist on the shared ability_manager.

    The ContextVar ``_active_office_claw_tool_ids`` cannot propagate to the
    DeepAgent's supervisor task because that task is created (via
    ``asyncio.create_task``) at session startup — before the caller enters
    ``bind_active_office_claw_mcp_tools``.  Storing the ids on the shared
    ability_manager gives the round task a way to re-bind the ContextVar
    before invoking an OfficeClaw MCP tool.
    """

    carrier = _office_claw_tool_ids_carrier(agent)
    if carrier is None:
        return
    ids = frozenset(str(tid) for tid in (tool_ids or ()) if str(tid))
    if ids:
        setattr(carrier, _OFFICE_CLAW_TOOL_IDS_ATTR, ids)
    else:
        try:
            delattr(carrier, _OFFICE_CLAW_TOOL_IDS_ATTR)
        except AttributeError:
            pass


def clear_agent_office_claw_tool_ids(agent: Any) -> None:
    """Remove the request-scoped allowlist from the shared ability_manager."""

    carrier = _office_claw_tool_ids_carrier(agent)
    if carrier is None:
        return
    try:
        delattr(carrier, _OFFICE_CLAW_TOOL_IDS_ATTR)
    except AttributeError:
        pass


@contextmanager
def bind_office_claw_from_agent(agent: Any) -> Iterator[None]:
    """Re-bind the ContextVar from the shared ability_manager for the current task.

    Call this inside the round task (e.g. in ``ProgressiveToolRail``) right
    before invoking an OfficeClaw MCP tool.  If the shared ability_manager
    carries a stored allowlist, the ContextVar is set for the duration of the
    call so that ``ensure_request_scoped_office_claw_tool_allowed`` passes.
    """

    carrier = _office_claw_tool_ids_carrier(agent)
    ids = getattr(carrier, _OFFICE_CLAW_TOOL_IDS_ATTR, None) if carrier is not None else None
    if not ids:
        logger.debug(
            "OfficeClaw request-scoped allowlist not found on carrier; "
            "leaving ContextVar unbound for this invoke (agent_type=%s)",
            type(agent).__name__ if agent is not None else None,
        )
        yield
        return
    token = _active_office_claw_tool_ids.set(ids)
    try:
        yield
    finally:
        _active_office_claw_tool_ids.reset(token)


def ensure_request_scoped_office_claw_tool_allowed(tool_id: str) -> None:
    """Refuse request-scoped OfficeClaw tools outside the active request allowlist."""

    normalized = str(tool_id or "").strip()
    if not normalized.startswith(OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX):
        return
    allowed = _active_office_claw_tool_ids.get()
    if allowed is None:
        raise RuntimeError(
            "OfficeClaw MCP tool invoked without an active request binding; "
            "refusing unbound request-scoped invoke"
        )
    if normalized not in allowed:
        raise RuntimeError(
            "OfficeClaw MCP tool is bound to another request; refusing cross-request invoke"
        )


def ensure_office_claw_tool_invocation_matches_active(
    tool: RequestScopedOfficeClawMcpTool,
) -> None:
    """Refuse when the tool's MCP env invocation disagrees with the active allowlist."""

    expected = resolve_active_office_claw_invocation_id()
    if not expected:
        return
    actual = _office_claw_invocation_id_from_params(getattr(tool, "_params", None))
    if actual and actual != expected:
        raise RuntimeError(
            "OfficeClaw MCP tool env invocation mismatches active request binding; "
            "refusing cross-request invoke"
        )


def get_active_office_claw_mcp_tool_ids() -> frozenset[str] | None:
    """Return the request-local OfficeClaw tool id allowlist, if bound."""

    return _active_office_claw_tool_ids.get()


def resolve_active_office_claw_tool_id(tool_name: str) -> str | None:
    """Map a short tool name to this request's OfficeClaw tool id.

    Request-scoped ids look like
    ``office-claw-request-<hash>.office-claw.<tool_name>``. ProgressiveToolRail
    may hold a stale AbilityManager card whose id points at another request's
    (already cleaned-up) registration; the active allowlist is the authority
    for *this* chat.send.
    """

    name = str(tool_name or "").strip()
    if not name:
        return None
    allowed = _active_office_claw_tool_ids.get()
    if not allowed:
        return None
    suffix = f".{name}"
    matches = [
        tool_id
        for tool_id in allowed
        if tool_id.endswith(suffix) or tool_id.rsplit(".", 1)[-1] == name
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Prefer the canonical request-scoped shape when multiple ids match.
    preferred = [
        tool_id
        for tool_id in matches
        if tool_id.startswith(OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX) and tool_id.endswith(suffix)
    ]
    return preferred[0] if preferred else matches[0]


def extract_office_claw_mcp(params: Any) -> dict[str, Any] | None:
    """Return only the legacy ``office_claw_mcp`` request field."""

    if not isinstance(params, dict):
        return None
    raw = params.get("office_claw_mcp")
    if not isinstance(raw, dict) or not raw:
        return None
    return dict(raw)


def extract_request_mcp_servers(params: Any) -> dict[str, dict[str, Any]] | None:
    """取 ``request_mcp_servers.mcpServers`` 的用户连接器 map。

    Relay 的 buildMcpRequestFields 把用户配置的连接器放这里，值仅含启动配置
    （stdio: {command,args,cwd,env?}；remote: {type,url,auth_headers?}），无 tool schema，
    需由 list_request_mcp_server_tools 发现。无该字段返回 None。
    """

    if not isinstance(params, dict):
        return None
    raw = params.get("request_mcp_servers")
    if not isinstance(raw, dict):
        return None
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        return None
    cleaned: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        server_name = str(name or "").strip()
        if not server_name or not isinstance(cfg, dict):
            continue
        cleaned[server_name] = dict(cfg)
    return cleaned or None


def _normalized_path(value: str) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))


def validate_office_claw_mcp_config(
    config: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate request config against Relay's startup-time MCP identity.

    The request may choose only callback-related environment values. The
    executable, arguments, and working directory must exactly match the values
    with which Relay started Jiuwen, preventing this request field from
    becoming a general-purpose subprocess execution surface.
    """

    env_source = os.environ if environ is None else environ
    expected_command = str(env_source.get("OFFICE_CLAW_MCP_COMMAND") or "").strip()
    expected_args_raw = str(env_source.get("OFFICE_CLAW_MCP_ARGS_JSON") or "").strip()
    expected_cwd = str(env_source.get("OFFICE_CLAW_MCP_CWD") or "").strip()
    if not expected_command or not expected_args_raw or not expected_cwd:
        raise ValueError("Relay OfficeClaw MCP startup identity is incomplete")

    try:
        expected_args = json.loads(expected_args_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("OFFICE_CLAW_MCP_ARGS_JSON is invalid") from exc
    if not isinstance(expected_args, list):
        raise ValueError("OFFICE_CLAW_MCP_ARGS_JSON must contain a list")
    expected_args = [str(item) for item in expected_args]

    command = str(config.get("command") or "").strip()
    args = config.get("args")
    cwd = str(config.get("cwd") or "").strip()
    request_env = config.get("env")
    if not command:
        raise ValueError("office_claw_mcp command is required")
    if not isinstance(args, list):
        raise ValueError("office_claw_mcp args must be a list")
    if not cwd:
        raise ValueError("office_claw_mcp cwd is required")
    if not isinstance(request_env, dict):
        raise ValueError("office_claw_mcp env must be an object")

    normalized_args = [str(item) for item in args]
    if _normalized_path(command) != _normalized_path(expected_command):
        raise ValueError(
            "office_claw_mcp command does not match Relay startup identity"
        )
    if normalized_args != expected_args:
        raise ValueError("office_claw_mcp args do not match Relay startup identity")
    if _normalized_path(cwd) != _normalized_path(expected_cwd):
        raise ValueError("office_claw_mcp cwd does not match Relay startup identity")

    unknown_env_keys = {str(key) for key in request_env}.difference(
        _OFFICE_CLAW_MCP_ENV_KEYS
    )
    if unknown_env_keys:
        raise ValueError(
            "office_claw_mcp env contains unsupported keys: "
            + ", ".join(sorted(unknown_env_keys))
        )

    return {
        "command": command,
        "args": normalized_args,
        "cwd": cwd,
        "env": {
            str(key): str(value)
            for key, value in request_env.items()
            if key is not None and value is not None
        },
    }


def _stdio_server_parameters(params: Mapping[str, Any]):
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command=str(params["command"]),
        args=list(params["args"]),
        env=dict(params.get("env") or {}),
        cwd=str(params["cwd"]),
        encoding_error_handler="strict",
    )


async def _list_office_claw_mcp_tools_uncached(
    params: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Start OfficeClaw MCP once, collect its tool schemas, then stop it."""

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    stack = AsyncExitStack()
    try:
        read, write = await stack.enter_async_context(
            stdio_client(_stdio_server_parameters(params))
        )
        session = await stack.enter_async_context(
            ClientSession(read, write, sampling_callback=None)
        )
        await session.initialize()
        response = await session.list_tools()
        return [
            {
                "name": tool.name,
                "description": getattr(tool, "description", "") or "",
                "input_params": getattr(tool, "inputSchema", {}) or {},
            }
            for tool in response.tools
        ]
    finally:
        await stack.aclose()


async def list_request_mcp_server_tools(
    server_name: str,
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """发现单个用户连接器的 tool schema。

    config 是 Relay 的启动载荷（无 tool schema），需起一次服务调 list_tools() 收集，
    对齐 _list_office_claw_mcp_tools_uncached。支持 stdio / sse / streamable-http。
    返回 (tool_defs, connect_params)：connect_params 是经 create_mcp_tool 安全层过滤后的
    连接描述，带 ``_mcp_client_type`` 字段供 ``_run_mcp_worker`` 按 transport 分派，
    交给 RequestScopedOfficeClawMcpTool。任意失败返回 ([], {}) 以免单个坏连接器中断注册。
    """

    # 经 create_mcp_tool 安全层（危险参数过滤/stdio 命令校验/SSRF 主机屏蔽），
    # 而非 office-claw 身份 pin（validate_office_claw_mcp_config，专用于 Relay 自带 office-claw）。
    config_with_name = {**dict(config), "name": server_name}
    try:
        server_cfg = create_mcp_tool(json.dumps(config_with_name))
    except ValueError as exc:
        logger.warning(
            "request-scoped MCP connector '%s' rejected by create_mcp_tool: %s",
            server_name,
            exc,
        )
        return [], {}

    client_type = str(getattr(server_cfg, "client_type", "") or "").lower()
    if client_type == "sse" or client_type == "streamable-http":
        return await _list_remote_mcp_connector_tools(server_name, server_cfg, client_type)

    if client_type != "stdio":
        logger.warning(
            "request-scoped MCP connector '%s' transport '%s' not supported; skipping",
            server_name,
            client_type or "unknown",
        )
        return [], {}

    # stdio：起一次进程，list_tools 后关闭。
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    params = dict(getattr(server_cfg, "params", {}) or {})
    if "command" not in params or "args" not in params:
        logger.warning(
            "request-scoped MCP connector '%s' stdio params incomplete: %s",
            server_name,
            params,
        )
        return [], {}

    stack = AsyncExitStack()
    try:
        read, write = await stack.enter_async_context(
            stdio_client(_stdio_server_parameters(params))
        )
        session = await stack.enter_async_context(
            ClientSession(read, write, sampling_callback=None)
        )
        # 防护 connector 启动卡死（用户配置的连接器是任意命令，不像可信的 office-claw）。
        # 同样用 anyio.fail_after 而非 asyncio.wait_for（破坏 stdio cancel-scope 不变量）。
        try:
            with anyio.fail_after(_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S):
                await session.initialize()
                response = await session.list_tools()
        except TimeoutError:
            logger.warning(
                "request-scoped MCP connector '%s' discovery timed out after "
                "%.0fs (initialize/list_tools hung)",
                server_name,
                _MCP_CONNECTOR_DISCOVERY_TIMEOUT_S,
            )
            return [], {}
        tool_defs = _extract_mcp_tool_defs(response)
        # 标记 transport，供 _run_mcp_worker 分派（默认 None 等价 stdio）。
        params["_mcp_client_type"] = "stdio"
        return tool_defs, params
    except Exception as exc:
        logger.warning(
            "request-scoped MCP connector '%s' tool discovery failed: %s",
            server_name,
            exc,
        )
        return [], {}
    finally:
        await stack.aclose()


async def _list_remote_mcp_connector_tools(
    server_name: str,
    server_cfg: Any,
    client_type: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """sse/streamable-http 连接器发现：复用 openjiuwen 的高层 MCP client。

    ``SseClient`` / ``StreamableHttpClient`` 自带 owner-task/cancel-scope/超时/重连，
    且 connect 时经 ``Runner.callback_framework.trigger(TOOL_AUTH)`` 注入 auth_headers
    （由 ``auth_callback`` 的 ``HeaderQueryAuthStrategy`` 转 ``AuthHeaderAndQueryProvider``，
    把 relay 下发的 ``Authorization: Bearer xxx`` 加到请求头）。import 链自动注册 handler。
    """
    client_cls = _remote_mcp_client_cls(client_type)
    if client_cls is None:
        logger.warning(
            "request-scoped MCP connector '%s' transport '%s' has no client class; skipping",
            server_name,
            client_type,
        )
        return [], {}

    connect_params: dict[str, Any] = {
        "_mcp_client_type": client_type,
        "server_name": str(getattr(server_cfg, "server_name", "") or server_name),
        "server_id": str(getattr(server_cfg, "server_id", "") or ""),
        "server_path": str(getattr(server_cfg, "server_path", "") or ""),
        "auth_headers": dict(getattr(server_cfg, "auth_headers", {}) or {}),
        "auth_query_params": dict(getattr(server_cfg, "auth_query_params", {}) or {}),
        "params": dict(getattr(server_cfg, "params", {}) or {}),
    }

    # _run_mcp_worker 用 connect_params 字段经 _build_remote_mcp_config 重建 client。
    rebuild_cfg = _build_remote_mcp_config(server_name, connect_params, client_type)
    client = client_cls(rebuild_cfg)
    connected = False
    try:
        try:
            # remote transport（sse/streamable-http）的 client.connect 内部会 enter
            # async context manager（streamable-http transport 尤其用 anyio cancel
            # scope）；若用 anyio.fail_after 包，外层 scope 与 transport 的 scope 跨
            # task 退出会抛 "Attempted to exit a cancel scope that isn't the current
            # tasks's current cancel scope"（实测企查查 streamable-http 即此故障）。
            # 改用 asyncio.wait_for：在新 task 里跑协程，scope 隔离，与 SseClient
            # 内部超时机制（同样用 asyncio.wait_for）一致。
            connected = await asyncio.wait_for(
                client.connect(timeout=_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S),
                timeout=_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "request-scoped MCP connector '%s' discovery timed out after "
                "%.0fs (connect/list_tools hung)",
                server_name,
                _MCP_CONNECTOR_DISCOVERY_TIMEOUT_S,
            )
            return [], {}
        if not connected:
            logger.warning(
                "request-scoped MCP connector '%s' (%s) connect failed: %s",
                server_name,
                client_type,
                connect_params["server_path"],
            )
            return [], {}
        try:
            # 同理避免 anyio.fail_after 撞 streamable-http transport 的 cancel scope。
            # StreamableHttpClient.list_tools 的 timeout 参数当前未生效（直接调
            # session.list_tools 无超时），必须靠这层 asyncio.wait_for 兜底防卡死。
            tools = await asyncio.wait_for(
                client.list_tools(timeout=_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S),
                timeout=_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "request-scoped MCP connector '%s' list_tools timed out after %.0fs",
                server_name,
                _MCP_CONNECTOR_DISCOVERY_TIMEOUT_S,
            )
            return [], {}
        tool_defs = [
            {
                "name": getattr(t, "name", "") or "",
                "description": getattr(t, "description", "") or "",
                "input_params": getattr(t, "input_params", None)
                or getattr(t, "inputSchema", {})
                or {},
            }
            for t in (tools or [])
        ]
        return tool_defs, connect_params
    except Exception as exc:
        logger.warning(
            "request-scoped MCP connector '%s' (%s) tool discovery failed: %s",
            server_name,
            client_type,
            exc,
        )
        return [], {}
    finally:
        if connected:
            try:
                await client.disconnect(timeout=10.0)
            except Exception as exc:
                logger.debug(
                    "request-scoped MCP connector '%s' discovery disconnect failed: %s",
                    server_name,
                    exc,
                )


def _remote_mcp_client_cls(client_type: str) -> Any | None:
    """返回 sse/streamable-http 对应的高层 MCP client 类（openjiuwen 提供）。"""
    try:
        if client_type == "sse":
            from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

            return SseClient
        if client_type == "streamable-http":
            from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
                StreamableHttpClient,
            )

            return StreamableHttpClient
    except ImportError as exc:
        logger.warning(
            "remote MCP client class for transport '%s' import failed: %s",
            client_type,
            exc,
        )
    return None


def _extract_mcp_tool_defs(response: Any) -> list[dict[str, Any]]:
    """从 mcp ClientSession.list_tools() 的响应里抽 tool schema（stdio 发现复用）。"""
    tools = getattr(response, "tools", None) or []
    return [
        {
            "name": getattr(tool, "name", "") or "",
            "description": getattr(tool, "description", "") or "",
            "input_params": getattr(tool, "inputSchema", {}) or {},
        }
        for tool in tools
    ]


async def _discover_and_cache_office_claw_mcp_schema(
    params: Mapping[str, Any],
    cache_key: str,
    loop_key: tuple[int, int, str],
    generation: int,
) -> list[dict[str, Any]]:
    """Producer: run one discovery, write the cache, then clean up in-flight.

    The cache write and the in-flight removal are bound to THIS task's own
    completion (its ``finally``), never to whichever waiter happened to start
    it. A cancelled waiter cannot abort the shared discovery or orphan its
    result, because ``asyncio.shield`` keeps this task alive regardless of
    waiter cancellation; this task owns both the write-back and the cleanup.
    """
    started_at = time.monotonic()
    try:
        tools = await _list_office_claw_mcp_tools_uncached(params)
        frozen = copy.deepcopy(tools)
        with _office_claw_mcp_schema_cache_lock:
            if generation == _office_claw_mcp_schema_generation:
                _office_claw_mcp_schema_cache[cache_key] = frozen
        logger.info(
            "OfficeClaw MCP schema cache filled: key=%s tools=%s duration_ms=%.1f",
            cache_key[:12],
            len(frozen),
            (time.monotonic() - started_at) * 1000,
        )
        return frozen
    finally:
        current = asyncio.current_task()
        with _office_claw_mcp_schema_cache_lock:
            if _office_claw_mcp_schema_inflight.get(loop_key) is current:
                _office_claw_mcp_schema_inflight.pop(loop_key, None)


async def list_office_claw_mcp_tools(
    params: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return immutable OfficeClaw tool schemas, coalescing identical discovery.

    The tool list / name / description / JSON Schema is static while the MCP
    bundle file is unchanged; only callback tokens, credentials and call
    results are per-request. When ``JIUWENSWARM_MCP_SCHEMA_CACHE`` is enabled
    (Relay default), the first discovery fills a process-local cache keyed by
    the command/args/cwd/excluded-tools and the bundle build fingerprint
    (size + mtime_ns); subsequent identical discoveries return a deep copy so
    callers cannot mutate the frozen schema. Concurrent discoveries for the
    same event loop + generation + key are coalesced into one in-flight
    producer task; waiters are pure consumers wrapped in ``asyncio.shield``,
    so a cancelled waiter cannot abort the shared discovery or evict it from
    the in-flight table. ``invalidate_office_claw_mcp_schema_cache`` drops
    everything on catalog revision change. Setting the env var to
    ``0/false/no/off`` falls back to the uncached path.
    """

    if not _office_claw_mcp_schema_cache_enabled():
        return await _list_office_claw_mcp_tools_uncached(params)

    cache_key = _office_claw_mcp_schema_cache_key(params)
    with _office_claw_mcp_schema_cache_lock:
        generation = _office_claw_mcp_schema_generation
        loop_key = (id(asyncio.get_running_loop()), generation, cache_key)
        cached = _office_claw_mcp_schema_cache.get(cache_key)
        if cached is not None:
            logger.debug("OfficeClaw MCP schema cache hit: key=%s", cache_key[:12])
            return copy.deepcopy(cached)
        task = _office_claw_mcp_schema_inflight.get(loop_key)
        if task is None:
            task = asyncio.create_task(
                _discover_and_cache_office_claw_mcp_schema(
                    params, cache_key, loop_key, generation
                ),
                name=f"office-claw-mcp-schema-{cache_key[:12]}",
            )
            _office_claw_mcp_schema_inflight[loop_key] = task

    # Pure consumer: a cancelled waiter must not affect the shared producer
    # task. The producer owns the cache write and its own in-flight cleanup.
    tools = await asyncio.shield(task)
    return copy.deepcopy(tools)


class RequestScopedOfficeClawMcpTool(Tool):
    """Invoke one OfficeClaw tool through a long-lived request-scoped process.

    A stdio MCP process (and the browser it drives, for chrome-devtools-mcp)
    is spawned once per ``(request_id, server_name)`` on first invoke and kept
    alive for the rest of the request so a stateful browser session does not
    flash-open/close on every tool call. The process is torn down by
    ``release_request_scoped_mcp_sessions`` during request cleanup.
    """

    def __init__(
        self,
        card: ToolCard,
        params: Mapping[str, Any],
        request_id: str = "",
        server_name: str = "",
    ) -> None:
        super().__init__(card)
        # Deep-copy env so concurrent registrations cannot mutate each other's
        # OFFICE_CLAW_INVOCATION_ID via a shared nested dict reference.
        copied = dict(params)
        env = copied.get("env")
        if isinstance(env, Mapping):
            copied["env"] = dict(env)
        self._params = copied
        self._request_id = str(request_id or "")
        self._server_name = str(server_name or "")

    async def stream(self, inputs: Any, **kwargs: Any):
        raise build_error(StatusCode.TOOL_STREAM_NOT_SUPPORTED, card=self._card)

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        tool_id = str(getattr(self._card, "id", "") or "")
        if get_active_office_claw_mcp_tool_ids() is None and tool_id.startswith(
            OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX
        ):
            live = self._resolve_unbound_office_claw_allowlist(tool_id, kwargs)
            if live is not None:
                with bind_active_office_claw_mcp_tools(live):
                    return await self._invoke_with_active_binding(inputs, **kwargs)
        return await self._invoke_with_active_binding(inputs, **kwargs)

    def _resolve_unbound_office_claw_allowlist(
        self,
        tool_id: str,
        kwargs: Mapping[str, Any],
    ) -> frozenset[str] | None:
        """Re-bind only when the caller or live registry proves ownership."""

        expected_raw = kwargs.get(OFFICE_CLAW_EXPECTED_TOOL_IDS_KWARG)
        if expected_raw is not None:
            expected = frozenset(
                str(item).strip() for item in expected_raw if str(item).strip()
            )
            return expected if tool_id in expected else None

        live = get_live_office_claw_allowlist_for_tool_instance(self)
        if live is not None and tool_id in live:
            return live

        tool_name = str(getattr(self._card, "name", "") or "")
        if is_office_claw_tool_name_live_concurrent(tool_name):
            return None

        live_by_id = get_live_office_claw_allowlist_for_tool_id(tool_id)
        if live_by_id is not None and tool_id in live_by_id:
            return live_by_id
        return None

    async def _invoke_with_active_binding(
        self, inputs: Any, **kwargs: Any
    ) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        tool_id = str(getattr(self._card, "id", "") or "")
        try:
            ensure_request_scoped_office_claw_tool_allowed(tool_id)
            ensure_office_claw_tool_invocation_matches_active(self)
        except RuntimeError as exc:
            raise build_error(
                StatusCode.TOOL_MCP_EXECUTION_ERROR,
                cause=exc,
                reason=str(exc),
                method="invoke",
                card=self._card,
            ) from exc

        arguments = inputs if isinstance(inputs, dict) else {}
        try:
            session = await acquire_request_scoped_mcp_session(
                self._request_id, self._server_name, self._params
            )
            try:
                result = await session.call_tool(self._card.name, arguments=arguments)
            except Exception:
                # 池化进程可能在请求中途死掉（崩溃/stdin 关闭）：丢弃重建一次再重试。
                session = await acquire_request_scoped_mcp_session(
                    self._request_id,
                    self._server_name,
                    self._params,
                    force_rebuild=True,
                )
                result = await session.call_tool(self._card.name, arguments=arguments)
            result_content: str | None = None
            if result.content:
                result_content = getattr(result.content[-1], "text", None)
            return {"result": result_content}
        except Exception as exc:
            raise build_error(
                StatusCode.TOOL_MCP_EXECUTION_ERROR,
                cause=exc,
                reason=str(exc),
                method="invoke",
                card=self._card,
            ) from exc


__all__ = [
    "OFFICE_CLAW_EXPECTED_TOOL_IDS_KWARG",
    "OFFICE_CLAW_REQUEST_TOOL_ID_PREFIX",
    "OfficeClawMcpRegistration",
    "RequestScopedOfficeClawMcpTool",
    "acquire_request_scoped_mcp_session",
    "bind_active_office_claw_mcp_tools",
    "bind_office_claw_from_agent",
    "clear_agent_office_claw_tool_ids",
    "set_agent_office_claw_tool_ids",
    "build_enabled_mcp_server_configs",
    "build_mcp_server_config",
    "create_mcp_tool",
    "ensure_office_claw_tool_invocation_matches_active",
    "ensure_request_scoped_office_claw_tool_allowed",
    "extract_enabled_mcp_server_entries",
    "extract_office_claw_mcp",
    "extract_request_mcp_servers",
    "get_active_office_claw_mcp_tool_ids",
    "get_live_office_claw_allowlist_for_tool_id",
    "get_live_office_claw_allowlist_for_tool_instance",
    "invalidate_office_claw_mcp_schema_cache",
    "is_office_claw_tool_name_live_concurrent",
    "list_office_claw_mcp_tools",
    "list_request_mcp_server_tools",
    "preflight_mcp_server_reachable",
    "publish_live_office_claw_allowlist",
    "register_live_office_claw_tool_instance",
    "release_request_scoped_mcp_sessions",
    "resolve_active_office_claw_invocation_id",
    "resolve_active_office_claw_tool_id",
    "revoke_live_office_claw_allowlist",
    "unregister_live_office_claw_tool_instance",
    "validate_office_claw_mcp_config",
    "_check_dangerous_args",
    "_is_blocked_host",
    "_loopback_mcp_allowed",
    "_normalize_mcp_client_type",
    "_normalize_stdio_command_kind",
    "_optional_auth_dict",
    "_path_is_under_trusted_root",
    "_pick_mcp_url",
    "_trusted_cat_cafe_stdio_roots",
    "_validate_cat_cafe_request_scoped_stdio",
    "_validate_request_scoped_remote_mcp",
]
