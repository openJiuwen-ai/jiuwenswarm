# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The cua approval and the permission rail share the root ``task_tool`` call (#5487).

Both rails gate the same call. Before agent-core routed each answer to the
rail that raised the interrupt, the permission rail also read the answer to
the cua card; any input it could not parse as a confirmation (for example a
new query landing on the paused call) was re-asked as a generic
"操作需要授权 / task_tool" card, and the cua card itself was never shown.
These tests drive a real DeepAgent with both rails and a scripted model.
"""

from __future__ import annotations

import json
import uuid

import pytest
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.cua_delegation_approval_rail import (
    CuaDelegationApprovalRail,
    is_cua_delegation_approval_message,
)

# Default tier: every tool not listed is allowed, task_tool included.
_DEFAULT_TIER_PERMISSIONS = {"enabled": True, "defaults": {"*": "allow"}, "tools": {}}

# What interface.py maps the "本次允许" / "会话内记住" options to (web and voice alike).
_ALLOW_ONCE = {"approved": True, "auto_confirm": False, "feedback": ""}
_SESSION_ALLOW = {"approved": True, "auto_confirm": True, "persist_allow": False, "feedback": ""}


class _ScriptedModel:
    """Asks for one cua delegation per user turn, then answers."""

    def __init__(self):
        self.calls = 0
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="local-test", api_base="http://127.0.0.1:1",
        )
        self.model_config = ModelRequestConfig(model_name="cua-interplay-test")

    async def invoke(self, messages, **kwargs):
        self.calls += 1
        last_role = getattr(messages[-1], "role", None) if messages else None
        tool_calls = []
        if last_role == "user":
            tool_calls = [
                ToolCall(
                    id=f"cua-call-{self.calls}",
                    type="function",
                    name="task_tool",
                    arguments=json.dumps(
                        {"subagent_type": "cua_agent", "task_description": "打开记事本，追加一行'一二三'"},
                        ensure_ascii=False,
                    ),
                )
            ]
        return AssistantMessage(
            content="" if tool_calls else "done",
            tool_calls=tool_calls,
            usage_metadata=UsageMetadata(model_name="cua-interplay-test", finish_reason="stop"),
        )

    async def stream(self, messages, **kwargs):
        response = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(
            content=response.content, tool_calls=response.tool_calls, usage_metadata=response.usage_metadata,
        )


class _TaskTool(Tool):
    """Stands in for the delegation; records how often it ran."""

    def __init__(self):
        super().__init__(ToolCard(name="task_tool", description="Delegate a task to a subagent"))
        self.calls = 0

    async def invoke(self, inputs, **kwargs):
        self.calls += 1
        return "delegation finished"

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


def _pending(result):
    if result.get("result_type") != "interrupt":
        return []
    return [(item.payload.id, item.payload.value) for item in result.get("state", [])]


def _is_cua_card(request) -> bool:
    return is_cua_delegation_approval_message(request.message)




@pytest.fixture
async def harness(tmp_path, request):
    # Production registers the cua rail before the permission rail; both have
    # priority 90, so test the other order too.
    cua_first = request.param
    await Runner.start()
    model, tool = _ScriptedModel(), _TaskTool()
    cua_rail = CuaDelegationApprovalRail(language="cn")
    permission_rail = PermissionInterruptRail(config=_DEFAULT_TIER_PERMISSIONS)
    agent = create_deep_agent(
        model=model,
        tools=[tool],
        workspace=str(tmp_path),
        rails=[cua_rail, permission_rail] if cua_first else [permission_rail, cua_rail],
        enable_task_loop=False,
        max_iterations=5,
        enable_model_anomaly_detection_rail=False,
        enable_read_image_multimodal=False,
    )
    session = create_agent_session(session_id=f"cua-interplay-{uuid.uuid4().hex}", card=agent.card)
    await session.pre_run(inputs={})
    try:
        yield agent, session, tool
    finally:
        await agent.stop()
        await session.post_run()
        await Runner.stop()


async def _ask_cua(agent, session):
    result = await agent.invoke({"query": "打开记事本，追加一行'一二三'"}, session=session)
    pending = _pending(result)
    assert len(pending) == 1, result
    call_id, request = pending[0]
    # The user must see the cua card, never a generic permission card.
    assert _is_cua_card(request), request.message
    return call_id


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", [True, False], indirect=True, ids=["cua_first", "permission_first"])
async def test_answering_the_cua_card_runs_the_delegation_without_a_permission_reask(harness):
    agent, session, tool = harness
    call_id = await _ask_cua(agent, session)

    answer = InteractiveInput()
    answer.update(call_id, _ALLOW_ONCE)
    result = await agent.invoke({"query": answer}, session=session)

    assert result["result_type"] == "answer", _pending(result)
    assert tool.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", [True, False], indirect=True, ids=["cua_first", "permission_first"])
async def test_input_landing_on_a_paused_cua_card_never_becomes_a_permission_reask(harness):
    # A paused call is resumed by whatever input arrives next in the session,
    # e.g. a new (voice) query. The permission rail did not ask on this call,
    # so it must not treat that input as an unparsable answer of its own.
    agent, session, tool = harness
    await _ask_cua(agent, session)

    result = await agent.invoke({"query": "打开记事本，追加一行'一二三'"}, session=session)

    # Only the cua rail may ask again; a permission-rail interrupt here is the
    # generic "操作需要授权 / task_tool" card of the report.
    cua_owner = CuaDelegationApprovalRail().interrupt_owner
    assert [request.interrupt_owner for _, request in _pending(result)] == [cua_owner], _pending(result)
    assert tool.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", [True], indirect=True, ids=["cua_first"])
async def test_session_allow_skips_the_cua_card_for_later_delegations(harness):
    # "会话内记住" covers later cua delegations in the same session: no card at
    # all, and the permission rail (task_tool allowed by default) adds none.
    agent, session, tool = harness
    call_id = await _ask_cua(agent, session)
    answer = InteractiveInput()
    answer.update(call_id, _SESSION_ALLOW)
    assert (await agent.invoke({"query": answer}, session=session))["result_type"] == "answer"

    result = await agent.invoke({"query": "再打开一次记事本"}, session=session)

    assert result["result_type"] == "answer", _pending(result)
    assert tool.calls == 2
