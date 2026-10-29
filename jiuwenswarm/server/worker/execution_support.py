# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared execution state and bounded keepalive ownership helpers."""

from __future__ import annotations

from jiuwenswarm.runtime.client_tools import ClientToolAnswerMismatch
from jiuwenswarm.runtime.plan import PlanModeController
from jiuwenswarm.runtime.session.model import NoControlTargetError
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
from weakref import WeakValueDictionary

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import TERMINAL_ERROR_EVENT_TYPES, RuntimeEvent
from jiuwenswarm.server.worker.execution_services import HostServices, TransportClosed

logger = logging.getLogger(__name__)


async def _reuse_server_runtime_dependencies() -> None:
    """Compatibility runtime wrappers borrow dependencies owned by AgentServer."""


_GATEWAY_ADAPTER_LEGACY_METHODS = frozenset(
    {
        ReqMethod.SESSION_DELETE,
        ReqMethod.SESSION_RENAME,
    }
)


_STREAM_KEEPALIVE_INTERVAL_SECONDS = 10.0


_STREAM_KEEPALIVE_STOP_TIMEOUT_SECONDS = 1.0


def _consume_keepalive_task_result(
    keepalive_task: asyncio.Task,
    request_id: str,
) -> None:
    """Observe an auxiliary keepalive result without replacing stream errors."""
    try:
        keepalive_task.result()
    except asyncio.CancelledError:
        return
    except TransportClosed:
        logger.info(
            "[AgentWebSocketServer] keepalive task stopped after connection closed: "
            "request_id=%s",
            request_id,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception(
            "[AgentWebSocketServer] keepalive task failed: request_id=%s",
            request_id,
        )


async def _stop_stream_keepalive(
    keepalive_task: asyncio.Task,
    keepalive_stop_event: asyncio.Event,
    stream_activity_event: asyncio.Event,
    request_id: str,
) -> None:
    """Stop an owned stream keepalive without blocking its owner indefinitely."""
    keepalive_stop_event.set()
    stream_activity_event.set()
    phase_timeout = _STREAM_KEEPALIVE_STOP_TIMEOUT_SECONDS / 2.0
    try:
        done, _ = await asyncio.wait(
            {keepalive_task},
            timeout=phase_timeout,
        )
        if keepalive_task not in done:
            logger.warning(
                "[AgentWebSocketServer] keepalive task did not stop cooperatively; "
                "cancelling: request_id=%s",
                request_id,
            )
            keepalive_task.cancel()
            done, _ = await asyncio.wait(
                {keepalive_task},
                timeout=phase_timeout,
            )
    except asyncio.CancelledError:
        if keepalive_task.done():
            _consume_keepalive_task_result(keepalive_task, request_id)
        else:
            keepalive_task.add_done_callback(
                lambda finished: _consume_keepalive_task_result(
                    finished,
                    request_id,
                )
            )
            keepalive_task.cancel()
        raise
    if keepalive_task not in done:
        logger.error(
            "[AgentWebSocketServer] keepalive task did not stop after bounded cleanup: "
            "request_id=%s timeout=%.3fs",
            request_id,
            _STREAM_KEEPALIVE_STOP_TIMEOUT_SECONDS,
        )
        keepalive_task.add_done_callback(
            lambda finished: _consume_keepalive_task_result(
                finished,
                request_id,
            )
        )
        return
    _consume_keepalive_task_result(keepalive_task, request_id)


@dataclass(slots=True)
class _SessionMessageResumeState:
    """Stable mailbox identity while one user answer resumes a target turn."""

    message_id: str
    target_session_id: str
    interrupt_request_id: str
    interrupt_source: str
    waiting_user: bool = False


@dataclass(slots=True)
class _TurnOutcomeTracker:
    """Classify Runtime events only when they contain a terminal signal."""

    saw_chat_final: bool = False
    saw_runtime_accepted: bool = False
    saw_team_input_accepted: bool = False
    waiting_user: bool = False
    saw_error: bool = False
    error: str = ""

    def observe(self, event: RuntimeEvent) -> None:
        event_type = event.event_type
        if event_type == "chat.final":
            self.saw_chat_final = True
        elif event_type == "runtime.accepted":
            self.saw_runtime_accepted = True
        elif event_type in {
            "team.input.accepted",
            "chat.processing_status_deferred",
        }:
            self.saw_team_input_accepted = True
        elif event_type == "chat.ask_user_question":
            self.waiting_user = True
        if not event.ok or event_type in TERMINAL_ERROR_EVENT_TYPES:
            self.fail(self._event_error(event))

    def fail(self, error: str) -> None:
        self.saw_error = True
        if error:
            self.error = error

    def outcome(self) -> str:
        if self.saw_error:
            return "failed"
        if self.waiting_user:
            return "waiting_user"
        if self.saw_chat_final:
            return "succeeded"
        return "unknown"

    def unconfirmed_error(self) -> str:
        if self.saw_runtime_accepted:
            return "Runtime accepted the request without confirming completion"
        return "Runtime stream ended without confirming completion"

    @staticmethod
    def _event_error(event: RuntimeEvent) -> str:
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("error") or payload.get("message") or "")


