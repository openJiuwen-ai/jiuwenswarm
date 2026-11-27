# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Registry for AskUserQuestion tool: correlate Gateway push with chat.user_answer."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

ASK_REQUEST_PREFIX = "ask_uq_"

_interactive_ask_cv: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "jiuwenclaw_interactive_ask",
    default=False,
)
_session_id_cv: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jiuwenclaw_ask_session_id",
    default="",
)
_stream_request_id_cv: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jiuwenclaw_ask_stream_request_id",
    default="",
)
_channel_id_cv: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jiuwenclaw_ask_channel_id",
    default="",
)
_event_type_prefix_cv: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jiuwenclaw_ask_event_type_prefix",
    default="chat",
)


@contextlib.asynccontextmanager
async def ask_user_question_request_scope(
    *,
    interactive_ask: bool,
    session_id: str,
    stream_request_id: str,
    channel_id: str,
    event_type_prefix: str = "chat",
) -> AsyncIterator[None]:
    """Bind per-request context for AskUserQuestion (push routing / user_answer)."""
    reg = AskUserQuestionRegistry.get_instance()
    sr = str(stream_request_id or "").strip()
    sid = str(session_id or "").strip()
    if sr or sid:
        reg.bind_stream_chat_flags(sr, bool(interactive_ask), session_id=sid)
    t_ia = _interactive_ask_cv.set(bool(interactive_ask))
    t_sid = _session_id_cv.set(session_id or "")
    t_rid = _stream_request_id_cv.set(stream_request_id or "")
    t_cid = _channel_id_cv.set(channel_id or "")
    t_prefix = _event_type_prefix_cv.set((event_type_prefix or "chat").strip() or "chat")
    try:
        yield
    finally:
        _interactive_ask_cv.reset(t_ia)
        _session_id_cv.reset(t_sid)
        _stream_request_id_cv.reset(t_rid)
        _channel_id_cv.reset(t_cid)
        _event_type_prefix_cv.reset(t_prefix)
        if sr:
            reg.unbind_stream_chat_flags(sr)


def get_ask_request_context() -> tuple[bool, str, str, str, str]:
    """Return (interactive_ask, session_id, stream_request_id, channel_id, event_type_prefix)."""
    return (
        _interactive_ask_cv.get(),
        _session_id_cv.get(),
        _stream_request_id_cv.get(),
        _channel_id_cv.get(),
        _event_type_prefix_cv.get(),
    )


class AskUserQuestionRegistry:
    """Maps ask correlation ids to Futures completed by chat.user_answer."""

    _instance: ClassVar["AskUserQuestionRegistry | None"] = None

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[list[Any]]] = {}
        self._pending_sessions: dict[str, str] = {}
        self._stream_interactive_ask: dict[str, bool] = {}
        # 与单次 stream_request_id 并行：同一 session 在本次 HTTP 流内是否开启过 interactive_ask。
        # 部分环境下工具第二次执行时 ContextVar 未继承，仅靠 stream_rid 查表会在 unbind 后失效。
        self._session_interactive_ask: dict[str, bool] = {}

    @classmethod
    def get_instance(cls) -> "AskUserQuestionRegistry":
        if cls._instance is None:
            cls._instance = AskUserQuestionRegistry()
        return cls._instance

    @classmethod
    def reset_instance_for_tests(cls) -> None:
        cls._instance = None

    def bind_stream_chat_flags(
        self, stream_request_id: str, interactive_ask: bool, *, session_id: str = "",
    ) -> None:
        rid = str(stream_request_id or "").strip()
        if rid:
            self._stream_interactive_ask[rid] = bool(interactive_ask)
        sid = str(session_id or "").strip()
        if sid:
            self._session_interactive_ask[sid] = bool(interactive_ask)

    def unbind_stream_chat_flags(self, stream_request_id: str) -> None:
        self._stream_interactive_ask.pop(str(stream_request_id or "").strip(), None)

    def stream_interactive_ask_enabled(self, stream_request_id: str) -> bool:
        rid = str(stream_request_id or "").strip()
        return bool(rid) and bool(self._stream_interactive_ask.get(rid))

    def session_interactive_ask_enabled(self, session_id: str) -> bool:
        sid = str(session_id or "").strip()
        return bool(sid) and bool(self._session_interactive_ask.get(sid))

    def resolve(self, request_id: str, answers: Any) -> bool:
        rid = str(request_id or "").strip()
        if not rid:
            return False
        fut = self._pending.pop(rid, None)
        self._pending_sessions.pop(rid, None)
        if fut is None or fut.done():
            return False
        norm: list[Any] = answers if isinstance(answers, list) else []
        fut.set_result(norm)
        logger.info("[AskUserQuestionRegistry] resolved request_id=%s", rid)
        return True

    def register(self, request_id: str, session_id: str) -> asyncio.Future[list[Any]]:
        rid = str(request_id or "").strip()
        if not rid:
            raise ValueError("request_id 不能为空")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[Any]] = loop.create_future()
        self._pending[rid] = fut
        self._pending_sessions[rid] = str(session_id or "")
        return fut

    def cleanup(self, request_id: str) -> None:
        rid = str(request_id or "").strip()
        fut = self._pending.pop(rid, None)
        self._pending_sessions.pop(rid, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def cancel_for_session(self, session_id: str) -> None:
        sid = str(session_id or "")
        if not sid:
            return
        to_cancel = [rid for rid, rid_sid in self._pending_sessions.items() if rid_sid == sid]
        for rid in to_cancel:
            fut = self._pending.pop(rid, None)
            self._pending_sessions.pop(rid, None)
            if fut is not None and not fut.done():
                fut.cancel()
        if to_cancel:
            logger.info(
                "[AskUserQuestionRegistry] cancelled pending ask requests for session_id=%s count=%d",
                sid,
                len(to_cancel),
            )
        self._session_interactive_ask.pop(sid, None)

    async def wait_for_answer(self, request_id: str, *, timeout: float) -> list[Any]:
        interactive_ask, session_id, _, _, _ = get_ask_request_context()
        effective_interactive = interactive_ask
        if not effective_interactive and session_id:
            effective_interactive = self.session_interactive_ask_enabled(session_id)
        fut = self.register(request_id, session_id if effective_interactive else "")
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.cleanup(request_id)
            raise
        except asyncio.CancelledError:
            self.cleanup(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)
            self._pending_sessions.pop(request_id, None)
