# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Admin-env preset custom MCPs (management-plane env channel).

The management plane delivers preset custom connectors to AgentServer pods
as a single env var (rendered into the pod spec by agent-runtime's
config_sync; an env change rolls pods, the value is a process constant)::

    JIUWENSWARM_PRESET_MCP_SERVERS = {
      "ts": 1730000000000,
      "servers": {
        "dify-echo": {"transport": "streamable-http", "url": "http://...",
                      "timeout_s": 600}
      }
    }

``ts`` (epoch ms, management-plane clock; refresh on every content change)
fences reconciliation against the ``preset.ts`` recorded in each tenant's
``mcp/state.json``: entries are (re)applied only when the incoming ts is
greater, so a stale pod generation during a rolling rebuild can never write
an older preset back, and user deletions between pushes are respected. To
withdraw presets, push a new (higher) ts with an empty ``servers`` map —
unsetting the env var is not a push and changes nothing on disk.

``ensure_managed_mcps`` runs once per tenant per process from
``TenantAgentPool._ensure_agent_manager`` (the tenant context is bound
there); it is fail-open everywhere: a malformed env value must never break
manager creation or user requests.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

ENV_KEY = "JIUWENSWARM_PRESET_MCP_SERVERS"

# Fields accepted per server entry (same shape register_custom_mcp takes).
_ALLOWED_KEYS = ("transport", "command", "args", "env", "url", "headers", "timeout_s")

# Process-level parse cache: the env value is a process constant, so the raw
# string is compared and the parse result reused until it changes.
_cache_raw: str | None = None
_cache: tuple[int, dict[str, dict[str, Any]]] = (0, {})


def _reset_preset_cache() -> None:
    """Test hook: drop the parse cache so the next read re-parses."""
    global _cache_raw, _cache
    _cache_raw = None
    _cache = (0, {})


def _string_map(value: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in value.items()}


