# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import InvokeInputs, ModelCallInputs

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import LocalSectionName
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import ResponsePromptRail
from jiuwenswarm.server.runtime.a2ui.config import A2UIConfig


class _FakePromptBuilder:
    def __init__(self) -> None:
        self.language = "en"
        self.sections = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


@pytest.mark.asyncio
async def test_response_prompt_rail_does_not_inject_a2ui_for_non_web_channel(monkeypatch):
    """Non-Web model calls should not receive A2UI prompt instructions."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()

    await rail.before_model_call(SimpleNamespace(inputs={"channel": "feishu"}))

    assert "response" in rail.system_prompt_builder.sections
    assert LocalSectionName.A2UI not in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_response_prompt_rail_keeps_a2ui_for_web_channel(monkeypatch):
    """Web model calls should keep existing A2UI prompt behavior."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()

    await rail.before_model_call(SimpleNamespace(inputs={"channel": "web"}))

    assert "response" in rail.system_prompt_builder.sections
    assert LocalSectionName.A2UI in rail.system_prompt_builder.sections
    content = rail.system_prompt_builder.sections[LocalSectionName.A2UI].content
    assert "browser_preflight_submit" not in content["en"]


@pytest.mark.asyncio
async def test_response_prompt_rail_scopes_browser_rules_to_browser_request(monkeypatch):
    """Browser A2UI flows remain available without polluting ordinary A2UI requests."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()
    extra = {}

    await rail.before_invoke(
        SimpleNamespace(
            inputs=InvokeInputs(
                query="帮我搜索 Gmail 并起草需要回复的邮件",
                conversation_id="sess_browser",
            ),
            extra=extra,
        )
    )
    await rail.before_model_call(SimpleNamespace(inputs=SimpleNamespace(), extra=extra))

    content = rail.system_prompt_builder.sections[LocalSectionName.A2UI].content
    assert "browser_preflight_submit" in content["en"]
    assert "gmail_email_select" in content["en"]
    assert "social_post_confirm" in content["en"]


@pytest.mark.asyncio
async def test_response_prompt_rail_keeps_museum_card_prompt_generic(monkeypatch):
    """The reported museum-card request must not receive browser workflow rules."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()
    extra = {}

    await rail.before_invoke(
        SimpleNamespace(
            inputs=InvokeInputs(
                query="生成一个带有故宫图片的介绍故宫的A2UI卡片",
                conversation_id="sess_museum",
            ),
            extra=extra,
        )
    )
    await rail.before_model_call(SimpleNamespace(inputs=SimpleNamespace(), extra=extra))

    content = rail.system_prompt_builder.sections[LocalSectionName.A2UI].content
    assert "browser_preflight_submit" not in content["cn"]
    assert "gmail_email_select" not in content["cn"]


@pytest.mark.asyncio
async def test_response_prompt_rail_logs_switch_and_injection_decision(
    monkeypatch,
):
    """Logs must expose the effective switch and injection state."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=False),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()
    log_calls = []
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.response_prompt_rail.logger.info",
        lambda message, *args: log_calls.append((message, args)),
    )

    await rail.before_model_call(
        SimpleNamespace(
            inputs={
                "channel": "web",
                "request_id": "req-a2ui-log",
                "conversation_id": "sess-a2ui-log",
            }
        )
    )

    assert len(log_calls) == 1
    _, args = log_calls[0]
    assert args[:6] == (
        "req-a2ui-log",
        "sess-a2ui-log",
        "web",
        False,
        False,
        False,
    )


@pytest.mark.asyncio
async def test_response_prompt_rail_removes_a2ui_when_request_skips_it(monkeypatch):
    """Repair fallback retries need a request-scoped way to remove A2UI instructions."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()

    await rail.before_model_call(SimpleNamespace(inputs={"channel": "web"}))
    assert LocalSectionName.A2UI in rail.system_prompt_builder.sections

    await rail.before_model_call(SimpleNamespace(inputs={"channel": "web", "skip_a2ui": True}))

    assert "response" in rail.system_prompt_builder.sections
    assert LocalSectionName.A2UI not in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_response_prompt_rail_keeps_web_channel_from_invoke_context(monkeypatch):
    """Model-call inputs drop channel, so Web channel must survive via invoke context."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()
    extra = {}

    await rail.before_invoke(
        SimpleNamespace(
            inputs=InvokeInputs(query="generate an A2UI form", conversation_id="web_session_1"),
            extra=extra,
        )
    )
    await rail.before_model_call(SimpleNamespace(inputs=SimpleNamespace(), extra=extra))

    assert LocalSectionName.A2UI in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_response_prompt_rail_maps_sess_prefix_to_web(monkeypatch):
    """Web sessions use sess_* ids and should still receive Web-only A2UI."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()

    await rail.before_model_call(
        SimpleNamespace(inputs=InvokeInputs(query="generate an A2UI form", conversation_id="sess_123"))
    )

    assert "response" in rail.system_prompt_builder.sections
    assert LocalSectionName.A2UI in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_response_prompt_rail_uses_runtime_channel_for_model_call_inputs(monkeypatch):
    """Real ReAct model-call inputs need the adapter-synced runtime channel."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()
    rail.set_channel("web")

    await rail.before_model_call(SimpleNamespace(inputs=ModelCallInputs()))

    assert "response" in rail.system_prompt_builder.sections
    assert LocalSectionName.A2UI in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_response_prompt_rail_keeps_tui_runtime_channel_disabled(monkeypatch):
    """Runtime channel sync must not make non-Web ReAct model calls A2UI-aware."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()
    rail.set_channel("tui")

    await rail.before_model_call(SimpleNamespace(inputs=ModelCallInputs()))

    assert "response" in rail.system_prompt_builder.sections
    assert LocalSectionName.A2UI not in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_response_prompt_rail_keeps_non_web_bypass_from_invoke_context(monkeypatch):
    """Non-Web channel inferred from conversation id should still bypass A2UI."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()
    extra = {}

    await rail.before_invoke(
        SimpleNamespace(
            inputs=InvokeInputs(query="generate an A2UI form", conversation_id="feishu_session_1"),
            extra=extra,
        )
    )
    await rail.before_model_call(SimpleNamespace(inputs=SimpleNamespace(), extra=extra))

    assert LocalSectionName.A2UI not in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_response_prompt_rail_does_not_default_missing_channel_to_web(monkeypatch):
    """Missing channel context should not silently enable Web-only A2UI."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()

    await rail.before_model_call(
        SimpleNamespace(inputs=InvokeInputs(query="generate a report", conversation_id="session1"))
    )

    assert "response" in rail.system_prompt_builder.sections
    assert LocalSectionName.A2UI not in rail.system_prompt_builder.sections


@pytest.mark.asyncio
async def test_response_prompt_rail_does_not_inject_a2ui_for_tui_session_prefix(monkeypatch):
    """TUI sessions inferred from conversation id should bypass A2UI."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()

    await rail.before_model_call(
        SimpleNamespace(inputs=InvokeInputs(query="generate a report", conversation_id="tui_session_1"))
    )

    assert "response" in rail.system_prompt_builder.sections
    assert LocalSectionName.A2UI not in rail.system_prompt_builder.sections
