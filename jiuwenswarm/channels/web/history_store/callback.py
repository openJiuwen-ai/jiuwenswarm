# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved

"""Web 会话历史帧回调与 ``HistoryFrameRunner``。

从同步 WS 代理 / Listen 入队线程把 async ``record_*`` 桥接到 store。
本模块不 import foundation。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from .store import ChatHistoryStore

logger = logging.getLogger("jiuwenswarm.web.history")

_REQUEST_METHODS = frozenset({"chat.send", "chat.resume", "chat.user_answer"})
_FINAL_EVENTS = frozenset({"chat.final", "chat.error"})

# history.get 流式路径在 AgentServer 本地无历史文件时的固定报错。它不是 assistant
# 回复，不能落库——此前 chat.error 分支会把它当成 assistant 消息写进 PG，
# 会话列表的 last_preview 因此出现这行错误文本。
_HISTORY_NOT_FOUND_SNIPPET = "invalid page_idx or session history not found"

FrameCallback = Callable[[str, str, "str | None"], Awaitable[None]]


def make_history_callback(store: "ChatHistoryStore") -> FrameCallback:
    """产出 on_frame 回调：白名单 → pending 回填 → store.record_*。"""
    pending: dict[str, dict[str, Any]] = {}
    assistant_buf: dict[str, str] = {}

    def _identity_from_params(params: dict[str, Any]) -> dict[str, Any]:
        """从 browser 帧 params 提取身份/归属（由 WS/HTTP 入口注入的连接级权威值）。"""
        fields: dict[str, Any] = {}
        for key in ("group_id", "bot_id", "project_id", "cron_id", "work_mode"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                fields[key] = value.strip()
        return fields

    async def _handle_browser(data: dict[str, Any]) -> None:
        if data.get("type") != "req":
            return
        method = data.get("method")
        if method not in _REQUEST_METHODS:
            return
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        query = params.get("query") or params.get("content")
        if not isinstance(query, str) or not query:
            return
        request_id = data.get("id")
        if not isinstance(request_id, str):
            return
        session_id = params.get("session_id")
        user = params.get("user") or params.get("user_id")
        if not isinstance(user, str) or not user.strip():
            user = None
        else:
            user = user.strip()
        identity = _identity_from_params(params)
        ts = time.time()
        if isinstance(session_id, str) and session_id:
            await store.record_user(
                request_id=request_id, session_id=session_id, query=query, ts=ts, user=user,
                **identity,
            )
        else:
            pending[request_id] = {
                "query": query, "ts": ts, "method": method, "user": user, **identity,
            }
            logger.debug(
                "[history] 暂存 pending user(无 sid): rid=%s method=%s pending=%d",
                request_id, method, len(pending),
            )

    async def _handle_uplink(data: dict[str, Any]) -> None:
        if data.get("type") != "event":
            return
        event = data.get("event")
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            payload_rid = payload.get("request_id")
            if isinstance(payload_rid, str) and payload_rid:
                request_id = payload_rid

        if event == "chat.delta" and isinstance(request_id, str):
            delta = payload.get("content")
            if isinstance(delta, str) and delta:
                assistant_buf[request_id] = assistant_buf.get(request_id, "") + delta
            return

        if event not in _FINAL_EVENTS:
            return
        if event == "chat.error" and _HISTORY_NOT_FOUND_SNIPPET in str(payload.get("error") or ""):
            # history.get 的"本地无历史"报错不落库
            logger.debug("[history] history.get 未命中报错不落库: rid=%s", request_id)
            return
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            logger.warning(
                "[history] 终态帧缺 session_id，丢弃: event=%s rid=%s",
                event, request_id if isinstance(request_id, str) else "",
            )
            return
        if event == "chat.final":
            content = assistant_buf.pop(request_id, "") or payload.get("content") or ""
        else:
            content = payload.get("error") or payload.get("content") or assistant_buf.pop(request_id, "")
        if not isinstance(content, str) or not content:
            logger.debug("[history] 终态帧无内容，丢弃: event=%s rid=%s", event, request_id)
            return
        ts = time.time()
        if isinstance(request_id, str) and request_id in pending:
            p = pending.pop(request_id)
            await store.record_user(
                request_id=request_id, session_id=session_id,
                query=p["query"], ts=p["ts"], user=p.get("user"),
                group_id=p.get("group_id"), bot_id=p.get("bot_id"),
                project_id=p.get("project_id"), cron_id=p.get("cron_id"),
                work_mode=p.get("work_mode"),
            )
            logger.info("[history] pending 回填 user: rid=%s sid=%s", request_id, session_id)
        await store.record_assistant(
            request_id=request_id if isinstance(request_id, str) else "",
            session_id=session_id, content=content, event_type=event, ts=ts,
        )

    async def cb(direction: str, raw: str, conn_id: str | None = None) -> None:  # noqa: ARG001
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("[history] 非 JSON 帧，忽略: dir=%s head=%r", direction, (raw or "")[:120])
            return
        if not isinstance(data, dict):
            return
        try:
            if direction == "browser":
                await _handle_browser(data)
            elif direction == "uplink":
                await _handle_uplink(data)
        except Exception:
            logger.exception("[history] on_frame 处理失败: dir=%s", direction)

    return cb


class HistoryFrameRunner:
    """Run async history callbacks from sync WS proxy / Listen enqueue threads."""

    def __init__(self, store: "ChatHistoryStore") -> None:
        self._callback = make_history_callback(store)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="web-history", daemon=True)
        self._started = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> None:
        if self._started:
            return
        self._thread.start()
        self._started = True

    def submit(self, direction: str, raw: str) -> None:
        if not self._started:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._callback(direction, raw, None), self._loop)
        except Exception:
            logger.warning("[history] submit frame failed dir=%s", direction, exc_info=True)

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001
            logger.debug("[history] stop loop failed", exc_info=True)
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._started = False
