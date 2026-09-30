# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared WebSocket relay using a server-owned provider configuration."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

async def _send_gateway_error(websocket: WebSocket, code: str, message: str) -> None:
    if websocket.client_state == WebSocketState.DISCONNECTED:
        return
    try:
        await websocket.send_text(json.dumps({
            "type": "error",
            "error": {"code": code, "message": message},
        }, ensure_ascii=False))
    except (RuntimeError, WebSocketDisconnect):
        return


def _safe_upstream_error(exc: BaseException, api_key: str) -> str:
    message = str(exc).strip() or type(exc).__name__
    if api_key:
        message = message.replace(api_key, "******")
    return message[:1_000]


async def _relay_browser_to_upstream(websocket: WebSocket, upstream: object) -> None:
    while True:
        message = await websocket.receive()
        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            return
        text = message.get("text")
        data = message.get("bytes")
        if text is not None:
            await upstream.send(text)  # type: ignore[attr-defined]
        elif data is not None:
            await upstream.send(data)  # type: ignore[attr-defined]


async def _relay_upstream_to_browser(websocket: WebSocket, upstream: object) -> None:
    async for message in upstream:  # type: ignore[attr-defined]
        if isinstance(message, bytes):
            await websocket.send_bytes(message)
        else:
            await websocket.send_text(message)


async def serve_realtime_websocket(websocket: WebSocket, config, provider: str = "qwen") -> None:
    """Relay one browser session to Realtime without exposing its API key."""
    await websocket.accept()
    try:
        upstream_url = config.upstream_with_model()
    except ValueError as exc:
        await _send_gateway_error(websocket, f"{provider}_gateway_config_error", str(exc))
        await websocket.close(code=1008, reason="Realtime gateway is not configured")
        return

    close_code, close_reason = 1000, ""
    try:
        async with websockets.connect(
            upstream_url,
            additional_headers={"Authorization": f"Bearer {config.api_key}"},
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=30,
            max_size=8 * 1024 * 1024,
        ) as upstream:
            logger.info("Realtime relay connected model=%s", config.model)
            browser_task = asyncio.create_task(_relay_browser_to_upstream(websocket, upstream))
            upstream_task = asyncio.create_task(_relay_upstream_to_browser(websocket, upstream))
            tasks = {browser_task, upstream_task}
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled():
                        continue
                    exception = task.exception()
                    if exception is not None:
                        raise exception
                close_code = getattr(upstream, "close_code", None) or 1000
                close_reason = getattr(upstream, "close_reason", "") or ""
                if upstream_task in done and browser_task not in done:
                    message = _safe_upstream_error(
                        RuntimeError(f"Realtime 连接已关闭（{close_code}）：{close_reason or '服务端未提供具体原因'}"),
                        config.api_key,
                    )
                    await _send_gateway_error(websocket, f"{provider}_gateway_upstream_closed", message)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except ConnectionClosed as exc:
        close_code = exc.rcvd.code if exc.rcvd else 1011
        close_reason = exc.rcvd.reason if exc.rcvd else "Upstream disconnected without a close frame"
        await _send_gateway_error(
            websocket, f"{provider}_gateway_upstream_closed", _safe_upstream_error(exc, config.api_key)
        )
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001 - isolate one upstream session
        message = _safe_upstream_error(exc, config.api_key)
        logger.warning("Realtime relay failed: %s", message)
        await _send_gateway_error(websocket, f"{provider}_gateway_upstream_error", message)
        close_code, close_reason = 1011, "Realtime upstream connection failed"
    finally:
        close_reason = _safe_upstream_error(Exception(close_reason), config.api_key) if close_reason else ""
        logger.info("Realtime relay closed code=%s reason=%s", close_code, close_reason)
        if close_code in {1004, 1005, 1006, 1015}:
            close_reason = f"Upstream close {close_code}: {close_reason}"
            close_code = 1011
        close_reason = close_reason.encode("utf-8")[:123].decode("utf-8", errors="ignore")
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close(code=close_code, reason=close_reason)
            except (RuntimeError, WebSocketDisconnect):
                pass
