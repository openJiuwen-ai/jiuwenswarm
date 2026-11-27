from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from jiuwenclaw.channel.base import BaseChannel, RobotMessageRouter
from jiuwenclaw.e2a.acp.protocol import (
    build_acp_initialize_result,
    build_acp_prompt_result,
    build_acp_session_list_result,
    build_acp_session_new_result,
)
from jiuwenclaw.e2a.acp.session_updates import (
    build_acp_final_text_update,
    build_acp_session_update,
    build_acp_usage_update,
)
from jiuwenclaw.e2a.adapters import envelope_from_acp_jsonrpc
from jiuwenclaw.e2a.constants import (
    E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR,
    E2A_RESPONSE_KIND_ACP_PROMPT_RESULT,
    E2A_RESPONSE_KIND_ACP_SESSION_UPDATE,
    E2A_RESPONSE_KIND_E2A_CHUNK,
    E2A_RESPONSE_STATUS_FAILED,
    E2A_RESPONSE_STATUS_IN_PROGRESS,
    E2A_RESPONSE_STATUS_SUCCEEDED,
    E2A_SOURCE_PROTOCOL_E2A,
)
from jiuwenclaw.e2a.models import E2AEnvelope, E2AProvenance, E2AResponse, utc_now_iso
from jiuwenclaw.schema.message import EventType, Message, Mode, ReqMethod

logger = logging.getLogger(__name__)

_ACP_STDOUT = getattr(sys, "__stdout__", sys.stdout)
_STDIN_EOF_GRACE_SECONDS = 5.0
_PROMPT_IDLE_FINALIZE_SECONDS = 3.0
_ACP_PENDING_RPC_TIMEOUT_SECONDS = 60.0
_ACP_GATEWAY_CONNECT_MAX_ATTEMPTS = 12
_ACP_GATEWAY_CONNECT_BASE_DELAY_SEC = 0.15


@dataclass
class AcpChannelConfig:
    enabled: bool = True
    channel_id: str = "acp"
    default_session_id: str = "acp_cli_session"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _AcpRequestContext:
    jsonrpc_id: str | int | None
    method: str | None
    response_mode: str = "e2a"
    session_id: str | None = None
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    assistant_text: str | None = None
    thought_message_id: str | None = None
    thought_text: str | None = None
    tool_call_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_stop_reason: str | None = None
    saw_chat_final: bool = False
    saw_processing_idle: bool = False
    sequence: int = 0
    idle_finalize_task: asyncio.Task | None = None


@dataclass
class AcpGatewayRequestContext:
    jsonrpc_id: str | int | None
    session_id: str
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    assistant_text: str | None = None
    thought_message_id: str | None = None
    thought_text: str | None = None
    tool_call_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_stop_reason: str | None = None
    saw_chat_final: bool = False
    saw_processing_idle: bool = False
    client_ws: Any | None = None
    idle_finalize_task: asyncio.Task | None = None


def _cancel_idle_finalize_task(ctx: Any) -> None:
    task = getattr(ctx, "idle_finalize_task", None)
    if task is not None:
        task.cancel()
        ctx.idle_finalize_task = None


def _mark_stream_activity(ctx: Any) -> None:
    setattr(ctx, "saw_processing_idle", False)
    _cancel_idle_finalize_task(ctx)


def _should_wait_for_final_text_before_end_turn(ctx: Any) -> bool:
    if bool(getattr(ctx, "saw_chat_final", False)):
        return False
    return bool(
        getattr(ctx, "assistant_text", None)
        or getattr(ctx, "assistant_message_id", None)
    )


