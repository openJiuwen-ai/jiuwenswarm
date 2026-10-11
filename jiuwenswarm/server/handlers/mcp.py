# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""config.yaml 里的 MCP 服务管理，对应 /mcp 命令。

MCP 是给 Agent 接外部工具的一种协议。这条命令负责增删改查配置里的服务。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, NamedTuple

from jiuwenswarm.common.config import (
    get_config,
    get_config_yaml_mcp_servers,
    get_mcp_server_config,
    get_mcp_servers,
    remove_mcp_server,
    set_mcp_server_enabled,
    upsert_mcp_server,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


class McpUpsertTypes(NamedTuple):
    """新增或更新时，成功和失败两种回包类型放在一起，避免参数太多。"""

    ok: str
    fail: str


# 字段名里出现这些词，就当成密码、令牌一类敏感信息，回包时打码。
_MCP_KEY_SENSITIVE_SUBSTRINGS = frozenset({
    "api_key", "access_key", "secret_key", "project_id",
    "auth_code", "auth_token", "amap_key", "map_ak",
    "token", "authorization", "secret",
})

def _mask_sensitive_fields(payload: Any) -> Any:
    if isinstance(payload, dict):
        masked: dict[str, Any] = {}
        for key, value in payload.items():
            key_text = str(key).lower()
            value_text = value.lower() if isinstance(value, str) else ""
            key_sensitive = any(t in key_text for t in _MCP_KEY_SENSITIVE_SUBSTRINGS)
            value_sensitive = any(t in value_text for t in ("bearer ", "api-key ", "secret-"))
            if (key_sensitive or value_sensitive) and not isinstance(value, (dict, list)):
                masked[key] = "***"
            else:
                masked[key] = _mask_sensitive_fields(value)
        return masked
    if isinstance(payload, list):
        return [_mask_sensitive_fields(item) for item in payload]
    return payload

async def _pre_check_mcp_server(server_payload: dict[str, Any]) -> tuple[bool, str]:
    """Try a temporary connection to verify the MCP server is reachable.

    Uses ``logging.disable(CRITICAL)`` to silence the SDK's verbose
    "Failed to parse JSONRPC message" tracebacks and wraps everything
    in tight timeouts so a broken server cannot block the caller.

    Returns ``(ok, message)``.
    """
    import logging as _logging
    from openjiuwen.core.foundation.tool import McpServerConfig
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    name = server_payload.get("name", "")
    transport = server_payload.get("transport", "")

    # Build McpServerConfig (same logic as _fetch_mcp_tools_from_config)
    payload: dict[str, Any] = {"server_name": name, "client_type": transport}
    if transport == "stdio":
        command = server_payload.get("command", "")
        if not command:
            return True, "skipped: no command"
        # stdio 预检查改为纯静态校验,静态校验零 spawn、零 anyio。
        if not shutil.which(command):
            return False, f"{name} (stdio) pre-check failed: command not found in PATH: {command}"
        raw_args = server_payload.get("args") or []
        if isinstance(raw_args, list):
            for arg in raw_args:
                if not isinstance(arg, str):
                    continue
                looks_like_path = (
                    arg.startswith(("/", "./", "../", "~"))
                    or arg.endswith((".js", ".mjs", ".cjs", ".json", ".py", ".sh"))
                )
                if looks_like_path and not Path(arg).expanduser().exists():
                    return False, f"{name} (stdio) pre-check failed: file not found: {arg}"
        return True, f"{name} (stdio) pre-check passed (static)"
    else:
        url = server_payload.get("url", "")
        if not url:
            return True, "skipped: no url"
        payload["server_path"] = url
        params = {}
        if isinstance(server_payload.get("headers"), dict):
            params["headers"] = {str(k): str(v) for k, v in server_payload["headers"].items()}
        if params:
            payload["params"] = params

    cfg = McpServerConfig(**payload)
    client = ToolMgr._create_client(cfg)
    _logging.disable(_logging.CRITICAL)
    try:
        connected = await asyncio.wait_for(client.connect(), timeout=15.0)
        if not connected:
            return False, f"{name} ({transport}) pre-check failed: connection refused"
        return True, f"{name} ({transport}) pre-check passed"
    except asyncio.TimeoutError:
        return False, f"{name} ({transport}) pre-check failed: connection timed out"
    except Exception as exc:
        return False, f"{name} ({transport}) pre-check failed: {exc}"
    finally:
        _logging.disable(_logging.NOTSET)
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            # disconnect runs anyio task-group teardown which raises an
            # ExceptionGroup (Exception subclass) on SSE/HTTP streamable
            # clients. The pre-check already returns a (ok, msg) tuple, so
            # just swallow the disconnect error.
            logger.debug(
                "[mcp] pre-check disconnect for %s failed: %r", name, exc,
            )

async def _pre_check_mcp_http_auth(
    server_payload: dict[str, Any]
) -> tuple[bool, str]:
    """Config-time HTTP probe: reject bad auth (401/403), timeouts, and
    unreachable hosts before writing config.yaml. Delegates to
    ``preflight_mcp_server_reachable`` (shared with cold-start) so both
    gates stay identical. See that function for the anyio-corruption
    rationale.
    """
    from jiuwenswarm.common.mcp_config import (
        build_mcp_server_config,
        preflight_mcp_server_reachable,
    )

    name = str(server_payload.get("name", "") or "").strip()
    transport = str(server_payload.get("transport", "") or "").strip().lower()
    cfg = build_mcp_server_config(server_payload, server_id_scope="jiuwenswarm")
    if cfg is None:
        return False, f"{name} ({transport}) pre-check failed: invalid config entry"
    ok, reason = await preflight_mcp_server_reachable(cfg)
    if ok:
        return True, f"{name} ({transport}) pre-check passed: {reason}"
    return False, f"{name} ({transport}) pre-check failed: {reason}"

async def _fetch_mcp_tools_from_config(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Create a temporary MCP connection from config entry and list tools.

    Thin delegate to the shared :func:`fetch_mcp_tools_via_temp_connection`
    so the gateway-layer ``mcp.show`` and the agent-layer command.mcp paths
    use one implementation (placeholder resolve, anyio exception-group
    coercion, disconnect cleanup all live in one place)."""
    from jiuwenswarm.common.mcp_config import fetch_mcp_tools_via_temp_connection
    return await fetch_mcp_tools_via_temp_connection(entry)

def _normalize_mcp_payload(
        params: dict[str, Any], current: dict[str, Any] | None = None
) -> dict[str, Any]:
    merged = dict(current or {})
    merged.update(params)
    name = str(merged.get("name", "")).strip()
    transport = str(merged.get("transport", "")).strip().lower()
    if not name:
        raise ValueError("MCP server name is required")
    if transport not in {"stdio", "sse", "http", "streamable-http", "streamable_http"}:
        raise ValueError("transport must be one of stdio|sse|http")

    payload: dict[str, Any] = {
        "name": name,
        "enabled": bool(merged.get("enabled", True)),
        "transport": transport,
    }
    if transport == "stdio":
        command = str(merged.get("command", "")).strip()
        if not command:
            raise ValueError("stdio transport requires command")
        payload["command"] = command
        args = merged.get("args")
        if isinstance(args, list):
            payload["args"] = [str(item) for item in args]
        else:
            # openjiuwen's StdioServerParameters requires args as a list;
            # None fails validation. Default [] for a bare command.
            payload["args"] = []
        cwd = merged.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            payload["cwd"] = cwd.strip()
        env = merged.get("env")
        if isinstance(env, dict):
            payload["env"] = {str(k): str(v) for k, v in env.items()}
    else:
        url = str(merged.get("url", "")).strip()
        if not url:
            raise ValueError(f"{transport} transport requires url")
        payload["url"] = url
        headers = merged.get("headers")
        if isinstance(headers, dict):
            payload["headers"] = {str(k): str(v) for k, v in headers.items()}
        timeout_s = merged.get("timeout_s")
        if isinstance(timeout_s, (int, float)):
            payload["timeout_s"] = int(timeout_s)
    return payload

def _normalize_mcp_add_payload(params: dict[str, Any]) -> dict[str, Any]:
    return _normalize_mcp_payload(params)

def _normalize_mcp_update_payload(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    if not name:
        raise ValueError("MCP server name is required")
    current = get_mcp_server_config(name)
    if current is None:
        raise KeyError(f"MCP server '{name}' not found")
    return _normalize_mcp_payload(params, current=current)

async def _persist_and_probe_mcp(
    services,
    request: AgentRequest,
    payload: dict[str, Any],
    old_item: dict[str, Any] | None,
    types: McpUpsertTypes,
    item: dict[str, Any] | None = None,
) -> AgentResponse:
    """Shared add/update persistence + live-connect probe.

    Unchanged re-upsert stays ``connected`` (no probe, no reload). A
    changed/new MCP is written ``connecting``, then probed before reload:
    on failure roll back and return ``types.fail`` without reloading;
    on success flip to ``connected`` and reload. config.yaml legacy stock
    skips the probe (no connection_state, state.json-only rollback).
    """
    name = str(payload.get("name", "") or "").strip()
    # old_item may carry internal fields (e.g. server_id_scope) the
    # normalized payload never emits — diff only the payload's fields,
    # else a state.json MCP always looks changed and gets needlessly probed.
    if old_item is None:
        config_changed = True
    else:
        old_relevant = {k: old_item[k] for k in payload if k in old_item}
        config_changed = old_relevant != payload
    if not config_changed:
        logger.info("[command.mcp] add/update skipped reload: '%s' config unchanged", name)
    # config.yaml legacy stock has no connection_state and no state.json
    # rollback target — skip the probe; keep the direct write+reload path.
    is_config_yaml = any(
        str(s.get("name", "")).strip() == name
        for s in get_config_yaml_mcp_servers()
    )
    upsert_mcp_server(
        payload,
        state="connected" if is_config_yaml
        else ("connecting" if config_changed else "connected"),
    )
    if not config_changed:
        resp_payload: dict[str, Any] = {
            "type": "updated", "name": payload["name"], "applied": True,
        }
        if item is not None:
            resp_payload["item"] = item
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=resp_payload,
        )
    if is_config_yaml:
        probe_ok, probe_reason = True, ""
    else:
        probe_ok, probe_reason = (
            await services.agent_manager.probe_mcp_live_connection(name)
            if name else (True, "")
        )
    if not probe_ok:
        logger.warning("[command.mcp] live-probe failed: %s", probe_reason)
        try:
            from jiuwenswarm.server.runtime.mcp.registry import (
                rollback_failed_connect,
            )
            rollback_failed_connect(name)
        except Exception as rollback_exc:  # noqa: BLE001
            logger.warning("[command.mcp] rollback '%s' failed: %s", name, rollback_exc)
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "type": types.fail,
                "name": payload["name"],
                "error": probe_reason or "MCP live-connect probe failed",
                "code": "MCP_UNREACHABLE",
            },
        )
    if not is_config_yaml:
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import set_mcp_state
            set_mcp_state(name, state="connected")
        except Exception as state_exc:  # noqa: BLE001
            logger.warning("[command.mcp] flip '%s' to connected failed: %s", name, state_exc)
    applied = True
    error_message = ""
    try:
        await services.agent_manager.reload_agents_config(get_config(), None)
    except Exception as reload_exc:  # noqa: BLE001
        applied = False
        error_message = str(reload_exc)
        logger.warning("[command.mcp] reload after %s failed: %s", types.fail, reload_exc)
    resp_payload = {
        "type": types.ok,
        "name": payload["name"],
        "applied": applied,
    }
    if error_message:
        resp_payload["error"] = error_message
    if item is not None:
        resp_payload["item"] = item
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=resp_payload,
    )

async def handle_command_mcp(ctx: RequestContext) -> None:
    request = ctx.request
    services = ctx.services
    try:
        params = request.params or {}
        action = str(params.get("action", "list")).strip().lower()

        if action == "list":
            items = [_mask_sensitive_fields(item) for item in get_mcp_servers()]
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "type": "list",
                    "items": items,
                    **({"cache": items.cache} if hasattr(items, "cache") else {}),
                },
            )
        elif action == "show":
            name = str(params.get("name", "")).strip()
            if name:
                item = get_mcp_server_config(name)
                if item is None:
                    raise KeyError(f"MCP server '{name}' not found")
                masked = _mask_sensitive_fields(item)
                # Enrich with tool count
                tool_count = 0
                try:
                    from openjiuwen.core.runner import Runner
                    resource_registry = getattr(Runner.resource_mgr, "_resource_registry", None)
                    if resource_registry is not None:
                        tool_mgr = resource_registry.tool()
                        server_ids = tool_mgr.get_mcp_server_ids(name)
                        if not server_ids:
                            for sid, res in getattr(tool_mgr, "_mcp_server_resources", {}).items():
                                if getattr(res.config, "server_name", "") == name:
                                    server_ids.append(sid)
                        _seen: set[str] = set()
                        for sid in server_ids:
                            for _tid in tool_mgr.get_mcp_tool_ids(sid):
                                _t = getattr(tool_mgr, "_tools", {}).get(_tid)
                                if _t is not None and hasattr(_t, "card"):
                                    _n = _t.card.name
                                    if _n not in _seen:
                                        _seen.add(_n)
                                        tool_count += 1
                except Exception as exc:
                    logger.debug("[command.mcp] show tool_count from ToolMgr failed: %s", exc)
                # If ToolMgr has no data, try temporary connection
                if tool_count == 0 and bool(item.get("enabled", True)):
                    try:
                        tools = await _fetch_mcp_tools_from_config(item)
                        tool_count = len(tools)
                    except Exception as exc:
                        logger.warning("[command.mcp] show tool_count from temp connection failed: %s", exc)
                masked["tool_count"] = tool_count
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "detail", "item": masked},
                )
            else:
                enabled_items = [
                    _mask_sensitive_fields(item)
                    for item in get_mcp_servers()
                    if bool(item.get("enabled", True))
                ]
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "list", "items": enabled_items},
                )
        elif action == "add":
            server_payload = _normalize_mcp_add_payload(params)

            # Reject a broken MCP entry before it is persisted to config.yaml;
            # a bad entry (e.g. wrong Bearer → HTTP 401) surviving to
            # cold-start corrupts the anyio task group ("restart then can't
            # chat" symptom).
            pre_check_failed = False
            if bool(server_payload.get("enabled", True)):
                _need_pre_check = False
                _transport = str(server_payload.get("transport", "") or "").strip().lower()
                if _transport == "stdio":
                    # Static-only probe (zero spawn): verify command in PATH
                    # and arg paths exist, catching typos like "pyhton".
                    _need_pre_check = bool(server_payload.get("command", ""))
                elif _transport in ("sse", "http", "streamable-http", "streamable_http"):
                    # HTTP-family always probed via pure httpx (never
                    # client.connect(), which leaks on 401/timeout).
                    _need_pre_check = True
                if _need_pre_check:
                    if _transport == "stdio":
                        check_ok, check_msg = await _pre_check_mcp_server(server_payload)
                    else:
                        check_ok, check_msg = await _pre_check_mcp_http_auth(server_payload)
                    if not check_ok:
                        logger.warning("[command.mcp] add pre-check failed: %s", check_msg)
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={
                                "type": "add_failed",
                                "name": server_payload["name"],
                                "error": check_msg,
                            },
                        )
                        pre_check_failed = True
                    else:
                        logger.info("[command.mcp] add pre-check ok: %s", check_msg)

            if not pre_check_failed:
                name = server_payload.get("name", "")
                old_item = get_mcp_server_config(name) if name else None
                resp = await _persist_and_probe_mcp(services, 
                    request, server_payload, old_item,
                    types=McpUpsertTypes(ok="added", fail="add_failed"),
                )
        elif action in {"enable", "disable"}:
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("MCP server name is required")
            enabled = action == "enable"

            # 读取旧状态以判断 enabled 是否真的变化（容忍读取失败/不存在，
            # 此时回退为"按变化处理"，由 set_mcp_server_enabled 自己
            # 校验存在性并在缺失时抛 KeyError 交外层统一处理）。
            old_enabled = None
            try:
                old_item = get_mcp_server_config(name)
                if old_item is not None:
                    old_enabled = bool(old_item.get("enabled", True))
            except Exception:  # noqa: BLE001
                old_enabled = None

            # set_mcp_server_enabled 双查(state.json 优先，兜底 config.yaml)，
            # 在 server 不存在时抛 KeyError，由外层统一返回 MCP_NOT_FOUND。
            item = set_mcp_server_enabled(name, enabled)

            # 只有 enabled 状态真的改变才需要 reload；无法判断旧状态时保守 reload。
            config_changed = (old_enabled is None) or (old_enabled != enabled)
            if not config_changed:
                logger.info(
                    "[command.mcp] %s skipped reload: '%s' already %s",
                    action, name, "enabled" if enabled else "disabled",
                )

            applied = True
            error_message = ""
            if config_changed:
                try:
                    await services.agent_manager.reload_agents_config(get_config(), None)
                except Exception as reload_exc:  # noqa: BLE001
                    applied = False
                    error_message = str(reload_exc)
                    logger.warning("[command.mcp] reload after %s failed: %s", action, reload_exc)

            payload = {
                "type": "enabled" if enabled else "disabled",
                "name": name,
                "applied": applied,
                "item": _mask_sensitive_fields(item),
            }
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        elif action in {"remove", "delete"}:
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("MCP server name is required")
            # remove_mcp_server 双查(state.json 优先，兜底 config.yaml)，
            # 在 server 不存在时抛 KeyError，由外层统一返回 MCP_NOT_FOUND，
            # 且不会触发 reload（删除不存在 = 无变化）。
            removed = remove_mcp_server(name)
            applied = True
            error_message = ""
            try:
                await services.agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:  # noqa: BLE001
                applied = False
                error_message = str(reload_exc)
                logger.warning("[command.mcp] reload after remove failed: %s", reload_exc)
            payload = {
                "type": "removed",
                "name": name,
                "applied": applied,
                "item": _mask_sensitive_fields(removed),
            }
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        elif action == "update":
            normalized = _normalize_mcp_update_payload(params)

            # Same config-time pre-check as add: reject before upserting
            # (avoid cold-start 401 → anyio corruption). Kept from develop's
            # HTTP/stdio pre-check enhancement; the upsert target switched
            # to upsert_mcp_server (state.json-routing) to match the
            # dynamic-loading MCP feature on the feature branch.
            pre_check_failed = False
            if bool(normalized.get("enabled", True)):
                _transport = str(normalized.get("transport", "") or "").strip().lower()
                if _transport in ("sse", "http", "streamable-http", "streamable_http"):
                    check_ok, check_msg = await _pre_check_mcp_http_auth(normalized)
                elif _transport == "stdio":
                    check_ok, check_msg = await _pre_check_mcp_server(normalized)
                else:
                    check_ok, check_msg = True, "skipped"
                if not check_ok:
                    logger.warning("[command.mcp] update pre-check failed: %s", check_msg)
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={
                            "type": "update_failed",
                            "name": normalized["name"],
                            "error": check_msg,
                        },
                    )
                    pre_check_failed = True

            if not pre_check_failed:
                upd_name = normalized.get("name", "")
                old_item = get_mcp_server_config(upd_name) if upd_name else None
                resp = await _persist_and_probe_mcp(services, 
                    request, normalized, old_item,
                    types=McpUpsertTypes(ok="updated", fail="update_failed"),
                    item=_mask_sensitive_fields(normalized),
                )
        elif action == "list_tools":
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("MCP server name is required")
            tools_info: list[dict[str, Any]] = []
            # 1) Try from ToolMgr (already registered)
            try:
                from openjiuwen.core.runner import Runner
                resource_registry = getattr(Runner.resource_mgr, "_resource_registry", None)
                if resource_registry is not None:
                    tool_mgr = resource_registry.tool()
                    server_ids = list(tool_mgr.get_mcp_server_ids(name))
                    if not server_ids:
                        for sid, res in getattr(tool_mgr, "_mcp_server_resources", {}).items():
                            if getattr(res.config, "server_name", "") == name:
                                server_ids.append(sid)
                    seen_tool_names: set[str] = set()
                    for sid in server_ids:
                        tool_ids = tool_mgr.get_mcp_tool_ids(sid)
                        for tid in tool_ids:
                            tool = getattr(tool_mgr, "_tools", {}).get(tid)
                            if tool is not None and hasattr(tool, "card"):
                                card = tool.card
                                if card.name in seen_tool_names:
                                    continue
                                seen_tool_names.add(card.name)
                                params_schema = card.input_params if hasattr(card, "input_params") else {}
                                if hasattr(params_schema, "model_dump"):
                                    params_schema = params_schema.model_dump()
                                tools_info.append({
                                    "id": card.id,
                                    "name": card.name,
                                    "description": card.description or "",
                                    "parameters": params_schema,
                                    "server_name": name,
                                })
            except Exception as exc:
                logger.debug("[command.mcp] list_tools from ToolMgr failed: %s", exc)
            # 2) If no tools found, try temporary MCP connection from config
            if not tools_info:
                try:
                    config_entry = get_mcp_server_config(name)
                    if config_entry and bool(config_entry.get("enabled", True)):
                        tools_info = await _fetch_mcp_tools_from_config(config_entry)
                except Exception as exc:
                    logger.warning("[command.mcp] list_tools from temp connection failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "tools", "tools": tools_info, "server_name": name},
            )
        else:
            raise ValueError("Unsupported action, must be one of " \
                             "list|show|add|update|enable|disable|remove|list_tools")
    except KeyError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "MCP_NOT_FOUND"},
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.mcp failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)
