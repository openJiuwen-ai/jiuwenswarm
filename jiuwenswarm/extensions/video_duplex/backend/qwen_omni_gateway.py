# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Authenticated WebSocket relay for Alibaba Cloud Qwen-Omni Realtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

QWEN_OMNI_PROXY_PATH = "/ws/video/qwen-omni"
_DEFAULT_MODEL = "qwen3.5-omni-flash-realtime"


@dataclass(frozen=True)
class QwenOmniRealtimeConfig:
    upstream_url: str
    api_key: str
    model: str
    voice: str

    @classmethod
    def from_environment(cls) -> QwenOmniRealtimeConfig:
        return cls(
            upstream_url=os.environ.get("QWEN_OMNI_REALTIME_URL", "").strip(),
            api_key=os.environ.get("QWEN_OMNI_API_KEY", "").strip(),
            model=os.environ.get("QWEN_OMNI_MODEL_NAME", _DEFAULT_MODEL).strip()
            or _DEFAULT_MODEL,
            voice=os.environ.get("QWEN_OMNI_VOICE", "Ethan").strip() or "Ethan",
        )

    def validate(self) -> None:
        if not self.upstream_url:
            raise ValueError("请配置 QWEN_OMNI_REALTIME_URL")
        parsed = urlsplit(self.upstream_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ValueError("QWEN_OMNI_REALTIME_URL 必须是有效的 ws:// 或 wss:// 地址")
        if parsed.username or parsed.password:
            raise ValueError("QWEN_OMNI_REALTIME_URL 不得包含用户名或密码")
        if not self.api_key:
            raise ValueError("请配置 QWEN_OMNI_API_KEY")
        if not self.model:
            raise ValueError("请配置 QWEN_OMNI_MODEL_NAME")

    def upstream_with_model(self) -> str:
        self.validate()
        parsed = urlsplit(self.upstream_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["model"] = self.model
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


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


async def serve_qwen_omni_websocket(websocket: WebSocket) -> None:
    """Relay one browser session to Qwen-Omni without exposing its API key."""
    await websocket.accept()
    config = QwenOmniRealtimeConfig.from_environment()
    try:
        upstream_url = config.upstream_with_model()
    except ValueError as exc:
        await _send_gateway_error(websocket, "qwen_gateway_config_error", str(exc))
        await websocket.close(code=1008, reason="Qwen-Omni gateway is not configured")
        return

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
            logger.info("Qwen-Omni Realtime relay connected model=%s", config.model)
            tasks = {
                asyncio.create_task(_relay_browser_to_upstream(websocket, upstream)),
                asyncio.create_task(_relay_upstream_to_browser(websocket, upstream)),
            }
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled():
                        continue
                    exception = task.exception()
                    if exception is not None:
                        raise exception
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (ConnectionClosed, WebSocketDisconnect):
        pass
    except Exception as exc:  # noqa: BLE001 - isolate one upstream session
        message = _safe_upstream_error(exc, config.api_key)
        logger.warning("Qwen-Omni Realtime relay failed: %s", message)
        await _send_gateway_error(websocket, "qwen_gateway_upstream_error", message)
    finally:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close(code=1000)
            except (RuntimeError, WebSocketDisconnect):
                pass
