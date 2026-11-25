# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run ACP-supplied MCP servers in the stdio bridge process."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

MCP_CALL_METHOD = "_jiuwenswarm/mcp/call"
MCP_TOOLS_PARAM = "_jiuwenswarm_acp_mcp_tools"

McpTransport = Callable[[], AbstractAsyncContextManager]


class SessionMcp:
    def __init__(self, servers: list[tuple[str, McpTransport]]) -> None:
        self.token = uuid4().hex
        self._servers = servers
        self._ready = asyncio.get_running_loop().create_future()
        self._tools: dict[str, tuple[ClientSession, str]] = {}
        self._task = asyncio.create_task(self._run(), name="acp-session-mcp")

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                declarations = []
                for server_name, connect in self._servers:
                    reader, writer, *_ = await stack.enter_async_context(connect())
                    client = await stack.enter_async_context(ClientSession(reader, writer))
                    await client.initialize()
                    cursor = None
                    while True:
                        result = await client.list_tools(cursor=cursor)
                        for tool in result.tools:
                            name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server_name}__{tool.name}")
                            if len(name) > 64 or name in self._tools:
                                name = f"{name[:43]}_{uuid4().hex[:20]}"
                            self._tools[name] = client, tool.name
                            declarations.append({
                                "name": name, "description": tool.description or tool.name,
                                "schema": tool.inputSchema, "token": self.token,
                            })
                        cursor = result.nextCursor
                        if not cursor:
                            break
                self._ready.set_result(declarations)
                await asyncio.Future()
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            self._tools.clear()

    async def declarations(self) -> list[dict]:
        return await asyncio.wait_for(asyncio.shield(self._ready), timeout=20)

    async def call(self, name: str, arguments: dict) -> dict:
        client, original_name = self._tools[name]
        result = await client.call_tool(original_name, arguments)
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def close(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        if not self._ready.done():
            self._ready.cancel()
        elif not self._ready.cancelled():
            self._ready.exception()


def _remote_server(entry: dict, transport: str) -> McpTransport:
    url = entry.get("url")
    if not isinstance(url, str) or any(char.isspace() for char in url):
        raise ValueError("Remote MCP server requires a valid HTTP(S) URL")
    try:
        endpoint = urlsplit(url)
        port = endpoint.port
    except ValueError:
        raise ValueError("Remote MCP server requires a valid HTTP(S) URL") from None
    if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
        raise ValueError("Remote MCP server requires a valid HTTP(S) URL")
    if endpoint.username is not None or endpoint.fragment or port == 0:
        raise ValueError("Remote MCP server requires a valid HTTP(S) URL")
    headers = _name_value_pairs(entry.get("headers", []), "headers")
    if any(
        not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
        or any(char in value for char in "\r\n\x00")
        for key, value in headers.items()
    ):
        raise ValueError("Invalid remote MCP header")
    connect = streamablehttp_client if transport == "http" else sse_client
    return partial(connect, url, headers=headers, timeout=10)


def _name_value_pairs(items: object, field: str) -> dict[str, str]:
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str)
        and isinstance(item.get("value"), str) for item in items
    ):
        raise ValueError(f"MCP {field} must contain name/value pairs")
    return {item["name"]: item["value"] for item in items}


def _is_session_dir(cwd: object) -> bool:
    return isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).is_dir()


def _session_servers(params: dict) -> list[tuple[str, McpTransport]]:
    entries = params.get("mcpServers", [])
    if not isinstance(entries, list):
        raise ValueError("mcpServers must be an array")
    cwd = params.get("cwd")
    if entries and not _is_session_dir(cwd):
        raise ValueError("MCP requires an existing absolute session cwd")
    servers = []
    names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("MCP servers must be objects")
        transport = entry.get("type", "stdio")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("MCP server names must be unique and nonempty")
        names.add(name)
        if transport in ("http", "sse"):
            servers.append((name, _remote_server(entry, transport)))
            continue
        if transport != "stdio":
            raise ValueError("Unsupported MCP server type")
        command, args = entry.get("command"), entry.get("args", [])
        if not isinstance(command, str) or not command:
            raise ValueError("stdio MCP command must be a nonempty string")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("stdio MCP args must be an array of strings")
        server_params = StdioServerParameters(
            command=command, args=args, env=_name_value_pairs(entry.get("env", []), "env"), cwd=cwd,
        )
        servers.append((name, partial(stdio_client, server_params)))
    return servers


