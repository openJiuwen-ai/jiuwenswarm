"""Client tools follow the permission rules like every tool, in manual and in Smart mode.

With no rule of their own (the shipped ``"*": allow``) the client gets the call
without a prompt. An explicit ask asks first, and the client gets the call
once, after the approval. A deny ends the call before the client sees it.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
import yaml
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.foundation.tool import ClientTool, ToolCard
from openjiuwen.core.single_agent.interrupt import InterruptKind
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY, pending_interrupts
from agent_gateway.common.interrupt_resume import CLIENT_TOOL_INTERRUPT_SOURCE

from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
from jiuwenswarm.common import config, utils
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.runtime.agent_adapter import interface, interface_deep
from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import _request, answer_host  # noqa: F401
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold  # noqa: F401
from tests.unit_tests.runtime.test_answer_after_idle_eviction import _Manager
from tests.unit_tests.runtime.test_runtime_service import FakePlanController, _collect_events

pytestmark = pytest.mark.asyncio


class _ClientProbe(ClientTool):
    """A tool the connected client runs; keeps every request it was asked to build."""

    def __init__(self, name: str) -> None:
        super().__init__(ToolCard(id=name, name=name, description="Client probe", input_params={
            "type": "object", "properties": {"value": {"type": "string"}},
        }))
        self.requests: list[dict] = []

    def build_request(self, arguments, context):
        self.requests.append(arguments)
        return {"input": arguments}


@pytest.fixture
def client_host(answer_host):
    h = answer_host
    h.tool = _ClientProbe(h.tool.card.name)
    h.raw["permissions"]["defaults"] = {"*": "allow"}
    h.raw["permissions"]["tools"].pop(h.tool.card.name)
    return h


def _tool_texts(h) -> list[str]:
    """What the model was last shown as the probe call's result."""
    _args, kwargs = h.script.calls[-1]
    return [m.content for m in kwargs["messages"] if getattr(m, "tool_call_id", None) == "call_0"]


def _pending(h) -> list:
    return pending_interrupts(h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY))


async def _call_the_client_tool(h, *, mode: str, rule: str | None) -> None:
    h.raw["permissions"]["mode"] = mode
    if rule is not None:
        h.raw["permissions"]["tools"][h.tool.card.name] = rule
    await h.create()
    await h.adapter.start_interaction(h.adapter._parent_session_id)
    h.script.responses = [
        AssistantMessage(content="", tool_calls=[ToolCall(
            id="call_0", type="function", name=h.tool.card.name, arguments=json.dumps({"value": "once"}),
        )]),
        AssistantMessage(content="completed"),
    ]
    assert (await _request(h, query="Run the probe")).ok


async def _answer_the_client(h) -> None:
    (interrupt,) = _pending(h)
    assert interrupt.kind == InterruptKind.CLIENT_TOOL
    result = await _request(h, source=CLIENT_TOOL_INTERRUPT_SOURCE, answers=[{
        "interrupt_id": interrupt.interrupt_id, "request_id": "call_0",
        "result": {"content": [{"type": "text", "text": "probe ran"}], "is_error": False},
    }])
    assert result.ok, result.payload
    assert _pending(h) == []
    assert _tool_texts(h) == ["probe ran"]


def _approval(h) -> dict:
    (interrupt,) = _pending(h)
    assert interrupt.kind == InterruptKind.PERMISSION
    answer = {"selected_options": ["allow_once"]}
    cards = h.adapter._root_permission_queue.snapshot_scope(root_session_id=h.adapter._parent_session_id)
    if cards:
        answer["card_id"] = cards[0].tool_call_id
    return {"source": "permission_interrupt", "request_id": interrupt.tool_call_id,
            "interrupt_id": interrupt.interrupt_id, "answers": [answer]}


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_with_no_rule_the_client_gets_the_call_without_a_prompt(client_host, monkeypatch, mode):
    h = client_host
    await _call_the_client_tool(h, mode=mode, rule=None)

    assert h.tool.requests == [{"value": "once"}]
    await _answer_the_client(h)
    assert len(h.script.calls) == 2 and h.tool.requests == [{"value": "once"}]


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_an_ask_rule_asks_first_and_the_client_gets_the_call_once_after_approval(client_host, monkeypatch, mode):
    h = client_host
    await _call_the_client_tool(h, mode=mode, rule="ask")
    assert h.tool.requests == []

    assert (await _request(h, **_approval(h))).ok
    assert h.tool.requests == [{"value": "once"}]

    await _answer_the_client(h)
    assert h.tool.requests == [{"value": "once"}]


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_a_deny_rule_ends_the_call_before_the_client_sees_it(client_host, monkeypatch, mode):
    h = client_host
    await _call_the_client_tool(h, mode=mode, rule="deny")

    assert h.tool.requests == []
    assert _pending(h) == []
    (text,) = _tool_texts(h)
    assert "PERMISSION_DENIED" in text


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_the_paused_agent_says_what_its_pause_asks_until_it_is_answered(client_host, monkeypatch, mode):
    h = client_host
    session_id = h.adapter._parent_session_id
    await _call_the_client_tool(h, mode=mode, rule="ask")

    (question,) = await h.adapter.pending_interactions(session_id)
    (interrupt,) = _pending(h)
    assert (question["event_type"], question["source"], question["request_id"], question["interrupt_id"]) == (
        "chat.ask_user_question", "permission_interrupt", "call_0", interrupt.interrupt_id)
    assert ("card_id" in question["questions"][0]) is (mode == "auto")  # Smart answers its card

    assert (await _request(h, **_approval(h))).ok
    (request,) = await h.adapter.pending_interactions(session_id)
    (interrupt,) = _pending(h)
    assert request == {
        "event_type": "chat.client_tool_request", "request_id": "call_0", "interrupt_id": interrupt.interrupt_id,
        "source": "client_tool_interrupt", "tool_name": h.tool.card.name, "arguments": {"value": "once"},
        "client_tool": {"input": {"value": "once"}},
    }

    await _answer_the_client(h)
    assert await h.adapter.pending_interactions(session_id) == []


