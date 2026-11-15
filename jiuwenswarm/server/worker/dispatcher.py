# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execution dispatch inside the Worker. The Worker owns active requests."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict
from enum import Enum
from typing import Any
from jiuwenswarm.common.external_cli_runtime import activate_external_cli_runtime_paths
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session.model import (
    SessionExecutionSnapshot,
    SessionExecutionState,
    SessionLifecycleContext,
)
from jiuwenswarm.server.ipc.protocol import (
    KIND_CANCEL,
    KIND_DELIVERY_ACK,
    KIND_GATEWAY_ATTACH,
    KIND_GATEWAY_DETACH,
    KIND_HEARTBEAT,
    KIND_REQUEST,
    KIND_REQUEST_DONE,
    KIND_SHUTDOWN,
    KIND_SUBSCRIBE,
    KIND_SUBSCRIBE_BY_ID,
    KIND_WIRE,
    KIND_SNAPSHOT,
    cancel_request,
    load_agent_request,
    request_scope,
)
from jiuwenswarm.server.ipc.stream import IpcConnection
from jiuwenswarm.server.worker.diagnostics import LoopBeat, heartbeat_reply
from jiuwenswarm.server.worker.stall_context import request_metadata
from jiuwenswarm.server.worker.event_store import DispatchRecord
from jiuwenswarm.server.worker.gateway_bridge import (
    WorkerGatewayBridge,
    begin_delivery_tracking,
    begin_request_dispatch,
    begin_request_generation,
    end_delivery_tracking,
    end_request_dispatch,
    end_request_generation,
)

logger = logging.getLogger(__name__)

_MAX_QUEUED_REQUESTS = 32
_DRAIN_TIMEOUT = 5.0


def _question_body_matches(body: Any, response_kind: Any = None) -> dict[str, Any] | None:
    """提问 body 判定核心：展平 delta/result 嵌套后，event_type 缺失回落 response_kind。

    与 _question_recovery_filter 及身份型重放共用（atomgit 9722 id=200378287、
    9707 id=199769142-④）——两处判据同源，禁止各自演化。"""
    if not isinstance(body, dict):
        return None
    nested = body.get("delta") if isinstance(body.get("delta"), dict) else body.get("result")
    if isinstance(nested, dict):
        body = {**body, **nested}
    return body if (body.get("event_type") or response_kind) == "chat.ask_user_question" else None


def _question_frame_body(wire: Any) -> dict[str, Any] | None:
    """wire 顶层帧 → 提问 body（同源判据）。response_kind 与提问标识穿透
    保留帧包装层（外层 wire 或内层壳均可携带）。"""
    if not isinstance(wire, dict):
        return None
    shell = wire.get("body") or wire.get("payload")
    if not isinstance(shell, dict):
        return None
    response_kind = wire.get("response_kind", shell.get("response_kind"))
    inner = shell.get("body") if isinstance(shell.get("body"), dict) else None
    body = {**shell, **inner} if inner else shell
    return _question_body_matches(body, response_kind)


