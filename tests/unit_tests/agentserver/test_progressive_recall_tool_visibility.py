# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""recall_compressed_context stays in the model tools schema once registered."""

from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.tool import ToolCard

from openjiuwen.harness.prompts.sections import SectionName

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)

_RECALL = "recall_compressed_context"
_MISSING = object()


@pytest.fixture(autouse=True)
def _navigation_section_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """This agent-core build has no ``TOOL_NAVIGATION`` constant.

    The rail still assigns that name when it builds the deferred-tool section.
    Set it only for these tests and let monkeypatch remove it afterwards, so
    other modules keep failing on the missing constant instead of inheriting it.
    """
    if not hasattr(SectionName, "TOOL_NAVIGATION"):
        monkeypatch.setattr(
            SectionName,
            "TOOL_NAVIGATION",
            "tool_navigation",
            raising=False,
        )


def _card(name: str, required_skill: object = _MISSING) -> ToolCard:
    properties = {}
    if required_skill is not _MISSING:
        properties["required_skill"] = required_skill
    return ToolCard(
        id=f"id-{name}",
        name=name,
        description=f"description-{name}",
        input_params={},
        properties=properties,
    )


class _Prompt:
    """Only ``add_section``. ``has_section`` would touch a section enum this tree lacks."""

    def __init__(self) -> None:
        self.sections: list[object] = []

    def add_section(self, section: object) -> None:
        self.sections.append(section)


def _agent(cards: list[ToolCard]) -> SimpleNamespace:
    return SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(cards)),
        system_prompt_builder=_Prompt(),
    )


def _rail(
    cards: list[ToolCard],
    *,
    eager_tools: list[str],
    disabled_tools: list[str] | None = None,
    active_skill: str | None = None,
    enabled: bool = True,
) -> tuple[ProgressiveToolRail, SimpleNamespace]:
    agent = _agent(cards)
    rail = ProgressiveToolRail(
        enabled=enabled,
        eager_tools=list(eager_tools),
        disabled_tools=disabled_tools,
        active_skill_provider=lambda: active_skill,
    )
    rail.init(agent)
    return rail, agent


def _names(tools: list[object]) -> list[str]:
    return [str(getattr(tool, "name", "") or "") for tool in tools]


def _nav_cn(agent: SimpleNamespace) -> str:
    sections = agent.system_prompt_builder.sections
    if not sections:
        return ""
    content = getattr(sections[0], "content", {})
    if isinstance(content, dict):
        return str(content.get("cn") or "")
    return str(content)


@pytest.mark.asyncio
async def test_registered_recall_stays_after_eager_and_active_gates() -> None:
    """Lazy load must keep an ungated recall tool, after eager and skill gates."""
    read_file = _card("read_file")
    gated = _card("gated_direct", "research-skill")
    deferred = _card("ordinary_deferred")
    recall = _card(_RECALL)
    cards = [deferred, recall, gated, read_file]
    rail, agent = _rail(
        cards,
        eager_tools=["read_file"],
        active_skill="research-skill",
    )
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(cards)))

    await rail.before_model_call(ctx)

    assert _names(ctx.inputs.tools) == ["read_file", "gated_direct", _RECALL]
    nav = _nav_cn(agent)
    assert "ordinary_deferred" in nav
    assert _RECALL not in nav


@pytest.mark.asyncio
async def test_recall_already_in_eager_list_keeps_that_position_once() -> None:
    read_file = _card("read_file")
    recall = _card(_RECALL)
    bash = _card("bash")
    deferred = _card("ordinary_deferred")
    cards = [bash, recall, deferred, read_file]
    rail, agent = _rail(
        cards,
        eager_tools=["read_file", _RECALL, "bash"],
    )
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(cards)))

    await rail.before_model_call(ctx)

    assert _names(ctx.inputs.tools) == ["read_file", _RECALL, "bash"]
    assert _names(ctx.inputs.tools).count(_RECALL) == 1


@pytest.mark.asyncio
async def test_disabled_recall_is_removed() -> None:
    read_file = _card("read_file")
    recall = _card(_RECALL)
    cards = [read_file, recall]
    rail, agent = _rail(
        cards,
        eager_tools=["read_file"],
        disabled_tools=[_RECALL],
    )
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(cards)))

    await rail.before_model_call(ctx)

    assert _names(ctx.inputs.tools) == ["read_file"]
    assert _RECALL not in _nav_cn(agent)


@pytest.mark.asyncio
async def test_skill_gated_recall_stays_hidden_until_its_skill_is_active() -> None:
    read_file = _card("read_file")
    other = _card("other_gated", "research-skill")
    recall = _card(_RECALL, "research-skill")
    cards = [recall, other, read_file]
    hidden_rail, hidden_agent = _rail(
        cards,
        eager_tools=["read_file"],
        active_skill=None,
    )
    hidden = SimpleNamespace(
        agent=hidden_agent,
        inputs=SimpleNamespace(tools=list(cards)),
    )

    await hidden_rail.before_model_call(hidden)

    assert _names(hidden.inputs.tools) == ["read_file"]

    shown_rail, shown_agent = _rail(
        cards,
        eager_tools=["read_file"],
        active_skill="research-skill",
    )
    shown = SimpleNamespace(
        agent=shown_agent,
        inputs=SimpleNamespace(tools=list(cards)),
    )

    await shown_rail.before_model_call(shown)

    assert _names(shown.inputs.tools) == ["read_file", _RECALL, "other_gated"]
    assert _names(shown.inputs.tools).count(_RECALL) == 1


@pytest.mark.asyncio
async def test_absent_recall_tool_is_not_invented() -> None:
    read_file = _card("read_file")
    deferred = _card("ordinary_deferred")
    cards = [deferred, read_file]
    rail, agent = _rail(cards, eager_tools=["read_file"])
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(cards)))

    await rail.before_model_call(ctx)

    assert _names(ctx.inputs.tools) == ["read_file"]
    assert _RECALL not in _nav_cn(agent)


@pytest.mark.asyncio
async def test_lazy_load_off_keeps_input_order_for_non_disabled_tools() -> None:
    read_file = _card("read_file")
    deferred = _card("ordinary_deferred")
    recall = _card(_RECALL)
    cards = [recall, deferred, read_file]
    rail, agent = _rail(
        cards,
        eager_tools=["read_file"],
        enabled=False,
    )
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(cards)))

    await rail.before_model_call(ctx)

    assert _names(ctx.inputs.tools) == [_RECALL, "ordinary_deferred", "read_file"]