class _StreamKeepalive:
    """Own the transport keepalive task for one streaming request."""

    def __init__(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        *,
        services: HostServices,
    ) -> None:
        self._services = services
        self._ws = ws
        self._request = request
        self._send_lock = send_lock
        self._channel_id = request.channel_id or "default"
        self._stop_event = asyncio.Event()
        self._activity_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start sending keepalives while the stream is idle."""
        if not self._services.capabilities.keepalive:
            return
        self._task = asyncio.create_task(
            self._run(),
            name=f"stream-keepalive:{self._request.request_id}",
        )

    def notify_activity(self, *, terminal: bool = False) -> None:
        """Restart the idle timer and optionally prevent future keepalives."""
        self._activity_event.set()
        if terminal:
            self._stop_event.set()

    def signal_stop(self) -> None:
        """Prevent new sends and wake an idle keepalive immediately."""
        self._stop_event.set()
        self._activity_event.set()

    async def stop(self) -> None:
        """Join the owned task within the configured total time budget."""
        self.signal_stop()
        if self._task is None:
            return
        await _stop_stream_keepalive(
            self._task,
            self._stop_event,
            self._activity_event,
            self._request.request_id,
        )

    async def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._activity_event.wait(),
                        timeout=_STREAM_KEEPALIVE_INTERVAL_SECONDS,
                    )
                    self._activity_event.clear()
                except asyncio.TimeoutError:
                    if self._stop_event.is_set():
                        break
                    if self._activity_event.is_set():
                        continue
                    keepalive_chunk = AgentResponseChunk(
                        request_id=self._request.request_id,
                        channel_id=self._channel_id,
                        payload={"event_type": "keepalive"},
                        is_complete=False,
                    )
                    if keepalive_chunk.agent_ref is None:
                        keepalive_chunk.agent_ref = self._request.agent_ref
                    wire = self._services.encode_chunk(
                        keepalive_chunk,
                        response_id=self._request.request_id,
                        sequence=-1,
                    )
                    async with self._send_lock:
                        if self._stop_event.is_set():
                            break
                        if self._activity_event.is_set():
                            continue
                        await self._services.send(self._ws, wire)
                    logger.info(
                        "[AgentWebSocketServer] keepalive chunk 发送: request_id=%s",
                        self._request.request_id,
                    )
        except TransportClosed:
            logger.info(
                "[AgentWebSocketServer] keepalive 停止，WebSocket 已关闭: "
                "request_id=%s",
                self._request.request_id,
            )


def _request_query_text(request: AgentRequest) -> str:
    """Return text chat query only; structured events are handled downstream."""
    if not isinstance(request.params, dict):
        return ""
    query = request.params.get("query")
    if not isinstance(query, str):
        return ""
    return query.strip()


_UNDELIVERED_ANSWER_ERRORS: tuple[type[Exception], ...] = (NoControlTargetError, ClientToolAnswerMismatch)


_DIAGNOSIS_REQ_METHODS = frozenset(
    {
        ReqMethod.DIAGNOSIS_RUN,
        ReqMethod.DIAGNOSIS_LIST,
        ReqMethod.DIAGNOSIS_GET,
        ReqMethod.DIAGNOSIS_CANCEL,
        ReqMethod.DIAGNOSIS_DELETE,
        ReqMethod.DIAGNOSIS_TTSE_TRIGGER,
    },
)


def _renew_server_plan_controller() -> PlanModeController:
    """Create the plan-state owner for the next AgentServer lifecycle.

    The compatibility aliases are rebound together so handlers and diagnostics
    cannot retain state from the Runtime that has just been closed.
    """
    global _SERVER_PLAN_CONTROLLER
    global _session_mode_sync_locks
    global _plan_exited_sessions
    global _plan_active_sessions

    controller = PlanModeController()
    _SERVER_PLAN_CONTROLLER = controller
    _session_mode_sync_locks = controller.sync_locks
    _plan_exited_sessions = controller.exited_sessions
    _plan_active_sessions = controller.active_sessions
    return controller


_SERVER_PLAN_CONTROLLER = PlanModeController()


def _group_organization_owner_team_ids(session_id: str) -> set[str]:
    """Owner instances created for a swarm group chat.

    The session's own team stays the outer group team. The expert team that
    owns the organization is a different id, and the web timeline only keeps
    a team's replies in the main chat when that team is marked as owner.
    """
    if not str(session_id or "").strip():
        return set()
    try:
        from jiuwenswarm.server.runtime.group_service import (
            group_organization_owner_team_ids,
        )

        return group_organization_owner_team_ids(session_id)
    except Exception:
        logger.debug(
            "[AgentWebSocketServer] group organization lookup failed session_id=%s",
            session_id,
            exc_info=True,
        )
        return set()


def _conversation_route(session_id: str) -> tuple[str, str]:
    """``(channel_id, owner_id)`` a client_gone resume of the conversation runs under."""
    metadata = get_session_metadata(session_id, enable_writeback=False) or {}
    return str(metadata.get("channel_id") or ""), str(metadata.get("user_id") or "")


def get_plan_active_sessions():
    """公开读取本进程 plan 活跃会话集合（handlers 用，避免 G.CLS.11）."""
    return _plan_active_sessions


_plan_active_sessions = _SERVER_PLAN_CONTROLLER.active_sessions


def get_session_switch_locks() -> WeakValueDictionary[str, asyncio.Lock]:
    """公开读取连接级会话切换锁表（handlers 用，避免 G.CLS.11）."""
    return _session_switch_locks


_session_switch_locks: WeakValueDictionary[str, asyncio.Lock] = (
    WeakValueDictionary()
)



class _AppField:
    """把原来放在 WebSocket 服务器上的字段，转到进程工具箱里的同名数据。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return getattr(obj._ensure_app_services(), self.name)

    def __set__(self, obj: Any, value: Any) -> None:
        setattr(obj._ensure_app_services(), self.name, value)
