# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for Web A2UI client-event request shaping."""

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


def test_agent_input_builder_reads_a2ui_client_event_from_content(monkeypatch):
    """Web A2UI client events should not need a dict-valued query param."""
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    content = {
        "type": "a2ui.client_event",
        "protocolVersion": "0.8",
        "event": {
            "userAction": {
                "name": "submit_form",
                "surfaceId": "surface-1",
                "sourceComponentId": "submit",
                "context": {"name": "张三"},
            },
        },
    }
    request = AgentRequest(
        request_id="req-a2ui-content",
        channel_id="web",
        session_id="web_session",
        params={"content": content, "mode": "agent"},
    )

    inputs, _, raw_query = interface_module.JiuWenSwarm().build_inputs(request)

    assert raw_query == content
    assert "submit_form" in inputs["query"]
    assert "张三" in inputs["query"]


def test_agent_input_builder_uses_request_channel_id_for_a2ui(monkeypatch):
    """Web A2UI should not depend on session ids being prefixed with web_."""
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    request = AgentRequest(
        request_id="req-web-uuid-session",
        channel_id="web",
        session_id="session-without-channel-prefix",
        params={
            "content": {
                "type": "a2ui.client_event",
                "event": {
                    "userAction": {
                        "name": "submit_form",
                        "surfaceId": "surface-1",
                        "sourceComponentId": "submit",
                        "context": {"name": "zhangsan"},
                    },
                },
            },
            "mode": "agent",
        },
    )

    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)

    assert inputs["channel"] == "web"
    assert "submit_form" in inputs["query"]


async def test_deep_adapter_slash_command_ignores_structured_query():
    """A stale client sending dict-valued query should not crash slash routing."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    class SlashCommandProbe(JiuWenSwarmDeepAdapter):
        async def handle_query(self, query):
            return await self._handle_slash_command(query)

    result = await SlashCommandProbe().handle_query({"type": "a2ui.client_event"})

    assert result is None
