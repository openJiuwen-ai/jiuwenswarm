"""Concurrent tool callbacks must keep permission and behavior state local."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.harness.common.rails.behavior_security_rail import BehaviorSecurityRail
from jiuwenswarm.common.behavior_security import ACTIVE_BEHAVIOR, BehaviorSecurityBridge


def test_child_stream_state_starts_empty_and_does_not_mutate_parent(tmp_path):
    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
    from openjiuwen.harness import create_deep_agent
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep  # noqa: F401
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail

    stream = JiuSwarmStreamEventRail()
    stream._abort_epoch["parent-session"] = 4
    parent = create_deep_agent(
        Model(model_client_config=ModelClientConfig(client_provider="OpenAI", api_key="test",
                                                   api_base="http://test.invalid/v1"),
              model_config=ModelRequestConfig(model="test")),
        workspace=str(tmp_path), auto_create_workspace=False, rails=[stream],
        add_general_purpose_agent=True, enable_read_image_multimodal=False,
    )
    first = parent.create_subagent("general-purpose", "child-A")
    second = parent.create_subagent("general-purpose", "child-B")
    left = first.find_rails_by_type((JiuSwarmStreamEventRail,))[0]
    right = second.find_rails_by_type((JiuSwarmStreamEventRail,))[0]
    assert left._abort_epoch == right._abort_epoch == {}
    assert left._symphony_stream_handler is not right._symphony_stream_handler
    left._inflight_tool_calls["call-A"] = {"tool": "echo"}
    assert not right._inflight_tool_calls and not stream._inflight_tool_calls


@pytest.mark.asyncio
async def test_parallel_tool_outputs_are_all_inspected_without_context_token_errors():
    events = []
    ready = asyncio.Event()
    entered = []

    async def transport(request):
        import json
        events.append(json.loads(request.content))
        return httpx.Response(200, json={"decision": "allow"})

    bridge = BehaviorSecurityBridge({}, transport=httpx.MockTransport(transport))
    rail = BehaviorSecurityRail(bridge)
    callbacks = AgentCallbackManager("isolated-behavior")
    agent = SimpleNamespace(agent_callback_manager=callbacks)
    for event, hook in rail.get_callbacks().items():
        await callbacks.register_callback(event, hook, rail.priority)
    manager = AbilityManager(owner_id="isolated-behavior")

    async def execute(label):
        active = ACTIVE_BEHAVIOR.get()
        entered.append((label, active[1]))
        if len(entered) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 2)
        assert active[1].inputs.tool_call.id == label
        return label

    tool = LocalFunction(ToolCard(id="echo", name="echo", description="test", input_params={
        "type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"],
    }), execute)
    manager.add_ability(tool.card, tool)
    session = Session(session_id="isolation")
    ctx = AgentCallbackContext(agent=agent, session=session, extra={"inherited": "kept"})
    calls = [ToolCall(id=name, name="echo", type="function", arguments='{"label":"' + name + '"}')
             for name in ("A", "B")]
    try:
        results = await manager.execute(ctx, calls, session)
        assert [r[0] for r in results] == ["A", "B"]
        assert sorted(event["tool"]["result"] for event in events) == ["A", "B"]
        assert all(local.extra["inherited"] == "kept" for _, local in entered)
        assert "behavior.finished" not in ctx.extra
        assert all("behavior.context_token" not in local.extra for _, local in entered)
        assert ACTIVE_BEHAVIOR.get() is None
    finally:
        manager.teardown_tools()
        await bridge.close()


@pytest.mark.asyncio
async def test_uninit_one_child_does_not_cancel_sibling_report():
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def transport(request):
        import json
        observed.append(json.loads(request.content)["sessionId"])
        if len(observed) == 2:
            started.set()
        await release.wait()
        return httpx.Response(200, json={"decision": "allow"})

    bridge = BehaviorSecurityBridge({}, transport=httpx.MockTransport(transport))
    rail = BehaviorSecurityRail(bridge)
    try:
        for name in ("child-A", "child-B"):
            bridge.report({"sessionId": name, "stage": "tool.before", "eventId": name})
        await asyncio.wait_for(started.wait(), 2)
        tasks = list(bridge.tasks)
        rail.uninit(SimpleNamespace(deep_config=SimpleNamespace(tool_owner_id="child-A")))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(task.cancelled() for task in tasks) == 1
        assert sum(not task.cancelled() and task.exception() is None for task in tasks) == 1
    finally:
        release.set()
        await bridge.close()


@pytest.mark.asyncio
async def test_cancelled_sequential_tool_does_not_leave_active_behavior_in_caller():
    bridge = BehaviorSecurityBridge({})
    rail = BehaviorSecurityRail(bridge)
    callbacks = AgentCallbackManager("cancelled-sequential")
    for event, hook in rail.get_callbacks().items():
        await callbacks.register_callback(event, hook, rail.priority)
    manager = AbilityManager(owner_id="cancelled-sequential")

    async def cancel():
        raise asyncio.CancelledError()

    tool = LocalFunction(ToolCard(id="cancel", name="cancel", description="test"), cancel)
    manager.add_ability(tool.card, tool)
    session = Session(session_id="cancelled-sequential")
    ctx = AgentCallbackContext(agent=SimpleNamespace(agent_callback_manager=callbacks), session=session)
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.execute(ctx, ToolCall(id="cancel-call", name="cancel", type="function", arguments="{}"),
                                  session, parallel_tool_calls=False)
        assert ACTIVE_BEHAVIOR.get() is None
    finally:
        ACTIVE_BEHAVIOR.set(None)
        manager.teardown_tools()
        await bridge.close()


@pytest.mark.asyncio
async def test_cross_context_token_cleanup_does_not_skip_output_inspection():
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs
    from jiuwenswarm.agents.harness.common.rails.behavior_security_rail import OUTPUT_BLOCK_MESSAGE

    inspected = []

    async def transport(request):
        inspected.append(request)
        return httpx.Response(200, json={"decision": "deny"})

    bridge = BehaviorSecurityBridge({}, transport=httpx.MockTransport(transport))
    rail = BehaviorSecurityRail(bridge)
    ctx = AgentCallbackContext(agent=None, session=Session(session_id="cross-context"), inputs=ToolCallInputs(
        tool_call=ToolCall(id="cross", name="echo", type="function", arguments="{}"),
        tool_name="echo", tool_args={}, tool_result="blocked", execution_started=True,
    ))
    try:
        await asyncio.create_task(rail.before_tool_call(ctx))
        await rail.after_tool_call(ctx)
        assert inspected and ctx.inputs.tool_result == OUTPUT_BLOCK_MESSAGE
        assert "behavior.context_token" not in ctx.extra
    finally:
        await bridge.close()
