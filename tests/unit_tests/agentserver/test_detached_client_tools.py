# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The agent side of detached client tools: the tool, its guard rail and their registration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openjiuwen.core.foundation.tool import ToolExposure
from openjiuwen.core.single_agent.rail.base import ToolCallInputs, UserMessageInputs

from jiuwenswarm.agents.harness.common.rails.client_tool_guard_rail import ClientToolGuardRail
from jiuwenswarm.agents.harness.common.tools.detached_client_tool import DetachedClientTool
from jiuwenswarm.runtime.client_tool_adapter import ClientToolAdapter, ClientToolStateStore
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn

AGENT_TOOL = "client__read_page"


async def _noop_emit(session_id, payload) -> None:
    del session_id, payload


async def _noop_deliver(call, envelope) -> None:
    del call, envelope


async def _adapter(tmp_path) -> ClientToolAdapter:
    adapter = ClientToolAdapter(store=ClientToolStateStore(lambda: tmp_path), emit=_noop_emit, deliver=_noop_deliver)
    await adapter.register("s1", "pane-1", [{"name": "read_page", "description": "Read one page"}])
    return adapter


def _tool(adapter: ClientToolAdapter) -> DetachedClientTool:
    [spec] = adapter.specs("s1")
    return DetachedClientTool(spec, session_id="s1", adapter=lambda: adapter)


def _callback_context(call_id: str = "call-1") -> SimpleNamespace:
    return SimpleNamespace(inputs=SimpleNamespace(tool_call=SimpleNamespace(id=call_id)))


async def test_calling_the_tool_starts_the_call_and_returns_at_once(tmp_path) -> None:
    adapter = await _adapter(tmp_path)

    output = await _tool(adapter).invoke({"page": 1}, _tool_callback_context=_callback_context())

    assert output.success is True
    assert output.data["call_id"] == "call-1"
    assert "arrive later" in output.data["content"]
    assert [event["call_id"] for event in adapter.list_pending("s1")] == ["call-1"]
    await adapter.aclose()


async def test_the_tool_is_offered_directly_not_behind_tool_search(tmp_path) -> None:
    # Regression (E2E): with progressive tool discovery an undeclared card is deferred, so the
    # model only saw the client tool after a tool_search it had no reason to make.
    adapter = await _adapter(tmp_path)

    card = _tool(adapter).card

    assert card.exposure is ToolExposure.DIRECT
    assert card.get_exposure_declared() is True
    await adapter.aclose()


async def test_the_tool_reports_an_unavailable_adapter_as_a_tool_error(tmp_path) -> None:
    adapter = await _adapter(tmp_path)
    [spec] = adapter.specs("s1")
    tool = DetachedClientTool(spec, session_id="s1", adapter=lambda: None)

    output = await tool.invoke({"page": 1})

    assert output.success is False


def _tool_ctx(arguments: dict, call_id: str = "call-2") -> SimpleNamespace:
    return SimpleNamespace(
        inputs=ToolCallInputs(tool_call=SimpleNamespace(id=call_id), tool_name=AGENT_TOOL, tool_args=arguments),
        extra={},
    )


async def test_the_rail_refuses_a_repeat_of_a_running_call(tmp_path) -> None:
    adapter = await _adapter(tmp_path)
    await adapter.open_call("s1", "call-1", AGENT_TOOL, {"page": 1})
    rail = ClientToolGuardRail()
    rail.bind("s1", lambda: adapter)

    repeat, other = _tool_ctx({"page": 1}), _tool_ctx({"page": 2}, call_id="call-3")
    await rail.before_tool_call(repeat)
    await rail.before_tool_call(other)

    assert repeat.extra["_skip_tool_calls"] == {"call-2": True}
    assert "call-1" in repeat.inputs.tool_msg.content
    assert other.extra == {}
    await adapter.aclose()


async def test_the_rail_lets_a_failed_call_run_again_only_after_the_user_speaks(tmp_path) -> None:
    adapter = await _adapter(tmp_path)
    await adapter.open_call("s1", "call-1", AGENT_TOOL, {"page": 1})
    await adapter.cancel("s1", "call-1")
    rail = ClientToolGuardRail()
    rail.bind("s1", lambda: adapter)

    envelope = '<client_tool_result call_id="call-9" tool="client__read_page" status="completed">\nx\n'
    await rail.on_user_message(SimpleNamespace(inputs=UserMessageInputs(parts=[envelope])))
    refused = _tool_ctx({"page": 1})
    await rail.before_tool_call(refused)
    assert "_skip_tool_calls" in refused.extra

    await rail.on_user_message(SimpleNamespace(inputs=UserMessageInputs(parts=["please try page 1 again"])))
    allowed = _tool_ctx({"page": 1}, call_id="call-4")
    await rail.before_tool_call(allowed)
    assert allowed.extra == {}


