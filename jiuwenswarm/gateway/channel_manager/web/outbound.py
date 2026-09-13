# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Request-scoped HTTP outbounds for Gateway Web HTTP (JSON unary / SSE)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from jiuwenswarm.edition import is_enterprise

logger = logging.getLogger(__name__)

# Agent ``session.create`` rejects params.session_id (restore via session.switch).
_METHODS_WITHOUT_PARAM_SESSION_ID = frozenset({"session.create"})


def bind_http_session(
    method: str,
    params: dict[str, Any] | None,
    *,
    header_session_id: str = "",
    bind_param: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Return ``(envelope_session_id, params)`` for an HTTP call.

    Envelope session_id is always set (routing / request outbound). Client-supplied
    ``params.session_id`` is not invented for methods that Agent rejects.
    When ``bind_param`` is False, a missing session_id is not injected into
    params (settings / catalog RPCs).
    """
    out = dict(params or {})
    client_sid = str(out.get("session_id") or header_session_id or "").strip()
    if method in _METHODS_WITHOUT_PARAM_SESSION_ID:
        out.pop("session_id", None)
        if method == "session.create" and not str(out.get("create_token") or "").strip():
            out["create_token"] = uuid.uuid4().hex
        return f"webhttp_{uuid.uuid4().hex[:12]}", out
    envelope = client_sid or f"webhttp_{uuid.uuid4().hex[:12]}"
    if not bind_param:
        if client_sid:
            out["session_id"] = client_sid
        else:
            out.pop("session_id", None)
        return envelope, out
    if not client_sid:
        out.setdefault("session_id", envelope)
    else:
        out["session_id"] = client_sid
    return envelope, out


def _is_sse_end_frame(frame: dict[str, Any], req_id: str = "") -> bool:
    ev = str(frame.get("event") or "")
    if ev == "chat.error":
        return True
    # 企业版 HTTP/SSE 的 chat.final 只结束当前回复段，必须继续转发到
    # processing_status(false)；个人版维持原有 chat.final 终止语义。
    if ev == "chat.final":
        if not is_enterprise():
            return True
        return False
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    if ev == "chat.processing_status" and payload.get("is_processing") is False:
        if not is_enterprise():
            return False
        # cancel-replace 时旧 rid 的 false 可能落到新 SSE；只结束本连接自己的流。
        frame_rid = str(payload.get("request_id") or "").strip()
        sse_rid = str(req_id or "").strip()
        if sse_rid and frame_rid and frame_rid != sse_rid:
            return False
        return True
    return ev == "history.message" and payload.get("status") == "done"


def _normalize_frame(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        return data
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


@runtime_checkable
class Outbound(Protocol):
    """Minimal request-scoped reply target for Web HTTP."""

    is_http_outbound: bool
    closed: bool
    outbound_id: str

    def accept_frame(self, frame: dict[str, Any]) -> None:
        ...

    async def close(self) -> None:
        ...


class _HttpOutboundBase:
    """Shared fields for JSON / SSE HTTP outbounds."""

    is_http_outbound = True

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        remote: tuple[str, int] = ("127.0.0.1", 0),
        path: str = "/api/v1",
        session_id: str = "",
    ) -> None:
        self.request_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.remote_address = remote
        self.path = path
        self.session_id = str(session_id or "")
        self.closed = False
        self.outbound_id = uuid.uuid4().hex
        # Compatible with WebChannel metadata.ws_id / _lookup_peer.
        self._jiuwen_ws_id = self.outbound_id

    async def reply(
        self,
        req_id: str,
        *,
        ok: bool,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
        code: str | None = None,
    ) -> None:
        frame: dict[str, Any] = {
            "type": "res",
            "id": req_id,
            "ok": ok,
            "payload": payload or {},
        }
        if not ok:
            frame["error"] = error or "request failed"
            if code:
                frame["code"] = code
        self.accept_frame(frame)

    def emit(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        seq: int | None = None,
        stream_id: str | None = None,
    ) -> None:
        frame: dict[str, Any] = {"type": "event", "event": event, "payload": payload}
        if seq is not None:
            frame["seq"] = seq
        if stream_id is not None:
            frame["stream_id"] = stream_id
        self.accept_frame(frame)

    def accept_frame(self, frame: dict[str, Any]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def send(self, data: str | bytes) -> None:
        """Compat with writer-style ``ws.send`` (JSON string or bytes)."""
        if self.closed:
            raise ConnectionError("http outbound closed")
        frame = _normalize_frame(data)
        if frame is not None:
            self.accept_frame(frame)

    async def close(self) -> None:
        self.closed = True


class HttpJsonOutbound(_HttpOutboundBase):
    """Capture the matching ``res`` frame for unary HTTP handlers."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._frames: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)

    def accept_frame(self, frame: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            self._frames.put_nowait(frame)
        except asyncio.QueueFull:  # pragma: no cover
            logger.warning("[WebHTTP] HttpJsonOutbound queue full; dropping frame")

    async def wait_response(
        self,
        req_id: str,
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "error": "request timeout",
                    "code": "TIMEOUT",
                    "payload": {},
                }
            try:
                frame = await asyncio.wait_for(self._frames.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "error": "request timeout",
                    "code": "TIMEOUT",
                    "payload": {},
                }
            if frame is None:
                return {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "error": "connection closed",
                    "code": "INTERNAL_ERROR",
                    "payload": {},
                }
            if frame.get("type") == "res" and frame.get("id") == req_id:
                return frame

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._frames.put_nowait(None)
        except asyncio.QueueFull:
            pass


class HttpSseOutbound(_HttpOutboundBase):
    """Queue outbound frames for SSE / history aggregation consumers."""

    def __init__(self, *, maxsize: int = 256, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._frames: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=maxsize)

    def accept_frame(self, frame: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            self._frames.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning(
                "[WebHTTP] HttpSseOutbound queue full outbound_id=%s; dropping frame",
                self.outbound_id,
            )

    async def iter_sse_frames(
        self,
        req_id: str,
        *,
        timeout: float = 0.0,
        idle_timeout: float = 0.0,
        keepalive: float = 30.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield frames until stream end, optional total timeout, or optional idle timeout.

        ``timeout<=0`` means no total lifetime cap (long agent runs stay open).
        ``idle_timeout<=0`` emits keepalive while waiting and does not end on silence.
        When ``idle_timeout>0``, end with ``chat.error`` if no real frame arrives
        within that idle window.
        """
        loop = asyncio.get_running_loop()
        total_limit = float(timeout) if timeout and timeout > 0 else 0.0
        deadline = (loop.time() + total_limit) if total_limit > 0 else None
        idle_limit = float(idle_timeout) if idle_timeout and idle_timeout > 0 else 0.0
        last_frame_at = loop.time()
        ping = max(float(keepalive), 0.5)
        while True:
            now = loop.time()
            remaining = (deadline - now) if deadline is not None else None
            if remaining is not None and remaining <= 0:
                yield {
                    "type": "event",
                    "event": "chat.error",
                    "payload": {"error": "stream timeout", "session_id": ""},
                }
                return
            if idle_limit > 0 and (now - last_frame_at) >= idle_limit:
                yield {
                    "type": "event",
                    "event": "chat.error",
                    "payload": {"error": "stream idle timeout", "session_id": ""},
                }
                return
            wait_for = ping if remaining is None else min(remaining, ping)
            if idle_limit > 0:
                wait_for = min(wait_for, idle_limit - (now - last_frame_at))
            wait_for = max(wait_for, 0.05)
            try:
                frame = await asyncio.wait_for(self._frames.get(), timeout=wait_for)
            except asyncio.TimeoutError:
                if idle_limit > 0 and (loop.time() - last_frame_at) >= idle_limit:
                    yield {
                        "type": "event",
                        "event": "chat.error",
                        "payload": {"error": "stream idle timeout", "session_id": ""},
                    }
                    return
                yield {"type": "keepalive"}
                continue
            if frame is None:
                return
            last_frame_at = loop.time()
            ftype = frame.get("type")
            if ftype == "res" and frame.get("id") == req_id:
                yield frame
                if not frame.get("ok", True):
                    return
                continue
            if ftype == "event":
                yield frame
                if _is_sse_end_frame(frame, req_id):
                    return

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._frames.put_nowait(None)
        except asyncio.QueueFull:
            pass
