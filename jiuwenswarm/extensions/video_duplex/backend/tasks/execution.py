"""Bind Host requests and output leases, and observe native execution settlement."""

import asyncio
from contextlib import asynccontextmanager
import logging
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

from jiuwenswarm.server.runtime.agent_adapter.managed_subagent_control import (
    bind_managed_task_request,
    bind_managed_task_session,
    reset_managed_task_request,
    unbind_managed_task_session,
)

from .checkpoint import TaskCheckpoint
from .bridge import RemoteTaskEndpoint
from .diagnostics import trace_task

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
    # Design runs through Team, whose stream entry owns this task binding.
    # Binding the forwarding DeepAdapter too would consume the same invocation twice.
    if (getattr(request, "params", None) or {}).get("mode") == "team.design":
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
    trace_task("execution_binding_entered", **endpoint.identity)
    previous_output = getattr(adapter, "_interaction_output_handoff", None)
    if previous_output is not None:
        trace_task("execution_handoff_waiting", **endpoint.identity)
        try:
            await previous_output.wait()
        except BaseException as exc:
            trace_task("execution_handoff_failed", **endpoint.identity,
                       error_type=type(exc).__name__, error=str(exc)[:1000])
            raise
        trace_task("execution_handoff_finished", **endpoint.identity)
    if (not str(request.session_id or "").startswith("managed-task-")
            and not isinstance(inputs.get("query"), InteractiveInput)
            and getattr(adapter.task_execution_binding[0], "active_round", None) is not None):
        raise RuntimeError("当前会话仍有任务或文字回复在执行，请完成后再发起全双工工具任务。")
    if adapter.task_execution_binding[1] is None:
        trace_task("execution_rail_installing", **endpoint.identity)
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
    admission = await endpoint.call("bind")
    if admission.get("cancelled"):
        trace_task("execution_admission_cancelled", **endpoint.identity)
        raise RuntimeError("TASK_CANCELLED_BEFORE_EXECUTION")
    trace_task("execution_bound", **endpoint.identity)
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
    managed_request_token = bind_managed_task_request(request.request_id)
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
        trace_task("execution_unwinding", **endpoint.identity, cancelled=invocation_cancelled,
                   native_execution_count=len(checkpoint.native_executions))
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
        reset_managed_task_request(managed_request_token)

        # A callback can capture this exact request task as the native execution.
        # Reaching this finally block means the managed invocation has already
        # returned or unwound through cancellation. Waiting for that same task
        # from a detached reporter creates a self-dependency and the reporter can
        # be discarded with the request before it acknowledges settlement.
        current_execution = asyncio.current_task()
        pending_executions = {
            native
            for native in checkpoint.native_executions
            if native is not current_execution
        }
        # Closing or cancelling the output consumer does not prove that the
        # scheduler execution has finished cleaning up its synchronous subagent.

        def retain_reporter(reporter):
            _settlements.add(reporter)

            def finished(done):
                _settlements.discard(done)
                if not done.cancelled() and done.exception() is not None:
                    logger.warning(
                        "Task settlement acknowledgement failed: %s",
                        done.exception(),
                    )

            reporter.add_done_callback(finished)

        async def report_settlement():
            await endpoint.call("close")
            if checkpoint.settlement_unobserved and not invocation_cancelled:
                logger.warning(
                    "Managed task settlement is unobserved: request=%s",
                    request.request_id,
                )
                return
            await asyncio.gather(
                *(asyncio.shield(t) for t in pending_executions),
                return_exceptions=True,
            )
            if invocation_cancelled:
                await endpoint.call("cancel_settle")
            else:
                await endpoint.call(
                    "settle", cancelled=any(
                        native.cancelled() or native.cancelling() > 0
                        for native in checkpoint.native_executions
                    ),
                )

        # Retain the receipt independently of the stream consumer. A subsequent
        # cancellation must not lose the close/settle acknowledgement.
        reporter = asyncio.create_task(report_settlement())
        retain_reporter(reporter)
        if all(t.done() for t in pending_executions):
            try:
                await asyncio.shield(reporter)
            except asyncio.CancelledError:
                pass


async def bind_task_output(rail, request, stream):
    """Keep the exact output lease so cancellation can wake an idle consumer."""
    binding = getattr(rail, "managed_tasks", {}).get(request.session_id)
    if binding is None or binding.request_id != request.request_id:
        return
    binding.capture_execution(in_callback=False)
    binding.output_stream = stream
    if (await binding.endpoint.call("status"))["status"] == "cancelling":
        await stream.close(abort_active_round=False)


def capture_task_execution(rail, request, execution):
    """Retain an exact resumed execution before cancellation removes ownership."""
    binding = getattr(rail, "managed_tasks", {}).get(request.session_id)
    expected = (request.params or {}).get("managed_task_request")
    if binding is None or not expected or binding.request_id != expected:
        return
    binding.native_executions.add(execution)
    binding.settlement_unobserved = False


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