async def test_a_delivered_result_as_the_host_renders_it_does_not_count_as_the_user_speaking(tmp_path) -> None:
    # Regression (E2E): rails see the input after UserTurn.render wrapped it in the host's
    # JSON envelope, and the bare-prefix check took the delivery for user input.
    adapter = await _adapter(tmp_path)
    await adapter.open_call("s1", "call-1", AGENT_TOOL, {"page": 1})
    await adapter.cancel("s1", "call-1")
    rail = ClientToolGuardRail()
    rail.bind("s1", lambda: adapter)
    envelope = '<client_tool_result call_id="call-9" tool="client__read_page" status="completed">\nx\n</client_tool_result>'
    rendered = UserTurn(text=envelope, channel="web", language="zh", files={}).render()

    await rail.on_user_message(SimpleNamespace(inputs=UserMessageInputs(parts=[rendered])))
    refused = _tool_ctx({"page": 1})
    await rail.before_tool_call(refused)

    assert "_skip_tool_calls" in refused.extra
    user_spoke = UserTurn(text="please try page 1 again", channel="web", language="zh", files={}).render()
    await rail.on_user_message(SimpleNamespace(inputs=UserMessageInputs(parts=[user_spoke])))
    allowed = _tool_ctx({"page": 1}, call_id="call-4")
    await rail.before_tool_call(allowed)
    assert allowed.extra == {}


async def test_the_rail_leaves_other_tools_alone(tmp_path) -> None:
    adapter = await _adapter(tmp_path)
    rail = ClientToolGuardRail()
    rail.bind("s1", lambda: adapter)
    ctx = SimpleNamespace(inputs=ToolCallInputs(tool_call=SimpleNamespace(id="x"), tool_name="read_file"), extra={})

    await rail.before_tool_call(ctx)

    assert ctx.extra == {}


async def test_the_rail_reminds_the_model_of_running_calls(tmp_path) -> None:
    adapter = await _adapter(tmp_path)
    await adapter.open_call("s1", "call-1", AGENT_TOOL, {"page": 1})
    rail = ClientToolGuardRail()
    rail.bind("s1", lambda: adapter)
    builder = MagicMock()
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    await rail.before_model_call(SimpleNamespace())

    builder.remove_section.assert_called_with("client_tools")
    [section] = [call.args[0] for call in builder.add_section.call_args_list]
    assert "call-1" in section.content["en"]
    await adapter.aclose()


def _deep_adapter(registered: list, removed: list) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    ability_manager = MagicMock()
    ability_manager.add.side_effect = lambda card: registered.append(card.name)
    adapter._instance = SimpleNamespace(ability_manager=ability_manager)
    adapter._last_mode = "agent"
    adapter._is_cron_execution = False
    adapter._client_tool_guard_rail = ClientToolGuardRail()
    adapter._client_tools = {}
    adapter._register_agent_owned_tool = lambda tool, owner_id: None
    adapter._tool_owner_id = lambda: "owner"
    adapter._remove_registered_tools = lambda tools: removed.extend(tool.card.name for tool in tools)
    return adapter


async def test_the_agent_is_offered_exactly_the_declared_client_tools(tmp_path) -> None:
    client_tools = await _adapter(tmp_path)
    registered: list[str] = []
    removed: list[str] = []
    deep = _deep_adapter(registered, removed)

    with patch("jiuwenswarm.runtime.context.get_current_runtime",
               return_value=SimpleNamespace(client_tool_adapter=client_tools)):
        deep._ensure_client_tools_registered("s1", "web")
        deep._ensure_client_tools_registered("s1", "web")
        assert registered == [AGENT_TOOL]

        await client_tools.register("s1", "pane-1", [], replace_owned=True)
        deep._ensure_client_tools_registered("s1", "web")

    assert removed == [AGENT_TOOL]
    assert deep._client_tools == {}


async def test_a_channel_without_clients_is_offered_no_client_tools(tmp_path) -> None:
    client_tools = await _adapter(tmp_path)
    registered: list[str] = []
    deep = _deep_adapter(registered, [])

    with patch("jiuwenswarm.runtime.context.get_current_runtime",
               return_value=SimpleNamespace(client_tool_adapter=client_tools)):
        deep._ensure_client_tools_registered("s1", "feishu")

    assert registered == []
