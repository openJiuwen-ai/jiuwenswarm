# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deep adapter must pin plan mode tools eager so the approval rail sees exit_plan_mode.

deferred 工具经 invoke_tool 嵌套执行不经过 before_tool_call 链路
（progressive_tool_rail 直接 invoke 目标工具），PlanApprovalInterruptRail
只匹配顶层 ctx.inputs.tool_name，拦不到 invoke_tool 包装下的
exit_plan_mode，Web plan 审批卡静默缺失（issue 5223 E2E 实证）。
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.tool import ToolCard

from jiuwenswarm.agents.harness.common.rails import progressive_tool_rail as ptr
from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import ProgressiveToolRail
from jiuwenswarm.agents.harness.common.tools.invoke_tool_tool import InvokeToolInput
from jiuwenswarm.agents.harness.work.prompt.work_plan_prompts import WORK_PLAN_ALLOWED_TOOLS
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _PLAN_MODE_EAGER_TOOLS,
    _WORK_PLAN_EAGER_TOOLS,
    JiuWenSwarmDeepAdapter,
)


_LAZY_CONFIG = {"tool_lazy_load": {"enabled": True, "eager_tools": ["read_file"]}}


def _rail(mode):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._session_instance_mode = mode
    rail = adapter._build_progressive_tool_rail(dict(_LAZY_CONFIG))
    assert rail is not None
    return rail


def _card(name, description="tool description"):
    return ToolCard(id=f"id-{name}", name=name, description=description, input_params={})


def _context(rail, cards, model_name="glm-5"):
    agent = SimpleNamespace(
        model_name=model_name,
        ability_manager=SimpleNamespace(list=lambda: list(cards)),
        system_prompt_builder=None,
    )
    rail.init(agent)
    return SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(cards)))


@pytest.mark.parametrize("mode", ["agent", "agent.plan", None])
def test_plan_capable_mode_pins_plan_tools_eager(mode):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._session_instance_mode = mode
    config = dict(_LAZY_CONFIG)
    rail = adapter._build_progressive_tool_rail(config)
    assert rail is not None
    # work 装配只钉 enter/exit（评审 CR-1，2026-11-10）：switch_mode 双态被
    # WorkAgentModeRail 过滤，钉了只喂 ②号防线每轮假阳性。
    assert set(_WORK_PLAN_EAGER_TOOLS) <= set(rail.eager_tools)
    assert "switch_mode" not in rail.eager_tools
    # ②号防线校验收敛到 work plan 白名单（白名单外成员设计性缺席）。
    assert rail.plan_check_allowed_tools == frozenset(WORK_PLAN_ALLOWED_TOOLS)
    # 与 code 侧同款纪律：追加不得回写调用方 config。
    assert config["tool_lazy_load"]["eager_tools"] == ["read_file"]


@pytest.mark.parametrize("mode", ["team", "team.plan", "code.team"])
def test_team_mode_does_not_pin_plan_tools_eager(mode):
    # team 装配不挂 plan rails（无 AgentModeRail 过滤/审批 rail），钉 eager 会外泄。
    rail = _rail(mode)
    assert not set(_WORK_PLAN_EAGER_TOOLS) & set(rail.eager_tools)
    assert rail.plan_check_allowed_tools is None


def test_flash_adapter_does_not_pin_plan_tools_eager():
    # flash 白名单裁掉 plan rails，经 super() 复用 deep 建轨时也不得钉 eager。
    from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
        JiuwenSwarmFlashAdapter,
    )

    adapter = JiuwenSwarmFlashAdapter()
    rail = adapter._build_progressive_tool_rail(dict(_LAZY_CONFIG))
    assert rail is not None
    assert not set(_WORK_PLAN_EAGER_TOOLS) & set(rail.eager_tools)


def test_code_adapter_keeps_trio_pin_and_scopes_plan_check():
    # code 装配保持三件套（normal 态 switch_mode 是唯一 plan 入口），但
    # ②号防线校验收敛到 code plan 白名单（plan 态 trio 白名单外成员缺席
    # 是设计内过滤，不得告警）。
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        _CODE_PLAN_ALLOWED_TOOLS,
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    rail = adapter._build_progressive_tool_rail(dict(_LAZY_CONFIG))
    assert rail is not None
    assert set(_PLAN_MODE_EAGER_TOOLS) <= set(rail.eager_tools)
    assert rail.plan_check_allowed_tools == frozenset(_CODE_PLAN_ALLOWED_TOOLS)


