# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm's native ACP stdio entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

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
            session = self._session_ctx.get(str(params.get("sessionId") or "").strip())
            if session is None:
                raise ValueError("Unknown sessionId")
            cwd = session["cwd"]
            if not Path(cwd).is_dir():
                raise ValueError("session cwd no longer exists")
            await super()._handle_jsonrpc_session_prompt(
                rpc_id, {**params, "cwd": cwd, "project_dir": cwd, "workspace_dir": cwd},
            )

        async def _handle_gateway_frame(self, data: dict) -> None:
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

    channel = NativeAcpChannel()
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
