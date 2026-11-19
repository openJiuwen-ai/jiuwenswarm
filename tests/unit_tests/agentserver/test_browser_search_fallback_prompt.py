# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Search availability must only affect browser prompt guidance."""

import runpy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openjiuwen.harness import tools as sdk_tools
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.schema.config import SubAgentConfig

from jiuwenswarm.agents.harness.common.prompt.browser_task_prompt import build_browser_task_prompt
from jiuwenswarm.agents.harness.common.rails import browser_task_prompt_rail
from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import BrowserTaskPromptRail


_PAID_KEYS = ("BOCHA_API_KEY", "SERPER_API_KEY", "JINA_API_KEY", "PERPLEXITY_API_KEY")
_FREE_FLAGS = ("FREE_SEARCH_DDG_ENABLED", "FREE_SEARCH_BING_ENABLED")


@pytest.fixture(autouse=True)
def clear_search_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PAID_KEYS + _FREE_FLAGS:
        monkeypatch.delenv(name, raising=False)


def _make_rail(
    language: str = "en",
    *,
    runtime: bool = False,
    browser: bool = True,
    rail_type: type[BrowserTaskPromptRail] = BrowserTaskPromptRail,
) -> tuple[BrowserTaskPromptRail, AgentCallbackContext]:
    names = ["research_agent"]
    if browser:
        names.append("browser_agent")
    agent = SimpleNamespace(
        card=AgentCard(name="main", description="main"),
        deep_config=SimpleNamespace(
            subagents=[
                SubAgentConfig(agent_card=AgentCard(name=name, description=name), system_prompt=name)
                for name in names
            ],
        ),
        system_prompt_builder=SystemPromptBuilder(language=language),
        ability_manager=Mock(),
    )
    rail = rail_type(enable_subagent_runtime=runtime)
    rail.init(agent)
    ctx = AgentCallbackContext(agent=agent, inputs=None, session=Mock(), extra={})
    return rail, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["cn", "en"])
@pytest.mark.parametrize("runtime", [False, True])
async def test_no_search_configuration_adds_fallback_to_sync_task_prompt(
    language: str,
    runtime: bool,
) -> None:
    rail, ctx = _make_rail(language, runtime=runtime)
    tools_before = tuple(rail.tools)

    await rail.before_model_call(ctx)

    section = rail.system_prompt_builder.get_section("task_tool")
    assert section is not None
    base = build_browser_task_prompt(language)
    extended = build_browser_task_prompt(language, search_fallback_enabled=True)
    assert extended.startswith(base + "\n\n")
    assert extended in section.content[language]
    assert tuple(rail.tools) == tools_before
    if runtime:
        tools = {tool.card.name: tool for tool in rail.tools}
        assert tools["task_tool"]._allowed_subagent_types == frozenset({"browser_agent"})
        assert tools["subagent_spawn"]._allowed_subagent_types == frozenset({"research_agent"})
        runtime_section = rail.system_prompt_builder.get_section("subagent_tools")
        assert runtime_section is not None
        assert extended not in runtime_section.content[language]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _PAID_KEYS + _FREE_FLAGS)
async def test_configured_search_preserves_original_browser_prompt(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setenv(name, "enabled" if name in _FREE_FLAGS else "test-key")
    rail, ctx = _make_rail()

    await rail.before_model_call(ctx)

    assert rail._task_prompt_extension(ctx, "en") == build_browser_task_prompt("en")
    section = rail.system_prompt_builder.get_section("task_tool")
    assert "Browser Search When No Search Engine Is Configured" not in section.content["en"]
    assert "test-key" not in section.content["en"]


@pytest.mark.asyncio
async def test_empty_keys_and_disabled_flags_still_add_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PAID_KEYS:
        monkeypatch.setenv(name, "  ")
    monkeypatch.setenv("FREE_SEARCH_DDG_ENABLED", "false")
    monkeypatch.setenv("FREE_SEARCH_BING_ENABLED", "0")
    rail, ctx = _make_rail()

    await rail.before_model_call(ctx)

    section = rail.system_prompt_builder.get_section("task_tool")
    assert "Browser Search When No Search Engine Is Configured" in section.content["en"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _PAID_KEYS + _FREE_FLAGS)
@pytest.mark.parametrize("runtime", [False, True])
async def test_prompt_refreshes_after_search_configuration_changes(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    runtime: bool,
) -> None:
    rail, ctx = _make_rail(runtime=runtime)
    heading = "Browser Search When No Search Engine Is Configured"

    await rail.before_model_call(ctx)
    assert heading in rail.system_prompt_builder.get_section("task_tool").content["en"]

    monkeypatch.setenv(name, "true" if name in _FREE_FLAGS else "test-key")
    await rail.before_model_call(ctx)
    assert heading not in rail.system_prompt_builder.get_section("task_tool").content["en"]

    monkeypatch.delenv(name)
    await rail.before_model_call(ctx)
    assert rail.system_prompt_builder.get_section("task_tool").content["en"].count(heading) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [False, True])
