# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""注册即可见 floor（BUG20261117410859）：protected_eager_tools 语义测试。

- 白名单模型下，已注册的保护名单工具必须留在 schema（懒加载裁剪不得隐藏）
- declared（required_skill）工具不走 floor，保持技能门控："可见 ⟺ 可直接调用"
- floor 优先于 eager 交集；DisabledToolsRail 黑名单仍最高优先
- 显式置空 [] 关闭保护；缺省键时代码内置默认名单生效（主/subagent/team 共用）
- deferred 池排除保护名单工具；tools_search / invoke_tool 对其给出"已直接可见"指引
- 排序稳定性与 prompt allowlist 同步（eager → protected）
"""

# pylint: disable=protected-access

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from openjiuwen.harness.prompts.builder import SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    DEFAULT_PROGRESSIVE_PROTECTED_EAGER_TOOLS,
    ProgressiveToolRail,
    resolve_protected_eager_tools,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.progressive_tools import (
    build_progressive_tool_rail_from_config,
)

_WHITELIST_MODEL = "glm-5.3"


def _card(name: str, properties: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        id=f"id-{name}",
        description=f"desc-{name}",
        input_params={},
        **({"properties": properties} if properties is not None else {}),
    )


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


def _build_rail(
    eager_tools: list[str],
    protected_eager_tools: list[str] | None,
    *,
    cards: list[SimpleNamespace] | None = None,
    active_skill_provider=None,
    disabled_tools: list[str] | None = None,
) -> tuple[ProgressiveToolRail, SimpleNamespace]:
    agent = SimpleNamespace(
        ability_manager=_AbilityManager(
            cards
            if cards is not None
            else [_card("read_file"), _card("mcp_x"), _card("open_pptx")]
        ),
        system_prompt_builder=SystemPromptBuilder(language="cn"),
        model_name="",
    )
    kwargs: dict = {
        "eager_tools": list(eager_tools),
        "protected_eager_tools": (
            list(protected_eager_tools) if protected_eager_tools is not None else None
        ),
        "agent_card_id": f"s2_{uuid4().hex}",
    }
    if active_skill_provider is not None:
        kwargs["active_skill_provider"] = active_skill_provider
    if disabled_tools is not None:
        kwargs["disabled_tools"] = disabled_tools
    rail = ProgressiveToolRail(**kwargs)
    rail.init(agent)
    return rail, agent


async def _run_turn(
    rail: ProgressiveToolRail, agent: SimpleNamespace, model: str
) -> list[str]:
    agent.model_name = model
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=[]), extra={})
    await rail.before_invoke(ctx)
    ctx.inputs.tools = list(agent.ability_manager.list())
    await rail.before_model_call(ctx)
    return [tool.name for tool in ctx.inputs.tools]


def _tool_ctx(agent: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=[]), extra={})


@pytest.mark.asyncio
async def test_protected_tool_stays_visible_for_whitelisted_model():
    """白名单模型 + 窄版 eager 列表：已注册的保护名单工具不被懒加载裁剪。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
    )

    names = await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert "open_pptx" in names
    assert "read_file" in names
    assert "mcp_x" not in names


@pytest.mark.asyncio
async def test_floor_skips_declared_tool_when_skill_inactive():
    """declared（required_skill）保护工具不走 floor：技能未激活时不可见也不可调。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx", "gated_extra"],
        cards=[
            _card("read_file"),
            _card("open_pptx", properties={"required_skill": "pptx-craft"}),
            _card("gated_extra", properties={"required_skill": "pptx-craft"}),
        ],
        active_skill_provider=lambda: None,
    )

    names = await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert "open_pptx" not in names
    assert "gated_extra" not in names
    # 执行层一致：技能未激活时直接调用同样被拦（可见 ⟺ 可调用的失效半边）。
    assert await rail._skill_gate_blocks_direct_call(_tool_ctx(agent), "open_pptx") is True


@pytest.mark.asyncio
async def test_declared_floor_tool_visible_when_skill_active():
    """declared 保护工具技能激活后走既有门控路径：直接可见可调用。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
        cards=[
            _card("read_file"),
            _card("open_pptx", properties={"required_skill": "pptx-craft"}),
        ],
        active_skill_provider=lambda: "pptx-craft",
    )

    names = await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert "open_pptx" in names
    assert await rail._skill_gate_blocks_direct_call(_tool_ctx(agent), "open_pptx") is False


