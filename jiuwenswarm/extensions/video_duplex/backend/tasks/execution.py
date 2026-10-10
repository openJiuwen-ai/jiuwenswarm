"""Bind Host requests and output leases, and observe native execution settlement."""

import asyncio
from contextlib import asynccontextmanager
import logging

from .checkpoint import TaskCheckpoint
from .bridge import RemoteTaskEndpoint

_settlements = set()
logger = logging.getLogger(__name__)


class TaskExecutionBinding(TaskCheckpoint):
    """A checkpoint plus the native handles owned by one Host invocation."""

    def __init__(self, endpoint, task, root, harness=None):
        super().__init__(endpoint, task, root)
        self.harness = harness
        self.native_executions = set()
        self.settlement_unobserved = False
        self.output_stream = None

    def capture_execution(self, *, in_callback=True):
        # A Core output stream can close before its cancelled scheduler task drains.
        # Capture that exact handle while the round still owns it; never infer
        # settlement from a terminal status or disappearance from the scheduler.
        active = getattr(self.harness, "active_round", None)
        controller = getattr(self.harness, "loop_controller", None)
        scheduler = getattr(controller, "task_scheduler", None)
        entry = getattr(scheduler, "_running_tasks", {}).get(
            getattr(active, "task_id", None)
        )
        if entry and entry[1] is not None:
            self.native_executions.add(entry[1])
            self.settlement_unobserved = False
        elif active is not None and in_callback:
            # Interrupt resumes may execute directly rather than in the scheduler
            # map. Capture the exact callback's task, never infer completion from
            # a missing scheduler entry or the preceding interrupted round.
            native = asyncio.current_task()
            if native is not None:
                self.native_executions.add(native)
                self.settlement_unobserved = False
            else:
                self.settlement_unobserved = True
        elif active is not None and not self.native_executions:
            self.settlement_unobserved = True


@asynccontextmanager
async def bind_task_execution(request, adapter, inputs):
    managed = request.channel_id == "video_tool" and str(request.session_id or "").startswith("managed-task-")
    if not managed:
        yield
        return
    endpoint = RemoteTaskEndpoint(request)
    harness, rail = adapter.task_execution_binding
    root = getattr(harness, "_react_agent", None)
    if root is None or not getattr(adapter, "_is_session_scoped_adapter", False):
        raise RuntimeError("Managed tasks require the session-owned Agent")
    if rail is None:
        raise RuntimeError("Managed task callback rail is unavailable")
    bindings = rail.managed_tasks
    if request.session_id in bindings:
        raise RuntimeError("Task execution already bound")
    await endpoint.call("bind")
    task = {"id": endpoint.identity["task_id"], "request_id": request.request_id}
    checkpoint = TaskExecutionBinding(endpoint, task, root, harness)
    bindings[request.session_id] = checkpoint
    previous = inputs.get("run")
    run = dict(previous or {})
    context = dict(run.get("context") or {})
    context["extra"] = {
        **context.get("extra", {}),
        "managed_task_request": request.request_id,
    }
    inputs["run"] = {**run, "context": context}
    invocation_cancelled = False
    try:
        yield
    except asyncio.CancelledError:
        # Cancellation can arrive during setup, before the first model callback
        # captures a scheduler handle. The bound invocation itself is evidence.
        invocation_cancelled = True
        raise
    finally:
        # Setup/output checks can fail before any model callback is reached.
        # Outside a callback, only the scheduler handle is execution evidence.
        checkpoint.capture_execution(in_callback=False)
        checkpoint.closed = True
        bindings.pop(request.session_id, None)
        if previous is None:
            inputs.pop("run", None)
        else:
            inputs["run"] = previous

        async def report_settlement():
            await endpoint.call("close")
            if checkpoint.settlement_unobserved:
                return
            # A separate reporter may await the enclosing invocation itself.
            # Shield native handles so a reporting timeout cannot cancel Agent work.
            await asyncio.gather(
                *(asyncio.shield(t) for t in checkpoint.native_executions),
                return_exceptions=True,
            )
            await endpoint.call(
                "settle", cancelled=invocation_cancelled or any(
                    native.cancelled() or native.cancelling() > 0
                    for native in checkpoint.native_executions
                ),
            )

        if all(t.done() for t in checkpoint.native_executions):
            await report_settlement()
        else:
            reporter = asyncio.create_task(report_settlement())
            _settlements.add(reporter)

            def finished(done):
                _settlements.discard(done)
                if not done.cancelled() and done.exception() is not None:
                    logger.warning("Task settlement acknowledgement failed: %s", done.exception())

            reporter.add_done_callback(finished)


async def bind_task_output(rail, request, stream):
    """Keep the exact output lease so cancellation can wake an idle consumer."""
    binding = getattr(rail, "managed_tasks", {}).get(request.session_id)
    if binding is None or binding.request_id != request.request_id:
        return
    binding.capture_execution(in_callback=False)
    binding.output_stream = stream
    if (await binding.endpoint.call("status"))["status"] == "cancelling":
        await stream.close(abort_active_round=False)


async def close_task_output(rail, request):
    binding = getattr(rail, "managed_tasks", {}).get(request.session_id)
    expected = (request.params or {}).get("managed_task_request")
    if binding is None or not expected or binding.request_id != expected:
        return
    task = (await binding.endpoint.call("status"))
    if task["status"] != "cancelling":
        return
    if binding.output_stream is not None:
        # cancel_round has already signalled the execution. Closing its output
        # lease is separate: cancellation alone may leave next_output waiting.
        # The binding still waits for native execution handles before settlement.
        await binding.output_stream.close(abort_active_round=False)
