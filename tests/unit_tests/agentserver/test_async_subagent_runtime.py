"""Task-group results use existing desktop events, one card per child."""

import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
    BrowserTaskPromptRail,
)


class Stream:
    def __init__(self):
        self.chunks = []

    def get_session_id(self):
        return "session"

    async def write_stream(self, chunk):
        self.chunks.append(chunk)


def ctx(stream, call_id, arguments, result=None):
    call = SimpleNamespace(id=call_id, name="task_tool", arguments=arguments)
    return SimpleNamespace(
        session=stream,
        inputs=ToolCallInputs(
            tool_call=call,
            tool_name="task_tool",
            tool_args=arguments,
            tool_result=result,
        ),
        extra={
            "task_group.run": ("run", None),
            JiuSwarmStreamEventRail._SID_KEY: "session",
        },
        exception=None,
    )


def tasks(*names):
    return {
        "action": "dispatch",
        "tasks": [
            {"subagent_type": "general-purpose", "task_description": name}
            for name in names
        ],
    }


def snapshot(call_id, index, status="completed"):
    return {
        "task_id": f"{call_id}-{index}",
        "dispatch_tool_call_id": call_id,
        "dispatch_index": index,
        "status": status,
        "output": f"result-{index}",
    }


def events(stream, kind):
    return [c.payload[kind] for c in stream.chunks if c.type == kind]


@pytest.mark.asyncio
async def test_batch_projects_two_cards_and_retrieved_results_keep_original_identity():
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    first = ctx(stream, "ab", tasks("A", "B"))
    await rail.before_tool_call(first)
    assert [e["tool_call_id"] for e in events(stream, "tool_call")] == [
        "subtask:ab:0",
        "subtask:ab:1",
    ]
    first.inputs.tool_result = SimpleNamespace(
        success=True, data={"ready": [snapshot("ab", 0)], "pending_ids": ["ab-1"]}
    )
    await rail.after_tool_call(first)
    assert [e["tool_call_id"] for e in events(stream, "tool_result")] == [
        "subtask:ab:0"
    ]
    # B may have finished while the old stream was closed. Its result is projected
    # into the current stream when the parent actually retrieves it.
    resumed = Stream()
    wait = ctx(
        resumed,
        "wait",
        {"action": "wait", "task_ids": ["ab-1"]},
        SimpleNamespace(success=True, data={"ready": [snapshot("ab", 1)]}),
    )
    await rail.before_tool_call(wait)
    await rail.after_tool_call(wait)
    await rail.after_tool_call(wait)
    assert not events(resumed, "tool_call")
    assert [e["tool_call_id"] for e in events(resumed, "tool_result")] == [
        "subtask:ab:1"
    ]


@pytest.mark.asyncio
async def test_failed_dispatch_closes_projected_children_without_creating_control_card():
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    call = ctx(
        stream,
        "bad",
        tasks("A", "B"),
        SimpleNamespace(success=False, error="invalid task", data=None),
    )
    await rail.before_tool_call(call)
    await rail.after_tool_call(call)
    results = events(stream, "tool_result")
    assert len(results) == 2 and all(item["is_error"] for item in results)
    assert all(item["tool_call_id"].startswith("subtask:bad:") for item in results)


@pytest.mark.asyncio
async def test_approval_retry_never_duplicates_children_and_summary_finishes_pending():
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    call = ctx(
        stream, "approval", tasks("A", "B"), {"result_type": "interrupt", "state": []}
    )
    await rail.before_tool_call(call)
    await rail.after_tool_call(call)
    await rail.before_tool_call(call)
    assert len(events(stream, "tool_call")) == 2
    assert not events(stream, "tool_result")
    call.inputs = SimpleNamespace(
        result={
            "task_group_summary": {
                "tasks": [snapshot("approval", 0, "cancelled"), snapshot("approval", 1)]
            }
        }
    )
    # Higher-priority SubagentRail has already reset its ContextVar binding.
    call.extra.pop("task_group.run")
    await rail.after_invoke(call)
    assert len(events(stream, "tool_result")) == 2


@pytest.mark.asyncio
async def test_cancel_collection_skips_control_call_and_isolates_session():
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    await rail.before_tool_call(ctx(stream, "ab", tasks("A", "B")))
    rail.collect_cancelled_tool_updates("session")
    assert {r["tool_call_id"] for r in rail.get_cancelled_tool_results("session")} == {
        "subtask:ab:0",
        "subtask:ab:1",
    }
    assert rail.get_cancelled_tool_results("other") == []