@pytest.mark.asyncio
async def test_non_declared_floor_tool_direct_call_not_blocked():
    """非 declared 的保护工具：schema 可见且执行层不拦（可见 ⟺ 可调用的成立半边）。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
    )

    names = await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert "open_pptx" in names
    assert await rail._skill_gate_blocks_direct_call(_tool_ctx(agent), "open_pptx") is False


@pytest.mark.asyncio
async def test_disabled_tool_wins_over_floor():
    """DisabledToolsRail 黑名单最高优先：禁用的保护名单工具仍被滤除。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
        disabled_tools=["open_pptx"],
    )

    names = await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert "open_pptx" not in names
    assert "read_file" in names


@pytest.mark.asyncio
async def test_explicit_empty_list_disables_floor():
    """显式 protected_eager_tools=[] 表示整体关闭保护。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=[],
    )

    names = await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert "open_pptx" not in names
    assert "mcp_x" not in names


@pytest.mark.asyncio
async def test_floor_covers_delivery_and_delegation_tools():
    """生产事故场景：窄版 eager 列表缺交付/委派工具，floor 补齐注册即可见。"""
    rail, agent = _build_rail(
        eager_tools=[
            "tools_search",
            "invoke_tool",
            "ask_user",
            "read_file",
            "todo",
        ],
        protected_eager_tools=["send_file_to_user", "task_tool", "open_pptx"],
        cards=[
            _card("tools_search"),
            _card("invoke_tool"),
            _card("ask_user"),
            _card("read_file"),
            _card("todo"),
            _card("send_file_to_user"),
            _card("task_tool"),
            _card("open_pptx"),
        ],
    )

    names = await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert {"send_file_to_user", "task_tool", "open_pptx"} <= set(names)


@pytest.mark.asyncio
async def test_deferred_pool_excludes_protected_tools():
    """保护名单工具 schema 直接可见，不得进 deferred 池被导航宣称需间接调用。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
    )

    await _run_turn(rail, agent, _WHITELIST_MODEL)

    deferred_names = {
        tool.name for tool in rail._cached_deferred_tool_infos
    }
    assert "mcp_x" in deferred_names
    assert "open_pptx" not in deferred_names


@pytest.mark.asyncio
async def test_search_tools_hints_directly_visible_for_floor_tool():
    """tools_search 反查 floor 工具不得报"未找到"——指引直接调用。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    result = await rail._search_tools(None, SimpleNamespace(tool_name="open_pptx"))

    assert result["success"] is False
    assert result["matches"] == []
    assert "直接" in result["message"]
    assert "未找到" not in result["message"]


@pytest.mark.asyncio
async def test_invoke_tool_hints_resident_for_floor_tool():
    """invoke_tool 代理 floor 工具不得报"未注册"——指引直接调用。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    result = await rail._invoke_target_tool_with_binding(
        None, SimpleNamespace(tool_name="open_pptx", arguments={})
    )

    assert result["success"] is False
    assert "常驻可见" in result["error"]
    assert "未注册" not in result["error"]


