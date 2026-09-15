# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP 域 handler"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import (
    get_config,
    get_mcp_server_config,
    get_mcp_servers,
    remove_mcp_server_in_config,
    set_mcp_server_enabled_in_config,
    upsert_mcp_server_in_config,
)
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)

# 企业版 MCP 仅由管理端模板下发；本地 /mcp（含 list/show/list_tools）一律禁止，避免与生效配置脱节。
_MCP_ENTERPRISE_FORBIDDEN = (
    "企业版禁止使用本地 /mcp 命令，请在管理端通过 MCP 模板下发与查看。"
)


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


def _mask_sensitive_fields(payload: Any) -> Any:
    if isinstance(payload, dict):
        masked: dict[str, Any] = {}
        for key, value in payload.items():
            key_text = str(key).lower()
            value_text = value.lower() if isinstance(value, str) else ""
            key_sensitive = any(
                token in key_text for token in ("api_key", "token", "authorization", "secret")
            )
            value_sensitive = any(token in value_text for token in ("bearer ", "api-key ", "secret-"))
            if key_sensitive or value_sensitive:
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
        except Exception:  # noqa: BLE001 - asyncio.TimeoutError 亦是 Exception 子类
            pass


async def _fetch_mcp_tools_from_config(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Create a temporary MCP connection from config entry and list tools."""
    from openjiuwen.core.foundation.tool import McpServerConfig
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr
    name = str(entry.get("name", "")).strip()
    transport = str(entry.get("transport", "")).strip().lower()
    if not name or transport not in {"stdio", "sse", "http", "streamable-http", "streamable_http"}:
        logger.warning("[command.mcp] _fetch skipped: name=%r transport=%r", name, transport)
        return []
    # Build McpServerConfig same as interface_deep._build_mcp_server_config
    payload: dict[str, Any] = {"server_name": name, "client_type": transport}
    if transport == "stdio":
        command = str(entry.get("command", "")).strip()
        if not command:
            logger.warning("[command.mcp] _fetch skipped: no command for stdio")
            return []
        params: dict[str, Any] = {"command": command}
        if isinstance(entry.get("args"), list):
            params["args"] = [str(x) for x in entry["args"]]
        if isinstance(entry.get("cwd"), str) and entry["cwd"].strip():
            params["cwd"] = entry["cwd"].strip()
        if isinstance(entry.get("env"), dict):
            params["env"] = {str(k): str(v) for k, v in entry["env"].items()}
        payload["server_path"] = f"stdio://{name}"
        payload["params"] = params
    else:
        url = str(entry.get("url", "")).strip()
        if not url:
            logger.warning("[command.mcp] _fetch skipped: no url for sse")
            return []
        payload["server_path"] = url
        params = {}
        if isinstance(entry.get("headers"), dict):
            params["headers"] = {str(k): str(v) for k, v in entry["headers"].items()}
        if params:
            payload["params"] = params
    cfg = McpServerConfig(**payload)
    client = ToolMgr._create_client(cfg)
    try:
        connected = await client.connect()
        if not connected:
            return []
        cards = await client.list_tools()
        tools_info = []
        for card in (cards or []):
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
        return tools_info
    finally:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.warning("[command.mcp] _fetch disconnect failed: %s", exc)


def _normalize_mcp_add_payload(ctx, params: dict[str, Any]) -> dict[str, Any]:
    return _normalize_mcp_payload(params)


def _normalize_mcp_update_payload(ctx, params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    if not name:
        raise ValueError("MCP server name is required")
    current = get_mcp_server_config(name)
    if current is None:
        raise KeyError(f"MCP server '{name}' not found")
    return _normalize_mcp_payload(params, current=current)


async def handle_command_mcp(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        params = request.params or {}
        action = str(params.get("action", "list")).strip().lower()

        if is_enterprise():
            logger.info(
                "[command.mcp] reject local mcp command in enterprise: action=%s",
                action,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": _MCP_ENTERPRISE_FORBIDDEN,
                    "code": "MCP_FORBIDDEN",
                    "action": action,
                },
            )
        elif action == "list":
            items = [_mask_sensitive_fields(item) for item in get_mcp_servers()]
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "list", "items": items},
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
            server_payload = _normalize_mcp_add_payload(ctx, params)

            # Pre-check: only validate when stdio args contain a local file
            # path (e.g. "node /path/to/server.js").  Skip for package
            # managers like npx which may need to download first.
            pre_check_failed = False
            if bool(server_payload.get("enabled", True)):
                _need_pre_check = False
                if server_payload.get("transport") == "stdio":
                    _args = server_payload.get("args")
                    if isinstance(_args, list):
                        _need_pre_check = any(
                            isinstance(a, str)
                            and (a.startswith(("/", "./", "../"))
                                 or a.endswith((".js", ".mjs", ".json", ".py")))
                            for a in _args
                        )
                if _need_pre_check:
                    check_ok, check_msg = await _pre_check_mcp_server(server_payload)
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
                # 对于 update，先读旧配置，判断是否真有变化
                name = server_payload.get("name", "")
                old_item = get_mcp_server_config(name) if name else None

                _, created = upsert_mcp_server_in_config(server_payload)
                applied = True
                error_message = ""

                # 判断是否需要 reload: 新增必然需要；更新时做完整比较，
                # 配置完全一致才跳过（dict 比较成本极低，避免漏字段导致改了不生效）。
                config_changed = created
                if not created and old_item is not None:
                    config_changed = (dict(old_item) != dict(server_payload))
                    if not config_changed:
                        logger.info(
                            "[command.mcp] add/update skipped reload: '%s' config unchanged", name
                        )

                if config_changed:
                    try:
                        await ctx.services.agent_manager.reload_agents_config(get_config(), None)
                    except Exception as reload_exc:  # noqa: BLE001
                        applied = False
                        error_message = str(reload_exc)
                        logger.warning("[command.mcp] reload after add failed: %s", reload_exc)

                resp_payload: dict[str, Any] = {
                    "type": "added" if created else "updated",
                    "name": server_payload["name"],
                    "applied": applied,
                }
                if error_message:
                    resp_payload["error"] = error_message
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=resp_payload,
                )
        elif action in {"enable", "disable"}:
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("MCP server name is required")
            enabled = action == "enable"

            # 读取旧状态以判断 enabled 是否真的变化（容忍读取失败/不存在，
            # 此时回退为"按变化处理"，由 set_mcp_server_enabled_in_config 自己
            # 校验存在性并在缺失时抛 KeyError 交外层统一处理）。
            old_enabled = None
            try:
                old_item = get_mcp_server_config(name)
                if old_item is not None:
                    old_enabled = bool(old_item.get("enabled", True))
            except Exception:  # noqa: BLE001
                old_enabled = None

            # set_mcp_server_enabled_in_config 在 server 不存在时抛 KeyError，
            # 由外层统一返回 MCP_NOT_FOUND。
            item = set_mcp_server_enabled_in_config(name, enabled)

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
                    await ctx.services.agent_manager.reload_agents_config(get_config(), None)
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
            # remove_mcp_server_in_config 在 server 不存在时抛 KeyError，
            # 由外层统一返回 MCP_NOT_FOUND，且不会触发 reload（删除不存在 = 无变化）。
            removed = remove_mcp_server_in_config(name)
            applied = True
            error_message = ""
            try:
                await ctx.services.agent_manager.reload_agents_config(get_config(), None)
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
            normalized = _normalize_mcp_update_payload(ctx, params)
            _, _created = upsert_mcp_server_in_config(normalized)
            applied = True
            error_message = ""
            try:
                await ctx.services.agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:  # noqa: BLE001
                applied = False
                error_message = str(reload_exc)
                logger.warning("[command.mcp] reload after update failed: %s", reload_exc)
            payload = {
                "type": "updated",
                "name": normalized["name"],
                "applied": applied,
                "item": _mask_sensitive_fields(normalized),
            }
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
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
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)