class WorkerDispatcher:
    """Turn IPC messages into Runtime calls and stream wire frames back."""

    def __init__(self, bridge: WorkerGatewayBridge, beat: LoopBeat | None = None) -> None:
        self._bridge = bridge
        self._beat = beat
        self._lock = asyncio.Lock()
        self._runtime: Any = None
        self._ready = False
        self._attached = False
        self._queued: list[tuple[int, AgentRequest, str]] = []
        self._tasks: dict[asyncio.Task[None], int] = {}
        self._request_tasks: dict[str, asyncio.Task[Any]] = {}
        self._state_lock = asyncio.Lock()
        self.shutdown_requested = False
        self._draining = False
        self._failure = ""
        self._startup_priorities: dict[str, tuple[Any, Any, object]] = {}

    async def bind_runtime(self, runtime: Any) -> None:
        """Publish the Runtime before it reports ``AGENT_READY``."""
        async with self._state_lock:
            self._runtime = runtime
        # This is a Worker-owned event sink even while Gateway is offline.
        await self._bind_runtime_gateway(runtime)

    async def _bind_runtime_gateway(self, runtime: Any) -> None:
        bind = getattr(runtime, "bind_gateway_transport", None)
        if not callable(bind):
            bind = getattr(runtime, "attach_gateway_connection", None)
        if callable(bind):
            result = bind(self._bridge, self._lock)
            if inspect.isawaitable(result):
                await result

    async def mark_ready(self) -> None:
        # This callback runs before optional tasks released by minimum_ready.
        # Claim priority before set_available can suspend and before spawning
        # queued dispatch; pausing inside Runtime dispatch alone is too late.
        runtime = self._runtime
        preparation = getattr(runtime, "_preparation_registry", None)
        if preparation is not None and not getattr(runtime, "_execution_minimum_observed", False):
            for _generation, request, dispatch_id in self._queued:
                if (request.req_method not in {ReqMethod.CHAT_SEND, ReqMethod.CHAT_RESUME, ReqMethod.CHAT_ANSWER}
                        or dispatch_id in self._startup_priorities):
                    continue
                token = preparation.pause_background()
                tokens = getattr(runtime, "_startup_preparation_tokens", None)
                if tokens is None:
                    tokens = runtime._startup_preparation_tokens = set()
                tokens.add(token)
                self._startup_priorities[dispatch_id] = (runtime, preparation, token)
        # Mailbox execution is owned by the resident Runtime. It must not wait
        # for the legacy in-process WebSocket connection handler to run.
        service = getattr(self._runtime, "_session_message_service", None)
        if service is not None:
            try:
                await service.set_available(True)
            except BaseException:
                for dispatch_id in list(self._startup_priorities):
                    self._release_startup_priority(dispatch_id)
                raise
        async with self._state_lock:
            self._ready = True
            queued = self._queued
            self._queued = []
        for generation, request, dispatch_id in queued:
            if self._draining:
                self._release_startup_priority(dispatch_id)
                self._bridge.events.finish(dispatch_id)
                continue
            self._spawn(self._run_request(request, generation, dispatch_id), generation)

    def _release_startup_priority(self, dispatch_id: str) -> None:
        owner = self._startup_priorities.pop(dispatch_id, None)
        if owner is None:
            return
        runtime, preparation, token = owner
        preparation.resume_background(token)
        getattr(runtime, "_startup_preparation_tokens", set()).discard(token)

    async def mark_failed(self, reason: str) -> None:
        async with self._state_lock:
            self._failure = reason
            queued, self._queued = self._queued, []
        for generation, request, dispatch_id in queued:
            self._release_startup_priority(dispatch_id)
            self._bridge.events.complete(dispatch_id, state="failed", error=reason)
            await self._finish(generation, request.request_id, ok=False, error=reason,
                               delivered=False, dispatch_id=dispatch_id)

    async def handle(self, message: dict[str, Any], conn: IpcConnection) -> bool:
        """Handle one frame. Return False when the session should close."""
        kind = str(message.get("kind") or "")
        generation = self._bridge.holder.generation
        if not _frame_matches_session(message, generation):
            logger.warning(
                "[Worker] dropped stale ipc kind=%s generation=%s current=%s",
                kind,
                message.get("generation"),
                generation,
            )
            return True
        if kind == KIND_HEARTBEAT:
            # Loop-driven fallback: the named-pipe fast path already answered
            # this ping off-loop, so this branch serves stream transports.
            beat_age = self._beat.age if self._beat is not None else None
            await conn.send(heartbeat_reply(beat_age))
            return True
        if kind == KIND_DELIVERY_ACK:
            self._bridge.acknowledge(message)
            return True
        if kind == KIND_SUBSCRIBE:
            self._spawn(self._subscribe(message, conn, generation), generation)
            return True
        if kind == KIND_SUBSCRIBE_BY_ID:
            self._spawn(self._subscribe_by_id(message, conn, generation), generation)
            return True
        if kind == KIND_SHUTDOWN:
            self.shutdown_requested = True
            return False
        if kind == KIND_GATEWAY_ATTACH:
            async with self._state_lock:
                self._attached = True
                runtime = self._runtime
            if runtime is not None:
                await self._bind_runtime_gateway(runtime)
            return True
        if kind == KIND_GATEWAY_DETACH:
            await self.detach_gateway(message.get("remote"))
            return True
        if kind == KIND_CANCEL:
            request = cancel_request(message)
            if await self._drop_queued_cancel(request):
                await self._finish(
                    generation,
                    str(request.request_id or ""),
                    ok=True,
                    error="",
                    delivered=False,
                )
                return True
            self._spawn(self._run_request(request, generation, str(message.get("dispatch_id") or "")), generation)
            return True
        if kind == KIND_REQUEST:
            payload = message.get("payload")
            if not isinstance(payload, dict):
                await self._done(
                    conn,
                    str(message.get("request_id") or ""),
                    generation=generation,
                    ok=False,
                    error="missing payload",
                )
                return True
            request = load_agent_request(payload)
            dispatch_id = str(message.get("dispatch_id") or uuid.uuid4().hex)
            self._reconcile_execution_state(request)
            try:
                record = self._bridge.events.register(request, dispatch_id)
                if request.is_stream:
                    record.delivery_generation = generation
            except ValueError as exc:
                await self._done(conn, str(request.request_id or ""), generation=generation,
                                 dispatch_id=dispatch_id, ok=False, error=str(exc))
                return True
            if request.req_method == ReqMethod.CHAT_CANCEL and not self._ready:
                await self._drop_queued_cancel(request)
                # The reader must remain available to receive delivery ACKs.
                self._spawn(self._cancel_response(request, generation, dispatch_id), generation)
            else:
                self._spawn(self._run_request(request, generation, dispatch_id), generation)
            return True
        logger.info("[Worker] ignored ipc kind=%s", kind)
        return True

    @staticmethod
    def _record_executions(
        record: DispatchRecord,
        executions: Sequence[SessionExecutionSnapshot],
    ) -> list[SessionExecutionSnapshot]:
        matching_executions = []
        for execution in executions:
            matches_execution = execution.execution_id == record.last_execution_id
            matches_request = record.request.request_id in {execution.request_id, execution.root_request_id}
            if matches_execution or matches_request:
                matching_executions.append(execution)
        return matching_executions

    @staticmethod
    def _execution_is_processing(execution: SessionExecutionSnapshot) -> bool:
        return not execution.state.terminal and execution.state is not SessionExecutionState.WAITING_FOR_INPUT

    def _reconcile_execution_state(self, request: AgentRequest, *, across_channels: bool = False):
        """Distinguish a returned output segment from a terminal execution."""
        if self._runtime is None:
            return None
        getter = getattr(self._runtime, "_execution_runtime", None)
        runtime = getter() if callable(getter) else self._runtime
        snapshot_getter = getattr(runtime, "get_session_snapshot", None)
        if not callable(snapshot_getter):
            return None
        state = snapshot_getter(request.params.get("session_id") or request.session_id or "")
        if state is None:
            return None
        scope = request_scope(request)
        for record in list(self._bridge.events.records.values()):
            matches_scope = record.scope[1:] == scope[1:] if across_channels else record.scope == scope
            if not matches_scope:
                continue
            if record.is_passive_subscription:
                record.is_processing = False
                continue
            executions = self._record_executions(record, state.executions)
            if not executions:
                # The Coordinator retains live executions. A returned handler
                # with no remaining owner must not keep a pruned input busy.
                # Active handlers can still be warming or awaiting admission.
                if not record.active:
                    # An empty ledger only proves the Coordinator stopped
                    # retaining the execution. A single-agent turn keeps
                    # streaming after its handler returns, and a freshly
                    # created session may not have registered its execution
                    # yet, so pruning must not cancel the stream's own
                    # processing claim (chat.processing_status). Records whose
                    # busy flag came from elsewhere keep the old pruning.
                    if not record.stream_processing_claim:
                        record.is_processing = False
                        record.interactions.clear()
                    self._bridge.events.finish(record.dispatch_id)
                continue
            ongoing = [item for item in executions if not item.state.terminal]
            waiting = {control_id for item in ongoing for control_id in item.waiting_control_ids}
            record.interactions = {key: value for key, value in record.interactions.items() if key in waiting}
            record.is_processing = any(self._execution_is_processing(item) for item in ongoing)
            if ongoing:
                record.terminal = None
            elif not record.active:
                record.terminal = {"event_type": "runtime.completed", "state": executions[-1].state.value}
                self._bridge.events.finish(record.dispatch_id)
        return state

    async def detach_gateway(self, remote: object = None) -> None:
        """Detach delivery only. Execution and scheduled work remain Worker-owned."""
        async with self._state_lock:
            if not self._attached:
                return
            self._attached = False
        self._bridge.detach(self._bridge.holder.generation)

    async def retire_generation(self, generation: int) -> None:
        """Invalidate transport delivery waiters, retaining accepted execution."""
        self._bridge.detach(generation)

    async def drain(self, *, timeout: float = _DRAIN_TIMEOUT) -> bool:
        """Stop new work and cancel active dispatch.

        Return False when a task is still running after ``timeout``. The caller
        must not shut the Runtime down while that task can still use it.
        """
        async with self._state_lock:
            self._draining = True
            self.shutdown_requested = True
            for dispatch_id in list(self._startup_priorities):
                self._release_startup_priority(dispatch_id)
            for _generation, _request, dispatch_id in self._queued:
                self._bridge.events.complete(dispatch_id, state="cancelled", error="worker shutting down")
            self._queued.clear()
            tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if not tasks:
            return True
        done, pending = await asyncio.wait(set(tasks), timeout=timeout)
        for task in pending:
            logger.warning("[Worker] dispatch still running after shutdown timeout")
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.warning("[Worker] dispatch ended during shutdown: %s", exc)
        return not pending

    def _spawn(self, coro: Any, generation: int) -> None:
        task = asyncio.create_task(coro)
        self._tasks[task] = generation
        task.add_done_callback(self._forget_task)

    def _forget_task(self, task: asyncio.Task) -> None:
        self._tasks.pop(task, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("[Worker] background dispatch failed", exc_info=task.exception())

    async def _drop_queued_cancel(self, request: AgentRequest) -> bool:
        """Drop a cancel that arrived before Runtime exists. Return True when handled."""
        target = str((request.params or {}).get("target_request_id")
                     or (request.params or {}).get("request_id") or "")
        session_id = request.session_id or ""
        async with self._state_lock:
            if self._ready or self._draining:
                return self._draining
            kept: list[tuple[int, AgentRequest, str]] = []
            dropped: list[tuple[int, AgentRequest, str]] = []
            for generation, queued, dispatch_id in self._queued:
                same_request = bool(target) and str(queued.request_id or "") == target
                same_session = bool(session_id) and (queued.session_id or "") == session_id
                same_owner = request_scope(queued)[:4] == request_scope(request)[:4]
                if same_owner and ((same_request and (not session_id or same_session))
                                   if target else same_session):
                    dropped.append((generation, queued, dispatch_id))
                    continue
                kept.append((generation, queued, dispatch_id))
            self._queued = kept
        for generation, queued, dispatch_id in dropped:
            self._release_startup_priority(dispatch_id)
            logger.info(
                "[Worker] cancelled queued request_id=%s generation=%s",
                queued.request_id,
                generation,
            )
            await self._finish(
                generation,
                str(queued.request_id or ""),
                ok=False,
                error="cancelled before runtime ready",
                delivered=False,
                dispatch_id=dispatch_id,
            )
            self._bridge.events.complete(dispatch_id, state="cancelled", error="cancelled before runtime ready")
        return True

    async def _run_request(self, request: AgentRequest, generation: int, dispatch_id: str = "") -> None:
        beat = getattr(self, "_beat", None)
        if beat is None:
            await self._dispatch_request(request, generation, dispatch_id)
        else:
            with beat.activity(request_metadata(request, dispatch_id)):
                await self._dispatch_request(request, generation, dispatch_id)

    async def _dispatch_request(self, request: AgentRequest, generation: int, dispatch_id: str = "") -> None:
        if not dispatch_id:
            dispatch_id = uuid.uuid4().hex
            record = self._bridge.events.register(request, dispatch_id)
            if request.is_stream:
                record.delivery_generation = generation
        if request.req_method == ReqMethod.CHAT_CANCEL:
            # A session interrupt ends the round it targets. Its pending
            # interactions (an ask_user popup waiting for input, a permission
            # card) are no longer answerable, and the snapshot must not report
            # the paused session as busy — otherwise every later metadata query
            # triggers a forced after:0 replay that resurrects the popup.
            self._bridge.events.retire_session_interactions(request)
        async with self._state_lock:
            if self._draining:
                self._release_startup_priority(dispatch_id)
                logger.info(
                    "[Worker] dropped request_id=%s generation=%s draining=%s",
                    request.request_id,
                    generation,
                    self._draining,
                )
                self._bridge.events.complete(dispatch_id, state="cancelled", error="worker shutting down")
                return
            runtime = self._runtime
            if self._failure:
                overflow = True
            elif runtime is None or not self._ready:
                if len(self._queued) >= _MAX_QUEUED_REQUESTS:
                    overflow = True
                else:
                    self._queued.append((generation, request, dispatch_id))
                    return
            else:
                overflow = False
        if overflow:
            self._release_startup_priority(dispatch_id)
            await self._finish(
                generation,
                str(request.request_id or ""),
                ok=False,
                error=self._failure or "worker queue is full",
                delivered=False,
                dispatch_id=dispatch_id,
            )
            self._bridge.events.complete(dispatch_id, state="failed", error=self._failure or "worker queue is full")
            return
        if request.req_method == ReqMethod.CHAT_CANCEL:
            await self._cancel_target_dispatch(request)
        # A resident worker outlives SDK installs made from the settings panel
        # (the gateway process), so refresh the managed paths before this turn;
        # team members backed by external CLIs probe and import them here.
        try:
            activate_external_cli_runtime_paths()
        except Exception:  # noqa: BLE001 - optional capability must not fail the turn
            logger.warning(
                "[Worker] managed external CLI runtime activation failed; "
                "external CLI members may stay unavailable this turn",
                exc_info=True,
            )
        gen_token = begin_request_generation(generation)
        dispatch_token = begin_request_dispatch(dispatch_id)
        from jiuwenswarm.runtime.execution_telemetry import execution_entry_sink, sdk_entry_sink
        entry_token = execution_entry_sink.set(self._bridge.observe_execution_entry)
        minimum_token = sdk_entry_sink.set(getattr(runtime, "observe_execution_minimum_ready", None))
        token, box = begin_delivery_tracking()
        error = ""
        ok = True
        cancelled = False
        current_task = asyncio.current_task()
        if current_task is not None:
            self._request_tasks[dispatch_id] = current_task
        try:
            await runtime.dispatch_parsed_request(self._bridge, request, self._lock)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # noqa: BLE001
            ok = False
            error = str(exc)
            logger.exception(
                "[Worker] dispatch failed: request_id=%s", request.request_id
            )
        finally:
            self._request_tasks.pop(dispatch_id, None)
            self._release_startup_priority(dispatch_id)
            execution_entry_sink.reset(entry_token)
            sdk_entry_sink.reset(minimum_token)
            end_delivery_tracking(token)
            end_request_generation(gen_token)
            end_request_dispatch(dispatch_token)
            self._bridge.events.finish(dispatch_id)
            if cancelled:
                # A targeted cancel may stop this dispatch before Runtime admits
                # it. Front still awaits request.done for this request, so report
                # the terminal here instead of leaving that waiter until the
                # connection closes. The canceling side owns the visible
                # interrupt result, so do not turn this into a gateway error.
                logger.info(
                    "[Worker] dispatch cancelled: request_id=%s generation=%s",
                    request.request_id,
                    generation,
                )
                await self._finish(
                    generation,
                    str(request.request_id or ""),
                    ok=True,
                    error="request cancelled",
                    delivered=False,
                    dispatch_id=dispatch_id,
                )
        await self._finish(
            generation,
            str(request.request_id or ""),
            ok=ok,
            error=error,
            delivered=bool(box),
            dispatch_id=dispatch_id,
        )

    async def _cancel_target_dispatch(self, request: AgentRequest) -> None:
        """Cancel the exact response owner even before Runtime allocates execution."""
        params = request.params or {}
        target = str(params.get("target_request_id") or "")
        if not target or params.get("expected_execution_id"):
            return
        state = self._reconcile_execution_state(request)
        if state is not None and any(
            item.request_id == target or item.root_request_id == target for item in state.executions
        ):
            # Once admitted, Runtime must stop the domain producer before the
            # response task exits (particularly for Team streams).
            return
        scope = request_scope(request)
        matches = [
            record for record in self._bridge.events.records.values()
            if record.scope == scope and record.request.request_id == target and record.active
        ]
        for record in matches:
            task = self._request_tasks.get(record.dispatch_id)
            if task is None or task is asyncio.current_task() or task.done():
                continue
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._bridge.events.complete(record.dispatch_id, state="cancelled", error="request cancelled")

    async def _finish(
        self,
        generation: int,
        request_id: str,
        *,
        ok: bool,
        error: str,
        delivered: bool,
        dispatch_id: str = "",
    ) -> None:
        record = self._bridge.events.records.get(dispatch_id)
        if record is not None and record.request.is_stream:
            async with self._bridge.publication_lock(record.scope):
                # request.done is not a business event, but its outcome must
                # survive the same detach as the stream it completes.
                record.completion = {"ok": ok, "error": error[:4096], "delivered": delivered}
                current = self._bridge.holder.generation
                conn = self._bridge.holder.conn
                if conn is None or record.delivery_generation not in {None, current}:
                    return
                try:
                    await self._done(conn, request_id, generation=current, dispatch_id=dispatch_id,
                                     ok=ok, error=error, delivered=delivered)
                except (OSError, RuntimeError):
                    logger.info("[Worker] stream completion retained; supervisor disconnected")
            return
        if generation != self._bridge.holder.generation:
            logger.info(
                "[Worker] dropped request.done request_id=%s generation=%s current=%s",
                request_id,
                generation,
                self._bridge.holder.generation,
            )
            return
        conn = self._bridge.holder.conn
        if conn is None:
            return
        try:
            await self._done(
                conn,
                request_id,
                generation=generation,
                ok=ok,
                error=error,
                delivered=delivered,
                dispatch_id=dispatch_id,
            )
        except (OSError, RuntimeError):
            logger.info("[Worker] request.done dropped; supervisor disconnected")

    async def _done(
        self,
        conn: IpcConnection,
        request_id: str,
        *,
        generation: int,
        ok: bool,
        error: str = "",
        delivered: bool = False,
        dispatch_id: str = "",
    ) -> None:
        await conn.send(
            {
                "kind": KIND_REQUEST_DONE,
                "request_id": request_id,
                "generation": generation,
                "ok": ok,
                "error": error,
                "delivered": delivered,
                "dispatch_id": dispatch_id,
            }
        )

    async def _cancel_response(self, request: AgentRequest, generation: int, dispatch_id: str) -> None:
        from jiuwenswarm.common.schema.agent import AgentResponse
        from jiuwenswarm.server.front.protocol import encode_response
        token = begin_request_dispatch(dispatch_id)
        gen_token = begin_request_generation(generation)
        try:
            response = AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                                     ok=True, payload={"event_type": "chat.interrupt_result",
                                                       "success": True, "queued": True},
                                     metadata=request.metadata)
            await self._bridge.send(json.dumps(encode_response(response, response_id=request.request_id)))
            await self._finish(generation, request.request_id, ok=True, error="", delivered=True,
                               dispatch_id=dispatch_id)
        except Exception as exc:
            await self._finish(generation, request.request_id, ok=False, error=str(exc), delivered=False,
                               dispatch_id=dispatch_id)
        finally:
            end_request_dispatch(token)
            end_request_generation(gen_token)
            self._bridge.events.finish(dispatch_id)

    async def _subscribe(self, message: dict[str, Any], conn: IpcConnection, generation: int) -> None:
        # Publish replay before later live events. IPC reads continue in the
        # service task, so acknowledgements and cancel cannot deadlock replay.
        request = load_agent_request(message.get("payload") or {})
        async with self._bridge.publication_lock(request_scope(request)):
            if self._bridge.holder.generation != generation:
                return
            await self._subscribe_locked(message, conn, generation)

    def _question_recovery_filter(
        self, request: AgentRequest, snapshot: dict[str, Any], frames: list[dict[str, Any]],
    ) -> Callable[[dict[str, Any]], bool]:
        runtime_getter = getattr(self._runtime, "_execution_runtime", None)
        runtime = runtime_getter() if callable(runtime_getter) else self._runtime
        resolve = getattr(runtime, "recoverable_control", None)
        session_id = request.params.get("session_id") or request.session_id or ""

        def question(body: Any, event: str | None = "") -> dict[str, Any] | None:
            return _question_body_matches(body, event)

        def owner(body: dict[str, Any], dispatch_id: str) -> tuple[int, str, str] | None:
            if not callable(resolve):
                snapshot["recovery_pending"] = True
                snapshot["recovery_reason"] = "control_authority_unavailable"
                return None
            record = self._bridge.events.records.get(dispatch_id)
            execution_id = body.get("execution_id") or (record.last_execution_id if record else "")
            control_id = str(body.get("interaction_id") or body.get("control_id") or body.get("request_id") or "")
            if not execution_id or not control_id:
                snapshot["recovery_pending"] = True
                snapshot["recovery_reason"] = "control_owner_unavailable"
                return None
            current = resolve(session_id, control_id, execution_id)
            return (current.generation, current.execution_id, control_id) if current else None

        # Use all retained scoped frames to select the latest current question,
        # even when that version lies before the requested replay cursor.
        latest = {}
        for frame in frames:
            wire = frame["payload"]
            body = question(wire.get("body") or wire.get("payload") or {}, wire.get("response_kind"))
            key = owner(body, frame["dispatch_id"]) if body is not None else None
            if key is not None:
                latest[key] = frame

        seen = set()
        for dispatch in reversed(snapshot["dispatches"]):
            pending = []
            for body in dispatch["pending_interactions"]:
                item = question(body)
                if item is None:
                    pending.append(body)
                    continue
                key = owner(item, dispatch["dispatch_id"])
                newest = latest.get(key)
                if key is None or key in seen:
                    continue
                if newest is not None and newest["dispatch_id"] != dispatch["dispatch_id"]:
                    continue
                seen.add(key)
                pending.append(body)
            dispatch["pending_interactions"] = pending

        def include(frame: dict[str, Any]) -> bool:
            wire = frame["payload"]
            body = question(wire.get("body") or wire.get("payload") or {}, wire.get("response_kind"))
            if body is None:
                return True
            key = owner(body, frame["dispatch_id"])
            return key is not None and latest.get(key) is frame

        return include

    async def _subscribe_locked(self, message: dict[str, Any], conn: IpcConnection, generation: int) -> None:
        request = load_agent_request(message.get("payload") or {})
        if message.get("dispatch_id"):
            await self._resume_dispatch_locked(message, request, conn, generation)
            return
        after = max(0, int(message.get("after") or 0))
        state = self._reconcile_execution_state(request, across_channels=True)
        snapshot = self._bridge.events.snapshot(request, after=after)
        work_records = []
        scope = request_scope(request)
        for record in self._bridge.events.records.values():
            if record.scope[1:] != scope[1:] or record.is_passive_subscription:
                continue
            if _is_session_work_request(record.request):
                work_records.append(record)
        # Activity crosses channels for the same Session owner; replay and
        # dispatch details remain caller-scoped. Navigation/history handlers
        # do not represent conversation execution.
        snapshot["is_processing"] = any(record.active or record.is_processing for record in work_records)
        if message.get("worker_instance") not in {None, "", self._bridge.events.instance}:
            snapshot["history_required"] = True
            snapshot["interrupted"] = True
            after = 0
        if state is not None:
            allowed = {item["request_id"] for item in snapshot["dispatches"]}
            executions = [asdict(item) for item in state.executions
                          if item.request_id in allowed or item.root_request_id in allowed]
            snapshot["executions"] = json.loads(
                json.dumps(
                    executions, default=lambda value: (
                        value.value if isinstance(value, Enum)
                        else sorted(value) if isinstance(value, frozenset) else str(value)
                    )
                )
            )
            # A known execution owns activity, not its response handler:
            # persistent Team handlers remain attached while waiting for
            # input. Preserve active dispatches without an execution yet
            # so warming/queued work still appears busy.
            snapshot["is_processing"] = any(
                record.is_processing
                or (record.active and not self._record_executions(record, state.executions))
                for record in work_records
            )
            # The only remaining response owner may be a passive observer
            # while teammates execute. Read generation-scoped Team activity
            # without treating the observer's lifetime as conversation work.
            runtime_getter = getattr(self._runtime, "_execution_runtime", None)
            runtime = runtime_getter() if callable(runtime_getter) else self._runtime
            team_lifecycle = getattr(runtime, "_session_runtime_lifecycle", None)
            if team_lifecycle is not None:
                snapshot["is_processing"] = snapshot["is_processing"] or team_lifecycle.is_running(
                    SessionLifecycleContext(
                        session_id=request.params.get("session_id") or request.session_id or "",
                        generation=state.generation,
                    )
                )
            ongoing = [item for item in state.executions if not item.state.terminal]
            waiting = {control_id for item in ongoing for control_id in item.waiting_control_ids}
            for record in self._bridge.events.records.values():
                if record.scope != request_scope(request):
                    continue
                record.interactions = {key: value for key, value in record.interactions.items()
                                       if key in waiting or value.get("event_type") == "chat.ask_user_question"}
            snapshot["dispatches"] = self._bridge.events.snapshot(request, after=after)["dispatches"]
        # Copy the replay before the first await: other scopes can publish and
        # evict journal frames while this scope's snapshot is being delivered.
        needs_questions = message.get("subscribe") or any(
            item["pending_interactions"] for item in snapshot["dispatches"]
        )
        frames = self._bridge.events.replay(request, after=0) if needs_questions else []
        include = self._question_recovery_filter(request, snapshot, frames)
        frames = [frame for frame in frames if frame["sequence"] > after] if message.get("subscribe") else []
        await conn.send({"kind": KIND_SNAPSHOT, "query_id": message.get("query_id"),
                         "generation": generation, "payload": snapshot})
        await self._bridge.replay(frames, generation, include=include)
        if message.get("subscribe"):
            for record in self._bridge.events.records.values():
                if record.scope == scope and record.request.is_stream:
                    record.delivery_generation = generation

    async def _resume_dispatch_locked(
        self, message: dict[str, Any], request: AgentRequest, conn: IpcConnection, generation: int,
    ) -> None:
        """Resume delivery of one accepted stream without dispatching it again."""
        dispatch_id = str(message["dispatch_id"])
        after = max(0, int(message.get("after") or 0))
        record = self._bridge.events.records.get(dispatch_id)
        error = ""
        if message.get("worker_instance") != self._bridge.events.instance:
            error = "runtime worker changed during stream recovery"
        elif record is None or record.scope != request_scope(request) or not record.request.is_stream:
            error = "runtime worker no longer retains the original stream"
        elif after < record.expired_sequence:
            error = "runtime worker stream replay expired; reload session history"
        snapshot = {"worker_instance": self._bridge.events.instance,
                    "dispatch_id": dispatch_id, "recovery_error": error}
        if not error:
            state = self._bridge.events.snapshot(request, after=after)
            frames = self._bridge.events.replay(request, after=0)
            include = self._question_recovery_filter(request, state, frames)
            frames = [frame for frame in frames
                      if frame["dispatch_id"] == dispatch_id and frame["sequence"] > after]
            await self._bridge.replay(frames, generation, include=include)
        # The snapshot acknowledges replay, not merely a socket attachment.
        # The publication lock keeps later live output and completion behind it.
        await conn.send({"kind": KIND_SNAPSHOT, "query_id": message.get("query_id"),
                         "generation": generation, "payload": snapshot})
        if error:
            return
        record.delivery_generation = generation
        if record.completion is not None:
            result = record.completion
            await self._done(conn, str(record.request.request_id or ""), generation=generation,
                             dispatch_id=dispatch_id, ok=result["ok"], error=result["error"],
                             delivered=result["delivered"])


    def _replayed_question_expired(self, record: "DispatchRecord", body: dict[str, Any]) -> bool:
        """待重放提问帧是否已失效——按 control 本体有效性判定，与
        ``_question_recovery_filter`` 的 authority 同源（``recoverable_control``）。

        ``record.interactions`` 是混合交互字典（waiting 控制集 ∪ 提问帧），非空≠
        提问在飞：残留其它控制时旧粗启发式会把已终结提问误判在飞重投
        （openjiuwen-AIReview 9722 id=200409667——与 9707 authority 判定分叉的移植
        修复）。authority/标识不可达时回退旧启发式（字典空判），保守维持原重放行为。
        """
        runtime_getter = getattr(self._runtime, "_execution_runtime", None)
        runtime = runtime_getter() if callable(runtime_getter) else self._runtime
        resolve = getattr(runtime, "recoverable_control", None)
        control_id = str(body.get("interaction_id") or body.get("control_id")
                         or body.get("request_id") or "")
        execution_id = str(body.get("execution_id") or record.last_execution_id or "")
        if not callable(resolve) or not control_id or not execution_id:
            return not record.interactions
        session_id = (str(record.scope[4]) if len(record.scope) > 4
                      else str(record.request.session_id or ""))
        return resolve(session_id, control_id, execution_id) is None

    async def _subscribe_by_id(self, message: dict[str, Any], conn: IpcConnection, generation: int) -> None:
        """P6 身份型重放：按台账身份键反查单请求保留帧并回放（携 wire_id 回执链）。

        反查键=(scope[:4], request_id)（与 ``WorkerEventStore._active`` 同键构造）；
        miss → status=miss；worker_instance 不匹配（worker 重启丢保留帧）→
        status=history_required；命中 → status=ok 后逐帧 KIND_WIRE 回放。
        supervisor 侧对 miss/history_required 转占位卡路径。
        """
        rid = str(message.get("request_id") or "")
        identity = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        after = max(0, int(message.get("after") or 0))
        query_id = message.get("query_id")

        async def _reply(status: str, **extra: Any) -> None:
            frame: dict[str, Any] = {"kind": KIND_SUBSCRIBE_BY_ID, "query_id": query_id,
                                     "request_id": rid, "status": status, "generation": generation}
            frame.update(extra)
            try:
                await conn.send(frame)
            except (OSError, RuntimeError):
                logger.info("[Worker] subscribe_by_id reply lost on IPC detach")

        if not rid:
            await _reply("miss")
            return
        channel = str(identity.get("channel_id") or "")
        user = str(identity.get("user_id") or "")
        # request_scope 真源口径：identity 四元组由 supervisor 台账（request_scope
        # 归一化结果）透传，open_id 私改回落会造成两口径各自演化（atomgit 08:27 评）。
        app = str(identity.get("app_id") or "")
        agent_ref = identity.get("agent_ref")
        if agent_ref is not None and not isinstance(agent_ref, str):
            agent_ref = json.dumps(agent_ref, sort_keys=True, default=str)
        agent_ref_json = agent_ref or json.dumps(
            {"mode": "agent", "id": "default"}, sort_keys=True, default=str)
        identity_key = (channel, user, app, str(agent_ref_json), rid)

        record: DispatchRecord | None = None
        dispatch_hint = str(message.get("dispatch_id") or "")
        hinted = self._bridge.events.records.get(dispatch_hint) if dispatch_hint else None
        if hinted is not None and (*hinted.scope[:4], str(hinted.request.request_id or "")) == identity_key:
            record = hinted
        if record is None:
            for candidate in reversed(self._bridge.events.records.values()):
                if (*candidate.scope[:4], str(candidate.request.request_id or "")) == identity_key:
                    record = candidate
                    break
        if record is None:
            # 反查 miss：记录已完成被修剪或键不符 → supervisor 转占位卡路径。
            await _reply("miss")
            return
        if message.get("worker_instance") not in {None, "", self._bridge.events.instance}:
            # worker 重启过（保留帧已失）：supervisor 直接占位卡，不做全量 history 重放。
            await _reply("history_required", dispatch_id=record.dispatch_id)
            return
        # 重放帧与该 scope 的实时帧共用发布锁（同 _subscribe 重放先例）：先持锁再判
        # 缺口并取快照、逐帧 send——缺口判定留在锁外会有 TOCTOU 窗口（检查与取帧
        # 之间并发 append 淘汰保留帧，静默跳帧）；同锁保证重放与实时帧对同一 scope
        # 的到达顺序有全序。
        async with self._bridge.publication_lock(record.scope):
            # 回执前复校验 generation/连接：连接已换代则不回执不重放——
            # 空帧集也回 ok 会把「未送达」记账成已送达（atomgit 9722 id=199769146-④）。
            if self._bridge.holder.generation != generation or self._bridge.holder.conn is None:
                return
            if self._bridge.events.replay_gapped(record.scope, rid, after):
                # 该请求自身的保留帧已被淘汰至游标之下：重放会静默跳帧，
                # status=ok 会掩盖缺口 → supervisor 占位卡路径。水位按
                # (scope, request_id) 判定，别的请求的淘汰不误报本请求缺口。
                await _reply("history_required", dispatch_id=record.dispatch_id)
                return
            frames = self._bridge.events.replay_by_request(record.scope, rid, after=after)
            # ok 回执先行是 test_e 锁定的契约：supervisor 对 ok 零消费（只处理
            # miss/history_required），重放中断的兜底＝supervisor sweep 重试＋
            # 占位卡到期，不依赖 ok 语义（atomgit 06:53 评「ok 记成成功」前提
            # 不成立，回执时序维持原契约）。
            await _reply("ok", dispatch_id=record.dispatch_id, count=len(frames))
            for index, frame in enumerate(frames):
                if self._bridge.holder.generation != generation or self._bridge.holder.conn is None:
                    return
                payload = frame["payload"]
                if isinstance(payload, dict):
                    qbody = _question_frame_body(payload)
                    if qbody is not None and self._replayed_question_expired(record, qbody):
                        # 失效提问卡不随重放重投（authority＝recoverable_control 按
                        # control 逐个判，残留其它控制交互不再误判在飞；
                        # openjiuwen-AIReview 9722 id=200409667＝9707 判定同源化移植，
                        # 承接 atomgit id=200378287、9707 id=199769142-④／06:53 评）。
                        continue
                    # 浅拷贝隔离后注入 replayed 标记：不得写回 event_store 保留帧
                    # （原地写会污染日志对象，其后真实首投被消费侧误认已重放帧——
                    # 恰为本 PR 要修的静默吞回复类失效；atomgit 9722 id=200375260）。
                    payload = {**payload}
                    metadata = dict(payload.get("metadata") or {})
                    runtime_event = dict(metadata.get("runtime_event") or {})
                    runtime_event["replayed"] = True
                    metadata["runtime_event"] = runtime_event
                    payload["metadata"] = metadata
                try:
                    await conn.send({**frame, "payload": payload, "kind": KIND_WIRE,
                                     "wire_id": uuid.uuid4().hex,
                                     "generation": generation, "replayed": True})
                except (OSError, RuntimeError):
                    logger.info("[Worker] subscribe_by_id replay lost on IPC detach")
                    return
                if index % 32 == 0:
                    await asyncio.sleep(0)


def _is_session_work_request(request: AgentRequest) -> bool:
    """Keep page restoration and passive Goal subscriptions out of busy state."""
    if request.params.get("attach_goal") is True:
        return False
    if request.req_method == ReqMethod.COMMAND_GOAL:
        return str(request.params.get("action", "get")).strip().lower() in {"set", "resume"}
    return request.req_method in {
        ReqMethod.CHAT_SEND,
        ReqMethod.CHAT_RESUME,
        ReqMethod.CHAT_ANSWER,
        ReqMethod.CHAT_SWARMFLOW_REPLY,
        ReqMethod.SESSION_MESSAGE_CONTINUE_QUEUED,
    }


def _frame_matches_session(message: dict[str, Any], generation: int) -> bool:
    raw = message.get("generation")
    if raw is None:
        return True
    return isinstance(raw, int) and raw == generation
