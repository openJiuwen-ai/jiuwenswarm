"""Bind Host requests and output leases, and observe native execution settlement."""

import asyncio
from contextlib import asynccontextmanager
import logging
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from jiuwenswarm.common.schema.message import ReqMethod

from jiuwenswarm.server.runtime.agent_adapter.managed_subagent_control import (
    bind_managed_task_session,
    unbind_managed_task_session,
)

from .checkpoint import TaskCheckpoint
from .bridge import RemoteTaskEndpoint

_settlements = set()
# Requests whose settlement reporter is still waiting on native work.
_reporting_requests = set()
logger = logging.getLogger(__name__)


class TaskExecutionBinding(TaskCheckpoint):
    """A checkpoint plus the native handles owned by one Host invocation."""

    def __init__(self, endpoint, task, root, harness=None):
        super().__init__(endpoint, task, root)
        self.harness = harness
        self.native_executions = set()
        self.settlement_unobserved = False
        self.unobserved_round = None
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
            # The round's own task outlives the scheduler entry, so it is settlement evidence.
            round_task = getattr(self.harness, "_interaction_round_task", None)
            if isinstance(round_task, asyncio.Task):
                self.native_executions.add(round_task)
                self.settlement_unobserved = False
            else:
                self.settlement_unobserved = True
                self.unobserved_round = active


@asynccontextmanager
async def bind_task_execution(request, adapter, inputs):
    # Design runs through Team, whose stream entry owns this task binding.
    # Binding the forwarding DeepAdapter too would consume the same invocation twice.
    if (getattr(request, "params", None) or {}).get("mode") == "team.design":
        yield
        return
    # A form answer resumes the bound execution; binding again would be rejected.
    if getattr(request, "req_method", None) == ReqMethod.CHAT_ANSWER:
        yield
        return
    managed = request.channel_id == "video_tool" and (
        bool((getattr(request, "params", None) or {}).get("managed_task_binding"))
        or str(request.session_id or "").startswith("managed-task-")
    )
    if not managed:
        yield
        return
    # The shared adapter receives the request first and forwards it to the
    # session-owned child. Binding happens on that second call.
    if not getattr(adapter, "_is_session_scoped_adapter", False):
        yield
        return
    endpoint = RemoteTaskEndpoint(request)
    previous_output = getattr(adapter, "_interaction_output_handoff", None)
    if previous_output is not None:
        await previous_output.wait()
    if (not str(request.session_id or "").startswith("managed-task-")
            and not isinstance(inputs.get("query"), InteractiveInput)
            and getattr(adapter.task_execution_binding[0], "active_round", None) is not None):
        raise RuntimeError("当前会话仍有任务或文字回复在执行，请完成后再发起全双工工具任务。")
    # Cards are click-only (#5487). Runtime refuses while a live turn waits on a
    # card; this covers a card restored from disk, which would take the task as its answer.
    loop_session = getattr(adapter.task_execution_binding[0], "loop_session", None)
    if (not isinstance(inputs.get("query"), InteractiveInput) and loop_session is not None
            and getattr(loop_session.get_state(INTERRUPTION_KEY), "interrupted_tools", None)):
        raise RuntimeError("当前对话有待处理的确认卡片，请先在界面上点击处理，再发起全双工工具任务。")
    if adapter.task_execution_binding[1] is None:
        await adapter.install_voice_task_rail()
    harness, rail = adapter.task_execution_binding
    root = getattr(harness, "_react_agent", None)
    if root is None:
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
    managed_session_binding = bind_managed_task_session(
        request.session_id,
        request.request_id,
    )
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
        unbind_managed_task_session(managed_session_binding)

        # Awaiting this same request task from the reporter would deadlock on itself.
        current_execution = asyncio.current_task()
        pending_executions = set()
        if not invocation_cancelled:
            pending_executions = {
                native
                for native in checkpoint.native_executions
                if native is not current_execution
            }
        # After cancellation unwinds, a leftover scheduler entry must not keep the task cancelling.

        def retain_reporter(reporter):
            _settlements.add(reporter)
            _reporting_requests.add(request.request_id)

            def finished(done):
                _settlements.discard(done)
                _reporting_requests.discard(request.request_id)
                if not done.cancelled() and done.exception() is not None:
                    logger.warning("Task settlement acknowledgement failed: %s", done.exception())

            reporter.add_done_callback(finished)

        if invocation_cancelled:
            # Start the durable write before awaiting so a second cancel cannot drop it.
            reporter = asyncio.create_task(endpoint.call("settle", cancelled=True))
            retain_reporter(reporter)
            try:
                await asyncio.shield(reporter)
            except asyncio.CancelledError:
                pass
        else:
            async def report_settlement():
                await endpoint.call("close")
                if checkpoint.settlement_unobserved:
                    # No task handle is visible; wait for the round to leave the harness before settling.
                    while getattr(checkpoint.harness, "active_round", None) is checkpoint.unobserved_round:
                        # Same 50 ms settlement poll as TaskService._cancel.
                        await asyncio.sleep(0.05)
                # Shield native handles so a reporting timeout cannot cancel Agent work.
                await asyncio.gather(
                    *(asyncio.shield(t) for t in pending_executions),
                    return_exceptions=True,
                )
                await endpoint.call(
                    "settle", cancelled=any(
                        native.cancelled() or native.cancelling() > 0
                        for native in checkpoint.native_executions
                    ),
                )

            if not checkpoint.settlement_unobserved and all(t.done() for t in pending_executions):
                await report_settlement()
            else:
                reporter = asyncio.create_task(report_settlement())
                retain_reporter(reporter)


async def bind_task_output(rail, request, stream):
    """Keep the exact output lease so cancellation can wake an idle consumer."""
    binding = getattr(rail, "managed_tasks", {}).get(request.session_id)
    if binding is None or binding.request_id != request.request_id:
        return
    binding.capture_execution(in_callback=False)
    binding.output_stream = stream
    if (await binding.endpoint.call("status"))["status"] == "cancelling":
        await stream.close(abort_active_round=False)


def managed_request_detached(rail, request):
    """True when this Core holds nothing for the request, so cancelling the round would hit other work (#5486)."""
    expected = str((request.params or {}).get("managed_task_request") or "").strip()
    if not expected or expected in _reporting_requests:
        return False
    bindings = getattr(rail, "managed_tasks", None) or {}
    return all(getattr(b, "request_id", None) != expected for b in bindings.values())


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
