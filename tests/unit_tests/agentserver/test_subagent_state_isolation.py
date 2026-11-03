"""Concurrent tool callbacks must keep permission and behavior state local."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.security import PermissionLevel, PermissionResult

from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_SESSION_ID
from jiuwenswarm.agents.harness.common.rails.behavior_security_rail import BehaviorSecurityRail
from jiuwenswarm.common.behavior_security import (
    ACTIVE_BEHAVIOR, BehaviorSecurityBridge, check_agent_skill_install, raw_event,
)


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


def _report_context(owner_id, call_id):
    agent = SimpleNamespace(
        deep_config=SimpleNamespace(tool_owner_id=owner_id),
        card=SimpleNamespace(id="parent-runtime" if owner_id is None else "shared-config-card"),
    )
    # Tool hooks run on the inner ReActAgent, whose card is not the runtime owner.
    ctx = AgentCallbackContext(
        agent=SimpleNamespace(card=SimpleNamespace(id="inner-" + call_id)),
        session=Session(session_id=owner_id or "parent-session"),
        inputs=ToolCallInputs(
            tool_call=ToolCall(id=call_id, name="echo", type="function", arguments="{}"),
            tool_name="echo", tool_args={},
        ),
    )
    return agent, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("local_level", [PermissionLevel.ALLOW, PermissionLevel.UNDETERMINED])
async def test_uninit_one_child_does_not_cancel_sibling_report(local_level):
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def transport(request):
        observed.append(json.loads(request.content))
        if len(observed) == 3:
            started.set()
        await release.wait()
        return httpx.Response(200, json={"decision": "allow"})

    bridge = BehaviorSecurityBridge({}, transport=httpx.MockTransport(transport))
    rails = {}
    token = CURRENT_SESSION_ID.set("desktop-conversation")
    contexts = {name: _report_context(owner, name) for name, owner in (
        ("parent", None), ("A", "child-A"), ("B", "child-B"),
    )}
    tasks = {}
    expected = []
    try:
        for name, (agent, ctx) in contexts.items():
            rail = rails[name] = BehaviorSecurityRail(bridge)
            rail.init(agent)
            await rail.before_tool_call(ctx)
            try:
                expected.append({**raw_event(ctx, "tool.before"), "mode": "report"})
                previous = set(bridge.tasks)
                await bridge.on_permission_evaluated(SimpleNamespace(
                    ctx=ctx, result=PermissionResult(local_level),
                ))
                tasks[name] = (bridge.tasks - previous).pop()
            finally:
                await rail.on_tool_exception(ctx)
        await asyncio.wait_for(started.wait(), 2)
        assert observed == expected
        assert all(event["sessionId"] == "desktop-conversation" for event in observed)

        # Missing ownership must never cancel another agent's pending reports.
        unbound = BehaviorSecurityRail(bridge)
        unbound.init(SimpleNamespace())
        unbound.uninit(SimpleNamespace())
        assert all(not task.cancelling() for task in tasks.values())
        rails["A"].uninit(contexts["A"][0])
        assert tasks["A"].cancelling()
        await asyncio.gather(tasks["A"], return_exceptions=True)
        assert tasks["A"].cancelled()
        assert not tasks["parent"].done() and not tasks["B"].done()
        assert tasks["A"] not in bridge._report_owners

        # A parent without explicit tool_owner_id uses its card's owner fallback.
        rails["parent"].uninit(contexts["parent"][0])
        assert tasks["parent"].cancelling()
        await asyncio.gather(tasks["parent"], return_exceptions=True)
        assert not tasks["B"].done()
        release.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        assert not tasks["B"].cancelled() and tasks["B"].exception() is None
        assert not bridge.tasks and not bridge._report_owners
    finally:
        CURRENT_SESSION_ID.reset(token)
        release.set()
        await bridge.close()


@pytest.mark.asyncio
async def test_unowned_report_is_not_cancelled_by_matching_public_session():
    started = asyncio.Event()
    release = asyncio.Event()

    async def transport(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"decision": "allow"})

    bridge = BehaviorSecurityBridge({}, transport=httpx.MockTransport(transport))
    rail = BehaviorSecurityRail(bridge)
    try:
        bridge.report({"sessionId": "child-A", "stage": "tool.before", "eventId": "unowned"})
        task, = bridge.tasks
        await asyncio.wait_for(started.wait(), 2)
        agent, _ = _report_context("child-A", "A")
        rail.init(agent)
        rail.uninit(agent)
        assert not task.cancelling()
        # Global shutdown still owns reports without a known runtime owner.
        await bridge.close()
        assert task.cancelled()
        assert not bridge.tasks and not bridge._report_owners
    finally:
        release.set()
        await bridge.close()


@pytest.mark.asyncio
async def test_skill_install_report_keeps_runtime_owner(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def transport(request):
        observed.append(json.loads(request.content))
        started.set()
        await release.wait()
        return httpx.Response(200, json={"decision": "allow"})

    bridge = BehaviorSecurityBridge({}, transport=httpx.MockTransport(transport))
    rail = BehaviorSecurityRail(bridge)
    agent, ctx = _report_context("child-A", "install-A")
    rail.init(agent)
    ctx.extra["behavior.local_permission"] = PermissionResult(PermissionLevel.ALLOW)
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "SKILL.md").write_text("local fixture", encoding="utf-8")
    session_token = CURRENT_SESSION_ID.set("desktop-conversation")
    await rail.before_tool_call(ctx)
    try:
        await check_agent_skill_install(staged, tmp_path / "installed", "fixture", "local")
        await asyncio.wait_for(started.wait(), 2)
        task, = bridge.tasks
        assert observed[0]["sessionId"] == "desktop-conversation"
        assert observed[0]["stage"] == "skill.before_install"
        assert "owner_id" not in observed[0]
        rail.uninit(agent)
        assert task.cancelling()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert not bridge.tasks and not bridge._report_owners
    finally:
        await rail.on_tool_exception(ctx)
        CURRENT_SESSION_ID.reset(session_token)
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