@pytest.mark.asyncio
async def test_plan_tools_survive_progressive_filter_and_invoke_tool_refusal():
    rail = _rail("agent")
    eager = [
        _card(name)
        for name in ["tools_search", "invoke_tool", "read_file", *_WORK_PLAN_EAGER_TOOLS]
    ]
    deferred = [_card("wps_1", "large schema description " * 50)]
    ctx = _context(rail, eager + deferred)

    await rail.before_model_call(ctx)

    names = {tool.name for tool in ctx.inputs.tools}
    assert set(_WORK_PLAN_EAGER_TOOLS) <= names
    assert "wps_1" not in names

    # 防线 1：exit_plan_mode 已常驻可见，经 invoke_tool 调用被拒绝，
    # 模型只能直呼（直呼走 before_tool_call 审批链）。
    result = await rail._invoke_target_tool(
        None, InvokeToolInput(tool_name="exit_plan_mode", arguments={})
    )
    assert result["success"] is False
    assert "常驻可见工具" in result["error"]

    # 阳性对照：deferred 工具经 invoke_tool 仍可正常调用，拒绝只因 eager。
    target = SimpleNamespace(card=deferred[0], invoke=AsyncMock(return_value="ok"))
    rail._lookup_tool_instance = lambda tool_id: (
        target if tool_id == target.card.id else None
    )
    result = await rail._invoke_target_tool(
        None, InvokeToolInput(tool_name="wps_1", arguments={})
    )
    assert result["success"] is True
    target.invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_plan_eager_additions_do_not_bypass_disabled_tools():
    # 与 code 侧 test_code_eager_additions_do_not_bypass_disabled_tools 对齐：
    # eager 追加不得绕过统一工具开关（黑名单）。
    adapter = JiuWenSwarmDeepAdapter()
    adapter._session_instance_mode = "agent"
    rail = adapter._build_progressive_tool_rail(
        {
            "tool_lazy_load": {"enabled": True, "eager_tools": []},
            "disabled_tools": ["exit_plan_mode"],
        }
    )
    assert rail is not None
    cards = [_card("exit_plan_mode"), _card("wps_deferred")]
    ctx = _context(rail, cards)
    await rail.before_model_call(ctx)
    assert [tool.name for tool in ctx.inputs.tools] == []
    result = await rail._invoke_target_tool(
        None, InvokeToolInput(tool_name="exit_plan_mode", arguments={})
    )
    assert result["success"] is False


# ── ②号防线：eager 缺席告警仅 plan 态生效（评审 2026-11-09）────────────────


def _context_with_plan_state(rail, cards, plan_mode: str):
    """ctx 带会话 plan 态：agent.load_state(session).plan_mode.mode 可注入。"""
    agent = SimpleNamespace(
        model_name="glm-5",
        ability_manager=SimpleNamespace(list=lambda: list(cards)),
        system_prompt_builder=None,
        load_state=lambda session: SimpleNamespace(  # noqa: ARG005
            plan_mode=SimpleNamespace(mode=plan_mode)
        ),
    )
    rail.init(agent)
    return SimpleNamespace(
        agent=agent,
        session=SimpleNamespace(),
        inputs=SimpleNamespace(tools=list(cards)),
    )


@pytest.mark.asyncio
async def test_eager_missing_warning_silent_in_normal_mode(caplog):
    """normal 态 plan 工具缺席是 mode rail 设计内过滤 → 不告警。"""
    rail = _rail("agent")
    # 输入里无 plan 三工具（模拟 mode rail 已按 normal 过滤）
    cards = [_card("tools_search"), _card("read_file")]
    ctx = _context_with_plan_state(rail, cards, plan_mode="normal")

    # 仓库 caplog 姿势（test_agent_ws_connection_close.py 同款）：
    # pytest.ini log_cli=true 下 caplog.at_level 靠传播捕获不生效，
    # 须把 caplog.handler 直接挂到目标 logger。
    ptr.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=ptr.logger.name)
    try:
        await rail.before_model_call(ctx)
    finally:
        ptr.logger.removeHandler(caplog.handler)

    assert not any("eager tools missing" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_eager_missing_warning_fires_in_plan_mode(caplog):
    """plan 态 eager 工具缺席即真异常（stale normal 竞速签名）→ 告警。"""
    rail = _rail("agent")
    # ttse_consult 随环境 config（react.ttse.enabled）可进 eager 且属 work
    # 白名单，输入补足该卡片使断言跨环境确定（CI ttse 关 → 不在 eager）。
    cards = [_card("tools_search"), _card("read_file"), _card("ttse_consult")]
    ctx = _context_with_plan_state(rail, cards, plan_mode="plan")

    ptr.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=ptr.logger.name)
    try:
        await rail.before_model_call(ctx)
    finally:
        ptr.logger.removeHandler(caplog.handler)

    warnings = [r for r in caplog.records if "eager tools missing" in r.message]
    assert warnings
    assert "enter_plan_mode" in warnings[0].getMessage()
    # 校验收敛（评审 CR-1，2026-11-10）：白名单外成员（lazy 支柱）不得掺入。
    assert "invoke_tool" not in warnings[0].getMessage()
    assert "tools_search" not in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_plan_check_skips_whitelist_excluded_eager_tools(caplog):
    """plan 态白名单外 eager 成员（lazy 支柱/switch_mode）缺席不告警（CR-1）。

    work plan 态输入即 WorkAgentModeRail 白名单过滤后形态：白名单成员在场，
    tools_search/invoke_tool/switch_mode 设计性缺席——收敛前这会每轮假阳性。
    ttse_consult 卡片补足环境 config 可能注入的 eager 白名单成员（见上例）。
    """
    rail = _rail("agent")
    cards = [
        _card("read_file"),
        _card("enter_plan_mode"),
        _card("exit_plan_mode"),
        _card("ttse_consult"),
    ]
    ctx = _context_with_plan_state(rail, cards, plan_mode="plan")

    ptr.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=ptr.logger.name)
    try:
        await rail.before_model_call(ctx)
    finally:
        ptr.logger.removeHandler(caplog.handler)

    assert not any("eager tools missing" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_plan_check_full_scope_when_no_whitelist_injected(caplog):
    """未注入白名单的直接建轨（plan_check_allowed_tools=None）保持全量校验。"""
    rail = ProgressiveToolRail(enabled=True, eager_tools=["read_file"])
    cards = [_card("read_file")]
    ctx = _context_with_plan_state(rail, cards, plan_mode="plan")

    ptr.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=ptr.logger.name)
    try:
        await rail.before_model_call(ctx)
    finally:
        ptr.logger.removeHandler(caplog.handler)

    warnings = [r for r in caplog.records if "eager tools missing" in r.message]
    assert warnings
    assert "invoke_tool" in warnings[0].getMessage()