def test_browser_metadata_is_removed_from_nested_batch_tasks():
    from openjiuwen.harness.tools.subagent.task_tool import create_task_tool

    parent = SimpleNamespace(
        card=SimpleNamespace(id="owner"), deep_config=SimpleNamespace(subagents=[])
    )
    rail = BrowserTaskPromptRail()
    rail.tools = create_task_tool(parent, "general-purpose")
    rail._sanitize_browser_metadata()
    properties = rail.tools[0].card.input_params["properties"]
    assert "browser_capabilities" not in properties
    assert "browser_capabilities" not in properties["tasks"]["items"]["properties"]


@pytest.mark.asyncio
async def test_batch_logs_each_child_execution_not_the_parent_wait(monkeypatch):
    from openjiuwen.core.foundation.tool import ToolCard
    from openjiuwen.core.session.agent import Session
    from openjiuwen.harness.tools.subagent.task_tool import TaskTool
    from jiuwenswarm.common import subagent_logging as observations

    observations.install_task_tool_logging()
    observed, logs = [], []
    monkeypatch.setattr(
        observations.SubagentTrace,
        "emit",
        lambda self, event, **kw: logs.append((self.task_id, event)),
    )
    gates = [asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event()]
    children = []

    class Child:
        card = SimpleNamespace(id="child")

        async def invoke(self, inputs):
            i = children.index(self)
            trace = observations.current_subagent_trace()
            observed.append(trace)
            started[i].set()
            await gates[i].wait()
            return {"result_type": "answer", "output": str(i)}

        async def cleanup_task_resources(self):
            pass

    def create(*args, **kwargs):
        child = Child()
        children.append(child)
        return child

    parent = SimpleNamespace(
        card=SimpleNamespace(id="owner"),
        deep_config=SimpleNamespace(subagents=[]),
        create_subagent=create,
        resolve_subagent_type=lambda name: "general-purpose",
    )
    tool = TaskTool(
        ToolCard(id="trace-test", name="task_tool"), parent
    )
    session = Session(session_id="trace-parent")
    waiter = asyncio.create_task(
        tool.invoke(tasks("A", "B"), session=session, tool_call_id="trace-ab")
    )
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started)), 2)
        assert all(observed) and observed[0] is not observed[1]
        gates[0].set()
        result = await asyncio.wait_for(waiter, 2)
        assert observed[0].parent_tool_call_id == "trace-ab"
        assert observed[0].task_id != observed[1].task_id
        assert (observed[0].task_id, "dispatch.end") in logs
        assert (observed[1].task_id, "dispatch.end") not in logs
        gates[1].set()
        await tool.invoke(
            {"action": "wait", "task_ids": result.data["pending_ids"]},
            session=session,
            tool_call_id="collect",
        )
        assert (observed[1].task_id, "dispatch.end") in logs
        assert observations.current_subagent_trace() is None
    finally:
        for gate in gates:
            gate.set()
        await tool.close_task_groups("trace-parent")
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_adapter_stop_closes_paused_task_group():
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(
        stop=AsyncMock(), cancel_background_subtasks=AsyncMock(return_value=None)
    )
    await adapter.stop_interaction()
    adapter._instance.cancel_background_subtasks.assert_awaited_once()
    adapter._instance.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_run_result_does_not_reset_new_run_projection():
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    old = ctx(stream, "reused", tasks("old"))
    await rail.before_tool_call(old)
    new = ctx(stream, "reused", tasks("new"))
    new.extra["task_group.run"] = ("new-run", None)
    await rail.before_tool_call(new)
    old.inputs.tool_result = SimpleNamespace(
        data={"ready": [snapshot("reused", 0)]}, success=True
    )
    await rail.after_tool_call(old)
    assert not events(stream, "tool_result")
    assert rail._task_group_runs["session"] == "new-run"
    new.inputs.tool_result = SimpleNamespace(
        data={"ready": [snapshot("reused", 0)]}, success=True
    )
    await rail.after_tool_call(new)
    assert len(events(stream, "tool_result")) == 1


@pytest.mark.asyncio
async def test_result_projection_can_retry_after_old_stream_write_failed():
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    call = ctx(stream, "ab", tasks("A"))
    await rail.before_tool_call(call)

    async def broken(chunk):
        raise RuntimeError("old stream closed")

    stream.write_stream = broken
    call.inputs.tool_result = SimpleNamespace(
        data={"ready": [snapshot("ab", 0)]}, success=True
    )
    await rail.after_tool_call(call)
    current = Stream()
    call.session = current
    await rail.after_tool_call(call)
    assert [r["tool_call_id"] for r in events(current, "tool_result")] == [
        "subtask:ab:0"
    ]


