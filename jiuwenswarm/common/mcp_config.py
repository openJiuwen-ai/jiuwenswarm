# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for converting ``config.yaml`` MCP entries to runtime configs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import McpServerConfig

_HTTP_MCP_TRANSPORTS = frozenset({"sse", "http", "streamable-http", "streamable_http"})


def extract_enabled_mcp_server_entries(config_base: dict[str, Any]) -> list[dict[str, Any]]:
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


def mcp_endpoint_blocked_reason(url: str) -> str:
    """Host exit (P3) check for a remote MCP endpoint; empty string means allowed."""
    from openjiuwen.harness.security.outbound import OutboundBlockedError, check_mcp_server_url

    try:
        check_mcp_server_url(url)
    except OutboundBlockedError as exc:
        return str(exc)
    except Exception as exc:
        logger.warning("[mcp-config] endpoint check error for %s: %s", url, exc)
        return f"outbound check error: {exc}"
    return ""


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
        blocked = mcp_endpoint_blocked_reason(url)
        if blocked:
            logger.warning("[mcp-config] skip server=%s: %s", name, blocked)
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
        return False, f"tcp connect to {host}:{port} failed: {type(exc).__name__}: {exc}"

    writer.close()
    try:
        await writer.wait_closed()
    except Exception as exc:
        logger.debug(
            "[mcp-preflight] reachability probe socket close failed for %s:%s: %r",
            host, port, exc,
        )
    return True, ""


#: TTL (seconds) for cached preflight verdicts in ``filter_unreachable_mcp_servers``.
PREFLIGHT_CACHE_TTL_S = 60.0
#: Hard cap on cached verdicts — the cache is write-only otherwise, and team
#: scenarios can mint fresh server_paths per team/expert, which would leak
#: entries for the process lifetime.
PREFLIGHT_CACHE_MAX_ENTRIES = 256
#: (server_path -> (reachable, reason, monotonic_ts)) shared verdict cache, so a
#: team with N members sharing one config.yaml pays one probe per endpoint.
#: Expired entries are purged lazily on every filter call; no background task.
_preflight_cache: dict[str, tuple[bool, str, float]] = {}


def _prune_preflight_cache(
        store: dict[str, tuple[bool, str, float]],
        *,
        now: float,
        cache_ttl_s: float,
) -> None:
    """Drop expired entries; if still over cap, evict oldest first."""
    expired = [key for key, (_, _, ts) in store.items() if now - ts >= cache_ttl_s]
    for key in expired:
        del store[key]
    overflow = len(store) - PREFLIGHT_CACHE_MAX_ENTRIES
    if overflow > 0:
        oldest = sorted(store, key=lambda key: store[key][2])[:overflow]
        for key in oldest:
            del store[key]


async def filter_unreachable_mcp_servers(
        mcps: list[McpServerConfig],
        *,
        timeout: float = 3.0,
        cache: dict[str, tuple[bool, str, float]] | None = None,
        cache_ttl_s: float = PREFLIGHT_CACHE_TTL_S,
) -> tuple[list[McpServerConfig], list[tuple[McpServerConfig, str]]]:
    """Split *mcps* into ``(kept, dropped)`` by HTTP reachability preflight.

    Team members register ``spec.mcps`` through openjiuwen's fail-fast
    ``DeepAgent._register_pending_mcps`` — one unreachable server raises and
    aborts the whole member init (leader: the entire team round fails;
    subprocess teammate: restart storm; in-process teammate: silent absence).
    Filtering at the async team-spec boundary keeps a dead endpoint from
    sinking a team round, and also avoids the anyio orphan-task noise that
    entering ``streamablehttp_client`` against a dead endpoint produces (see
    ``preflight_mcp_server_reachable``'s docstring).

    Non-HTTP transports (stdio/playwright/…) always pass — they are spawned
    locally and have no cheap probe. Probe verdicts are cached by
    ``server_path`` for ``cache_ttl_s`` seconds so N members sharing one
    config don't each re-probe the same endpoint; expired entries are purged
    lazily on every call and the cache is capped at
    ``PREFLIGHT_CACHE_MAX_ENTRIES`` (oldest-first eviction), so long-lived
    processes don't accumulate verdicts for dead teams/servers. A probe that
    itself raises is treated as *reachable* (fail-open): this filter must
    never break spec assembly, and the fail-soft registration patch is the
    second line of defense for non-network failures.
    """
    if not mcps:
        return [], []
    store = cache if cache is not None else _preflight_cache
    now = time.monotonic()
    _prune_preflight_cache(store, now=now, cache_ttl_s=cache_ttl_s)
    verdicts: dict[int, tuple[bool, str]] = {}
    probes: dict[int, asyncio.Task] = {}
    for idx, cfg in enumerate(mcps):
        key = (getattr(cfg, "server_path", "") or "").strip()
        cached = store.get(key) if key else None
        if cached is not None and now - cached[2] < cache_ttl_s:
            verdicts[idx] = (cached[0], cached[1])
            continue
        probes[idx] = asyncio.ensure_future(
            preflight_mcp_server_reachable(cfg, timeout=timeout)
        )
    for idx, task in probes.items():
        try:
            ok, reason = await task
        except Exception as exc:
            # 探测自身永不炸装配链——异常按可达放行，留给注册期 fail-soft 兜底。
            ok, reason = True, f"preflight probe error (kept): {type(exc).__name__}: {exc}"
        verdicts[idx] = (ok, reason)
        key = (getattr(mcps[idx], "server_path", "") or "").strip()
        if key:
            store[key] = (ok, reason, now)

    kept: list[McpServerConfig] = []
    dropped: list[tuple[McpServerConfig, str]] = []
    for idx, cfg in enumerate(mcps):
        ok, reason = verdicts[idx]
        if ok:
            kept.append(cfg)
        else:
            dropped.append((cfg, reason))
    return kept, dropped


def _stable_mcp_server_id(scope: str, name: str, payload: dict[str, Any]) -> str:
    stable_payload = {
        key: value
        for key, value in payload.items()
        if key != "server_id"
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


__all__ = [
    "build_enabled_mcp_server_configs",
    "build_mcp_server_config",
    "extract_enabled_mcp_server_entries",
    "mcp_endpoint_blocked_reason",
    "preflight_mcp_server_reachable",
    "filter_unreachable_mcp_servers",
]
