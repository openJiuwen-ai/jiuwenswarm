# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``params.agent_subagents_available`` restricts one request's delegation.

The real adapter request path, the real SubagentRail and the real TaskTool
run. Only the external model is scripted, so no remote credentials are needed.
"""

import copy
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails import SubagentRail
from openjiuwen.harness.schema.config import SubAgentConfig

from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

ALLOWED = "explore_agent"
EXCLUDED = "research_agent"


class DelegatingModel:
    """Delegate to ``requested_type`` on the first turn, then answer."""

    def __init__(self, requested_type: str) -> None:
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI",
            api_key="local-test",
            api_base="http://127.0.0.1:1",
        )
        self.model_config = ModelRequestConfig(model_name="subagents-available-test")
        self.messages: list = []
        self.probe = lambda: None
        self._requested_type = requested_type

    async def invoke(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        if len(self.messages) == 1:
            self.probe()
            arguments = json.dumps(
                {
                    "subagent_type": self._requested_type,
                    "task_description": "probe",
                }
            )
            return AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="delegate-call",
                        type="function",
                        name="task_tool",
                        arguments=arguments,
                    )
                ],
                usage_metadata=UsageMetadata(
                    model_name="subagents-available-test",
                    finish_reason="tool_calls",
                ),
            )
        return AssistantMessage(
            content="done",
            usage_metadata=UsageMetadata(
                model_name="subagents-available-test", finish_reason="stop"
            ),
        )

    async def stream(self, messages, **kwargs):
        response = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(
            content=response.content,
            tool_calls=response.tool_calls,
            usage_metadata=response.usage_metadata,
        )


def _spec(name: str) -> SubAgentConfig:
    return SubAgentConfig(
        agent_card=AgentCard(name=name, description=f"{name} description"),
        system_prompt=f"You are {name}.",
    )


def _roster_names(agent) -> list[str]:
    return [
        spec.agent_card.name
        for spec in (agent.deep_config.subagents or [])
        if isinstance(spec, SubAgentConfig)
    ]


def _refused(model) -> bool:
    """Whether the model read a task_tool refusal in the last tool result."""
    return "not available through task_tool" in str(model.messages[-1])


def _task_tool_description(agent) -> str:
    for card in agent.ability_manager.list():
        if getattr(card, "name", "") == "task_tool":
            return str(getattr(card, "description", "") or "")
    return ""


async def _build_adapter(tmp_path, monkeypatch, model):
    """Build a started agent that offers two subagent templates."""
    await Runner.start()
    agent = create_deep_agent(
        model=model,
        workspace=str(tmp_path),
        subagents=[_spec(ALLOWED), _spec(EXCLUDED)],
        enable_task_loop=True,
        max_iterations=4,
        enable_model_anomaly_detection_rail=False,
        enable_read_image_multimodal=False,
    )
    session = create_agent_session(
        session_id=f"subagents-available-{uuid.uuid4().hex}", card=agent.card
    )
    await session.pre_run(inputs={})
    await agent.start(session=session)
    for rail in agent.find_pending_rails_by_type((SubagentRail,)):
        await agent.register_rail(rail)
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = session.get_session_id()
    # The host binds this reference the same way once the rails are built.
    adapter._subagent_rail = next(iter(agent.find_rails_by_type((SubagentRail,))), None)
    assert adapter._subagent_rail is not None
    # Isolate host configuration and observability. The adapter request path,
    # the SubagentRail and the TaskTool stay real.
    monkeypatch.setattr(adapter, "_model_config_error", lambda _request: None)
    monkeypatch.setattr(
        adapter, "_ensure_chat_extensions", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(adapter, "_update_runtime_config", AsyncMock())
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: model)
    monkeypatch.setattr(
        adapter,
        "_prepare_root_input_dispatch",
        AsyncMock(side_effect=lambda _req, inputs: inputs),
    )
    monkeypatch.setattr(
        agent_observability, "sync_agent_observability", lambda **_kwargs: None
    )
    return agent, session, adapter


async def _run_turn(adapter, session, params) -> list[dict]:
    request = AgentRequest(
        request_id=f"req-{uuid.uuid4().hex}",
        channel_id="web",
        session_id=session.get_session_id(),
        params=params,
    )
    payloads: list[dict] = []
    async for chunk in adapter._process_message_stream_impl(request, {"query": "go"}):
        if isinstance(chunk.payload, dict):
            payloads.append(chunk.payload)
    return payloads


@pytest.mark.asyncio
async def test_named_subagents_narrow_the_roster_and_the_tool_description(
    tmp_path, monkeypatch
):
    """A request that names one template may delegate to that template only."""
    seen: dict = {}
    model = DelegatingModel(EXCLUDED)
    agent, session, adapter = await _build_adapter(tmp_path, monkeypatch, model)
    model.probe = lambda: seen.update(
        roster=_roster_names(agent), description=_task_tool_description(agent)
    )

    await _run_turn(
        adapter,
        session,
        {"mode": "agent", "agent_subagents_available": [ALLOWED]},
    )

    assert seen.get("roster") == [ALLOWED], seen
    assert ALLOWED in seen.get("description", ""), seen
    assert EXCLUDED not in seen.get("description", ""), seen
    # task_tool refuses the excluded template instead of delegating to it, and
    # the model reads the refusal in the tool result.
    assert _refused(model), model.messages[-1]
    # The restriction lasted for the one request.
    assert sorted(_roster_names(agent)) == sorted([ALLOWED, EXCLUDED])


@pytest.mark.asyncio
async def test_absent_key_keeps_the_whole_roster(tmp_path, monkeypatch):
    """A request that omits the key delegates exactly as before."""
    seen: dict = {}
    model = DelegatingModel(EXCLUDED)
    agent, session, adapter = await _build_adapter(tmp_path, monkeypatch, model)
    model.probe = lambda: seen.update(
        roster=_roster_names(agent), description=_task_tool_description(agent)
    )

    await _run_turn(adapter, session, {"mode": "agent"})

    assert sorted(seen.get("roster", [])) == sorted([ALLOWED, EXCLUDED]), seen
    assert EXCLUDED in seen.get("description", ""), seen
    assert not _refused(model), model.messages[-1]


@pytest.mark.asyncio
async def test_unknown_template_name_fails_the_request(tmp_path, monkeypatch):
    """An unknown name fails the request, as an unknown agent_template does."""
    model = DelegatingModel(EXCLUDED)
    agent, session, adapter = await _build_adapter(tmp_path, monkeypatch, model)

    payloads = await _run_turn(
        adapter,
        session,
        {"mode": "agent", "agent_subagents_available": ["no_such_agent"]},
    )

    errors = [p for p in payloads if p.get("event_type") == "chat.error"]
    assert errors, payloads
    assert "no_such_agent" in str(errors[0].get("error") or ""), errors
    assert not model.messages, "the model must not run"
    assert sorted(_roster_names(agent)) == sorted([ALLOWED, EXCLUDED])


@pytest.mark.asyncio
async def test_non_list_value_fails_the_request(tmp_path, monkeypatch):
    """A value of the wrong type fails the request instead of being guessed."""
    model = DelegatingModel(EXCLUDED)
    _agent, session, adapter = await _build_adapter(tmp_path, monkeypatch, model)

    payloads = await _run_turn(
        adapter,
        session,
        {"mode": "agent", "agent_subagents_available": ALLOWED},
    )

    errors = [p for p in payloads if p.get("event_type") == "chat.error"]
    assert errors, payloads
    assert "list of strings" in str(errors[0].get("error") or ""), errors
    assert not model.messages, "the model must not run"


@pytest.mark.asyncio
async def test_empty_list_offers_no_delegation(tmp_path, monkeypatch):
    """An empty list is an explicit `no delegation` for the request."""
    seen: dict = {}
    model = DelegatingModel(EXCLUDED)
    agent, session, adapter = await _build_adapter(tmp_path, monkeypatch, model)
    model.probe = lambda: seen.update(
        roster=_roster_names(agent), description=_task_tool_description(agent)
    )

    await _run_turn(
        adapter, session, {"mode": "agent", "agent_subagents_available": []}
    )

    assert seen.get("roster") == [], seen
    assert ALLOWED not in seen.get("description", ""), seen
    assert EXCLUDED not in seen.get("description", ""), seen
    assert _refused(model), model.messages[-1]