@pytest.mark.asyncio
async def test_user_stop_keeps_completed_unretrieved_result_in_interrupt_history(
    monkeypatch,
):
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    stream, rail = Stream(), JiuSwarmStreamEventRail()
    await rail.before_tool_call(ctx(stream, "ab", tasks("A", "B")))
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._stream_event_rail = rail
    adapter._resolve_interrupt_session_id = lambda sid: sid
    adapter._cancel_session_agent_tasks = AsyncMock(return_value=0)
    adapter._instance = SimpleNamespace(
        cancel_background_subtasks=AsyncMock(
            return_value={
                "tasks": [snapshot("ab", 0, "cancelled"), snapshot("ab", 1)],
            }
        )
    )
    monkeypatch.setattr(
        "openjiuwen.core.sys_operation.shell_process_registry.kill_shell_processes_for_session_tree",
        lambda sid: 0,
    )
    results = await adapter._stop_session_interrupt_work("session", intent="cancel")
    (completed,) = [r for r in results if r["tool_call_id"] == "subtask:ab:1"]
    assert (
        completed["success"] is True and completed["raw_output"]["output"] == "result-1"
    )
    assert {r["tool_call_id"] for r in results} == {"subtask:ab:0", "subtask:ab:1"}


@pytest.mark.asyncio
async def test_permission_pause_before_batch_execution_keeps_children_pending():
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    call = ctx(stream, "before-dispatch", tasks("A", "B"),
               SimpleNamespace(success=False, data=None, error=""))
    interrupt_type = type("ToolInterruptException", (Exception,), {})
    call.exception = interrupt_type("approval required")
    call.exception.request = SimpleNamespace(tool_call_id="before-dispatch", message="Approve delegation")
    await rail.before_tool_call(call)
    await rail.after_tool_call(call)
    assert not events(stream, "tool_result")
    call.exception = None
    call.inputs.tool_result = SimpleNamespace(success=True, data={"ready": [snapshot("before-dispatch", 0)]})
    await rail.before_tool_call(call)
    await rail.after_tool_call(call)
    assert len(events(stream, "tool_call")) == 2
    assert [e["tool_call_id"] for e in events(stream, "tool_result")] == ["subtask:before-dispatch:0"]


@pytest.mark.asyncio
async def test_stream_cleanup_retains_card_identity_while_core_scope_is_paused():
    from openjiuwen.harness.tools.subagent.task_manager import AsyncSubagentTaskManager
    manager = AsyncSubagentTaskManager("owner")
    scope = manager.scope_for_session("session")
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    rail._deep_agent = SimpleNamespace(find_rails_by_type=lambda kinds: [SimpleNamespace(task_manager=manager)])
    dispatch_ctx = ctx(stream, "paused", tasks("A", "B"))
    dispatch_ctx.extra["task_group.run"] = (scope.run_id, None)
    try:
        await rail.before_tool_call(dispatch_ctx)
        # Adapter _unmark_session_active calls this even after HITL output.
        rail.cleanup_session("session")
        resumed = Stream()
        wait = ctx(resumed, "collect", {"action": "wait", "task_ids": ["paused-1"]},
                   SimpleNamespace(success=True, data={"ready": [snapshot("paused", 1)]}))
        wait.extra["task_group.run"] = (scope.run_id, None)
        await rail.before_tool_call(wait)
        await rail.after_tool_call(wait)
        assert [r["tool_call_id"] for r in events(resumed, "tool_result")] == ["subtask:paused:1"]
        await manager.close_run(scope, "test")
        rail.cleanup_session("session")
        assert not rail._task_group_views and not rail._task_group_runs
    finally:
        await manager.close_run(scope, "test")


