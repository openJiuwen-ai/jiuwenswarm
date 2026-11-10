# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared Qwen Omni configuration and authenticated provider connections."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import logging
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

logger = logging.getLogger(__name__)


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
            model=os.environ.get("QWEN_OMNI_MODEL_NAME", "").strip(),
            voice=os.environ.get("QWEN_OMNI_VOICE", "").strip() or "Tina",
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


@asynccontextmanager
async def open_qwen_omni_connection(
    config: QwenOmniRealtimeConfig, *, closing_tasks: set[asyncio.Task] | None = None,
):
    """Open one connection; consumers own session instructions and media policy.

    Successful callers may retain background close ownership in ``closing_tasks``.
    Failed or cancelled operations close inline before their exception propagates.
    """
    async with AsyncExitStack() as stack:
        upstream = await stack.enter_async_context(websockets.connect(
            config.upstream_with_model(),
            additional_headers={"Authorization": f"Bearer {config.api_key}"},
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=30,
            max_size=8 * 1024 * 1024,
        ))
        yield upstream
        if closing_tasks is not None:
            cleanup = stack.pop_all()
            task = asyncio.create_task(cleanup.aclose())
            closing_tasks.add(task)

            def closed(done: asyncio.Task) -> None:
                closing_tasks.discard(done)
                if not done.cancelled():
                    error = done.exception()
                    if error is not None:
                        logger.warning(
                            "Qwen Omni connection cleanup failed: %s", _safe_upstream_error(error, config.api_key),
                        )

            task.add_done_callback(closed)


def _safe_upstream_error(exc: BaseException, api_key: str) -> str:
    message = str(exc).strip() or type(exc).__name__
    if api_key:
        message = message.replace(api_key, "******")
    return message[:1_000]
