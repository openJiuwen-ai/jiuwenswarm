# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm's native ACP stdio entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from jiuwenswarm.acp.session_protocol import ACP_SESSION_EXTENSION
from jiuwenswarm.common.version import get_runtime_version


def _token_usage(payload: dict) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    fields = {
        "input_tokens": "inputTokens",
        "output_tokens": "outputTokens",
        "total_tokens": "totalTokens",
        "cache_tokens": "cachedReadTokens",
    }
    return {
        target: usage[source]
        for source, target in fields.items()
        if type(usage.get(source)) is int and usage[source] >= 0
    }


async def _run(gateway_url: str) -> None:
    from agent_gateway.channel_manager.protocol.acp.acp_connect import (
        AcpChannel,
        load_acp_channel_config,
    )
    from websockets.legacy.client import connect
    from websockets.exceptions import WebSocketException

    class NativeAcpChannel(AcpChannel):
        def __init__(self) -> None:
            super().__init__(load_acp_channel_config(), router=None, gateway_url=gateway_url)
            self._turn_usage: dict[str, dict[str, int]] = {}
            self._context_sizes: dict[str, tuple[int, int]] = {}
            self._turn_executions: dict[str, str] = {}
            self._session_rpc: dict[str, asyncio.Future] = {}
            self._persistent_sessions = False

        async def _ensure_gateway_connection(self) -> None:
            if self._gateway_ws is not None:
                return
            try:
                self._gateway_ws = await connect(gateway_url, open_timeout=3, close_timeout=1)
            except (OSError, TimeoutError, WebSocketException) as exc:
                raise SystemExit(f"JiuwenSwarm Gateway unreachable: {gateway_url}") from exc
            self._gateway_reader_task = asyncio.create_task(
                self._gateway_reader_loop(), name="acp-gateway-reader",
            )

        async def _gateway_session(self, operation: str, **params) -> dict:
            await self._ensure_gateway_connection()
            request_id = f"acp_options_{uuid4().hex}"
            response = asyncio.get_running_loop().create_future()
            self._session_rpc[request_id] = response
            try:
                await self._gateway_ws.send(json.dumps({
                    "type": "req", "id": request_id, "method": "initialize",
                    "params": {ACP_SESSION_EXTENSION: {"operation": operation, **params}},
                }))
                frame = await asyncio.wait_for(response, timeout=5)
            finally:
                self._session_rpc.pop(request_id, None)
            payload = frame.get("payload")
            extension = payload.get(ACP_SESSION_EXTENSION) if isinstance(payload, dict) else None
            return extension if isinstance(extension, dict) else {}

        async def _handle_jsonrpc_request(self, data: dict) -> None:
            method = data.get("method")
            params = data.get("params")
            params = params if isinstance(params, dict) else {}
            rpc_id = data.get("id")
            if method == "initialize":
                await self._notify_agent_initialize(params)
                result = self._initialize_result()
                try:
                    capabilities = await self._gateway_session("capabilities")
                except TimeoutError:
                    capabilities = {}
                self._persistent_sessions = capabilities.get("loadSession") is True
                if self._persistent_sessions:
                    agent_caps = result["agentCapabilities"]
                    agent_caps["loadSession"] = True
                    # Session entries are connection-local, so the bridge
                    # cannot provide a complete list of persisted sessions.
                    agent_caps.get("sessionCapabilities", {}).pop("list", None)
                await self._write_jsonrpc_result(rpc_id, result)
                return
            if self._persistent_sessions and method in (
                "session/new", "session/load", "session/set_model",
            ):
                await self._handle_persistent_session(rpc_id, method, params)
                return
            await super()._handle_jsonrpc_request(data)

        async def _handle_persistent_session(self, rpc_id, method: str, params: dict) -> None:
            session_id = params.get("sessionId")
            if method == "session/set_model":
                if not isinstance(session_id, str) or session_id not in self._session_ctx:
                    await self._write_jsonrpc_error(rpc_id, -32002, "Session not found or expired")
                    return
                operation = "set_model"
                args = {"sessionId": session_id, "modelId": params.get("modelId")}
            else:
                cwd = params.get("cwd")
                if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                    await self._write_jsonrpc_error(rpc_id, -32602, "cwd must be an existing absolute directory")
                    return
                operation = "new" if method == "session/new" else "resume"
                args = {"cwd": str(Path(cwd).resolve()), "sessionId": session_id}
            try:
                result = await self._gateway_session(operation, **args)
            except (OSError, TimeoutError, WebSocketException):
                result = {}
            if error := result.get("error"):
                await self._write_jsonrpc_error(rpc_id, error["code"], error["message"])
                return
            if not (session_id := result.get("sessionId")):
                await self._write_jsonrpc_error(rpc_id, -32603, "Gateway session operation failed")
                return
            if operation != "set_model":
                self._known_sessions.add(session_id)
                self._session_ctx[session_id] = {
                    "cwd": args["cwd"], "mcpServers": params.get("mcpServers", []),
                }
            self._session_ctx[session_id]["model_name"] = (
                result.get("models", {}).get("currentModelId", "")
            )
            await self._write_jsonrpc_result(rpc_id, result)

        async def _handle_jsonrpc_session_new(
            self, rpc_id: str | int | None, params: dict,
        ) -> None:
            cwd = params.get("cwd")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                raise ValueError("session/new cwd must be an existing absolute directory")
            await super()._handle_jsonrpc_session_new(rpc_id, params)

        async def _handle_jsonrpc_session_prompt(
            self, rpc_id: str | int | None, params: dict,
        ) -> None:
            session_id = str(params.get("sessionId") or "").strip()
            session = self._session_ctx.get(session_id)
            if session is None:
                await self._write_jsonrpc_error(rpc_id, -32002, "Session not found or expired")
                return
            cwd = session["cwd"]
            if not Path(cwd).is_dir():
                raise ValueError("session cwd no longer exists")
            turn = {**params, "cwd": cwd, "project_dir": cwd, "workspace_dir": cwd}
            if self._persistent_sessions and "model_name" in session:
                turn["model_name"] = session["model_name"]
                turn["eternal_conversation_enabled"] = True
            await super()._handle_jsonrpc_session_prompt(rpc_id, turn)

        async def _handle_gateway_frame(self, data: dict) -> None:
            if isinstance(data, dict) and data.get("type") == "res":
                pending = self._session_rpc.get(str(data.get("id")))
                if pending is not None:
                    if not pending.done():
                        pending.set_result(data)
                    return
            if isinstance(data, dict) and data.get("type") == "event":
                event = data.get("event")
                payload = data.get("payload")
                if isinstance(payload, dict):
                    await self._record_gateway_usage(event, payload)
            await super()._handle_gateway_frame(data)

        async def _record_gateway_usage(self, event: object, payload: dict) -> None:
            session_id = payload.get("session_id")
            request_id = (
                self._active_prompt_request_by_session.get(session_id)
                if isinstance(session_id, str) else None
            )
            execution_id = payload.get("execution_id")
            if isinstance(execution_id, str) and execution_id:
                # /acp can expose the internal session ID on usage frames.
                # Match those frames using the execution ID on the chat stream.
                if request_id is not None and event in ("chat.delta", "chat.final", "chat.reasoning"):
                    self._turn_executions.setdefault(execution_id, request_id)
                elif event in ("context.usage", "chat.usage_summary"):
                    request_id = self._turn_executions.get(execution_id, request_id)
            if request_id is None:
                return

            if event == "context.usage":
                size = payload.get("context_max")
                used = payload.get("tokens_used")
                if type(size) is int and size > 0 and type(used) is int and used >= 0:
                    self._context_sizes[request_id] = (size, used)
                return
            if event != "chat.usage_summary":
                return
            usage = _token_usage(payload)
            ctx = self._request_ctx.get(request_id)
            if not usage or ctx is None:
                return
            self._turn_usage[request_id] = usage
            context_size = self._context_sizes.get(request_id)
            if context_size is not None:
                size, used = context_size
                await self._write_acp_session_update(ctx.session_id, {
                    "sessionUpdate": "usage_update",
                    "size": size,
                    "used": used,
                    "_meta": {"usage": usage},
                })

        async def _write_jsonrpc_result(self, rpc_id: str | int | None, result: object) -> None:
            if isinstance(result, dict) and "stopReason" in result:
                for request_id, ctx in self._request_ctx.items():
                    if ctx.jsonrpc_id == rpc_id and request_id in self._turn_usage:
                        result = {**result, "usage": self._turn_usage[request_id]}
                        break
            await super()._write_jsonrpc_result(rpc_id, result)

        async def _clear_request_context(self, request_id: str) -> None:
            self._turn_usage.pop(request_id, None)
            self._context_sizes.pop(request_id, None)
            self._turn_executions = {
                execution_id: owner
                for execution_id, owner in self._turn_executions.items()
                if owner != request_id
            }
            await super()._clear_request_context(request_id)

    from jiuwenswarm.acp.session_mcp import SessionMcpChannelMixin

    class SessionMcpAcpChannel(SessionMcpChannelMixin, NativeAcpChannel):
        pass

    channel = SessionMcpAcpChannel()
    try:
        await channel.start()
    finally:
        await channel.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="jiuwenswarm-acp")
    parser.add_argument("--version", action="version", version=get_runtime_version())
    parser.add_argument("--gateway-url", "-g", metavar="URL")
    parser.add_argument("--agent-server-url", "-u", help=argparse.SUPPRESS)
    args = parser.parse_args()

    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    port = os.getenv("GATEWAY_PORT", "19001")
    url = args.gateway_url or args.agent_server_url or os.getenv("GATEWAY_URL") or f"ws://{host}:{port}/acp"

    # Gateway frames use the original stdout. Keep incidental logs on stderr.
    sys.stdout = sys.stderr
    from agent_gateway.common.logging_setup import setup_gateway_logger

    setup_gateway_logger()
    asyncio.run(_run(url))


if __name__ == "__main__":
    main()
