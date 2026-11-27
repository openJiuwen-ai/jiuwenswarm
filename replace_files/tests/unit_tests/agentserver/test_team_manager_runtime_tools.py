# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team member runtime tool registration."""

from types import SimpleNamespace

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.single_agent.ability_manager import AbilityManager

from jiuwenclaw.agentserver.team.team_manager import TeamManager


class _FakeResourceManager:
    def __init__(self) -> None:
        self.tools = {}

    def get_tool(self, tool_id):
        return self.tools.get(tool_id)

    def add_tool(self, tools):
        items = tools if isinstance(tools, list) else [tools]
        for tool in items:
            self.tools[tool.card.id] = tool


class _FakeCronRuntimeBridge:
    @staticmethod
    def build_tools(*, context, agent_id):
        _ = (context, agent_id)
        return [
            LocalFunction(
                card=ToolCard(
                    id="cron_list_jobs_member-agent",
                    name="cron_list_jobs",
                    description="cron list",
                    input_params={"type": "object"},
                ),
                func=lambda **_: None,
            )
        ]


class _FakeSendFileToolkit:
    def __init__(self, request_id, session_id, channel_id, *, metadata=None):
        self.request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self.metadata = metadata

    @staticmethod
    def get_tools():
        return [
            LocalFunction(
                card=ToolCard(
                    id="send_file_to_user_tool",
                    name="send_file_to_user",
                    description="send file",
                    input_params={"type": "object"},
                ),
                func=lambda **_: None,
            )
        ]


def test_register_member_runtime_tools_adds_cron_and_send_file(monkeypatch):
    resource_mgr = _FakeResourceManager()
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.deep_agent.cron_runtime.CronRuntimeBridge",
        _FakeCronRuntimeBridge,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.tools.send_file_to_user.SendFileToolkit",
        _FakeSendFileToolkit,
    )
    monkeypatch.setattr(
        "jiuwenclaw.config.get_config",
        lambda: {"channels": {"web": {"send_file_allowed": True}}},
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.resource_mgr",
        resource_mgr,
        raising=False,
    )

    agent = SimpleNamespace(
        card=SimpleNamespace(id="member-agent", name="member-agent"),
        ability_manager=AbilityManager(),
    )

    TeamManager.register_member_runtime_tools(
        agent,
        session_id="sess-1",
        request_id="req-1",
        channel_id="web",
        request_metadata={"request_id": "req-1"},
    )

    assert agent.ability_manager.get("cron_list_jobs") is not None
    assert agent.ability_manager.get("send_file_to_user") is not None