@pytest.mark.asyncio
@pytest.mark.parametrize("with_binding", [True, False])
async def test_uncollected_child_pushes_terminal_once_before_parent_waits(with_binding):
    from openjiuwen.harness.tools.subagent.task_manager import AsyncSubagentTaskManager, TaskRequest

    manager = AsyncSubagentTaskManager("owner")
    scope = manager.scope_for_session("session")
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    rail._deep_agent = SimpleNamespace(find_rails_by_type=lambda kinds: [SimpleNamespace(task_manager=manager)])
    gate, delivered = asyncio.Event(), asyncio.Event()
    original_write = stream.write_stream

    async def write(chunk):
        await original_write(chunk)
        if chunk.type == "tool_result":
            delivered.set()

    stream.write_stream = write
    call = ctx(stream, "dispatch", tasks("A"))
    if with_binding:
        call.extra["task_group.run"] = (scope.run_id, None)
    else:
        call.extra.pop("task_group.run")

    def executor(execution):
        async def execute(_query):
            await gate.wait()
            return {"result_type": "answer", "output": "uncollected result"}
        async def cleanup():
            pass
        return execute, cleanup

    try:
        await rail.before_tool_call(call)
        receipt = manager.submit_batch(scope, "dispatch", [TaskRequest("general-purpose", "A")],
                                       existing_ids=[], executor_factory=executor)
        call.inputs.tool_result = SimpleNamespace(success=True, data={"ready": [], "pending_ids": receipt.wait_ids})
        await rail.after_tool_call(call)
        assert not events(stream, "tool_result")
        gate.set()
        await asyncio.wait_for(delivered.wait(), 2)
        assert events(stream, "tool_result")[0]["tool_call_id"] == "subtask:dispatch:0"
        ready = await manager.wait_any(scope, receipt.wait_ids, 0)
        call.inputs.tool_result.data["ready"] = ready.ready
        await rail.after_tool_call(call)
        assert len(events(stream, "tool_result")) == 1
        report = await manager.close_run(scope, "test")
        assert len(report["uncollected"]) == 1
    finally:
        gate.set()
        await manager.close_run(scope, "test")
        rail.cleanup_session("session")


@pytest.mark.asyncio
async def test_terminal_notification_waits_for_live_stream_and_ignores_other_run():
    from openjiuwen.harness.tools.subagent.task_manager import SubagentTaskScope
    stream, rail = Stream(), JiuSwarmStreamEventRail()
    call = ctx(stream, "ab", tasks("A"))
    await rail.before_tool_call(call)
    key = ("session", "run", "subtask:ab:0")
    # Simulate the old stream ending at an approval while Core still owns the run.
    rail._task_group_views[key]["session"] = None
    rail._on_task_group_state(SubagentTaskScope("owner", "foreign", "run"), snapshot("ab", 0))
    rail._on_task_group_state(SubagentTaskScope("owner", "session", "old-run"), snapshot("ab", 0))
    assert not rail._task_group_emissions
    rail._on_task_group_state(SubagentTaskScope("owner", "session", "run"), snapshot("ab", 0))
    await asyncio.gather(*rail._task_group_emissions.values())
    assert not events(stream, "tool_result")
    current = Stream()
    resumed = ctx(current, "wait", {"action": "wait", "task_ids": ["ab-0"]})
    # Rebinding also retries a terminal snapshot that could not reach a closed stream.
    await rail._flush_task_group_events(resumed)
    await rail._flush_task_group_events(resumed)
    assert len(events(current, "tool_result")) == 1
    rail.uninit(None)


@pytest.mark.asyncio
async def test_missing_binding_uses_current_core_run_over_previous_cached_run():
    from openjiuwen.harness.tools.subagent.task_manager import AsyncSubagentTaskManager
    manager = AsyncSubagentTaskManager("owner")
    rail, stream = JiuSwarmStreamEventRail(), Stream()
    rail._deep_agent = SimpleNamespace(find_rails_by_type=lambda kinds: [SimpleNamespace(task_manager=manager)])
    first = manager.scope_for_session("session")
    call = ctx(stream, "same-call", tasks("A"))
    call.extra.pop("task_group.run")
    try:
        await rail.before_tool_call(call)
        assert rail._task_group_runs["session"] == first.run_id
        await manager.close_run(first, "first_finished")
        second = manager.scope_for_session("session")
        await rail.before_tool_call(call)
        assert rail._task_group_runs["session"] == second.run_id
        rail._on_task_group_state(first, snapshot("same-call", 0))
        assert not rail._task_group_emissions
        rail._on_task_group_state(second, snapshot("same-call", 0))
        await asyncio.gather(*rail._task_group_emissions.values())
        assert len(events(stream, "tool_result")) == 1
    finally:
        active = manager.scope_if_active("session")
        if active is not None:
            await manager.close_run(active, "test")
        rail.uninit(None)


@pytest.mark.asyncio
async def test_projection_does_not_create_scope_or_invent_run_when_none_is_active():
    from openjiuwen.harness.tools.subagent.task_manager import AsyncSubagentTaskManager
    manager = AsyncSubagentTaskManager("owner")
    rail, stream = JiuSwarmStreamEventRail(), Stream()
    rail._deep_agent = SimpleNamespace(find_rails_by_type=lambda kinds: [SimpleNamespace(task_manager=manager)])
    call = ctx(stream, "unknown", tasks("A"))
    call.extra.pop("task_group.run")
    try:
        await rail.before_tool_call(call)
        assert manager.scope_if_active("session") is None
        assert not rail._task_group_views and not rail._task_group_runs
        assert not events(stream, "tool_call")
    finally:
        rail.uninit(None)
