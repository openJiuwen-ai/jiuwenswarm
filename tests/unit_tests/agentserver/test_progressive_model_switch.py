# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ProgressiveToolRail keeps tools and TOOL_NAVIGATION consistent across model switches."""

# pylint: disable=protected-access

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from openjiuwen.harness.prompts.builder import SystemPromptBuilder
from openjiuwen.harness.prompts.sections import SectionName

from jiuwenswarm.agents.harness.common.rails import progressive_tool_rail
from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import ProgressiveToolRail


def _card(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, id=f"id-{name}", description=f"desc-{name}", input_params={})


class _AbilityManager:
    def __init__(self, cards: list[SimpleNamespace]) -> None:
        self._cards = {card.name: card for card in cards}

    def list(self) -> list[SimpleNamespace]:
        return list(self._cards.values())

    def get(self, name: str) -> SimpleNamespace | None:
        return self._cards.get(name)

    def add(self, card: SimpleNamespace) -> None:
        self._cards[card.name] = card

    def remove(self, name: str) -> None:
        self._cards.pop(name, None)


def _build_rail() -> tuple[ProgressiveToolRail, SimpleNamespace]:
    agent = SimpleNamespace(
        ability_manager=_AbilityManager([_card("read_file"), _card("mcp_x")]),
        system_prompt_builder=SystemPromptBuilder(language="cn"),
        model_name="",
    )
    rail = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        enable_for_models=["glm", "deepseek"],
        agent_card_id=f"s2_{uuid4().hex}",
    )
    rail.init(agent)
    return rail, agent


async def _run_turn(rail: ProgressiveToolRail, agent: SimpleNamespace, model: str) -> dict[str, object]:
    agent.model_name = model
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=[]), extra={})
    await rail.before_invoke(ctx)
    ctx.inputs.tools = list(agent.ability_manager.list())
    await rail.before_model_call(ctx)
    return {
        "tools": {tool.name for tool in ctx.inputs.tools},
        "navigation": agent.system_prompt_builder.has_section(SectionName.TOOL_NAVIGATION),
        "prompt": agent.system_prompt_builder.build(),
    }


@pytest.mark.asyncio
async def test_switching_out_of_whitelist_drops_navigation_and_back_restores_it():
    rail, agent = _build_rail()

    lazy = await _run_turn(rail, agent, "glm-5.3")
    assert lazy["navigation"] is True
    assert "mcp_x" in lazy["prompt"]
    assert "mcp_x" not in lazy["tools"]

    bypass = await _run_turn(rail, agent, "kimi-k3")
    assert bypass["navigation"] is False
    assert "tools_search" not in bypass["prompt"]
    assert {"mcp_x", "read_file"} <= bypass["tools"]
    assert "tools_search" not in bypass["tools"]

    restored = await _run_turn(rail, agent, "glm-5.3")
    assert restored["navigation"] is True
    assert "mcp_x" in restored["prompt"]
    assert {"tools_search", "invoke_tool"} <= restored["tools"]
    assert "mcp_x" not in restored["tools"]


@pytest.mark.asyncio
async def test_switching_inside_whitelist_keeps_navigation_text_for_prefix_cache():
    rail, agent = _build_rail()

    first = await _run_turn(rail, agent, "glm-5.3")
    second = await _run_turn(rail, agent, "deepseek-v4.1-flash")

    assert first["prompt"] == second["prompt"]
    assert first["tools"] == second["tools"]


@pytest.mark.asyncio
async def test_model_outside_whitelist_from_start_never_gets_navigation():
    rail, agent = _build_rail()

    result = await _run_turn(rail, agent, "kimi-k3")

    assert result["navigation"] is False
    assert {"mcp_x", "read_file"} <= result["tools"]


@pytest.mark.parametrize("inputs", [None, SimpleNamespace(tools=None)])
@pytest.mark.asyncio
async def test_bypass_drops_navigation_without_filterable_tools(inputs):
    rail, agent = _build_rail()
    await _run_turn(rail, agent, "glm-5.3")

    agent.model_name = "kimi-k3"
    await rail.before_model_call(SimpleNamespace(agent=agent, inputs=inputs, extra={}))

    assert agent.system_prompt_builder.has_section(SectionName.TOOL_NAVIGATION) is False


@pytest.mark.asyncio
async def test_bypass_drops_navigation_when_ctx_has_no_agent():
    rail, agent = _build_rail()
    await _run_turn(rail, agent, "glm-5.3")

    rail._cached_model_name = "kimi-k3"
    ctx = SimpleNamespace(agent=None, inputs=SimpleNamespace(tools=[]), extra={})
    await rail.before_model_call(ctx)

    assert agent.system_prompt_builder.has_section(SectionName.TOOL_NAVIGATION) is False


@pytest.mark.asyncio
async def test_uninit_drops_navigation_when_rail_is_removed():
    rail, agent = _build_rail()
    await _run_turn(rail, agent, "glm-5.3")

    rail.uninit(agent)

    assert agent.system_prompt_builder.has_section(SectionName.TOOL_NAVIGATION) is False


@pytest.mark.asyncio
async def test_bypass_log_only_fires_when_model_changes(monkeypatch):
    rail, agent = _build_rail()
    logger = MagicMock()
    monkeypatch.setattr(progressive_tool_rail, "logger", logger)

    for model in ("kimi-k3", "kimi-k3", "qwen3-32b", "glm-5.3", "kimi-k3"):
        await _run_turn(rail, agent, model)

    bypassed = [call.args[2] for call in logger.info.call_args_list if "bypassed" in call.args[0]]
    assert bypassed == ["kimi-k3", "qwen3-32b", "kimi-k3"]