def _parse_entry(name: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and normalize one server entry; None (skip) when invalid.

    Mirrors register_custom_mcp's semantics: transport aliases normalize
    (http/streamable_http -> streamable-http), stdio requires command and
    splits a full invocation when no explicit args are given, remote
    transports require url. Unknown fields are dropped.
    """
    raw_transport = str(cfg.get("transport", "") or "").strip().lower()
    if raw_transport in ("http", "streamable_http"):
        raw_transport = "streamable-http"
    entry: dict[str, Any] = {"transport": raw_transport}
    if raw_transport == "stdio":
        command = str(cfg.get("command", "") or "").strip()
        if not command:
            logger.warning("[mcp.preset] entry '%s': stdio requires command; skip", name)
            return None
        args = cfg.get("args")
        if isinstance(args, list):
            entry["command"] = command
            entry["args"] = [str(a) for a in args]
        elif args is None or (isinstance(args, str) and not args.strip()):
            # The command may hold the whole invocation ("npx -y x") — split
            # it the way register_custom_mcp does for MCP's stdio transport.
            parts = command.split()
            entry["command"] = parts[0]
            entry["args"] = parts[1:] if len(parts) > 1 else []
        else:
            entry["command"] = command
            entry["args"] = [str(args)]
        if isinstance(cfg.get("env"), dict):
            entry["env"] = _string_map(cfg["env"])
    elif raw_transport in ("sse", "streamable-http"):
        url = str(cfg.get("url", "") or "").strip()
        if not url:
            logger.warning(
                "[mcp.preset] entry '%s': remote transport requires url; skip", name
            )
            return None
        entry["url"] = url
        if isinstance(cfg.get("headers"), dict):
            entry["headers"] = _string_map(cfg["headers"])
        if isinstance(cfg.get("env"), dict):
            entry["env"] = _string_map(cfg["env"])
    else:
        logger.warning(
            "[mcp.preset] entry '%s': transport must be one of "
            "stdio|sse|http|streamable-http; skip",
            name,
        )
        return None
    timeout_s = cfg.get("timeout_s")
    if isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool):
        if int(timeout_s) > 0:
            entry["timeout_s"] = int(timeout_s)
    entry["server_id_scope"] = f"mcp:{name}"
    return entry


def _parse_env(raw: str) -> tuple[int, dict[str, dict[str, Any]]]:
    """Parse the env envelope; any structural problem yields (0, {})."""
    text = raw.strip()
    if not text:
        return (0, {})
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("[mcp.preset] invalid %s JSON: %s; ignore preset", ENV_KEY, exc)
        return (0, {})
    if not isinstance(data, dict):
        logger.error("[mcp.preset] %s must be a JSON object; ignore preset", ENV_KEY)
        return (0, {})
    ts = data.get("ts")
    if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
        logger.error(
            "[mcp.preset] %s requires an integer 'ts' (epoch ms); ignore preset",
            ENV_KEY,
        )
        return (0, {})
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        logger.error("[mcp.preset] %s 'servers' must be an object; ignore preset", ENV_KEY)
        return (0, {})
    defs: dict[str, dict[str, Any]] = {}
    for raw_name, cfg in servers.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(cfg, dict):
            logger.warning("[mcp.preset] skip malformed entry %r", raw_name)
            continue
        entry = _parse_entry(name, cfg)
        if entry is not None:
            defs[name] = entry
    return (ts, defs)


def preset_mcp_definitions() -> tuple[int, dict[str, dict[str, Any]]]:
    """Parse ``JIUWENSWARM_PRESET_MCP_SERVERS`` into ``(ts, {name: entry})``.

    Fail-open: a missing or malformed value returns ``(0, {})`` — callers
    treat ts<=0 as "no preset" and do nothing.
    """
    global _cache_raw, _cache
    raw = os.getenv(ENV_KEY, "")
    if raw == _cache_raw:
        return _cache
    parsed = _parse_env(raw)
    _cache_raw = raw
    _cache = parsed
    return parsed


def ensure_managed_mcps(tenant_key: str | None = None) -> None:
    """Reconcile the env preset into the given tenant's state.json.

    Binds the tenant context itself (scoped): the pool may create the
    manager BEFORE the request layer binds TenantContext — the HTTP invoke
    path resolves the manager first and ``strategy.bind_request`` binds
    afterwards — so an ambient-context guard alone would silently skip the
    first seeding, and with the manager then pool-cached it would never
    retry. Falls back to the ambient tenant key when ``tenant_key`` is
    omitted; without any key (startup-time paths) there is nothing to seed.
    Credential files of withdrawn entries are dropped best-effort after the
    state write; failures never propagate to the caller.
    """
    ts, defs = preset_mcp_definitions()
    if ts <= 0:
        # No env var at all is not a push — see module docstring.
        return
    try:
        from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
    except ImportError:
        logger.debug("[mcp.preset] tenant context unavailable; skip preset seed")
        return
    key = str(tenant_key or "").strip() or TenantContext.current_tenant_key()
    if not key:
        return
    with TenantContext.scoped(key):
        if TenantContext.current_agent_workspace_root() is None:
            return
        from jiuwenswarm.server.runtime.mcp import state_store

        result = state_store.apply_managed_preset(ts, defs)
    if not result.get("applied"):
        return
    for name in result.get("removed", []):
        try:
            from jiuwenswarm.server.runtime.mcp.credential import CredentialStore

            CredentialStore().delete_mcp(name)
        except Exception:  # noqa: BLE001 — credential cleanup is best-effort
            logger.warning(
                "[mcp.preset] failed to drop credentials of withdrawn '%s'",
                name,
                exc_info=True,
            )
    logger.info(
        "[mcp.preset] applied ts=%s entries=%s removed=%s skipped_user=%s",
        ts,
        sorted(defs),
        result.get("removed", []),
        result.get("skipped_user", []),
    )