class AcpGatewayBridge:
    """Reusable ACP JSON-RPC bridge for Gateway websocket routes."""

    def __init__(
        self,
        on_message_cb: Callable[[Message], Any] | None,
        *,
        bind_session_client: Callable[[str, Any], None] | None = None,
        channel_id: str = "acp",
        idle_finalize_seconds: float | Callable[[], float] | None = None,
    ) -> None:
        self._on_message_cb = on_message_cb
        self._bind_session_client = bind_session_client
        self._channel_id = str(channel_id or "acp").strip() or "acp"
        self._idle_finalize_seconds = idle_finalize_seconds
        self._request_ctx_by_request_id: dict[str, AcpGatewayRequestContext] = {}
        self._active_prompt_request_by_session: dict[str, str] = {}
        self._known_sessions: set[str] = set()
        self._pending_client_rpc_count_by_session: dict[str, int] = {}
        # value: (session_id, ws, created_at)
        self._pending_client_rpc_session_by_id: dict[str, tuple[str, Any, float]] = {}

    @property
    def request_contexts(self) -> list[AcpGatewayRequestContext]:
        """Return a snapshot list of all pending gateway request contexts."""
        return list(self._request_ctx_by_request_id.values())

    async def inbound_intercept(self, ws: Any, data: dict[str, Any]) -> bool:
        """Intercept ACP JSON-RPC responses from the IDE client."""
        if not (
            isinstance(data, dict)
            and str(data.get("jsonrpc") or "").strip() == "2.0"
            and str(data.get("id") or "").strip() != ""
            and ("result" in data or "error" in data)
        ):
            return False

        from jiuwenclaw.e2a.adapters import build_acp_tool_response_message

        jsonrpc_id = str(data.get("id") or "").strip()
        pending = self._pending_client_rpc_session_by_id.pop(jsonrpc_id, None)
        session_id = ""
        if isinstance(pending, tuple) and len(pending) >= 2:
            session_id = str(pending[0] or "").strip()

        if not jsonrpc_id or not session_id:
            logger.info(
                "[ACP] ignoring unknown/late client rpc response: jsonrpc_id=%s session_id=%s",
                jsonrpc_id,
                session_id,
            )
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": jsonrpc_id,
                        "ok": True,
                        "payload": {
                            "accepted": False,
                            "ignored": True,
                            "reason": "unknown_or_late_response",
                        },
                    },
                    ensure_ascii=False,
                )
            )
            return True

        msg = build_acp_tool_response_message(
            jsonrpc_id=jsonrpc_id,
            response_data=data,
            session_id=session_id,
            channel_id=self._channel_id,
        )
        await self._dispatch_message(msg)
        if session_id:
            await self._resolve_pending_client_rpc(session_id)
        return True

    async def outbound_intercept(self, msg: Message, ws: Any) -> bool:
        """Intercept ACP output_request events and relay them as raw JSON-RPC."""
        if (
            msg.type == "event"
            and isinstance(msg.payload, dict)
            and str(msg.payload.get("event_type") or "").strip() == "acp.output_request"
        ):
            raw_jsonrpc = msg.payload.get("jsonrpc")
            if not isinstance(raw_jsonrpc, dict):
                logger.warning(
                    "[ACP] outbound_intercept: invalid jsonrpc payload (not dict), dropping frame"
                )
                return True
            jsonrpc_id = str(raw_jsonrpc.get("id") or "").strip()
            session_id = str(msg.session_id or "").strip()
            if not jsonrpc_id or not session_id:
                logger.warning(
                    "[ACP] outbound_intercept: missing jsonrpc_id=%r or session_id=%r, "
                    "forwarding without pending registration",
                    jsonrpc_id,
                    session_id,
                )
            else:
                self._pending_client_rpc_session_by_id[jsonrpc_id] = (session_id, ws, time.time())
                await self._register_pending_client_rpc(session_id)
            await self._sweep_stale_pending()
            await ws.send(json.dumps(raw_jsonrpc, ensure_ascii=False))
            return True
        return False

    def cleanup(self, ws: Any) -> None:
        """Clean up pending client RPC entries associated with a disconnected ws."""
        stale = [
            jsonrpc_id
            for jsonrpc_id, entry in self._pending_client_rpc_session_by_id.items()
            if len(entry) >= 2 and entry[1] is ws
        ]
        for jsonrpc_id in stale:
            entry = self._pending_client_rpc_session_by_id.pop(jsonrpc_id, None)
            if isinstance(entry, tuple) and len(entry) >= 1:
                self._decrease_pending_client_rpc_count(str(entry[0] or "").strip())

    async def handle_jsonrpc_request(self, ws: Any, data: dict[str, Any]) -> bool:
        if not self.is_jsonrpc_request(data):
            return False

        rpc_id = data.get("id")
        method = str(data.get("method") or "").strip()
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        try:
            if method == "initialize":
                await self._send_raw_jsonrpc_result(ws, rpc_id, build_acp_initialize_result())
                return True
            if method == "session/new":
                session_id = str(params.get("sessionId") or f"acp_{uuid.uuid4().hex[:12]}").strip()
                self._bind_session(ws, session_id)
                await self._send_raw_jsonrpc_result(ws, rpc_id, build_acp_session_new_result(session_id))
                return True
            if method == "session/prompt":
                session_id = str(params.get("sessionId") or "").strip()
                if not session_id:
                    raise ValueError("sessionId is required")
                text = self.extract_prompt_text(params)
                if not text:
                    raise ValueError("prompt is required")

                request_id = f"acp_{uuid.uuid4().hex[:12]}"
                self._bind_session(ws, session_id)
                self._request_ctx_by_request_id[request_id] = AcpGatewayRequestContext(
                    jsonrpc_id=rpc_id,
                    session_id=session_id,
                    user_message_id=str(params.get("messageId") or "").strip() or None,
                    client_ws=ws,
                )
                self._active_prompt_request_by_session[session_id] = request_id
                await self._dispatch_message(
                    Message(
                        id=request_id,
                        type="req",
                        channel_id=self._channel_id,
                        session_id=session_id,
                        params={
                            **dict(params),
                            "content": text,
                            "query": text,
                            "session_id": session_id,
                        },
                        timestamp=time.time(),
                        ok=True,
                        req_method=ReqMethod.CHAT_SEND,
                        mode=Mode.AGENT_PLAN,
                        metadata={"acp": {"jsonrpc_id": rpc_id, "method": method}},
                    )
                )
                return True
            if method == "session/cancel":
                session_id = str(params.get("sessionId") or "").strip()
                if session_id:
                    request_id = self._active_prompt_request_by_session.pop(session_id, None)
                    if request_id is not None:
                        ctx = self._request_ctx_by_request_id.pop(request_id, None)
                        if ctx is not None:
                            await self._send_raw_jsonrpc_result(
                                ws,
                                ctx.jsonrpc_id,
                                build_acp_prompt_result(
                                    stop_reason="cancelled",
                                    user_message_id=ctx.user_message_id,
                                ),
                            )
                await self._send_raw_jsonrpc_result(ws, rpc_id, None)
                return True
            if method == "session/list":
                await self._send_raw_jsonrpc_result(
                    ws,
                    rpc_id,
                    build_acp_session_list_result(sorted(self._known_sessions)),
                )
                return True
            if method == "session/load":
                await self._send_raw_jsonrpc_error(
                    ws,
                    rpc_id,
                    -32601,
                    "Method not supported by agent capabilities: session/load",
                )
                return True
            await self._send_raw_jsonrpc_error(ws, rpc_id, -32601, f"Method not found: {method}")
        except ValueError as exc:
            await self._send_raw_jsonrpc_error(ws, rpc_id, -32602, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Gateway ACP JSON-RPC handler failed: method=%s", method)
            await self._send_raw_jsonrpc_error(ws, rpc_id, -32603, str(exc))
        return True

    async def send_message(self, msg: Message, ws: Any) -> bool:
        ctx = self._request_ctx_by_request_id.get(str(getattr(msg, "id", "")))
        payload = dict(getattr(msg, "payload", None) or {})
        session_id = str(getattr(msg, "session_id", None) or "")

        if ctx is None:
            return False

        if msg.type == "event" and msg.event_type == EventType.CHAT_DELTA:
            update = build_acp_session_update(msg, payload, ctx)
            if update is None:
                return True
            _mark_stream_activity(ctx)
            await self._send_raw_jsonrpc_notification(
                ws,
                "session/update",
                {"sessionId": session_id or ctx.session_id, "update": update},
            )
            usage_update = build_acp_usage_update(payload)
            if usage_update is not None:
                await self._send_raw_jsonrpc_notification(
                    ws,
                    "session/update",
                    {"sessionId": session_id or ctx.session_id, "update": usage_update},
                )
            return True

        if msg.type == "event" and msg.event_type in (
            EventType.CHAT_REASONING,
            EventType.CHAT_TOOL_CALL,
            EventType.CHAT_TOOL_UPDATE,
            EventType.CHAT_TOOL_RESULT,
            EventType.TODO_UPDATED,
            EventType.CHAT_SUBTASK_UPDATE,
        ):
            update = build_acp_session_update(msg, payload, ctx)
            if update is None:
                return True
            _mark_stream_activity(ctx)
            await self._send_raw_jsonrpc_notification(
                ws,
                "session/update",
                {"sessionId": session_id or ctx.session_id, "update": update},
            )
            return True

        if msg.type == "event" and msg.event_type == EventType.CHAT_FINAL:
            ctx.saw_chat_final = True
            update = build_acp_final_text_update(payload, ctx)
            if update is not None:
                await self._send_raw_jsonrpc_notification(
                    ws,
                    "session/update",
                    {"sessionId": session_id or ctx.session_id, "update": update},
                )
            usage_update = build_acp_usage_update(payload)
            if usage_update is not None:
                await self._send_raw_jsonrpc_notification(
                    ws,
                    "session/update",
                    {"sessionId": session_id or ctx.session_id, "update": usage_update},
                )
            if ctx.saw_processing_idle:
                if self._session_has_pending_client_rpcs(session_id or ctx.session_id):
                    ctx.pending_stop_reason = "end_turn"
                    return True
                _cancel_idle_finalize_task(ctx)
                await self._send_raw_jsonrpc_result(
                    ws,
                    ctx.jsonrpc_id,
                    build_acp_prompt_result(
                        stop_reason="end_turn",
                        user_message_id=ctx.user_message_id,
                    ),
                )
                self._clear_request_context(str(msg.id), ctx.session_id)
                return True
            _cancel_idle_finalize_task(ctx)
            return True

        if msg.type == "event" and msg.event_type == EventType.CHAT_ERROR:
            _cancel_idle_finalize_task(ctx)
            await self._send_raw_jsonrpc_error(
                ws,
                ctx.jsonrpc_id,
                -32603,
                str(payload.get("error") or payload.get("content") or "Agent error"),
            )
            self._clear_request_context(str(msg.id), ctx.session_id)
            return True

        if msg.type == "event" and msg.event_type == EventType.CHAT_PROCESSING_STATUS:
            update = build_acp_session_update(msg, payload, ctx)
            if update is not None:
                await self._send_raw_jsonrpc_notification(
                    ws,
                    "session/update",
                    {"sessionId": session_id or ctx.session_id, "update": update},
                )
                if payload.get("is_processing") is False:
                    ctx.saw_processing_idle = True
                    if self._session_has_pending_client_rpcs(session_id or ctx.session_id):
                        ctx.pending_stop_reason = "end_turn"
                        return True
                    if _should_wait_for_final_text_before_end_turn(ctx):
                        self._schedule_idle_finalize(str(msg.id), ctx, ws)
                        return True
                    _cancel_idle_finalize_task(ctx)
                    await self._send_raw_jsonrpc_result(
                        ws,
                        ctx.jsonrpc_id,
                        build_acp_prompt_result(
                            stop_reason="end_turn",
                            user_message_id=ctx.user_message_id,
                        ),
                    )
                    self._clear_request_context(str(msg.id), ctx.session_id)
                    return True
                if payload.get("is_processing") is True:
                    ctx.saw_processing_idle = False
                    _cancel_idle_finalize_task(ctx)
            return True

        return False

    @staticmethod
    def is_jsonrpc_request(data: Any) -> bool:
        return (
            isinstance(data, dict)
            and data.get("jsonrpc") == "2.0"
            and isinstance(data.get("method"), str)
        )

    @staticmethod
    def extract_prompt_text(params: dict[str, Any]) -> str:
        prompt = params.get("prompt")
        if isinstance(prompt, list):
            texts: list[str] = []
            for item in prompt:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        texts.append(text)
            if texts:
                return "\n".join(texts)
        for key in ("text", "content", "query"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    async def _dispatch_message(self, msg: Message) -> bool:
        if self._on_message_cb is None:
            return False
        result = self._on_message_cb(msg)
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    def _bind_session(self, ws: Any, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        self._known_sessions.add(sid)
        if self._bind_session_client is not None:
            self._bind_session_client(sid, ws)

    async def _register_pending_client_rpc(self, session_id: str | None) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        self._pending_client_rpc_count_by_session[sid] = (
            self._pending_client_rpc_count_by_session.get(sid, 0) + 1
        )
        for ctx in self._request_ctx_by_request_id.values():
            if str(ctx.session_id or "") == sid:
                task = ctx.idle_finalize_task
                if task is not None:
                    task.cancel()
                    ctx.idle_finalize_task = None

    async def _resolve_pending_client_rpc(self, session_id: str | None) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        self._decrease_pending_client_rpc_count(sid)
        await self._maybe_finalize_deferred_prompts(sid)

    def _decrease_pending_client_rpc_count(self, session_id: str | None) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        current = self._pending_client_rpc_count_by_session.get(sid, 0)
        if current <= 1:
            self._pending_client_rpc_count_by_session.pop(sid, None)
        else:
            self._pending_client_rpc_count_by_session[sid] = current - 1

    def _session_has_pending_client_rpcs(self, session_id: str | None) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        return self._pending_client_rpc_count_by_session.get(sid, 0) > 0

    async def _maybe_finalize_deferred_prompts(self, session_id: str | None) -> None:
        sid = str(session_id or "").strip()
        if not sid or self._session_has_pending_client_rpcs(sid):
            return

        matched_ids: list[str] = []
        for request_id, ctx in self._request_ctx_by_request_id.items():
            if str(ctx.session_id or "") != sid:
                continue
            if not str(ctx.pending_stop_reason or "").strip():
                continue
            matched_ids.append(request_id)
        for request_id in matched_ids:
            ctx = self._request_ctx_by_request_id.get(request_id)
            if ctx is None:
                continue
            stop_reason = str(ctx.pending_stop_reason or "").strip()
            if not stop_reason:
                continue
            ctx.pending_stop_reason = None
            ws = ctx.client_ws
            if ws is None or bool(getattr(ws, "closed", False)):
                continue
            if _should_wait_for_final_text_before_end_turn(ctx):
                self._schedule_idle_finalize(request_id, ctx, ws)
                continue
            _cancel_idle_finalize_task(ctx)
            await self._send_raw_jsonrpc_result(
                ws,
                ctx.jsonrpc_id,
                build_acp_prompt_result(
                    stop_reason=stop_reason,
                    user_message_id=ctx.user_message_id,
                ),
            )
            self._clear_request_context(request_id, sid)

    def _clear_request_context(self, request_id: str, session_id: str) -> None:
        ctx = self._request_ctx_by_request_id.pop(request_id, None)
        if ctx is not None and ctx.idle_finalize_task is not None:
            ctx.idle_finalize_task.cancel()
            ctx.idle_finalize_task = None
        if self._active_prompt_request_by_session.get(session_id) == request_id:
            self._active_prompt_request_by_session.pop(session_id, None)

    def _schedule_idle_finalize(self, request_id: str, ctx: AcpGatewayRequestContext, ws: Any) -> None:
        task = ctx.idle_finalize_task
        if task is not None:
            task.cancel()
        ctx.idle_finalize_task = asyncio.create_task(
            self._idle_finalize_after_timeout(request_id, ws),
            name=f"gateway-acp-idle-finalize-{request_id}",
        )

    async def _idle_finalize_after_timeout(self, request_id: str, ws: Any) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._get_idle_finalize_seconds())
            ctx = self._request_ctx_by_request_id.get(request_id)
            if ctx is None or ctx.idle_finalize_task is not current_task:
                return
            if self._session_has_pending_client_rpcs(ctx.session_id):
                ctx.pending_stop_reason = "end_turn"
                ctx.idle_finalize_task = None
                return
            await self._send_raw_jsonrpc_result(
                ws,
                ctx.jsonrpc_id,
                build_acp_prompt_result(
                    stop_reason="end_turn",
                    user_message_id=ctx.user_message_id,
                ),
            )
            self._clear_request_context(request_id, ctx.session_id)
        except asyncio.CancelledError:
            return

    def _get_idle_finalize_seconds(self) -> float:
        value = self._idle_finalize_seconds
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                logger.debug("[ACP] idle finalize seconds getter failed", exc_info=True)
                value = None
        if isinstance(value, (int, float)):
            return float(value)
        return _PROMPT_IDLE_FINALIZE_SECONDS

    async def _sweep_stale_pending(self) -> None:
        now = time.time()
        stale = [
            (jsonrpc_id, str(entry[0] or "").strip())
            for jsonrpc_id, entry in self._pending_client_rpc_session_by_id.items()
            if len(entry) >= 3 and (now - entry[2]) > _ACP_PENDING_RPC_TIMEOUT_SECONDS
        ]
        for jsonrpc_id, session_id in stale:
            self._pending_client_rpc_session_by_id.pop(jsonrpc_id, None)
            logger.info("[ACP] pending RPC entry expired: jsonrpc_id=%s", jsonrpc_id)
            await self._resolve_pending_client_rpc(session_id)

    @staticmethod
    async def _send_raw_jsonrpc_result(ws: Any, rpc_id: str | int | None, result: Any) -> None:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result}, ensure_ascii=False))

    @staticmethod
    async def _send_raw_jsonrpc_error(ws: Any, rpc_id: str | int | None, code: int, message: str) -> None:
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": code, "message": message},
                },
                ensure_ascii=False,
            )
        )

    @staticmethod
    async def _send_raw_jsonrpc_notification(ws: Any, method: str, params: dict[str, Any]) -> None:
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False))


