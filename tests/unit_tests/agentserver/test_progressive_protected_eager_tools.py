# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""注册即可见 floor（BUG20261117410859）：protected_eager_tools 语义测试。

- 白名单模型下，已注册的保护名单工具必须留在 schema（懒加载裁剪不得隐藏）
- declared（required_skill）工具不走 floor，保持技能门控："可见 ⟺ 可直接调用"
- floor 优先于 eager 交集；DisabledToolsRail 黑名单仍最高优先
- 显式置空 [] 关闭保护；缺省键时代码内置默认名单生效（主/subagent/team 共用）
- deferred 池排除保护名单工具；tools_search / invoke_tool 对其给出"已直接可见"指引
  （反查与 deferred 池同契约：大小写不敏感，回复用卡片规范名）
- 排序稳定性与 prompt allowlist 同步（eager → protected；declared 名单名被剔除；
  装配期缓存空窗返回未过滤名单，缓存填充后经 listener 回调补同步）
- 禁用名单优先于 floor：反查/代理入口禁用复核大小写归一，不得给出"直接可见"指引
- 大小写策略跨路径统一：反查 / invoke 代理 / schema floor 分支 / deferred 池同一归一
- 名单项大小写 ≠ 注册名（CR-16）：排序 key 小写归一不失配丢出 schema，schema 可见、
  deferred 排除、反查指引三者一致；allowlist 输出对齐注册卡片规范名并去重
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
async def test_search_tools_floor_hint_is_case_insensitive():
    """反查名单工具与 deferred 池同契约：大小写不敏感命中，回复用卡片规范名。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    result = await rail._search_tools(None, SimpleNamespace(tool_name="OPEN_PPTX"))

    assert result["success"] is False
    assert result["matches"] == []
    # 回复引用卡片规范名 open_pptx，而非查询原文 OPEN_PPTX。
    assert "open_pptx" in result["message"]
    assert "OPEN_PPTX" not in result["message"]
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


@pytest.mark.asyncio
async def test_floor_tool_kept_when_config_case_differs_from_registration():
    """CR-16：名单项大小写 ≠ 注册名时不得静默丢弃，三条链路结论一致。

    floor 分支按小写命中保留工具，但排序 key 携带名单原始大小写——排序若
    精确匹配会把该工具静默丢出 schema；deferred 池（小写归一）同刻排除，
    工具全失联，反查还会给出"直接可见"的虚假指引。回归断言：schema 可见
    且位于 floor 位次、deferred 池排除、tools_search 指引为真。
    """
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["Open_PPTX"],
        cards=[_card("read_file"), _card("open_pptx")],
    )

    names = await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert names == ["tools_search", "invoke_tool", "read_file", "open_pptx"]
    deferred_names = {tool.name for tool in rail._cached_deferred_tool_infos}
    assert "open_pptx" not in deferred_names
    result = await rail._search_tools(None, SimpleNamespace(tool_name="open_pptx"))
    assert "已注册且在当前 schema" in result["message"]


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


@pytest.mark.asyncio
async def test_prompt_tool_allowlist_excludes_declared_names():
    """prompt allowlist 剔除已注册的 declared（required_skill）名单名。

    eager 段（skill_heavy）与 floor 段（open_pptx / gated_extra）同理：declared
    工具条件可见，宣传面不得提前承诺（与 skill-gate 收窄一致）。技能激活态
    也不纳入——prompt 段是稳定集合，随 active skill 抖动会破坏 prefix cache，
    且技能降活后无同步点会把过期承诺留在提示里。
    """
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file", "skill_heavy"],
        protected_eager_tools=["open_pptx", "gated_extra"],
        cards=[
            _card("tools_search"),
            _card("invoke_tool"),
            _card("read_file"),
            _card("skill_heavy", properties={"required_skill": "pptx-craft"}),
            _card("open_pptx", properties={"required_skill": "pptx-craft"}),
            _card("gated_extra", properties={"required_skill": "pptx-craft"}),
        ],
        active_skill_provider=lambda: "pptx-craft",
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert rail.prompt_tool_allowlist() == [
        "tools_search",
        "invoke_tool",
        "read_file",
    ]


@pytest.mark.asyncio
async def test_prompt_tool_allowlist_canonicalizes_case_mismatched_names():
    """CR-16：allowlist 输出对齐注册卡片规范名并去重，名单笔误不失真。

    名单项写错大小写时宣传面不得出现 schema 中不存在的名字；eager 与
    floor 名单对齐后重叠的条目只保留一次。
    """
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["Open_PPTX"],
        cards=[_card("read_file"), _card("open_pptx")],
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert rail.prompt_tool_allowlist() == [
        "tools_search",
        "invoke_tool",
        "read_file",
        "open_pptx",
    ]


@pytest.mark.asyncio
async def test_prompt_allowlist_listener_fires_after_cache_fill():
    """CR-13：装配期缓存空窗返回未过滤名单；缓存填充后 listener 收到补同步回调。

    首次装配/热更同步先于卡片注册，declared 过滤彼时无从判定（与
    ContextAssemble 保存的未过滤名单一致）；缓存填充触发 listener 补一次
    同步，declared（skill_heavy）被剔除、非 declared floor（open_pptx）保留。
    """
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file", "skill_heavy"],
        protected_eager_tools=["open_pptx"],
        cards=[
            _card("tools_search"),
            _card("invoke_tool"),
            _card("read_file"),
            _card("skill_heavy", properties={"required_skill": "pptx-craft"}),
            _card("open_pptx"),
        ],
        active_skill_provider=lambda: None,
    )

    # 装配期（缓存空窗）：返回未过滤名单。
    assert "skill_heavy" in rail.prompt_tool_allowlist()

    received: list[list[str]] = []
    rail.add_prompt_allowlist_listener(
        lambda: received.append(rail.prompt_tool_allowlist())
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    assert received, "listener must fire after first cache fill"
    assert received[-1] == ["tools_search", "invoke_tool", "read_file", "open_pptx"]

    # allowlist 无变化时不再触发（避免每轮重复同步）。
    turns = len(received)
    await _run_turn(rail, agent, _WHITELIST_MODEL)
    assert len(received) == turns


@pytest.mark.asyncio
async def test_search_tools_disabled_floor_tool_falls_to_disabled_message():
    """CR-14：禁用名单优先于 floor——反查（含非规范大小写）禁用工具不得给出"直接可见"指引。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
        disabled_tools=["open_pptx"],
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    result = await rail._search_tools(None, SimpleNamespace(tool_name="OPEN_PPTX"))

    assert result["success"] is False
    assert "已禁用" in result["message"]
    assert "直接可见" not in result["message"]


@pytest.mark.asyncio
async def test_invoke_tool_disabled_floor_tool_case_insensitive():
    """CR-14：invoke 入口禁用复核大小写归一，与 tools_search 入口同契约。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
        disabled_tools=["open_pptx"],
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    result = await rail._invoke_target_tool_with_binding(
        None, SimpleNamespace(tool_name="OPEN_PPTX", arguments={})
    )

    assert result["success"] is False
    assert "已禁用" in result["error"]
    assert "常驻可见" not in result["error"]


@pytest.mark.asyncio
async def test_invoke_tool_floor_hint_is_case_insensitive():
    """CR-14：invoke 代理与 tools_search 反查策略一致——非规范大小写名单工具同样答"常驻可见"。"""
    rail, agent = _build_rail(
        eager_tools=["tools_search", "invoke_tool", "read_file"],
        protected_eager_tools=["open_pptx"],
    )
    await _run_turn(rail, agent, _WHITELIST_MODEL)

    result = await rail._invoke_target_tool_with_binding(
        None, SimpleNamespace(tool_name="OPEN_PPTX", arguments={})
    )

    assert result["success"] is False
    assert "常驻可见" in result["error"]
    assert "未注册" not in result["error"]


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
