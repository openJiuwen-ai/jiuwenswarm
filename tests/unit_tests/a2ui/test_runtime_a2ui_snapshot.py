# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""请求级 agent-config 快照优先于进程 get_config() 的 A2UI 门控。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import LocalSectionName
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import ResponsePromptRail
from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
from jiuwenswarm.server.runtime.a2ui import integration
from jiuwenswarm.server.runtime.a2ui.config import (
    get_current_a2ui_config,
    is_a2ui_enabled,
    a2ui_config_source,
)


class _FakePromptBuilder:
    def __init__(self) -> None:
        self.language = "en"
        self.sections = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


def test_get_current_a2ui_config_prefers_request_snapshot(monkeypatch) -> None:
    """企业快照 enabled=true 时，即使进程 yaml 关着也要开。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"a2ui": {"enabled": False}},
    )
    assert get_current_a2ui_config().enabled is False
    assert is_a2ui_enabled() is False

    with a2ui_config_source({"a2ui": {"enabled": True, "protocol_version": "0.8"}}):
        assert get_current_a2ui_config().enabled is True
        assert is_a2ui_enabled() is True

    assert get_current_a2ui_config().enabled is False


def test_get_current_a2ui_config_falls_back_to_process_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"a2ui": {"enabled": True, "protocol_version": "0.8"}},
    )
    assert get_current_a2ui_config().enabled is True


@pytest.mark.asyncio
async def test_response_prompt_rail_opens_when_snapshot_enabled(monkeypatch) -> None:
    """企业快照开时，Web rail 注入 A2UI 段（不读进程 yaml 残留 false）。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"a2ui": {"enabled": False}},
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()

    with a2ui_config_source({"a2ui": {"enabled": True, "protocol_version": "0.8"}}):
        await rail.before_model_call(SimpleNamespace(inputs={"channel": "web"}))

    assert LocalSectionName.A2UI in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_response_prompt_rail_stays_closed_when_snapshot_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"a2ui": {"enabled": True, "protocol_version": "0.8"}},
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()

    with a2ui_config_source({"a2ui": {"enabled": False, "protocol_version": "0.8"}}):
        await rail.before_model_call(
            SimpleNamespace(inputs={"channel": "web"})
        )

    assert LocalSectionName.A2UI not in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_finalizer_gate_follows_snapshot(monkeypatch) -> None:
    """finalizer 门控跟快照走，不跟进程 yaml。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"a2ui": {"enabled": False}},
    )
    finalize = AsyncMock(side_effect=lambda content, **kwargs: content)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.runtime.response_finalization."
        "finalize_a2ui_assistant_content",
        finalize,
    )

    with a2ui_config_source({"a2ui": {"enabled": True, "protocol_version": "0.8"}}):
        result = await integration.finalize_assistant_response_if_a2ui(
            "hello",
            channel="web",
            user_query="q",
            request_id="r1",
            repair_call=None,
        )

    assert result == "hello"
    assert finalize.await_count == 1
    assert finalize.await_args.kwargs["a2ui_enabled"] is True


@pytest.mark.asyncio
async def test_client_event_gate_follows_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"a2ui": {"enabled": False}},
    )
    monkeypatch.setattr(
        integration,
        "_build_a2ui_client_event_prompt",
        lambda content, channel, language: "a2ui-prompt",
    )
    event = {"type": "a2ui.client_event", "userAction": {"context": {"v": 1}}}

    assert (
        integration.build_user_prompt_if_a2ui_event(
            event, channel="web", language="zh"
        )
        is None
    )

    with a2ui_config_source({"a2ui": {"enabled": True, "protocol_version": "0.8"}}):
        prompt = integration.build_user_prompt_if_a2ui_event(
            event, channel="web", language="zh"
        )
    assert prompt == "a2ui-prompt"


@pytest.mark.parametrize(
    ("mode", "role"),
    [
        ("team", "leader"),
        ("team", "teammate"),
        ("team.plan", "leader"),
        ("team.plan", "teammate"),
    ],
)
def test_team_members_share_a2ui_via_runtime_snapshot(mode: str, role: str) -> None:
    """Team 成员共用同一 resolve 快照；不靠 DeepAgentSpec 字段拷贝 a2ui。"""
    shared = {
        "a2ui": {
            "enabled": True,
            "protocol_version": "0.8",
            "stream_validation_enabled": True,
            "dev_smoke_tools_enabled": False,
        },
        "kv_cache_affinity_config": {"enable_kv_cache_affinity": False},
    }
    # 构建成员 spec 不应依赖 / 要求 DeepAgentSpec.a2ui 字段；
    # 继承靠共享 config + 请求级快照。
    build_member_deep_agent_spec(shared, mode, role, DeepAgentSpec())

    with a2ui_config_source(shared):
        assert get_current_a2ui_config().enabled is True
        assert is_a2ui_enabled() is True