class AcpChannel(BaseChannel):
    """ACP stdio 通道。

    - 入站：stdin 每行一个 JSON，支持 E2AEnvelope 或 ACP JSON-RPC 请求。
    - 出站：stdout 每行一个 E2AResponse JSON。
    - 语义：将 ``session/prompt`` 映射为内部 ``chat.send``。
    """

    name = "acp"

    def __init__(
        self,
        config: AcpChannelConfig,
        router: RobotMessageRouter,
        *,
        gateway_url: str | None = None,
    ):
        super().__init__(config, router)
        self.config: AcpChannelConfig = config
        self._gateway_url = gateway_url
        self._gateway_ws = None
        self._gateway_reader_task: asyncio.Task | None = None
        self._on_message_cb: Callable[[Message], Any] | None = None
        self._request_ctx: dict[str, _AcpRequestContext] = {}
        self._session_ctx: dict[str, dict[str, Any]] = {}
        self._active_prompt_request_by_session: dict[str, str] = {}
        self._known_sessions: set[str] = set()
        # value: (session_id, created_at)
        self._pending_client_rpc_session_by_id: dict[str, tuple[str, float]] = {}

    @property
    def channel_id(self) -> str:
        return str(self.config.channel_id or self.name).strip() or self.name

    def on_message(self, callback: Callable[[Message], Any]) -> None:
        self._on_message_cb = callback

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._on_message_cb is None and self._gateway_url:
            await self._ensure_gateway_connection()
        stdin_eof = False
        stdin_eof_since: float | None = None
        while self._running:
            if stdin_eof:
                if not self._request_ctx and not self._pending_client_rpc_session_by_id and stdin_eof_since is not None:
                    if (time.time() - stdin_eof_since) >= _STDIN_EOF_GRACE_SECONDS:
                        break
                await asyncio.sleep(0.05)
                continue
            raw = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not raw:
                stdin_eof = True
                if stdin_eof_since is None:
                    stdin_eof_since = time.time()
                continue
            stdin_eof = False
            stdin_eof_since = None
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                await self._handle_raw_line(line)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[ACP] stdio inbound failed: %s", exc)
                await self._write_response(
                    E2AResponse(
                        response_id=f"acp-err-{uuid.uuid4().hex[:8]}",
                        request_id=None,
                        is_final=True,
                        status=E2A_RESPONSE_STATUS_FAILED,
                        response_kind=E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR,
                        timestamp=utc_now_iso(),
                        provenance=self._provenance("stdio_error"),
                        channel=self.channel_id,
                        body={"code": -32603, "message": str(exc)},
                    )
                )

    async def stop(self) -> None:
        self._running = False
        for request_id in list(self._request_ctx.keys()):
            await self._clear_request_context(request_id)
        self._pending_client_rpc_session_by_id.clear()
        await self._close_gateway_connection()

    async def _sweep_stale_pending(self) -> None:
        """移除超时的 pending RPC 条目，防止永久堆积。"""
        now = time.time()
        stale = [
            (jsonrpc_id, str(entry[0] or "").strip())
            for jsonrpc_id, entry in self._pending_client_rpc_session_by_id.items()
            if isinstance(entry, tuple) and len(entry) >= 2 and (now - entry[1]) > _ACP_PENDING_RPC_TIMEOUT_SECONDS
        ]
        affected_sessions: set[str] = set()
        for jsonrpc_id, session_id in stale:
            self._pending_client_rpc_session_by_id.pop(jsonrpc_id, None)
            logger.info("[ACP] pending RPC entry expired: jsonrpc_id=%s", jsonrpc_id)
            if session_id:
                affected_sessions.add(session_id)
        for session_id in affected_sessions:
            await self._maybe_finalize_deferred_prompts(session_id)

    async def send(self, msg: Message) -> None:
        ctx = self._request_ctx.get(str(msg.id))
        if ctx is None:
            logger.debug("[ACP] skip outbound without request context: id=%s", msg.id)
            return

        if ctx.response_mode == "jsonrpc":
            is_final = await self._send_jsonrpc_message(msg, ctx)
            if is_final:
                await self._clear_request_context(str(msg.id))
            return

        response = self._message_to_e2a_response(msg, ctx)
        if response is None:
            return
        await self._write_response(response)
        if response.is_final:
            await self._clear_request_context(str(msg.id))

    async def _handle_raw_line(self, line: str) -> None:
        data = json.loads(line)
        if self._is_jsonrpc_request(data):
            await self._handle_jsonrpc_request(data)
            return
        if self._is_jsonrpc_response(data):
            await self._handle_jsonrpc_response(data)
            return

        env = self._parse_envelope(data)
        msg = self._envelope_to_message(env)
        self._request_ctx[msg.id] = _AcpRequestContext(
            jsonrpc_id=env.jsonrpc_id,
            method=env.method,
            response_mode="e2a",
            session_id=msg.session_id,
        )

        await self._dispatch_message(msg)

    async def _handle_jsonrpc_response(self, data: dict[str, Any]) -> None:
        from jiuwenclaw.e2a.adapters import build_acp_tool_response_message

        jsonrpc_id = str(data.get("id") or "").strip()
        if not jsonrpc_id:
            return

        pending = self._pending_client_rpc_session_by_id.pop(jsonrpc_id, None)
        session_id = pending[0] if isinstance(pending, tuple) else None
        if not session_id:
            logger.info(
                "[ACP] ignoring unknown/late stdio jsonrpc response: jsonrpc_id=%s",
                jsonrpc_id,
            )
            return
        msg = build_acp_tool_response_message(
            jsonrpc_id=jsonrpc_id,
            response_data=data,
            session_id=session_id,
            channel_id=self.channel_id,
        )
        await self._dispatch_message(msg)
        if session_id:
            await self._maybe_finalize_deferred_prompts(session_id)

    def _parse_envelope(self, data: dict[str, Any]) -> E2AEnvelope:
        env = E2AEnvelope.from_dict(dict(data))
        if not env.request_id:
            env.request_id = f"acp_{uuid.uuid4().hex[:12]}"
        if not env.channel:
            env.channel = self.channel_id
        return env

    def _envelope_to_message(self, env: E2AEnvelope) -> Message:
        method = str(env.method or "").strip()
        params = dict(env.params or {})
        session_id = (
            env.session_id
            or params.get("session_id")
            or self.config.default_session_id
        )

        req_method = self._parse_req_method(method)
        if method == "session/prompt":
            text = self._extract_prompt_text(params)
            params.setdefault("content", text)
            params.setdefault("query", text)
            req_method = ReqMethod.CHAT_SEND
        if req_method is None:
            raise ValueError(f"unsupported ACP/E2A method: {method or '<empty>'}")

        if req_method == ReqMethod.CHAT_SEND:
            params.setdefault("query", params.get("content", ""))

        return Message(
            id=str(env.request_id),
            type="req",
            channel_id=self.channel_id,
            session_id=str(session_id),
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=req_method,
            is_stream=bool(env.is_stream or req_method == ReqMethod.CHAT_SEND),
            metadata={
                "acp": {
                    "jsonrpc_id": env.jsonrpc_id,
                    "method": method,
                    **dict(self.config.metadata or {}),
                }
            },
        )

    def _message_to_e2a_response(
        self,
        msg: Message,
        ctx: _AcpRequestContext,
    ) -> E2AResponse | None:
        payload = dict(msg.payload or {})
        ts = utc_now_iso()
        sequence = ctx.sequence

        if msg.type == "event" and msg.event_type == EventType.CHAT_DELTA:
            ctx.sequence += 1
            source_chunk_type = payload.get("source_chunk_type")
            delta_kind = "reasoning" if source_chunk_type == "llm_reasoning" else "text"
            return E2AResponse(
                response_id=f"{msg.id}:{sequence}",
                request_id=msg.id,
                jsonrpc_id=ctx.jsonrpc_id,
                sequence=sequence,
                is_final=False,
                status=E2A_RESPONSE_STATUS_IN_PROGRESS,
                response_kind=E2A_RESPONSE_KIND_E2A_CHUNK,
                timestamp=ts,
                provenance=self._provenance("chat.delta"),
                channel=self.channel_id,
                session_id=msg.session_id,
                is_stream=True,
                body={
                    "event_type": "chat.delta",
                    "delta_kind": delta_kind,
                    "delta": str(payload.get("content", "") or ""),
                    "payload": payload,
                },
            )

        if msg.type == "event" and msg.event_type == EventType.CHAT_ERROR:
            ctx.sequence += 1
            return E2AResponse(
                response_id=f"{msg.id}:{sequence}",
                request_id=msg.id,
                jsonrpc_id=ctx.jsonrpc_id,
                sequence=sequence,
                is_final=True,
                status=E2A_RESPONSE_STATUS_FAILED,
                response_kind=E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR,
                timestamp=ts,
                provenance=self._provenance("chat.error"),
                channel=self.channel_id,
                session_id=msg.session_id,
                body={
                    "code": -32603,
                    "message": str(payload.get("error") or payload.get("content") or "Agent error"),
                },
            )

        if msg.type == "event" and msg.event_type == EventType.CHAT_FINAL:
            ctx.sequence += 1
            result_body = dict(payload)
            result_body.setdefault("session_id", msg.session_id)
            return E2AResponse(
                response_id=f"{msg.id}:{sequence}",
                request_id=msg.id,
                jsonrpc_id=ctx.jsonrpc_id,
                sequence=sequence,
                is_final=True,
                status=E2A_RESPONSE_STATUS_SUCCEEDED,
                response_kind=E2A_RESPONSE_KIND_ACP_PROMPT_RESULT,
                timestamp=ts,
                provenance=self._provenance("chat.final"),
                channel=self.channel_id,
                session_id=msg.session_id,
                body=result_body,
            )

        if msg.type == "event":
            update = self._build_acp_session_update(msg, payload, ctx)
            if update is not None:
                ctx.sequence += 1
                return E2AResponse(
                    response_id=f"{msg.id}:{sequence}",
                    request_id=msg.id,
                    jsonrpc_id=ctx.jsonrpc_id,
                    sequence=sequence,
                    is_final=False,
                    status=E2A_RESPONSE_STATUS_IN_PROGRESS,
                    response_kind=E2A_RESPONSE_KIND_ACP_SESSION_UPDATE,
                    timestamp=ts,
                    provenance=self._provenance(str(msg.event_type.value if msg.event_type else "session.update")),
                    channel=self.channel_id,
                    session_id=msg.session_id,
                    is_stream=True,
                    body={
                        "sessionId": str(msg.session_id or ctx.session_id or ""),
                        "update": update,
                    },
                )

        if msg.type == "res" and msg.ok:
            if payload.get("accepted") is True:
                return None
            ctx.sequence += 1
            result_body = dict(payload)
            result_body.setdefault("session_id", msg.session_id)
            return E2AResponse(
                response_id=f"{msg.id}:{sequence}",
                request_id=msg.id,
                jsonrpc_id=ctx.jsonrpc_id,
                sequence=sequence,
                is_final=True,
                status=E2A_RESPONSE_STATUS_SUCCEEDED,
                response_kind=E2A_RESPONSE_KIND_ACP_PROMPT_RESULT,
                timestamp=ts,
                provenance=self._provenance("response.ok"),
                channel=self.channel_id,
                session_id=msg.session_id,
                body=result_body,
            )

        if msg.type == "event":
            # 辅助事件先忽略，避免把 processing_status/tool_call/todo 等中间态误判为最终失败。
            return None

        ctx.sequence += 1
        error_text = str(payload.get("error") or payload.get("content") or "request failed")
        return E2AResponse(
            response_id=f"{msg.id}:{sequence}",
            request_id=msg.id,
            jsonrpc_id=ctx.jsonrpc_id,
            sequence=sequence,
            is_final=True,
            status=E2A_RESPONSE_STATUS_FAILED,
            response_kind=E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR,
            timestamp=ts,
            provenance=self._provenance("response.error"),
            channel=self.channel_id,
            session_id=msg.session_id,
            body={"code": -32603, "message": error_text},
        )

    async def _write_response(self, response: E2AResponse) -> None:
        line = json.dumps(response.to_dict(), ensure_ascii=False)
        _ACP_STDOUT.buffer.write((line + "\n").encode("utf-8"))
        _ACP_STDOUT.buffer.flush()

    async def _write_jsonrpc_result(self, rpc_id: str | int | None, result: Any) -> None:
        payload = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        _ACP_STDOUT.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        _ACP_STDOUT.buffer.flush()

    async def _write_jsonrpc_error(
        self,
        rpc_id: str | int | None,
        code: int,
        message: str,
    ) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": code,
                "message": message,
            },
        }
        _ACP_STDOUT.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        _ACP_STDOUT.buffer.flush()

    async def _write_jsonrpc_notification(self, method: str, params: dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        _ACP_STDOUT.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        _ACP_STDOUT.buffer.flush()

    async def _handle_jsonrpc_request(self, data: dict[str, Any]) -> None:
        rpc_id = data.get("id")
        method = str(data.get("method") or "").strip()
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        try:
            if method == "initialize":
                await self._write_jsonrpc_result(rpc_id, self._initialize_result())
                await self._notify_agent_initialize(params)
                return
            if method == "session/new":
                await self._handle_jsonrpc_session_new(rpc_id, params)
                return
            if method == "session/prompt":
                await self._handle_jsonrpc_session_prompt(rpc_id, params)
                return
            if method == "session/cancel":
                await self._handle_jsonrpc_session_cancel(rpc_id, params)
                return
            if method == "session/list":
                await self._write_jsonrpc_result(
                    rpc_id,
                    build_acp_session_list_result(sorted(self._known_sessions)),
                )
                return
            if method == "session/load":
                await self._write_jsonrpc_error(
                    rpc_id,
                    -32601,
                    "Method not supported by agent capabilities: session/load",
                )
                return
            await self._write_jsonrpc_error(rpc_id, -32601, f"Method not found: {method}")
        except ValueError as exc:
            await self._write_jsonrpc_error(rpc_id, -32602, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ACP] jsonrpc request failed: %s", exc)
            await self._write_jsonrpc_error(rpc_id, -32603, str(exc))

    async def _notify_agent_initialize(self, params: dict[str, Any]) -> None:
        msg = Message(
            id=f"acp_init_{uuid.uuid4().hex[:12]}",
            type="req",
            channel_id=self.channel_id,
            session_id=self.config.default_session_id,
            params=dict(params),
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.INITIALIZE,
            is_stream=False,
            metadata={"acp": {"method": "initialize"}},
        )
        try:
            await self._dispatch_message(msg)
        except Exception:
            logger.debug("[ACP] failed to forward initialize to gateway", exc_info=True)

    def _initialize_result(self) -> dict[str, Any]:
        return build_acp_initialize_result()

    async def _handle_jsonrpc_session_new(
        self,
        rpc_id: str | int | None,
        params: dict[str, Any],
    ) -> None:
        session_id = str(params.get("sessionId") or f"acp_{uuid.uuid4().hex[:12]}").strip()
        self._known_sessions.add(session_id)
        self._session_ctx[session_id] = dict(params)
        await self._write_jsonrpc_result(
            rpc_id,
            build_acp_session_new_result(session_id),
        )

    async def _handle_jsonrpc_session_prompt(
        self,
        rpc_id: str | int | None,
        params: dict[str, Any],
    ) -> None:
        session_id = str(params.get("sessionId") or "").strip()
        if not session_id:
            raise ValueError("sessionId is required")
        self._known_sessions.add(session_id)

        rpc_params = dict(params)
        prompt = rpc_params.get("prompt")
        if not isinstance(prompt, list) or not prompt:
            text = self._extract_prompt_text(rpc_params)
            if not text:
                raise ValueError("prompt is required")
            rpc_params["prompt"] = [{"type": "text", "text": text}]
        rpc_params["session_id"] = session_id
        session_ctx = self._session_ctx.get(session_id)
        if isinstance(session_ctx, dict):
            for key, value in session_ctx.items():
                rpc_params.setdefault(key, value)

        env = envelope_from_acp_jsonrpc(
            method="session/prompt",
            params=rpc_params,
            jsonrpc_id=rpc_id,
            session_id=session_id,
            channel=self.channel_id,
        )
        env.request_id = f"acp_{uuid.uuid4().hex[:12]}"
        env.is_stream = True

        msg = self._envelope_to_message(env)
        self._request_ctx[msg.id] = _AcpRequestContext(
            jsonrpc_id=rpc_id,
            method=env.method,
            response_mode="jsonrpc",
            session_id=session_id,
            user_message_id=str(rpc_params.get("messageId") or "").strip() or None,
        )
        self._active_prompt_request_by_session[session_id] = msg.id
        await self._dispatch_message(msg)

    async def _handle_jsonrpc_session_cancel(
        self,
        rpc_id: str | int | None,
        params: dict[str, Any],
    ) -> None:
        session_id = str(params.get("sessionId") or "").strip()
        if not session_id:
            raise ValueError("sessionId is required")

        msg = Message(
            id=f"acp_cancel_{uuid.uuid4().hex[:12]}",
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params={"session_id": session_id},
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_CANCEL,
            is_stream=False,
            metadata={"acp": {"jsonrpc_id": rpc_id, "method": "session/cancel"}},
        )
        await self._dispatch_message(msg)
        await self._finalize_session_prompts(session_id, stop_reason="cancelled")
        await self._write_jsonrpc_result(rpc_id, None)

    async def _dispatch_message(self, msg: Message) -> None:
        handled = False
        if self._on_message_cb is not None:
            result = self._on_message_cb(msg)
            if asyncio.iscoroutine(result):
                result = await result
            handled = bool(result)
        elif self._gateway_url:
            await self._send_to_gateway(msg)
            handled = True

        if not handled:
            publish = getattr(self.bus, "publish_user_messages", None)
            if callable(publish):
                await publish(msg)

    async def _clear_request_context(self, request_id: str) -> None:
        ctx = self._request_ctx.pop(str(request_id), None)
        if ctx is None:
            return
        if ctx.session_id and self._active_prompt_request_by_session.get(ctx.session_id) == str(request_id):
            self._active_prompt_request_by_session.pop(ctx.session_id, None)
        task = ctx.idle_finalize_task
        if task is not None:
            ctx.idle_finalize_task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _session_has_pending_client_rpcs(self, session_id: str | None) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        return any(
            isinstance(entry, tuple) and len(entry) >= 1 and str(entry[0] or "").strip() == sid
            for entry in self._pending_client_rpc_session_by_id.values()
        )

    def _collect_finalizable_request_ids(self, session_id: str) -> list[str]:
        result = []
        for request_id, ctx in self._request_ctx.items():
            if ctx.response_mode != "jsonrpc":
                continue
            if str(ctx.session_id or "") != session_id:
                continue
            if not str(ctx.pending_stop_reason or "").strip():
                continue
            result.append(request_id)
        return result

    async def _maybe_finalize_deferred_prompts(self, session_id: str | None) -> None:
        sid = str(session_id or "").strip()
        if not sid or self._session_has_pending_client_rpcs(sid):
            return

        matched_ids = self._collect_finalizable_request_ids(sid)
        for request_id in matched_ids:
            ctx = self._request_ctx.get(request_id)
            if ctx is None:
                continue
            stop_reason = str(ctx.pending_stop_reason or "").strip()
            if not stop_reason:
                continue
            ctx.pending_stop_reason = None
            if _should_wait_for_final_text_before_end_turn(ctx):
                self._schedule_idle_finalize(request_id, ctx)
                continue
            _cancel_idle_finalize_task(ctx)
            await self._write_jsonrpc_result(
                ctx.jsonrpc_id,
                build_acp_prompt_result(
                    stop_reason=stop_reason,
                    user_message_id=ctx.user_message_id,
                ),
            )
            await self._clear_request_context(request_id)

    def _schedule_idle_finalize(self, request_id: str, ctx: _AcpRequestContext) -> None:
        task = ctx.idle_finalize_task
        if task is not None:
            task.cancel()
        new_task = asyncio.create_task(
            self._idle_finalize_after_timeout(str(request_id)),
            name=f"acp-idle-finalize-{request_id}",
        )
        ctx.idle_finalize_task = new_task

    async def _idle_finalize_after_timeout(self, request_id: str) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(_PROMPT_IDLE_FINALIZE_SECONDS)
            ctx = self._request_ctx.get(str(request_id))
            if ctx is None or ctx.response_mode != "jsonrpc":
                return
            # Guard: verify this task is still the active idle_finalize_task
            # to prevent a superseded task from finalizing after being replaced.
            if ctx.idle_finalize_task is not current_task:
                return
            if self._session_has_pending_client_rpcs(ctx.session_id):
                ctx.pending_stop_reason = "end_turn"
                ctx.idle_finalize_task = None
                return
            await self._write_jsonrpc_result(
                ctx.jsonrpc_id,
                build_acp_prompt_result(
                    stop_reason="end_turn",
                    user_message_id=ctx.user_message_id,
                ),
            )
            await self._clear_request_context(str(request_id))
        except asyncio.CancelledError:
            return

    async def _finalize_session_prompts(self, session_id: str, *, stop_reason: str) -> None:
        matched_ids = [
            request_id
            for request_id, ctx in self._request_ctx.items()
            if ctx.response_mode == "jsonrpc" and str(ctx.session_id or "") == session_id
        ]
        for request_id in matched_ids:
            ctx = self._request_ctx.get(request_id)
            if ctx is None:
                continue
            await self._write_jsonrpc_result(
                ctx.jsonrpc_id,
                build_acp_prompt_result(
                    stop_reason=stop_reason,
                    user_message_id=ctx.user_message_id,
                ),
            )
            await self._clear_request_context(request_id)

    async def _send_jsonrpc_message(
        self,
        msg: Message,
        ctx: _AcpRequestContext,
    ) -> bool:
        payload = dict(msg.payload or {})
        session_id = str(msg.session_id or ctx.session_id or "")

        if msg.type == "event" and msg.event_type == EventType.CHAT_DELTA:
            text = str(payload.get("content", "") or "")
            if not text:
                return False
            update = self._build_acp_session_update(msg, payload, ctx)
            if update is None:
                return False
            _mark_stream_activity(ctx)
            await self._write_acp_session_update(session_id, update)
            # 如果 CHAT_DELTA 携带 usage，也发送 usage_update
            usage_update = build_acp_usage_update(payload)
            if usage_update is not None:
                await self._write_acp_session_update(session_id, usage_update)
            return False

        if msg.type == "event" and msg.event_type in (
            EventType.CHAT_REASONING,
            EventType.CHAT_TOOL_CALL,
            EventType.CHAT_TOOL_UPDATE,
            EventType.CHAT_TOOL_RESULT,
            EventType.TODO_UPDATED,
            EventType.CHAT_SUBTASK_UPDATE,
        ):
            update = self._build_acp_session_update(msg, payload, ctx)
            if update is None:
                return False
            _mark_stream_activity(ctx)
            await self._write_acp_session_update(session_id, update)
            return False

        if msg.type == "event" and msg.event_type == EventType.CHAT_FINAL:
            # ACP fallback: defer end_turn until processing stops.
            ctx.saw_chat_final = True
            update = build_acp_final_text_update(payload, ctx)
            if update is not None:
                await self._write_acp_session_update(session_id, update)
            usage_update = build_acp_usage_update(payload)
            if usage_update is not None:
                await self._write_acp_session_update(session_id, usage_update)
            if ctx.saw_processing_idle:
                if self._session_has_pending_client_rpcs(session_id):
                    ctx.pending_stop_reason = "end_turn"
                    return False
                _cancel_idle_finalize_task(ctx)
                await self._write_jsonrpc_result(
                    ctx.jsonrpc_id,
                    build_acp_prompt_result(
                        stop_reason="end_turn",
                        user_message_id=ctx.user_message_id,
                    ),
                )
                return True
            _cancel_idle_finalize_task(ctx)
            return False

        if msg.type == "event" and msg.event_type == EventType.CHAT_ERROR:
            _cancel_idle_finalize_task(ctx)
            await self._write_jsonrpc_error(
                ctx.jsonrpc_id,
                -32603,
                str(payload.get("error") or payload.get("content") or "Agent error"),
            )
            return True

        if msg.type == "event" and msg.event_type == EventType.CHAT_INTERRUPT_RESULT:
            _cancel_idle_finalize_task(ctx)
            await self._write_jsonrpc_result(
                ctx.jsonrpc_id,
                build_acp_prompt_result(
                    stop_reason="cancelled",
                    user_message_id=ctx.user_message_id,
                ),
            )
            return True

        if msg.type == "event":
            if msg.event_type == EventType.CHAT_PROCESSING_STATUS:
                update = self._build_acp_session_update(msg, payload, ctx)
                if update is not None:
                    await self._write_acp_session_update(session_id, update)
                if payload.get("is_processing") is False:
                    ctx.saw_processing_idle = True
                    if self._session_has_pending_client_rpcs(session_id):
                        ctx.pending_stop_reason = "end_turn"
                        return False
                    if _should_wait_for_final_text_before_end_turn(ctx):
                        self._schedule_idle_finalize(str(msg.id), ctx)
                        return False
                    _cancel_idle_finalize_task(ctx)
                    await self._write_jsonrpc_result(
                        ctx.jsonrpc_id,
                        build_acp_prompt_result(
                            stop_reason="end_turn",
                            user_message_id=ctx.user_message_id,
                        ),
                    )
                    return True
                if payload.get("is_processing") is True:
                    ctx.saw_processing_idle = False
                    _cancel_idle_finalize_task(ctx)
                return False
            return False

        if msg.type == "res" and msg.ok:
            if payload.get("accepted") is True:
                return False
            _cancel_idle_finalize_task(ctx)
            await self._write_jsonrpc_result(
                ctx.jsonrpc_id,
                build_acp_prompt_result(
                    stop_reason="end_turn",
                    user_message_id=ctx.user_message_id,
                ),
            )
            return True

        await self._write_jsonrpc_error(
            ctx.jsonrpc_id,
            -32603,
            str(payload.get("error") or payload.get("content") or "request failed"),
        )
        return True

    async def _write_acp_session_update(self, session_id: str, update: dict[str, Any]) -> None:
        await self._write_jsonrpc_notification(
            "session/update",
            {
                "sessionId": session_id,
                "update": update,
            },
        )

    def _build_acp_session_update(
        self,
        msg: Message,
        payload: dict[str, Any],
        ctx: _AcpRequestContext,
    ) -> dict[str, Any] | None:
        return build_acp_session_update(msg, payload, ctx)


    @staticmethod
    def _is_jsonrpc_request(data: Any) -> bool:
        return (
            isinstance(data, dict)
            and data.get("jsonrpc") == "2.0"
            and isinstance(data.get("method"), str)
        )

    @staticmethod
    def _is_jsonrpc_response(data: Any) -> bool:
        return (
            isinstance(data, dict)
            and data.get("jsonrpc") == "2.0"
            and "id" in data
            and not isinstance(data.get("method"), str)
            and ("result" in data or "error" in data)
        )

    @staticmethod
    def _extract_prompt_text(params: dict[str, Any]) -> str:
        prompt = params.get("prompt")
        if isinstance(prompt, list):
            texts: list[str] = []
            for item in prompt:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        texts.append(text)
            if texts:
                return "\n".join(texts)
        for key in ("content", "query", "text"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _parse_req_method(method: str) -> ReqMethod | None:
        for item in ReqMethod:
            if item.value == method:
                return item
        return None

    @staticmethod
    def _provenance(kind: str) -> E2AProvenance:
        return E2AProvenance(
            source_protocol=E2A_SOURCE_PROTOCOL_E2A,
            converter="jiuwenclaw.channel.acp_channel:AcpChannel",
            converted_at=utc_now_iso(),
            details={"kind": kind},
        )

    async def _ensure_gateway_connection(self) -> None:
        if self._gateway_ws is not None:
            return

        try:
            from websockets.legacy.client import connect as ws_connect
        except Exception:  # pragma: no cover
            from websockets import connect as ws_connect

        last_exc: BaseException | None = None
        for attempt in range(1, _ACP_GATEWAY_CONNECT_MAX_ATTEMPTS + 1):
            try:
                self._gateway_ws = await ws_connect(
                    self._gateway_url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=30,
                )
                self._gateway_reader_task = asyncio.create_task(
                    self._gateway_reader_loop(),
                    name="acp-gateway-reader",
                )
                if attempt > 1:
                    logger.info("[ACP] gateway connected after %d attempts: %s", attempt, self._gateway_url)
                return
            except BaseException as exc:
                last_exc = exc
                self._gateway_ws = None
                self._gateway_reader_task = None
                if attempt >= _ACP_GATEWAY_CONNECT_MAX_ATTEMPTS:
                    break
                delay = min(
                    _ACP_GATEWAY_CONNECT_BASE_DELAY_SEC * (2 ** (attempt - 1)),
                    2.0,
                )
                logger.warning(
                    "[ACP] gateway connect attempt %d/%d failed (%s); retry in %.2fs",
                    attempt,
                    _ACP_GATEWAY_CONNECT_MAX_ATTEMPTS,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError(
            f"ACP gateway connect failed after {_ACP_GATEWAY_CONNECT_MAX_ATTEMPTS} attempts: {self._gateway_url}"
        ) from last_exc

    async def _close_gateway_connection(self) -> None:
        reader_task = self._gateway_reader_task
        self._gateway_reader_task = None
        if reader_task is not None:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

        ws = self._gateway_ws
        self._gateway_ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                logger.debug("[ACP] gateway websocket close ignored", exc_info=True)

    async def _send_to_gateway(self, msg: Message) -> None:
        await self._ensure_gateway_connection()
        params = dict(msg.params or {})
        if msg.session_id:
            params.setdefault("session_id", msg.session_id)
        if msg.mode is not None:
            params.setdefault("mode", msg.mode.to_runtime_mode())

        req_method = getattr(msg.req_method, "value", None)
        if not isinstance(req_method, str) or not req_method:
            raise ValueError("gateway forward requires req_method")

        frame = {
            "type": "req",
            "id": str(msg.id),
            "method": req_method,
            "params": params,
        }
        await self._gateway_ws.send(json.dumps(frame, ensure_ascii=False))

    async def _gateway_reader_loop(self) -> None:
        ws = self._gateway_ws
        if ws is None:
            return

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("[ACP] skip invalid gateway frame: %s", raw)
                    continue
                await self._handle_gateway_frame(data)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ACP] gateway reader failed: %s", exc)
        finally:
            if self._gateway_ws is ws:
                self._gateway_ws = None

    async def _handle_gateway_frame(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return

        if self._is_jsonrpc_request(data):
            await self._handle_gateway_jsonrpc_request(data)
            return

        frame_type = str(data.get("type") or "").strip()
        if frame_type == "res":
            msg = self._message_from_gateway_response(data)
        elif frame_type == "event":
            msg = self._message_from_gateway_event(data)
        else:
            msg = None

        if msg is not None:
            await self.send(msg)

    def set_pending_client_rpc_session_for_test(self, jsonrpc_id: str, session_id: str) -> None:
        """Public test helper to seed ACP client RPC session mappings."""
        self._pending_client_rpc_session_by_id[jsonrpc_id] = (session_id, time.time())

    def get_pending_client_rpc_session_for_test(self, jsonrpc_id: str) -> str | None:
        """Public test helper to inspect ACP client RPC session mappings."""
        entry = self._pending_client_rpc_session_by_id.get(jsonrpc_id)
        return entry[0] if isinstance(entry, tuple) else None

    async def handle_gateway_frame_for_test(self, data: dict[str, Any]) -> None:
        """Public test helper that delegates to gateway frame handling."""
        await self._handle_gateway_frame(data)

    async def _handle_gateway_jsonrpc_request(self, data: dict[str, Any]) -> None:
        jsonrpc_id = str(data.get("id") or "").strip()
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        session_id = str(params.get("sessionId") or params.get("session_id") or "").strip()
        if jsonrpc_id and session_id:
            self._known_sessions.add(session_id)
            self._pending_client_rpc_session_by_id[jsonrpc_id] = (session_id, time.time())
            for ctx in self._request_ctx.values():
                if ctx.response_mode == "jsonrpc" and str(ctx.session_id or "") == session_id:
                    task = ctx.idle_finalize_task
                    if task is not None:
                        task.cancel()
                        ctx.idle_finalize_task = None
        await self._sweep_stale_pending()
        _ACP_STDOUT.buffer.write((json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))
        _ACP_STDOUT.buffer.flush()

    def _message_from_gateway_response(self, data: dict[str, Any]) -> Message | None:
        request_id = str(data.get("id") or "").strip()
        if not request_id:
            return None

        ctx = self._request_ctx.get(request_id)
        if ctx is None:
            return None

        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        if not bool(data.get("ok", False)):
            payload = {
                **dict(payload),
                "error": str(data.get("error") or payload.get("error") or "request failed"),
            }

        return Message(
            id=request_id,
            type="res",
            channel_id=self.channel_id,
            session_id=str(ctx.session_id or ""),
            params={},
            timestamp=time.time(),
            ok=bool(data.get("ok", False)),
            payload=dict(payload),
        )

    def _message_from_gateway_event(self, data: dict[str, Any]) -> Message | None:
        event_name = str(data.get("event") or "").strip()
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
        request_id = self._active_prompt_request_by_session.get(session_id)
        if not request_id:
            return None

        event_type = self._parse_event_type(event_name)
        if event_type is None:
            return None

        return Message(
            id=request_id,
            type="event",
            channel_id=self.channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload=dict(payload),
            event_type=event_type,
        )

    @staticmethod
    def _parse_event_type(event_name: str) -> EventType | None:
        for item in EventType:
            if item.value == event_name:
                return item
        return None


def _load_acp_channel_config() -> AcpChannelConfig:
    from jiuwenclaw.config import get_config

    try:
        full_cfg = get_config()
        channels_cfg = full_cfg.get("channels") if isinstance(full_cfg, dict) else None
        acp_conf = channels_cfg.get("acp") if isinstance(channels_cfg, dict) else None
    except Exception:  # noqa: BLE001
        acp_conf = None

    acp_conf = acp_conf if isinstance(acp_conf, dict) else {}
    return AcpChannelConfig(
        enabled=bool(acp_conf.get("enabled", True)),
        channel_id=str(acp_conf.get("channel_id") or "acp").strip() or "acp",
        default_session_id=str(acp_conf.get("default_session_id") or "acp_cli_session").strip()
        or "acp_cli_session",
        metadata=acp_conf.get("metadata") if isinstance(acp_conf.get("metadata"), dict) else {},
    )


def load_acp_channel_config() -> AcpChannelConfig:
    return _load_acp_channel_config()


async def _run(gateway_url: str) -> None:
    acp_channel = AcpChannel(_load_acp_channel_config(), router=None, gateway_url=gateway_url)
    logger.info("[ACP] started: AcpChannel(stdio) -> Gateway(%s) -> AgentServer", gateway_url)
    try:
        await acp_channel.start()
    finally:
        await acp_channel.stop()


def main() -> None:
    # Keep ACP stdio protocol frames on the original stdout while redirecting
    # incidental process logs away from the protocol stream.
    sys.stdout = sys.stderr

    parser = argparse.ArgumentParser(
        prog="jiuwenclaw-acp",
        description="Start JiuwenClaw ACP stdio entrypoint.",
    )
    parser.add_argument(
        "--gateway-url",
        "-g",
        default=None,
        metavar="URL",
        help="Gateway WebSocket URL (default: GATEWAY_URL or ws://WEB_HOST:WEB_PORT/WEB_PATH).",
    )
    parser.add_argument(
        "--agent-server-url",
        "-u",
        default=None,
        metavar="URL",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    gateway_host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    gateway_port = os.getenv("GATEWAY_PORT", "19001")
    gateway_url = (
        getattr(args, "gateway_url", None)
        or getattr(args, "agent_server_url", None)
        or os.getenv("GATEWAY_URL")
        or f"ws://{gateway_host}:{gateway_port}/acp"
    )

    asyncio.run(_run(gateway_url))


if __name__ == "__main__":
    main()