def _asked(h) -> bool:
    return h.tool.requests == [] and [i.kind for i in _pending(h)] == [InterruptKind.PERMISSION]


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_a_default_of_ask_asks_before_a_client_tool_no_rule_names(client_host, mode):
    h = client_host
    h.raw["permissions"]["defaults"] = {"*": "ask"}
    await _call_the_client_tool(h, mode=mode, rule=None)

    assert _asked(h)


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_an_allow_rule_runs_the_client_tool_under_a_default_of_ask(client_host, mode):
    h = client_host
    h.raw["permissions"]["defaults"] = {"*": "ask"}
    await _call_the_client_tool(h, mode=mode, rule="allow")

    assert h.tool.requests == [{"value": "once"}]
    await _answer_the_client(h)


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_a_session_allow_runs_the_client_tool_under_a_default_of_ask(client_host, monkeypatch, tmp_path, mode):
    h = client_host
    h.raw["permissions"]["defaults"] = {"*": "ask"}
    h.session = {"allow_tools": [h.tool.card.name]}
    # Smart reads the User and Session layers from their files under the storage lock.
    session_file = tmp_path / "session_permissions.yaml"
    session_file.write_text(yaml.safe_dump(h.session), encoding="utf-8")
    monkeypatch.setattr(permissions_layers, "session_permissions_path", lambda _sid: session_file)
    monkeypatch.setattr(permissions_layers, "user_permissions_path", lambda: tmp_path / "user_permissions.yaml")
    await _call_the_client_tool(h, mode=mode, rule=None)

    assert h.tool.requests == [{"value": "once"}]
    await _answer_the_client(h)


async def test_an_owner_scope_deny_still_ends_the_call_in_smart_mode(client_host):
    h = client_host
    h.raw["permissions"]["mode"] = "auto"
    h.raw["permissions"]["owner_scopes"] = {"web": {"owner-1": {"tools": {h.tool.card.name: "deny"}}}}
    await h.create()
    await h.adapter.start_interaction(h.adapter._parent_session_id)
    h.script.responses = [
        AssistantMessage(content="", tool_calls=[ToolCall(
            id="call_0", type="function", name=h.tool.card.name, arguments=json.dumps({"value": "once"}),
        )]),
        AssistantMessage(content="completed"),
    ]
    request = AgentRequest(
        request_id="request-owner", session_id=h.adapter._parent_session_id, channel_id="web",
        req_method=ReqMethod.CHAT_SEND, metadata={"principal_user_id": "owner-1"},
        params={"mode": "agent", "project_dir": str(h.root), "query": "Run the probe"},
    )
    inputs, _, _ = h.facade.build_inputs(request)
    assert (await asyncio.wait_for(h.adapter.process_message_impl(request, inputs), 10)).ok

    assert h.tool.requests == [] and _pending(h) == []
    (text,) = _tool_texts(h)
    assert "owner_scopes: deny" in text


def _probe_call(h, call_id: str) -> AssistantMessage:
    return AssistantMessage(content="", tool_calls=[ToolCall(
        id=call_id, type="function", name=h.tool.card.name, arguments=json.dumps({"value": call_id}),
    )])