@pytest.mark.asyncio
async def test_search_tools_reports_missing_for_unregistered_floor_tool():
    """未注册的 floor 名字（如 web 模式未挂 pptx 套件）必须回落"未找到"，不得虚假指引。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
        cards=[_card("read_file")],
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    result = await rail._search_tools(None, SimpleNamespace(tool_name="open_pptx"))

    assert result["success"] is False
    assert "未找到" in result["message"]
    assert "已注册" not in result["message"]


@pytest.mark.asyncio
async def test_invoke_tool_unregistered_floor_name_falls_to_unregistered():
    """invoke 代理对未注册的 floor 名字不得报"常驻可见"，应走"未注册"回落。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
        cards=[_card("read_file")],
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    result = await rail._invoke_target_tool_with_binding(
        None, SimpleNamespace(tool_name="open_pptx", arguments={})
    )

    assert result["success"] is False
    assert "未注册" in result["error"]
    assert "常驻可见" not in result["error"]


@pytest.mark.asyncio
async def test_protected_tools_order_after_eager_and_not_dropped():
    """排序 key 必须包含保护名单：eager 之后、保序，不被静默丢弃。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx", "send_file_to_user"],
        cards=[_card("read_file"), _card("open_pptx"), _card("send_file_to_user")],
    )

    names = await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert names == [
        "tools_search",
        "invoke_tool",
        "read_file",
        "open_pptx",
        "send_file_to_user",
    ]


def test_prompt_tool_allowlist_covers_eager_and_protected():
    """prompt 工具段 allowlist 必须覆盖 eager + floor 的稳定集合，防止与 schema 脱节。"""
    rail, _ = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx", "send_file_to_user"],
    )

    assert rail.prompt_tool_allowlist() == [
        "tools_search",
        "invoke_tool",
        "read_file",
        "open_pptx",
        "send_file_to_user",
    ]


def test_resolve_protected_eager_tools_semantics():
    """缺省/None/malformed 回落默认名单；显式 [] 关闭；逗号串与列表均支持。"""
    assert resolve_protected_eager_tools({}) == list(DEFAULT_PROGRESSIVE_PROTECTED_EAGER_TOOLS)
    assert resolve_protected_eager_tools(None) == list(DEFAULT_PROGRESSIVE_PROTECTED_EAGER_TOOLS)
    assert resolve_protected_eager_tools({"protected_eager_tools": None}) == list(
        DEFAULT_PROGRESSIVE_PROTECTED_EAGER_TOOLS
    )
    assert resolve_protected_eager_tools({"protected_eager_tools": []}) == []
    assert resolve_protected_eager_tools({"protected_eager_tools": "a, b,,c"}) == ["a", "b", "c"]
    assert resolve_protected_eager_tools({"protected_eager_tools": {"bad": 1}}) == list(
        DEFAULT_PROGRESSIVE_PROTECTED_EAGER_TOOLS
    )


def test_build_from_config_default_applies_floor():
    """缺省 protected_eager_tools 键时代码内置默认名单生效。"""
    rail = build_progressive_tool_rail_from_config(
        {"tool_lazy_load": {"enabled": True}},
        language="cn",
    )

    assert rail is not None
    assert rail._protected_eager_tools == frozenset(DEFAULT_PROGRESSIVE_PROTECTED_EAGER_TOOLS)


def test_build_from_config_explicit_empty_disables_floor():
    """显式置空 [] 关闭保护，构建结果不得回退到默认名单。"""
    rail = build_progressive_tool_rail_from_config(
        {
            "tool_lazy_load": {
                "enabled": True,
                "protected_eager_tools": [],
            }
        },
        language="cn",
    )

    assert rail is not None
    assert rail._protected_eager_tools == frozenset()


def test_shipped_config_protected_list_matches_code_default():
    """随仓库发布的 config.yaml 保护名单必须与代码默认名单一致，防止漂移。"""
    repo_root = Path(__file__).resolve().parents[3]
    config_file = repo_root / "jiuwenswarm" / "resources" / "config.yaml"
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    shipped = config["react"]["tool_lazy_load"]["protected_eager_tools"]

    assert shipped == list(DEFAULT_PROGRESSIVE_PROTECTED_EAGER_TOOLS)