class SessionMcpChannelMixin:
    """Attach bridge-owned MCP transports to successful ACP sessions."""

    _REMOTE_MCP_CAPABILITIES = {"http": True, "sse": True}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._mcp_sessions: dict[str, tuple[SessionMcp, list[dict]]] = {}
        self._mcp_calls: set[asyncio.Task] = set()
        self._mcp_new_session: tuple[object, SessionMcp | None, list[dict]] | None = None

    def _initialize_result(self) -> dict:
        result = super()._initialize_result()
        result["agentCapabilities"]["mcpCapabilities"].update(self._REMOTE_MCP_CAPABILITIES)
        return result

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._ensure_gateway_connection()
        while self._running:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                break
            if not line.strip():
                continue
            try:
                await self._handle_raw_line(line.decode("utf-8", errors="replace"))
            except Exception:
                logger.exception("ACP stdio request failed")
                await self._write_jsonrpc_error(None, -32603, "ACP stdio request failed")

        # A half-closed stdio client can still read its in-flight prompt result.
        # Once responses drain, close the MCP children without an idle grace period.
        while (
            self._running and self._gateway_ws is not None
            and self._request_ctx and not self._pending_client_rpc_session_by_id
        ):
            await asyncio.sleep(0.05)

    async def _handle_jsonrpc_request(self, data: dict) -> None:
        method = data.get("method")
        params = data.get("params")
        params = params if isinstance(params, dict) else {}
        rpc_id = data.get("id")
        if method == "session/close":
            session_id = params.get("sessionId")
            if not isinstance(session_id, str) or session_id not in self._session_ctx:
                await self._write_jsonrpc_error(rpc_id, -32002, "Session not found or expired")
                return
            if session_id in self._active_prompt_request_by_session:
                await self._handle_jsonrpc_session_cancel(None, {"sessionId": session_id})
            self._session_ctx.pop(session_id, None)
            self._known_sessions.discard(session_id)
            binding = self._mcp_sessions.pop(session_id, None)
            if binding is not None:
                await binding[0].close()
            await self._write_jsonrpc_result(rpc_id, {})
            return

        if method not in ("session/new", "session/load", "session/resume"):
            await super()._handle_jsonrpc_request(data)
            return
        try:
            servers = _session_servers(params)
        except ValueError as exc:
            await self._write_jsonrpc_error(rpc_id, -32602, str(exc))
            return
        session = SessionMcp(servers) if servers else None
        self._mcp_new_session = (rpc_id, session, [])
        try:
            try:
                declarations = await session.declarations() if session is not None else []
            except Exception:
                await self._write_jsonrpc_error(rpc_id, -32603, "Could not initialize session MCP servers")
                return
            self._mcp_new_session = (rpc_id, session, declarations)
            clean_params = {key: value for key, value in params.items() if key != "mcpServers"}
            await super()._handle_jsonrpc_request({**data, "params": clean_params})
        finally:
            if session is not None and self._mcp_new_session is not None:
                await session.close()
            self._mcp_new_session = None

    async def _write_jsonrpc_result(self, rpc_id, result) -> None:
        pending = self._mcp_new_session
        if pending is not None and pending[0] == rpc_id and isinstance(result, dict):
            session_id = result.get("sessionId")
            if isinstance(session_id, str):
                _, session, declarations = pending
                prior = self._mcp_sessions.pop(session_id, None)
                if prior is not None:
                    await prior[0].close()
                if session is not None:
                    self._mcp_sessions[session_id] = session, declarations
                self._mcp_new_session = None
        await super()._write_jsonrpc_result(rpc_id, result)

    async def _handle_jsonrpc_session_prompt(self, rpc_id, params: dict) -> None:
        session_id = str(params.get("sessionId") or "").strip()
        binding = self._mcp_sessions.get(session_id)
        if binding is not None and session_id in self._active_prompt_request_by_session:
            await self._write_jsonrpc_error(rpc_id, -32600, "Session already has an active prompt")
            return
        turn = {key: value for key, value in params.items() if key != "mcpServers"}
        turn[MCP_TOOLS_PARAM] = binding[1] if binding is not None else []
        await super()._handle_jsonrpc_session_prompt(rpc_id, turn)

    async def _handle_gateway_jsonrpc_request(self, data: dict) -> None:
        if data.get("method") != MCP_CALL_METHOD:
            await super()._handle_gateway_jsonrpc_request(data)
            return
        task = asyncio.create_task(self._call_session_mcp(data), name="acp-mcp-call")
        self._mcp_calls.add(task)
        task.add_done_callback(self._mcp_calls.discard)

    async def _call_session_mcp(self, data: dict) -> None:
        params = data.get("params")
        params = params if isinstance(params, dict) else {}
        response = {"jsonrpc": "2.0", "id": data.get("id")}
        binding = next((
            session for session, _ in self._mcp_sessions.values()
            if session.token == params.get("token")
        ), None)
        try:
            if binding is None:
                raise ValueError("MCP session closed")
            async with asyncio.timeout(45):
                response["result"] = await binding.call(params["name"], params["arguments"])
        except Exception:
            response["error"] = {"code": -32603, "message": "Session MCP call failed"}
        if self._gateway_ws is not None:
            try:
                await self._gateway_ws.send(json.dumps(response))
            except (OSError, ConnectionClosed):
                pass

    async def stop(self) -> None:
        self._running = False
        bindings = tuple(self._mcp_sessions.values())
        self._mcp_sessions.clear()
        calls = tuple(self._mcp_calls)
        try:
            for task in calls:
                task.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            await asyncio.gather(*(server.close() for server, _ in bindings))
        finally:
            await super().stop()