async def test_always_allow_on_the_card_runs_the_rest_of_the_turn_in_smart_mode(client_host, monkeypatch, tmp_path):
    h = client_host
    h.raw["permissions"].update(mode="auto", defaults={"*": "ask"})
    config_file = tmp_path / "config" / "config.yaml"
    config_file.parent.mkdir()
    config_file.write_text(yaml.safe_dump({"permissions": h.raw["permissions"]}))
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", config_file)
    monkeypatch.setattr(utils, "get_config_dir", lambda: config_file.parent)
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    await h.create()
    await h.adapter.start_interaction(h.adapter._parent_session_id)
    h.script.responses = [_probe_call(h, "call_0"), _probe_call(h, "call_1"), AssistantMessage(content="completed")]
    assert (await _request(h, query="Run the probe twice")).ok

    approval = _approval(h)
    approval["answers"][0]["selected_options"] = ["always_allow"]
    assert (await _request(h, **approval)).ok
    (interrupt,) = _pending(h)
    assert (await _request(h, source=CLIENT_TOOL_INTERRUPT_SOURCE, answers=[{
        "interrupt_id": interrupt.interrupt_id, "request_id": "call_0",
        "result": {"content": [{"type": "text", "text": "probe ran"}], "is_error": False},
    }])).ok

    saved = yaml.safe_load(permissions_layers.user_permissions_path().read_text())
    assert h.tool.card.name in json.dumps(saved)
    assert h.tool.requests == [{"value": "call_0"}, {"value": "call_1"}]
    assert [i.kind for i in _pending(h)] == [InterruptKind.CLIENT_TOOL]


def _runtime_over_a_router(h) -> tuple[AgentRuntime, interface_deep.JiuWenSwarmDeepAdapter]:
    router = interface_deep.JiuWenSwarmDeepAdapter()

    async def get_or_create(session_id, **_kwargs):
        key = router._session_adapter_key(session_id)
        async with router._session_adapter_locks.setdefault(key, asyncio.Lock()):
            if key not in router._session_adapters:
                h.adapter._instance = None
                await h.create()
                await h.adapter.start_interaction(key)
                router._session_adapters[key] = h.adapter
            router._session_adapter_last_used[key] = time.time()
            return router._session_adapters[key]

    router._get_or_create_session_adapter = get_or_create
    facade = object.__new__(interface.JiuWenSwarm)
    facade._adapter = router
    facade._session_manager = SessionManager()

    async def turn(request):
        inputs, _, _ = facade.build_inputs(request)
        async for chunk in router.process_message_stream_impl(request, inputs):
            yield chunk

    facade.process_message_stream = turn
    runtime = AgentRuntime(agent_manager=_Manager(facade), initializer=AsyncMock(),
                           plan_controller=FakePlanController(), admission_controller=SessionRunAdmission())
    return runtime, router


@pytest.mark.parametrize("read_first", [True, False])
@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_a_pause_outlives_an_idle_eviction_of_its_agent(client_host, monkeypatch, tmp_path, mode, read_first):
    h = client_host
    session_id = h.adapter._parent_session_id
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda *_args, **_kwargs: None)
    h.raw["permissions"]["mode"] = mode
    runtime, router = _runtime_over_a_router(h)
    h.script.responses = [
        AssistantMessage(content="", tool_calls=[ToolCall(
            id="call_0", type="function", name=h.tool.card.name, arguments=json.dumps({"value": "once"}),
        )]),
        AssistantMessage(content="completed"),
    ]
    params = {"mode": "agent", "project_dir": str(h.root)}

    def request(request_id: str, **extra) -> AgentRequest:
        return AgentRequest(request_id=request_id, channel_id="web", session_id=session_id,
                            req_method=ReqMethod.CHAT_SEND, is_stream=True, params={**params, **extra})

    events = await asyncio.wait_for(_collect_events(runtime.stream(
        request("turn-1", query="Run the probe"), trigger_hook=False)), 15)
    assert [e.payload["request_id"] for e in events
            if (e.payload or {}).get("event_type") == "chat.client_tool_request"] == ["call_0"]
    (interrupt,) = _pending(h)

    assert await router.cleanup_session_adapter(session_id)
    if read_first:
        assert [e["request_id"] for e in await runtime.pending_interactions(session_id)] == ["call_0"]
    await asyncio.wait_for(_collect_events(runtime.stream(request(
        "resume-1", source=CLIENT_TOOL_INTERRUPT_SOURCE, answers=[{
            "interrupt_id": interrupt.interrupt_id, "request_id": "call_0",
            "result": {"content": [{"type": "text", "text": "probe ran"}], "is_error": False}}]),
        trigger_hook=False)), 15)

    assert _tool_texts(h) == ["probe ran"]
    assert await runtime.pending_interactions(session_id) == []
