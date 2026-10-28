# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import (
    DeepResearchExecutionRail,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.perf.context import clear_request_context, set_request_context
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


@pytest.mark.parametrize("action", ["upgrade_latest_brief", "refresh_latest_brief"])
def test_followup_metadata_preserves_action_and_source(action):
    request = AgentRequest(
        request_id="followup-request",
        channel_id="officeclaw",
        session_id="followup-session",
        metadata={
            "deepresearch_followup": {
                "v": 1,
                "action": action,
                "source_report_id": " brief-1 ",
            }
        },
    )

    assert interface_deep._extract_deepresearch_followup(request) == {
        "v": 1,
        "action": action,
        "source_report_id": "brief-1",
    }


@pytest.mark.parametrize("action,source", [
    ("unknown", "brief-1"),
    (["refresh_latest_brief"], "brief-1"),
    ("refresh_latest_brief", " "),
])
def test_invalid_followup_metadata_cannot_route_refresh(action, source):
    request = AgentRequest(
        request_id="invalid-followup-request",
        metadata={"deepresearch_followup": {
            "v": 1, "action": action, "source_report_id": source,
        }},
    )

    assert interface_deep._extract_deepresearch_followup(request) is None


def _builder():
    sections = {}
    builder = Mock(language="cn")
    builder.add_section.side_effect = lambda section: sections.update({section.name: section})
    builder.remove_section.side_effect = lambda name: sections.pop(name, None)
    return builder, sections


def _bind_request(action=None):
    set_request_context(
        session_id="refresh-rail-session",
        request_id="refresh-rail-request",
        channel_id="officeclaw",
        mode="agent",
        deepresearch_followup=(
            {"v": 1, "action": action, "source_report_id": "brief-1"}
            if action else None
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("next_action", [None, "upgrade_latest_brief"])
async def test_refresh_instruction_delivers_brief_and_does_not_leak_to_next_request(next_action):
    builder, sections = _builder()
    agent = SimpleNamespace(system_prompt_builder=builder)
    ctx = SimpleNamespace(agent=agent, session=None)
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    rail.init(agent)
    try:
        _bind_request("refresh_latest_brief")
        await rail.before_model_call(ctx)
        assert len(sections) == 1
        section = next(iter(sections.values()))
        instruction = section.content["cn"]
        assert "deepresearch_execute" in instruction
        assert "report_type='brief'" in instruction
        assert "upgrade_from_brief=False" in instruction
        assert "新版精简报告文件" in instruction
        assert "不要只在聊天里回答" in instruction

        _bind_request(next_action)
        await rail.before_model_call(ctx)
        assert sections == {}
    finally:
        clear_request_context(session_id="refresh-rail-session", request_id="refresh-rail-request")


@pytest.mark.asyncio
async def test_refresh_instruction_uses_reloaded_builder_and_uninit_cleans_it():
    old_builder, old_sections = _builder()
    agent = SimpleNamespace(system_prompt_builder=old_builder)
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    rail.init(agent)
    current_builder, current_sections = _builder()
    agent.system_prompt_builder = current_builder
    try:
        _bind_request("refresh_latest_brief")
        await rail.before_model_call(SimpleNamespace(agent=agent, session=None))
        assert old_sections == {}
        assert len(current_sections) == 1
        rail.uninit(agent)
        assert current_sections == {}
    finally:
        clear_request_context(session_id="refresh-rail-session", request_id="refresh-rail-request")


@pytest.mark.asyncio
async def test_missing_prompt_builder_keeps_other_agent_modes_usable():
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    await rail.before_model_call(SimpleNamespace(agent=None, session=None))
