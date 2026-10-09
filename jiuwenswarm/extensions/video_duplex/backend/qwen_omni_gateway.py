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


async def serve_qwen_omni_websocket(websocket: WebSocket) -> None:
    # Compatibility route for existing clients; new clients use bound sessions.
    from .realtime.gateway import serve_realtime_websocket
    await serve_realtime_websocket(websocket, QwenOmniRealtimeConfig.from_environment())
