# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mcp management; resources remain owned by its injected host."""

from __future__ import annotations

import os
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.server.ws_send import send_wire_payload

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
from typing import NamedTuple
from pathlib import Path
import asyncio
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.config import get_config_yaml_mcp_servers
from jiuwenswarm.common.config import get_mcp_server_config
from jiuwenswarm.common.config import get_mcp_servers
from jiuwenswarm.common.config import remove_mcp_server
from jiuwenswarm.common.config import set_mcp_server_enabled
import shutil
from jiuwenswarm.common.config import upsert_mcp_server

logger = logging.getLogger(__name__)


class McpUpsertTypes(NamedTuple):
    """Add/update response ``type`` pair, grouped to satisfy the arg-count lint."""

    ok: str
    fail: str


_MCP_KEY_SENSITIVE_SUBSTRINGS = frozenset(
    {
        "api_key",
        "access_key",
        "secret_key",
        "project_id",
        "auth_code",
        "auth_token",
        "amap_key",
        "map_ak",
        "token",
        "authorization",
        "secret",
    }
)


class McpManagement:
    def __init__(self, host):
        self.host = host

    @staticmethod
    def _mask_sensitive_fields(payload: Any) -> Any:
        if isinstance(payload, dict):
            masked: dict[str, Any] = {}
            for key, value in payload.items():
                key_text = str(key).lower()
                value_text = value.lower() if isinstance(value, str) else ""
                key_sensitive = any(
                    (t in key_text for t in _MCP_KEY_SENSITIVE_SUBSTRINGS)
                )
                value_sensitive = any(
                    (t in value_text for t in ("bearer ", "api-key ", "secret-"))
                )
                if (key_sensitive or value_sensitive) and (
                    not isinstance(value, (dict, list))
                ):
                    masked[key] = "***"
                else:
                    masked[key] = McpManagement._mask_sensitive_fields(value)
            return masked
        if isinstance(payload, list):
            return [McpManagement._mask_sensitive_fields(item) for item in payload]
        return payload

    @staticmethod
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
        payload: dict[str, Any] = {"server_name": name, "client_type": transport}
        if transport == "stdio":
            from jiuwenswarm.common.windows_cli_paths import resolve_preserved_cli_path

            command = resolve_preserved_cli_path(server_payload.get("command", ""))
            if not command:
                return True, "skipped: no command"
            from jiuwenswarm.common.mcp_config import resolve_node_package_runner
            command, _ = resolve_node_package_runner(command, [])
            # stdio 预检查改为纯静态校验,静态校验零 spawn、零 anyio。
            if not shutil.which(command):
                return (
                    False,
                    f"{name} (stdio) pre-check failed: command not found in PATH: {command}",
                )
            raw_args = server_payload.get("args") or []
            if isinstance(raw_args, list):
                for arg in raw_args:
                    if not isinstance(arg, str):
                        continue
                    arg = resolve_preserved_cli_path(arg)
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
                return (True, "skipped: no url")
            payload["server_path"] = url
            params = {}
            if isinstance(server_payload.get("headers"), dict):
                params["headers"] = {
                    str(k): str(v) for k, v in server_payload["headers"].items()
                }
            if params:
                payload["params"] = params
        cfg = McpServerConfig(**payload)
        # ToolMgr ships in openjiuwen and offers no public constructor for a client.
        client = ToolMgr._create_client(cfg)  # pylint: disable=protected-access
        _logging.disable(_logging.CRITICAL)
        try:
            connected = await asyncio.wait_for(client.connect(), timeout=15.0)
            if not connected:
                return (
                    False,
                    f"{name} ({transport}) pre-check failed: connection refused",
                )
            return (True, f"{name} ({transport}) pre-check passed")
        except asyncio.TimeoutError:
            return (
                False,
                f"{name} ({transport}) pre-check failed: connection timed out",
            )
        except Exception as exc:
            return (False, f"{name} ({transport}) pre-check failed: {exc}")
        finally:
            _logging.disable(_logging.NOTSET)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
            except Exception as exc:
                logger.debug("[mcp] pre-check disconnect for %s failed: %r", name, exc)

    @staticmethod
    async def _pre_check_mcp_http_auth(
        server_payload: dict[str, Any],
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
            return (
                False,
                f"{name} ({transport}) pre-check failed: invalid config entry",
            )
        ok, reason = await preflight_mcp_server_reachable(cfg)
        if ok:
            return (True, f"{name} ({transport}) pre-check passed: {reason}")
        return (False, f"{name} ({transport}) pre-check failed: {reason}")

    @staticmethod
    async def _fetch_mcp_tools_from_config(
        entry: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Create a temporary MCP connection from config entry and list tools.

        Thin delegate to the shared :func:`fetch_mcp_tools_via_temp_connection`
        so the gateway-layer ``mcp.show`` and the agent-layer command.mcp paths
        use one implementation (placeholder resolve, anyio exception-group
        coercion, disconnect cleanup all live in one place)."""
        from jiuwenswarm.common.mcp_config import fetch_mcp_tools_via_temp_connection

        return await fetch_mcp_tools_via_temp_connection(entry)

    @staticmethod
    def _normalize_mcp_payload(
        params: dict[str, Any], current: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        merged = dict(current or {})
        merged.update(params)
        name = str(merged.get("name", "")).strip()
        transport = str(merged.get("transport", "")).strip().lower()
        if not name:
            raise ValueError("MCP server name is required")
        if transport not in {
            "stdio",
            "sse",
            "http",
            "streamable-http",
            "streamable_http",
        }:
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

    def _normalize_mcp_add_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        host = self.host
        return host._normalize_mcp_payload(params)

    def _normalize_mcp_update_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        host = self.host
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("MCP server name is required")
        current = get_mcp_server_config(name)
        if current is None:
            raise KeyError(f"MCP server '{name}' not found")
        return host._normalize_mcp_payload(params, current=current)

    async def _persist_and_probe_mcp(
        self,
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
        host = self.host
        name = str(payload.get("name", "") or "").strip()
        if old_item is None:
            config_changed = True
        else:
            old_relevant = {k: old_item[k] for k in payload if k in old_item}
            config_changed = old_relevant != payload
        if not config_changed:
            logger.info(
                "[command.mcp] add/update skipped reload: '%s' config unchanged", name
            )
        is_config_yaml = any(
            (
                str(s.get("name", "")).strip() == name
                for s in get_config_yaml_mcp_servers()
            )
        )
        upsert_mcp_server(
            payload,
            state="connected"
            if is_config_yaml
            else "connecting"
            if config_changed
            else "connected",
        )
        if not config_changed:
            resp_payload: dict[str, Any] = {
                "type": "updated",
                "name": payload["name"],
                "applied": True,
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
            probe_ok, probe_reason = (True, "")
        else:
            probe_ok, probe_reason = (
                await host._agent_manager.probe_mcp_live_connection(name)
                if name
                else (True, "")
            )
        if not probe_ok:
            logger.warning("[command.mcp] live-probe failed: %s", probe_reason)
            try:
                from jiuwenswarm.server.runtime.mcp.registry import (
                    rollback_failed_connect,
                )

                rollback_failed_connect(name)
            except Exception as rollback_exc:
                logger.warning(
                    "[command.mcp] rollback '%s' failed: %s", name, rollback_exc
                )
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
            except Exception as state_exc:
                logger.warning(
                    "[command.mcp] flip '%s' to connected failed: %s", name, state_exc
                )
        applied = True
        error_message = ""
        try:
            await host._agent_manager.reload_agents_config(get_config(), None)
        except Exception as reload_exc:
            applied = False
            error_message = str(reload_exc)
            logger.warning(
                "[command.mcp] reload after %s failed: %s", types.fail, reload_exc
            )
        resp_payload = {"type": types.ok, "name": payload["name"], "applied": applied}
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

    async def _handle_command_mcp(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            params = request.params or {}
            action = str(params.get("action", "list")).strip().lower()

            if action == "oauth_callback":
                # 换码含外部 HTTP 往返（IdP 最长 20s），必须经异步孪生走
                # to_thread，不得在事件循环上同步执行（否则冻结本进程
                # 全部通道的流式帧与心跳）
                resp = await host._command_mcp_oauth_callback(ws, request)
            elif action == "list":
                items = [host._mask_sensitive_fields(item) for item in get_mcp_servers()]
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
                    masked = host._mask_sensitive_fields(item)
                    tool_count = 0
                    try:
                        from openjiuwen.core.runner import Runner

                        resource_registry = getattr(
                            Runner.resource_mgr, "_resource_registry", None
                        )
                        if resource_registry is not None:
                            tool_mgr = resource_registry.tool()
                            server_ids = tool_mgr.get_mcp_server_ids(name)
                            if not server_ids:
                                for sid, res in getattr(
                                    tool_mgr, "_mcp_server_resources", {}
                                ).items():
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
                        logger.debug(
                            "[command.mcp] show tool_count from ToolMgr failed: %s", exc
                        )
                    if tool_count == 0 and bool(item.get("enabled", True)):
                        try:
                            tools = await host._fetch_mcp_tools_from_config(item)
                            tool_count = len(tools)
                        except Exception as exc:
                            logger.warning(
                                "[command.mcp] show tool_count from temp connection failed: %s",
                                exc,
                            )
                    masked["tool_count"] = tool_count
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={"type": "detail", "item": masked},
                    )
                else:
                    enabled_items = [
                        host._mask_sensitive_fields(item)
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
                server_payload = host._normalize_mcp_add_payload(params)
                pre_check_failed = False
                if bool(server_payload.get("enabled", True)):
                    _need_pre_check = False
                    _transport = (
                        str(server_payload.get("transport", "") or "").strip().lower()
                    )
                    if _transport == "stdio":
                        _need_pre_check = bool(server_payload.get("command", ""))
                    elif _transport in (
                        "sse",
                        "http",
                        "streamable-http",
                        "streamable_http",
                    ):
                        _need_pre_check = True
                    if _need_pre_check:
                        if _transport == "stdio":
                            check_ok, check_msg = await host._pre_check_mcp_server(
                                server_payload
                            )
                        else:
                            check_ok, check_msg = await host._pre_check_mcp_http_auth(
                                server_payload
                            )
                        if not check_ok:
                            logger.warning(
                                "[command.mcp] add pre-check failed: %s", check_msg
                            )
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
                    resp = await host._persist_and_probe_mcp(
                        request,
                        server_payload,
                        old_item,
                        types=McpUpsertTypes(ok="added", fail="add_failed"),
                    )
            elif action in {"enable", "disable"}:
                name = str(params.get("name", "")).strip()
                if not name:
                    raise ValueError("MCP server name is required")
                enabled = action == "enable"
                old_enabled = None
                try:
                    old_item = get_mcp_server_config(name)
                    if old_item is not None:
                        old_enabled = bool(old_item.get("enabled", True))
                except Exception:
                    old_enabled = None
                item = set_mcp_server_enabled(name, enabled)
                config_changed = old_enabled is None or old_enabled != enabled
                if not config_changed:
                    logger.info(
                        "[command.mcp] %s skipped reload: '%s' already %s",
                        action,
                        name,
                        "enabled" if enabled else "disabled",
                    )
                applied = True
                error_message = ""
                if config_changed:
                    try:
                        await host._agent_manager.reload_agents_config(
                            get_config(), None
                        )
                    except Exception as reload_exc:
                        applied = False
                        error_message = str(reload_exc)
                        logger.warning(
                            "[command.mcp] reload after %s failed: %s",
                            action,
                            reload_exc,
                        )
                payload = {
                    "type": "enabled" if enabled else "disabled",
                    "name": name,
                    "applied": applied,
                    "item": host._mask_sensitive_fields(item),
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
                removed = remove_mcp_server(name)
                applied = True
                error_message = ""
                try:
                    await host._agent_manager.reload_agents_config(get_config(), None)
                except Exception as reload_exc:
                    applied = False
                    error_message = str(reload_exc)
                    logger.warning(
                        "[command.mcp] reload after remove failed: %s", reload_exc
                    )
                payload = {
                    "type": "removed",
                    "name": name,
                    "applied": applied,
                    "item": host._mask_sensitive_fields(removed),
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
                normalized = host._normalize_mcp_update_payload(params)
                pre_check_failed = False
                if bool(normalized.get("enabled", True)):
                    _transport = (
                        str(normalized.get("transport", "") or "").strip().lower()
                    )
                    if _transport in (
                        "sse",
                        "http",
                        "streamable-http",
                        "streamable_http",
                    ):
                        check_ok, check_msg = await host._pre_check_mcp_http_auth(
                            normalized
                        )
                    elif _transport == "stdio":
                        check_ok, check_msg = await host._pre_check_mcp_server(
                            normalized
                        )
                    else:
                        check_ok, check_msg = (True, "skipped")
                    if not check_ok:
                        logger.warning(
                            "[command.mcp] update pre-check failed: %s", check_msg
                        )
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
                    resp = await host._persist_and_probe_mcp(
                        request,
                        normalized,
                        old_item,
                        types=McpUpsertTypes(ok="updated", fail="update_failed"),
                        item=host._mask_sensitive_fields(normalized),
                    )
            elif action == "list_tools":
                name = str(params.get("name", "")).strip()
                if not name:
                    raise ValueError("MCP server name is required")
                tools_info: list[dict[str, Any]] = []
                try:
                    from openjiuwen.core.runner import Runner

                    resource_registry = getattr(
                        Runner.resource_mgr, "_resource_registry", None
                    )
                    if resource_registry is not None:
                        tool_mgr = resource_registry.tool()
                        server_ids = list(tool_mgr.get_mcp_server_ids(name))
                        if not server_ids:
                            for sid, res in getattr(
                                tool_mgr, "_mcp_server_resources", {}
                            ).items():
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
                                    params_schema = (
                                        card.input_params
                                        if hasattr(card, "input_params")
                                        else {}
                                    )
                                    if hasattr(params_schema, "model_dump"):
                                        params_schema = params_schema.model_dump()
                                    tools_info.append(
                                        {
                                            "id": card.id,
                                            "name": card.name,
                                            "description": card.description or "",
                                            "parameters": params_schema,
                                            "server_name": name,
                                        }
                                    )
                except Exception as exc:
                    logger.debug(
                        "[command.mcp] list_tools from ToolMgr failed: %s", exc
                    )
                if not tools_info:
                    try:
                        config_entry = get_mcp_server_config(name)
                        if config_entry and bool(config_entry.get("enabled", True)):
                            tools_info = await host._fetch_mcp_tools_from_config(
                                config_entry
                            )
                    except Exception as exc:
                        logger.warning(
                            "[command.mcp] list_tools from temp connection failed: %s",
                            exc,
                        )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "tools", "tools": tools_info, "server_name": name},
                )
            else:
                raise ValueError(
                    "Unsupported action, must be one of list|show|add|update|enable|disable|remove|list_tools"
                )
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
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] command.mcp failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_mcp_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.list`` RPC: list marketplace MCPs (read-only).

        filter: builtin(预置目录) | local(已连接预置 + 全部自定义)。兜底 builtin。
        web 不转发此入口（网关本地已处理），保留只为语义对齐。
        """
        host = self.host
        try:
            from jiuwenswarm.server.runtime.mcp.marketplace import list_mcps_with_hub

            params = request.params or {}
            filter_val = (
                str(params.get("filter") or "builtin").strip().lower() or "builtin"
            )
            if filter_val not in ("builtin", "local"):
                filter_val = "builtin"
            items = await list_mcps_with_hub(
                filter_val,
                cache_mode=params.get("cache_mode"),
                refresh=params.get("refresh") is True,
                query=str(params.get("query") or ""),
            )
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
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] mcp.list failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "internal_error",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_mcp_show(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.show`` RPC: return one MCP detail with tools."""
        host = self.host
        try:
            from jiuwenswarm.server.runtime.mcp.marketplace import show_mcp_with_hub

            params = request.params or {}
            name = str(params.get("id") or params.get("name") or "").strip()
            if not name:
                raise ValueError("mcp id or name is required")
            item = await show_mcp_with_hub(name)
            if item is None:
                raise KeyError(f"mcp '{name}' not found")
            from jiuwenswarm.common.mcp_config import fill_mcp_tools_fallback

            await fill_mcp_tools_fallback(item, name)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "detail", "item": item},
            )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "show_failed",
                    "error": str(exc),
                    "code": "MCP_NOT_FOUND",
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "bad_request",
                    "error": str(exc),
                    "code": "MCP_BAD_REQUEST",
                },
            )
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] mcp.show failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "internal_error",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_mcp_install(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Download, install, and connect a Hub MCP in one interaction."""
        host = self.host
        try:
            from jiuwenswarm.server.runtime.mcp.marketplace import install_hub_mcp
            from jiuwenswarm.server.runtime.mcp.registry import CliConnectError

            asset_id = str((request.params or {}).get("id") or "").strip()
            item = await install_hub_mcp(asset_id)
            name = str(item.get("name") or "").strip()
            try:
                connect = await host._run_mcp_connect_flow(
                    name, rollback_on_probe_failure=False
                )
            except CliConnectError as cli_exc:
                connect = {
                    "type": "connect_failed",
                    "error": str(cli_exc),
                    "code": cli_exc.code,
                    "runtime": cli_exc.runtime,
                    "install_cmd": cli_exc.install_cmd,
                    "name": name,
                }
            except Exception as connect_exc:
                logger.warning(
                    "[AgentWebSocketServer] mcp.install connect step failed for '%s': %s",
                    name,
                    connect_exc,
                )
                connect = {
                    "type": "connect_failed",
                    "error": str(connect_exc),
                    "code": "MCP_INTERNAL",
                    "name": name,
                }
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "installed", "item": item, "connect": connect},
            )
        except (KeyError, ValueError) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "install_failed",
                    "error": str(exc),
                    "code": "MCP_BAD_REQUEST",
                },
            )
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] mcp.install failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "install_failed",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_mcp_uninstall(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Disconnect and remove an installed Hub MCP package."""
        host = self.host
        try:
            from jiuwenswarm.server.runtime.mcp.marketplace import uninstall_hub_mcp

            asset_id = str((request.params or {}).get("id") or "").strip()
            item = await asyncio.to_thread(uninstall_hub_mcp, asset_id)
            name = str(item.get("name") or "").strip()
            applied = True
            error_message = ""
            agent_manager = getattr(host, "_agent_manager", None)
            if item.get("removed", True) and agent_manager is not None and name:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(agent_manager.apply_mcp_change(name, "remove")),
                        timeout=15.0,
                    )
                except asyncio.TimeoutError:
                    applied = False
                    error_message = "MCP unregister timed out (package removed; server will clear on next reload)"
                    logger.warning(
                        "[mcp] apply_mcp_change after Hub uninstall timed out for '%s'",
                        name,
                    )
                except asyncio.CancelledError:
                    logger.warning(
                        "[mcp] apply_mcp_change after Hub uninstall CancelledError for '%s'",
                        name,
                    )
                    raise
                except Exception as reload_exc:
                    applied = False
                    error_message = str(reload_exc)
                    logger.warning(
                        "[mcp] apply_mcp_change after Hub uninstall failed: %s",
                        reload_exc,
                    )
                try:
                    agent_manager.clear_mcp_credentials(name)
                except Exception as clear_exc:
                    logger.warning(
                        "[mcp] clear_mcp_credentials after Hub uninstall failed: %s",
                        clear_exc,
                    )
                try:
                    await agent_manager.refresh_skill_rails()
                except Exception as skill_exc:
                    logger.warning(
                        "[mcp] refresh_skill_rails after Hub uninstall failed: %s",
                        skill_exc,
                    )
            payload = {"type": "uninstalled", "item": item, "applied": applied}
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except (KeyError, ValueError) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "uninstall_failed",
                    "error": str(exc),
                    "code": "MCP_NOT_FOUND",
                },
            )
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] mcp.uninstall failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "uninstall_failed",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _run_mcp_connect_flow(
        self, name: str, *, rollback_on_probe_failure: bool = True
    ) -> dict[str, Any]:
        """Run the shared connect flow and return a frontend payload."""
        host = self.host
        from jiuwenswarm.server.runtime.mcp.registry import (
            connect_mcp,
            was_connect_cancelled,
        )

        item = await asyncio.to_thread(connect_mcp, name)
        if was_connect_cancelled(name):
            logger.info("[mcp] connect '%s' cancelled by user", name)
            return {"type": "cancelled", "name": name}
        if isinstance(item, dict) and item.get("auth_required"):
            return {"type": "auth_required", **host._mask_sensitive_fields(item)}
        if isinstance(item, dict) and item.get("credentials_required"):
            return {"type": "credentials_required", **item}
        probe_ok, _probe_reason = await host._agent_manager.probe_mcp_live_connection(
            name
        )
        if not probe_ok:
            if rollback_on_probe_failure:
                try:
                    from jiuwenswarm.server.runtime.mcp.registry import (
                        rollback_failed_connect,
                    )

                    rollback_failed_connect(name)
                except Exception as rollback_exc:
                    logger.warning(
                        "[mcp] rollback failed-probe entry '%s' failed: %s",
                        name,
                        rollback_exc,
                    )
            return {
                "type": "connect_failed",
                "error": "MCP live-connect probe failed",
                "code": "MCP_UNREACHABLE",
                "name": name,
            }
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import set_mcp_state

            set_mcp_state(name, state="connected")
        except Exception as flip_exc:
            logger.warning(
                "[mcp] flip connecting→connected after connect failed for '%s': %s",
                name,
                flip_exc,
            )
        try:
            host._agent_manager.sync_mcp_credentials()
        except Exception as sync_exc:
            logger.warning(
                "[mcp] sync_mcp_credentials after connect failed: %s", sync_exc
            )
        return {
            "type": "connected",
            "name": name,
            "applied": True,
            "item": host._mask_sensitive_fields(item),
        }

    async def _handle_mcp_connect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.connect``; CLI OAuth may return ``auth_required``."""
        host = self.host
        from jiuwenswarm.server.runtime.mcp.registry import CliConnectError

        try:
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            payload = await host._run_mcp_connect_flow(name)
            if payload.get("type") == "connect_failed":
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload=payload,
                )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=payload,
                )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "connect_failed",
                    "error": str(exc),
                    "code": "MCP_NOT_FOUND",
                },
            )
        except CliConnectError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "connect_failed",
                    "error": str(exc),
                    "code": exc.code,
                    "runtime": exc.runtime,
                    "install_cmd": exc.install_cmd,
                    "name": name,
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "bad_request",
                    "error": str(exc),
                    "code": "MCP_BAD_REQUEST",
                },
            )
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] mcp.connect failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "internal_error",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_mcp_cancel_connect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.cancel_connect``: abort an in-flight connect/auth flow.

        The user may mis-click or want to redo OAuth while a CLI MCP's
        ``mcp.wait_auth`` (or a slow ``mcp.connect``) is still holding the RPC
        open (up to 10 min). This marks the name cancelled so the poller /
        connect flow unwinds with a ``cancelled`` result, kills any pending
        authWaitForExit CLI proc, and rolls back the connecting state.json
        record. Idempotent — safe even when nothing is in flight. Because each
        incoming RPC is dispatched in its own task (the ws receive loop uses
        create_task), this runs concurrently with the hold-open wait_auth.
        """
        host = self.host
        from jiuwenswarm.server.runtime.mcp.registry import cancel_connect

        try:
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            payload = await asyncio.to_thread(cancel_connect, name)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "bad_request",
                    "error": str(exc),
                    "code": "MCP_BAD_REQUEST",
                },
            )
        except Exception as exc:
            logger.exception(
                "[AgentWebSocketServer] mcp.cancel_connect failed: %s", exc
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "internal_error",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_mcp_wait_auth(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.wait_auth`` RPC: hold-open poll for CLI OAuth completion.

        After ``mcp.connect`` returned ``auth_required`` (the frontend opened
        the browser when ``auth_url`` was non-empty), the frontend sends this
        RPC which polls :func:`complete_cli_auth` (via :meth:`_await_cli_auth`)
        until the user finishes OAuth in the browser, then returns the single
        ``connected``/``auth_failed`` response. Holds the RPC open for minutes;
        the frontend shows a "connecting…" spinner while waiting.
        """
        host = self.host
        from jiuwenswarm.server.runtime.mcp.oauth_flow import is_host_oauth_mcp
        from jiuwenswarm.server.runtime.mcp.registry import CliConnectError

        try:
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            step_index = int(params.get("step_index", 0) or 0)
            if is_host_oauth_mcp(name):
                result = await host._await_host_oauth_auth(name)
            else:
                result = await host._await_cli_auth(name, step_index)
            if result.get("type") == "auth_failed":
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={
                        "type": "connect_failed",
                        "name": name,
                        "error": str(result.get("error", "")),
                        "code": "MCP_AUTH_FAILED",
                    },
                )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=result,
                )
        except CliConnectError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "connect_failed",
                    "error": str(exc),
                    "code": exc.code,
                    "runtime": exc.runtime,
                    "install_cmd": exc.install_cmd,
                    "name": name,
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "bad_request",
                    "error": str(exc),
                    "code": "MCP_BAD_REQUEST",
                },
            )
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] mcp.wait_auth failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "internal_error",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_mcp_disconnect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.disconnect`` RPC: remove from config + reload."""
        host = self.host
        try:
            from jiuwenswarm.server.runtime.mcp.registry import disconnect_mcp

            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            removed = await asyncio.to_thread(disconnect_mcp, name)
            applied = True
            error_message = ""
            try:
                await asyncio.wait_for(
                    asyncio.shield(
                        host._agent_manager.apply_mcp_change(name, "remove")
                    ),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                applied = False
                error_message = "MCP unregister timed out (state updated; server will clear on next reload)"
                logger.warning(
                    "[mcp] apply_mcp_change after disconnect timed out for '%s'", name
                )
            except asyncio.CancelledError:
                logger.warning(
                    "[mcp] apply_mcp_change after disconnect CancelledError for '%s'",
                    name,
                )
                raise
            except Exception as reload_exc:
                applied = False
                error_message = str(reload_exc)
                logger.warning(
                    "[mcp] apply_mcp_change after disconnect failed: %s", reload_exc
                )
            try:
                host._agent_manager.clear_mcp_credentials(name)
            except Exception as clear_exc:
                logger.warning(
                    "[mcp] clear_mcp_credentials after disconnect failed: %s", clear_exc
                )
            try:
                await host._agent_manager.refresh_skill_rails()
            except Exception as skill_exc:
                logger.warning(
                    "[mcp] refresh_skill_rails after disconnect failed: %s", skill_exc
                )
            payload = {
                "type": "disconnected",
                "name": name,
                "applied": applied,
                "item": host._mask_sensitive_fields(removed),
            }
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "disconnect_failed",
                    "error": str(exc),
                    "code": "MCP_NOT_FOUND",
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "bad_request",
                    "error": str(exc),
                    "code": "MCP_BAD_REQUEST",
                },
            )
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] mcp.disconnect failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "internal_error",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _finalize_cli_auth(
        self, name: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the post-auth side-effects for a CLI MCP connect.

        Auth is done; skills were installed during registry ``_finalize_cli``.
        Syncs MCP tokens into os.environ so bundled skill scripts (BashTool
        inherits os.environ) see their token env vars, then promotes
        connecting → connected. The MCP is NOT loaded into the agent here —
        session-level enable is driven by the ``mcp`` field on chat.send (see
        reconcile_session_mcp). Returns a payload dict for the push signal.
        """
        host = self.host
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import set_mcp_state

            set_mcp_state(name, state="connected")
        except Exception as flip_exc:
            logger.warning(
                "[mcp] flip connecting→connected after CLI auth failed for '%s': %s",
                name,
                flip_exc,
            )
        try:
            host._agent_manager.sync_mcp_credentials()
        except Exception as sync_exc:
            logger.warning(
                "[mcp] sync_mcp_credentials after CLI auth failed: %s", sync_exc
            )
        return {
            "type": "connected",
            "name": name,
            "applied": True,
            "item": host._mask_sensitive_fields(item),
        }

    async def _await_cli_auth(
        self, name: str, step_index: int, *, max_attempts: int = 200, delay: float = 3.0
    ) -> dict[str, Any]:
        """Poll a CLI MCP's auth status until authenticated, returning the result.

        Called inline by :meth:`_handle_mcp_connect` after ``connect_mcp``
        returns an ``auth_required`` sentinel (the CLI process already opened
        the browser). This holds the RPC open — the frontend shows a
        "connecting…" spinner — and loops :func:`complete_cli_auth` until the
        user finishes OAuth, then runs the post-auth side-effects and returns
        a ``connected`` payload (or ``auth_failed`` on timeout/error) for the
        handler to send as the single RPC response. No push channel needed.

        Multi-step CLIs (e.g. feishu: config init → auth login): when a step
        completes and the next needs user action, ``complete_cli_auth`` returns
        ``auth_required=True`` with the new ``step_index``; we adopt it as
        ``cur_step`` so the next poll queries the new step — otherwise the
        poller would re-query the old step forever (a real dead-loop on
        multi-step CLIs).

        Only CLI MCPs reach here (form A/B/D return ``auth_required=False`` or
        ``credentials_required`` from :func:`connect_mcp`; only
        ``itype == "cli"`` calls ``_connect_cli`` which can return the
        ``auth_required`` sentinel). No form A/B/D path triggers this loop.
        """
        host = self.host
        from jiuwenswarm.server.runtime.mcp.registry import (
            CliConnectError,
            complete_cli_auth,
            was_connect_cancelled,
        )

        cur_step = max(0, int(step_index))
        last_output = ""
        try:
            for attempt in range(max_attempts):
                if was_connect_cancelled(name):
                    logger.info("[mcp] _await_cli_auth '%s' cancelled by user", name)
                    return {"type": "cancelled", "name": name}
                item = await asyncio.to_thread(complete_cli_auth, name, cur_step)
                if not isinstance(item, dict):
                    raise ValueError(f"complete_cli_auth returned non-dict: {item!r}")
                if item.get("auth_required"):
                    new_step = item.get("step_index")
                    if isinstance(new_step, (int, float)):
                        cur_step = max(cur_step, int(new_step))
                    last_output = str(item.get("output") or item.get("matched") or "")[
                        :300
                    ]
                    logger.debug(
                        "[mcp] _await_cli_auth '%s' still pending (attempt %d/%d, step %d): %s",
                        name,
                        attempt + 1,
                        max_attempts,
                        cur_step,
                        last_output[:200],
                    )
                    await asyncio.sleep(delay)
                    continue
                if was_connect_cancelled(name):
                    logger.info(
                        "[mcp] _await_cli_auth '%s' cancelled after auth completed",
                        name,
                    )
                    return {"type": "cancelled", "name": name}
                return await host._finalize_cli_auth(name, item)
            logger.warning(
                "[mcp] _await_cli_auth '%s' timed out after %d attempts (~%.0f min). last status output: %s",
                name,
                max_attempts,
                max_attempts * delay / 60,
                last_output,
            )
            timeout_error = "Authorization timed out. Retry connect."
            if last_output:
                timeout_error = f"{timeout_error} (last status: {last_output})"
            return {"type": "auth_failed", "name": name, "error": timeout_error}
        except CliConnectError:
            raise
        except Exception as exc:
            logger.exception(
                "[AgentWebSocketServer] _await_cli_auth '%s' failed: %s", name, exc
            )
            return {"type": "auth_failed", "name": name, "error": str(exc)}

    async def _handle_mcp_register_custom(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.register_custom`` RPC: write a user-defined MCP server config.

        Only persists the definition to state.json (state=registered) and returns
        immediately; the frontend follows up with ``mcp.connect`` to actually
        activate it. Editing an already-connected custom MCP removes the old live
        instance so the new config takes effect on connect.
        """
        host = self.host
        from jiuwenswarm.server.runtime.mcp.registry import McpRegistryError

        try:
            from jiuwenswarm.server.runtime.mcp.registry import register_custom_mcp

            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            config = {k: v for k, v in params.items() if k != "name"}
            entry = await asyncio.to_thread(register_custom_mcp, name, config)
            was_connected = bool(entry.pop("was_connected", False))
            if was_connected:
                try:
                    await host._agent_manager.apply_mcp_change(name, "remove")
                except Exception as reload_exc:
                    logger.warning(
                        "[mcp] remove old instance after register_custom '%s' failed: %s",
                        name,
                        reload_exc,
                    )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "type": "registered",
                    "name": name,
                    "item": host._mask_sensitive_fields(entry),
                },
            )
        except McpRegistryError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "bad_request",
                    "error": str(exc),
                    "code": exc.code,
                    "name": name,
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "bad_request",
                    "error": str(exc),
                    "code": "MCP_BAD_REQUEST",
                },
            )
        except Exception as exc:
            logger.exception(
                "[AgentWebSocketServer] mcp.register_custom failed: %s", exc
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "internal_error",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_mcp_delete_custom(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.delete_custom``: terminal teardown of a custom MCP."""
        host = self.host
        try:
            from jiuwenswarm.server.runtime.mcp.registry import delete_custom_mcp

            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            try:
                host._agent_manager.clear_mcp_credentials(name)
            except Exception as clear_exc:
                logger.warning(
                    "[mcp] clear_mcp_credentials before delete failed: %s", clear_exc
                )
            removed = await asyncio.to_thread(delete_custom_mcp, name)
            was_connected = bool(removed.get("was_connected", False))
            applied = True
            error_message = ""
            if was_connected:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(
                            host._agent_manager.apply_mcp_change(name, "remove")
                        ),
                        timeout=15.0,
                    )
                except asyncio.TimeoutError:
                    applied = False
                    error_message = "MCP unregister timed out (state deleted; server will clear on next reload)"
                    logger.warning(
                        "[mcp] apply_mcp_change after delete timed out for '%s'", name
                    )
                except asyncio.CancelledError:
                    logger.warning(
                        "[mcp] apply_mcp_change after delete CancelledError for '%s'",
                        name,
                    )
                    raise
                except Exception as reload_exc:
                    applied = False
                    error_message = str(reload_exc)
                    logger.warning(
                        "[mcp] apply_mcp_change after delete failed: %s", reload_exc
                    )
            try:
                await host._agent_manager.refresh_skill_rails()
            except Exception as skill_exc:
                logger.warning(
                    "[mcp] refresh_skill_rails after delete failed: %s", skill_exc
                )
            payload = {
                "type": "deleted",
                "name": name,
                "applied": applied,
                "item": host._mask_sensitive_fields(removed),
            }
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "delete_failed",
                    "error": str(exc),
                    "code": "MCP_NOT_FOUND",
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "bad_request",
                    "error": str(exc),
                    "code": "MCP_BAD_REQUEST",
                },
            )
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] mcp.delete_custom failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "internal_error",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_mcp_save_credentials(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.save_credentials`` RPC: persist user-supplied tokens.

        Form B connectors (tianyancha/gildata/gmail/jira...) declare ``${VAR}``
        placeholders in mcp.json; the user fills the values via a frontend
        prompt, which calls this RPC. Tokens are stored in the local
        CredentialStore (never returned in the response). Does NOT reload —
        the caller follows up with ``mcp.connect`` to actually register.
        """
        host = self.host
        try:
            from jiuwenswarm.server.runtime.mcp.registry import save_mcp_credentials

            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            tokens = params.get("tokens") or {}
            if not isinstance(tokens, dict) or not tokens:
                raise ValueError("tokens (non-empty dict) is required")
            result = await asyncio.to_thread(save_mcp_credentials, name, tokens)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "type": "credentials_saved",
                    "name": name,
                    "saved_keys": result.get("saved_keys", []),
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "bad_request",
                    "error": str(exc),
                    "code": "MCP_BAD_REQUEST",
                },
            )
        except Exception as exc:
            logger.exception(
                "[AgentWebSocketServer] mcp.save_credentials failed: %s", exc
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "internal_error",
                    "error": str(exc),
                    "code": "MCP_INTERNAL",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _await_host_oauth_auth(
        self, name: str, *, max_attempts: int = 200, delay: float = 1.0
    ) -> dict[str, Any]:
        """Poll locally stored OAuth credentials until the loopback callback lands."""
        host = self.host
        from jiuwenswarm.server.runtime.mcp.oauth_flow import load_oauth_credentials
        from jiuwenswarm.server.runtime.mcp.registry import (
            complete_host_oauth_auth,
            was_connect_cancelled,
        )

        for _attempt in range(max_attempts):
            if was_connect_cancelled(name):
                logger.info("[mcp] host OAuth '%s' cancelled by user", name)
                return {"type": "cancelled", "name": name}
            credentials = await asyncio.to_thread(load_oauth_credentials, name)
            if credentials is not None:
                item = await asyncio.to_thread(complete_host_oauth_auth, name)
                probe_ok, _reason = await host._agent_manager.probe_mcp_live_connection(name)
                if not probe_ok:
                    try:
                        from jiuwenswarm.server.runtime.mcp.registry import (
                            rollback_failed_connect,
                        )
                        rollback_failed_connect(name)
                    except Exception as rollback_exc:  # noqa: BLE001
                        logger.warning(
                            "[mcp] rollback failed host-OAuth probe '%s': %s",
                            name, rollback_exc,
                        )
                    return {
                        "type": "auth_failed",
                        "name": name,
                        "error": f"MCP '{name}' rejected the authorization token",
                    }
                try:
                    from jiuwenswarm.server.runtime.mcp.state_store import (
                        set_mcp_state,
                    )
                    set_mcp_state(name, state="connected")
                except Exception as flip_exc:  # noqa: BLE001
                    logger.warning(
                        "[mcp] flip host-OAuth '%s' connected failed: %s", name, flip_exc
                    )
                try:
                    host._agent_manager.sync_mcp_credentials()
                except Exception as sync_exc:  # noqa: BLE001
                    logger.warning("[mcp] sync credentials after host OAuth failed: %s", sync_exc)
                return {
                    "type": "connected",
                    "name": name,
                    "applied": True,
                    "item": host._mask_sensitive_fields(item),
                }
            await asyncio.sleep(delay)
        return {
            "type": "auth_failed",
            "name": name,
            "error": "Authorization timed out. Retry connect.",
        }

    @staticmethod
    def _command_mcp_internal_state(request: AgentRequest) -> AgentResponse:
        """Gateway 内部状态访问：command.mcp 的受限 action 族。

        为 gateway 侧远端 provider（拆仓后 ``server_store_bridge`` 的替代）
        暴露 MCP 连接状态/注册表/凭据三个域的原子读写。与 UI 导向的
        list/show/add/enable/update 不同，这些响应**不脱敏**（config 视图
        构建与凭据解析需要原始数据），因此整族 action 仅对 gateway 的
        内部通道放行，其余通道一律拒绝。
        """
        if str(getattr(request, "channel_id", "") or "") != "gateway":
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "internal MCP state actions are gateway-only",
                    "code": "PERMISSION",
                },
            )
        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action", "") or "").strip().lower()
        name = str(params.get("name", "") or "").strip()

        def _ok(payload: dict[str, Any]) -> AgentResponse:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )

        try:
            if action == "state_config_view":
                # config.py 视图循环的服务端整段执行：一次往返替代逐记录
                # is_stale/record_to_entry 的 N 次调用。
                from jiuwenswarm.server.runtime.mcp import registry as mcp_registry
                from jiuwenswarm.server.runtime.mcp import state_store

                entries = []
                for rec in state_store.list_connected_mcps():
                    rec_name = str(rec.get("name", "") or "")
                    if not rec_name or mcp_registry.is_stale_marketplace_record(rec_name, rec):
                        continue
                    entry = state_store.record_to_mcp_entry(rec_name, rec)
                    if entry is not None:
                        entries.append(entry)
                return _ok({"type": "state_config_view", "entries": entries})

            from jiuwenswarm.server.runtime.mcp import state_store

            if action == "state_list":
                view = str(params.get("view", "connected") or "connected").strip()
                view_map = {
                    "connected": state_store.list_connected_mcps,
                    "truly_connected": state_store.list_truly_connected_mcps,
                    "connecting": state_store.list_connecting_mcps,
                    "tui_enabled": state_store.list_tui_enabled_mcps,
                    "registered": state_store.list_registered_mcps,
                }
                items = view_map.get(view, state_store.list_connected_mcps)()
                return _ok({"type": "state_list", "view": view, "items": items})
            if action == "state_get_record":
                return _ok({"record": state_store.get_mcp_record(name)})
            if action == "state_upsert":
                record = state_store.upsert_mcp_record(
                    name,
                    params.get("entry") or {},
                    state=str(params.get("state", "connected") or "connected"),
                    integration_type=params.get("integration_type"),
                    skills=params.get("skills"),
                    enabled=params.get("enabled"),
                )
                return _ok({"record": record})
            if action == "state_set_enabled":
                state_store.set_mcp_enabled(name, enabled=bool(params.get("enabled")))
                return _ok({})
            if action == "state_remove":
                return _ok({"record": state_store.remove_mcp_record(name)})
            if action == "oauth_callback":
                # 已移交异步方法 _command_mcp_oauth_callback（外部 HTTP 往返）
                raise AssertionError("unreachable: oauth_callback dispatches to the async handler")
            if action == "credential_get_all":
                from jiuwenswarm.server.runtime.mcp.credential import CredentialStore

                return _ok({"tokens": CredentialStore().get_all(name)})
            if action == "credential_resolve":
                from jiuwenswarm.server.runtime.mcp.credential import (
                    CredentialStore,
                    resolve_placeholders,
                )

                resolved = resolve_placeholders(
                    params.get("entry") or {}, CredentialStore(), name or None
                )
                return _ok({"entry": resolved})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.mcp internal state failed: %s", exc)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "MCP_INTERNAL"},
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": f"unknown internal action: {action}", "code": "MCP_BAD_REQUEST"},
        )

    async def _command_mcp_oauth_callback(self, ws: Any, request: AgentRequest) -> AgentResponse:
        """``oauth_callback`` 的异步形态：换码经 to_thread 执行（见分发处注释）。

        门控为**连接级身份**（P1 修正）：``X-Jiuwen-Gateway-Internal`` 握手
        头部须携带与 ``JIUWENSWARM_GATEWAY_INTERNAL_TOKEN`` 一致的共享密
        钥——头部在 WS 握手时绑定到连接，浏览器/任意客户端无法经请求
        载荷伪造（channel_id 是载荷字段，不能作权限判据）。密钥未配置
        时 fail-closed（内部 action 一律拒绝并给出运维指引）。
        """
        host = self.host
        expected = os.getenv("JIUWENSWARM_GATEWAY_INTERNAL_TOKEN", "").strip()
        headers = getattr(ws, "request_headers", None) or {}
        provided = ""
        try:
            provided = str(headers.get("X-Jiuwen-Gateway-Internal", "") or "").strip()
        except Exception:
            provided = ""
        if not expected or not provided or provided != expected:
            logger.warning(
                "[AgentWebSocketServer] oauth_callback rejected: gateway internal token mismatch"
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "gateway internal actions require a connection-level token "
                    "(set JIUWENSWARM_GATEWAY_INTERNAL_TOKEN on both Gateway and AgentServer)",
                    "code": "PERMISSION",
                },
            )
        params = request.params if isinstance(request.params, dict) else {}
        try:
            from jiuwenswarm.server.runtime.mcp import registry as mcp_registry

            completed = await mcp_registry.complete_oauth_callback_async(
                params.get("query") or {}
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"completed": bool(completed)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.mcp oauth callback failed: %s", exc)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "MCP_INTERNAL"},
            )

    async def _handle_mcp_oauth_start(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.oauth.start`` for host-managed remote MCP OAuth.

        The frontend invokes this after ``mcp.connect`` returned the OAuth
        sentinel. The response carries the provider authorization URL; the
        same frontend then calls ``mcp.wait_auth``, which completes when the
        local HTTP callback exchanges the code and the live MCP probe passes.
        """
        host = self.host
        from jiuwenswarm.server.runtime.mcp.oauth_flow import (
            McpOAuthError,
            start_oauth_flow,
        )
        name = ""
        try:
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            callback_url = str(params.get("callback_url", "")).strip()
            if not callback_url:
                raise ValueError("callback_url is required")
            item = await asyncio.to_thread(start_oauth_flow, name, callback_url)
            payload = {"type": "auth_required", "name": name, **host._mask_sensitive_fields(item)}
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except McpOAuthError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "connect_failed",
                    "name": name,
                    "error": str(exc),
                    "code": "MCP_AUTH_FAILED",
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.oauth.start failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)




