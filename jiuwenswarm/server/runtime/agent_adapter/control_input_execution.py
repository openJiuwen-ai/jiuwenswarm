# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-owned continuation execution with durable output boundaries."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from jiuwenswarm.server.runtime.agent_adapter.history_committer import HistoryCommitter


@dataclass(frozen=True)
class ControlInputPorts:
    is_interrupt_resume_payload: Callable
    restore_equipment: Callable
    compute_mcp_needed: Callable
    persist_answer: Callable
    run_history_io: Callable
    append_history_record: Callable
    decorate_web: Callable
    decorate_heartbeat: Callable
    decorate_cross_session: Callable
    resolve_final_timestamp: Callable
    person_drove_turn: Callable
    complete_history_request: Callable
    session_message_owns_completion: Callable
    await_history_receipt: Callable


class ControlInputExecutionService:
    """Own one resume stream until its stream and accepted history have drained.

    The existing facade/adapter still own the Session and permission policy.
    This service never creates another Session, scheduling lane or SDK owner.
    A failed close keeps the service reachable from the facade for a retry.
    """

    def __init__(self, facade: Any, request: Any, ports: ControlInputPorts):
        self._facade, self.request, self._ports = facade, request, ports
        self.session_id = None
        self.history = None
        self._stream = None
        self._runner = None
        self._running = False
        self._exhausted = False
        self._close_lock = asyncio.Lock()
        self.closed = False
        self.completion = None

    async def run(self):
        self._runner = asyncio.current_task()
        self._running = True
        request, ports, facade = self.request, self._ports, self._facade
        try:
            if not ports.is_interrupt_resume_payload(request.params):
                raise ValueError("control input must answer an active interaction")
            adapter = facade._ensure_adapter(mode=facade._adapter_mode_for_request(request))
            self.session_id = facade._session_manager.get_session_id(request.session_id)
            params = request.params if isinstance(request.params, dict) else {}
            ports.restore_equipment(self.session_id, params)
            inputs, _memory_mode, _user_turn = facade._build_inputs(request)
            await facade.reconcile_session_mcp(
                request.session_id, ports.compute_mcp_needed(params),
                model_name=params.get("model_name"), history_before_request_id=request.request_id,
                execution_request=request, retain_moa_snapshot=True,
                **({"model_selection": params["model_selection"]} if params.get("model_selection") is not None else {}),
                **({"moa_enabled": params["moa_enabled"]} if "moa_enabled" in params else {}),
                **(adapter.moa_access_kwargs(request) if callable(getattr(adapter, "moa_access_kwargs", None)) else {}),
            )
            # The answered question keeps its original request id. Continuation
            # output belongs to the new envelope id and excludes resume source.
            await ports.persist_answer(request, self.session_id, params)

            async def write_record(**record):
                await ports.run_history_io(ports.append_history_record, wait_for_write=True, **record)

            self.history = HistoryCommitter(
                params=params, session_id=self.session_id,
                request_id=request.request_id, channel_id=request.channel_id,
                write_record=write_record, decorate_web=ports.decorate_web,
                decorate_heartbeat=ports.decorate_heartbeat,
                decorate_cross_session=ports.decorate_cross_session,
                resolve_final_timestamp=ports.resolve_final_timestamp,
                inherit_request_source=False,
            )
            from jiuwenswarm.server.runtime.agent_adapter.request_completion import RequestCompletion
            self.completion = RequestCompletion(request, self.session_id, is_team_mode=False, ports=ports)
            self._stream = adapter.process_message_stream_impl(request, inputs)
            async for chunk in self._stream:
                payload = getattr(chunk, "payload", None)
                if isinstance(payload, dict):
                    await self.history.commit_assistant_event(payload)
                # A failed final/tool/question write propagates before this
                # boundary can be acknowledged to the caller.
                self.completion.observe(chunk)
                yield chunk
            self._exhausted = True
        except (asyncio.CancelledError, GeneratorExit):
            if self.completion is not None:
                self.completion.status = "cancelled"
            raise
        except Exception:
            if self.completion is not None:
                self.completion.status = "error"
            raise
        finally:
            try:
                await self.aclose()
            finally:
                self._running = False

    async def aclose(self):
        if self.closed:
            return
        runner = self._runner
        if self._running and runner is not None and runner is not asyncio.current_task() and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        async with self._close_lock:
            if self.closed:
                return
            await HistoryCommitter._drain_accepted(self._finish())

    async def _finish(self):
        if self._stream is not None:
            await self._stream.aclose()
            self._stream = None
        if self.history is not None:
            await self.history.retry_pending_writes()
            if self._exhausted or self.history.saw_goal_stream_output:
                await self.history.persist_pending_final_text()
            await self.history.persist_pending_reasoning()
        if self.completion is not None:
            self.completion.complete(self.completion.status)
            await self.completion.wait()
        self.closed = True