async def test_no_browser_does_not_advertise_fallback(runtime: bool) -> None:
    rail, ctx = _make_rail(runtime=runtime, browser=False)

    await rail.before_model_call(ctx)

    assert rail._task_prompt_extension(ctx, "en") is None
    for name in ("task_tool", "subagent_tools"):
        section = rail.system_prompt_builder.get_section(name)
        if section is not None:
            assert "Browser Search When No Search Engine Is Configured" not in section.content["en"]


@pytest.mark.asyncio
async def test_unmounted_browser_removes_fallback_from_existing_prompt() -> None:
    rail, ctx = _make_rail()
    await rail.before_model_call(ctx)
    ctx.agent.deep_config.subagents = ctx.agent.deep_config.subagents[:1]

    await rail.before_model_call(ctx)

    assert "Browser Search When No Search Engine Is Configured" not in (
        rail.system_prompt_builder.get_section("task_tool").content["en"]
    )


def test_search_fallback_preserves_user_constraints_and_does_not_bypass_failures() -> None:
    prompt = build_browser_task_prompt("en", search_fallback_enabled=True)
    assert '`subagent_type="browser_agent"`' in prompt
    assert "no-browser/no-network use take precedence" in prompt
    assert "`fetch_webpage` remains available" in prompt
    assert "not timeouts, CAPTCHAs, access restrictions, low relevance, or exhausted budgets" in prompt
    assert "Do not use it to bypass retrieval budgets, access restrictions, or permission checks" in prompt
    assert "do not fall back to launching a browser from the command line" in prompt


def test_unknown_prompt_language_preserves_chinese_default() -> None:
    assert build_browser_task_prompt("unknown", search_fallback_enabled=True) == (
        build_browser_task_prompt("cn", search_fallback_enabled=True)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_helpers",
    [
        ("is_free_search_enabled",),
        ("is_paid_search_enabled",),
        ("is_free_search_enabled", "is_paid_search_enabled"),
    ],
)
@pytest.mark.parametrize("name", _PAID_KEYS + _FREE_FLAGS)
async def test_missing_sdk_search_helpers_preserve_prompt_refresh(monkeypatch, missing_helpers, name) -> None:
    for helper in missing_helpers:
        monkeypatch.delattr(sdk_tools, helper)
    namespace = runpy.run_path(browser_task_prompt_rail.__file__)
    rail, ctx = _make_rail(rail_type=namespace["BrowserTaskPromptRail"])
    heading = "Browser Search When No Search Engine Is Configured"

    monkeypatch.setenv("FREE_SEARCH_360_ENABLED", "true")
    for key in _PAID_KEYS:
        monkeypatch.setenv(key, "  ")
    for flag in _FREE_FLAGS:
        monkeypatch.setenv(flag, "false")
    await rail.before_model_call(ctx)
    assert heading in rail.system_prompt_builder.get_section("task_tool").content["en"]

    monkeypatch.setenv(name, " Enabled " if name in _FREE_FLAGS else " test-key ")
    await rail.before_model_call(ctx)
    prompt = rail.system_prompt_builder.get_section("task_tool").content["en"]
    assert heading not in prompt
    assert "test-key" not in prompt

    monkeypatch.setenv(name, "  ")
    await rail.before_model_call(ctx)
    assert heading in rail.system_prompt_builder.get_section("task_tool").content["en"]


def test_available_sdk_search_helpers_remain_authoritative(monkeypatch) -> None:
    free = Mock(return_value=True)
    paid = Mock(return_value=True)
    monkeypatch.setattr(sdk_tools, "is_free_search_enabled", free)
    monkeypatch.setattr(sdk_tools, "is_paid_search_enabled", paid)
    namespace = runpy.run_path(browser_task_prompt_rail.__file__)

    assert namespace["is_free_search_enabled"]() is True
    assert namespace["is_paid_search_enabled"]() is True
    free.assert_called_once_with()
    paid.assert_called_once_with()
