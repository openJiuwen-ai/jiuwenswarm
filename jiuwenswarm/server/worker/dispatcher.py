# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execution dispatch inside the Worker. The Worker owns active requests."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import uuid
from collections.abc import Sequence
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
    KIND_SNAPSHOT,
    cancel_request,
    load_agent_request,
    request_scope,
)
from jiuwenswarm.server.ipc.stream import IpcConnection
from jiuwenswarm.server.worker.diagnostics import LoopBeat, heartbeat_reply
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
                self._bridge.events.register(request, dispatch_id)
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
        target = str((request.params or {}).get("request_id") or "")
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
        if not dispatch_id:
            dispatch_id = uuid.uuid4().hex
            self._bridge.events.register(request, dispatch_id)
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
        try:
            await runtime.dispatch_parsed_request(self._bridge, request, self._lock)
        except Exception as exc:  # noqa: BLE001
            ok = False
            error = str(exc)
            logger.exception(
                "[Worker] dispatch failed: request_id=%s", request.request_id
            )
        finally:
            self._release_startup_priority(dispatch_id)
            execution_entry_sink.reset(entry_token)
            sdk_entry_sink.reset(minimum_token)
            end_delivery_tracking(token)
            end_request_generation(gen_token)
            end_request_dispatch(dispatch_token)
            self._bridge.events.finish(dispatch_id)
        await self._finish(
            generation,
            str(request.request_id or ""),
            ok=ok,
            error=error,
            delivered=bool(box),
            dispatch_id=dispatch_id,
        )

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

    async def _subscribe_locked(self, message: dict[str, Any], conn: IpcConnection, generation: int) -> None:
        request = load_agent_request(message.get("payload") or {})
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
                                       if key in waiting}
            snapshot["dispatches"] = self._bridge.events.snapshot(request, after=after)["dispatches"]
        # Copy the replay before the first await: other scopes can publish and
        # evict journal frames while this scope's snapshot is being delivered.
        frames = self._bridge.events.replay(request, after=after) if message.get("subscribe") else []
        await conn.send({"kind": KIND_SNAPSHOT, "query_id": message.get("query_id"),
                         "generation": generation, "payload": snapshot})
        await self._bridge.replay(frames, generation)


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
