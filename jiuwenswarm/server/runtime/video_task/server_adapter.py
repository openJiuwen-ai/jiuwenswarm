"""AgentServer endpoints for checkpoint ACKs and task-produced files."""

import asyncio

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter

from .bridge import resolve_checkpoint_ack


class VoiceTaskServerAdapter(GatewayAdapter):
    methods = frozenset({
        ReqMethod.VOICE_TASK_CHECKPOINT_ACK.value, ReqMethod.VOICE_TASK_FILES.value,
        ReqMethod.VOICE_SESSION_RECEIPTS.value, ReqMethod.VOICE_TASK_PENDING_INTERRUPT.value,
    })

    def __init__(self, pending_interrupt=None, runtime=None):
        # AgentManager.session_has_pending_interrupt; read-only.
        self._pending_interrupt = pending_interrupt
        self._runtime = runtime

    def _card_status(self, session, params):
        """Project the original execution; absence alone never confirms an answer."""
        snapshot = self._runtime().get_session_snapshot(session) if self._runtime else None
        binding = params.get("card_binding")
        if snapshot is None:
            return {"card_state": "unknown", "pending": False}
        if binding is not None:
            if not isinstance(binding, dict):
                return {"card_state": "unknown", "pending": False}
            for field, value_type in (
                ("generation", int), ("execution_id", str), ("control_id", str), ("deliveries", list),
            ):
                if not isinstance(binding.get(field), value_type):
                    return {"card_state": "unknown", "pending": False}
        if snapshot.state.value == "closed" or (binding and binding["generation"] != snapshot.generation):
            return {"card_state": "ended", "pending": False}
        executions = snapshot.executions
        if not binding:
            names = {params.get("interaction_id"), params.get("interrupt_id")} - {None, ""}
            parent = next((e for e in reversed(executions) if names & e.waiting_control_ids), None)
            if parent is None:
                return {"card_state": "unknown", "pending": False}
            binding = {
                "generation": snapshot.generation, "execution_id": parent.execution_id,
                "control_id": next(iter(names & parent.waiting_control_ids)),
                "deliveries": [e.execution_id for e in executions
                               if e.parent_execution_id == parent.execution_id and e.state.terminal],
            }
        parent = next((e for e in executions if e.execution_id == binding["execution_id"]), None)
        if parent is None:
            return {"card_state": "unknown", "pending": False}
        deliveries = []
        for execution in executions:
            if execution.parent_execution_id != parent.execution_id:
                continue
            if execution.execution_id in binding["deliveries"]:
                continue
            if binding["control_id"] in execution.answered_control_ids:
                deliveries.append(execution)
        state = "pending"
        if parent.state.value in {"failed", "cancelled"}:
            state = "ended"
        elif binding["control_id"] not in parent.waiting_control_ids:
            if any(e.state.value in {"succeeded", "waiting_for_control"} for e in deliveries):
                state = "applied"
            else:
                state = "ended" if parent.state.terminal else "unknown"
        return {"card_state": state, "card_binding": binding, "pending": state == "pending"}

    async def handle(self, request):
        if request.req_method == ReqMethod.VOICE_TASK_CHECKPOINT_ACK:
            payload = {"accepted": resolve_checkpoint_ack(request)}
        elif request.req_method == ReqMethod.VOICE_TASK_PENDING_INTERRUPT:
            from jiuwenswarm.server.runtime.session.lifecycle import LifecycleError, guard
            from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

            session = request.session_id or ""
            if not session or self._pending_interrupt is None:
                raise ValueError("Pending interrupt lookup is unavailable")
            try:
                guard(session)
            except LifecycleError as exc:
                if not (request.params or {}).get("card_binding") or exc.code not in {
                    "NOT_FOUND", "SESSION_ARCHIVED", "OPERATION_IN_PROGRESS",
                }:
                    raise
                return AgentResponse(
                    request_id=request.request_id, channel_id=request.channel_id,
                    ok=True, payload={"pending": False, "card_state": (
                        "ended" if exc.code == "SESSION_ARCHIVED" else "unknown"
                    )}, metadata=request.metadata,
                )
            metadata = await asyncio.to_thread(
                get_session_metadata, session, cache_bust=True, enable_writeback=False
            )
            if not metadata or str(metadata.get("user_id") or "").strip() != (request.user_id or ""):
                raise ValueError("Task session is unavailable to this user")
            payload = {"pending": bool(self._pending_interrupt(session))}
            if (request.params or {}).get("interaction_id") or (request.params or {}).get("card_binding"):
                payload.update(self._card_status(session, request.params))
        elif request.req_method == ReqMethod.VOICE_SESSION_RECEIPTS:
            from jiuwenswarm.extensions.video_duplex.backend.session_receipts import configure

            payload = await configure(request)
        else:
            from jiuwenswarm.server.runtime.session.lifecycle import guard
            from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
            from jiuwenswarm.server.runtime.session.session_history import (
                flush_pending_writes, load_history_records,
            )
            from jiuwenswarm.extensions.video_duplex.backend.video_files import (
                normalize_file_items,
            )

            session = request.session_id or ""
            execution_id = (request.params or {}).get("execution_request_id")
            if not session or not execution_id:
                raise ValueError("Invalid task execution identity")
            guard(session)
            metadata = await asyncio.to_thread(
                get_session_metadata, session, cache_bust=True, enable_writeback=False
            )
            if not metadata or str(metadata.get("user_id") or "").strip() != (request.user_id or ""):
                raise ValueError("Task session is unavailable to this user")
            # Tool completion only enqueues chat.file; wait for the existing
            # history writer before taking the final attachment snapshot.
            if not await asyncio.to_thread(flush_pending_writes, timeout=5):
                raise TimeoutError("Task file history is not yet durable")
            records = await asyncio.to_thread(load_history_records, session)
            files = []
            for record in records:
                if record.get("request_id") == execution_id and record.get("event_type") == "chat.file":
                    files.extend(normalize_file_items(record.get("files")))
            payload = {"files": files}
        return AgentResponse(
            request_id=request.request_id, channel_id=request.channel_id,
            ok=True, payload=payload, metadata=request.metadata,
        )
