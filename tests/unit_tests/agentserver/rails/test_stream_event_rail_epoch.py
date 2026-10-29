# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""StreamEventRail abort 世代化（epoch）回归测试.

根治点：abort 按会话世代计数，每个回合在 before_invoke 快照当前 epoch，
checkpoint 仅当「当前 epoch > 回合快照」时自杀——abort 永远不可能杀死
在它之后才出生的回合（忙时顶替/取消后抢发竞态的根治断言），同时
运行中的回合被取消仍然必死。

边界（by design）：ctx.extra 缺失快照的回调（agent-core 回调边界丢
extra 的工具回调等）无法归属世代，检查点不判定（免疫）——该形态的
在途旧轮回肠由 task.cancel + shell kill 兜底。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


async def _new_round(rail: JiuSwarmStreamEventRail, sid: str = "sess-a"):
    """按生产路径构造一次 invoke 的 ctx：before_invoke 打 abort-epoch 快照."""
    ctx = AgentCallbackContext(agent=object())
    ctx.inputs = InvokeInputs(query="q", conversation_id=sid)
    ctx.session = SimpleNamespace()  # rail 仅存储/身份比较，不调用其方法
    await rail.before_invoke(ctx)
    return ctx


@pytest.mark.asyncio
async def test_abort_kills_round_started_before_abort():
    rail = JiuSwarmStreamEventRail()
    ctx_old = await _new_round(rail)
    rail.abort("sess-a")
    with pytest.raises(asyncio.CancelledError):
        await rail.before_model_call(ctx_old)


@pytest.mark.asyncio
async def test_round_started_after_abort_is_immune():
    rail = JiuSwarmStreamEventRail()
    rail.abort("sess-a")  # 旧轮已被取消
    ctx_new = await _new_round(rail)  # 新回合在 abort 之后出生
    await rail.before_model_call(ctx_new)  # 不抛 = 免疫
    await rail.before_tool_call(ctx_new)


@pytest.mark.asyncio
async def test_takeover_race_old_dies_new_lives():
    """basic_013/concurrent_004/interrupt_009 竞态的根治断言.

    同一会话：旧回合运行中 → abort（顶替重放 cancel/用户取消）→
    新回合出生。旧回合检查点必死，新回合检查点必活。
    """
    rail = JiuSwarmStreamEventRail()
    ctx_old = await _new_round(rail)
    rail.abort("sess-a")
    ctx_new = await _new_round(rail)
    with pytest.raises(asyncio.CancelledError):
        await rail.before_model_call(ctx_old)
    with pytest.raises(asyncio.CancelledError):
        await rail.before_tool_call(ctx_old)
    await rail.before_model_call(ctx_new)
    await rail.before_tool_call(ctx_new)


@pytest.mark.asyncio
async def test_cancel_while_running_still_kills():
    """运行中的回合被用户取消必须死——世代化不得弱化既有取消语义."""
    rail = JiuSwarmStreamEventRail()
    ctx = await _new_round(rail)
    await rail.before_model_call(ctx)  # 运行中先通过一次检查点
    rail.abort("sess-a")
    with pytest.raises(asyncio.CancelledError):
        await rail.before_model_call(ctx)
    with pytest.raises(asyncio.CancelledError):
        await rail.before_tool_call(ctx)


@pytest.mark.asyncio
async def test_two_sessions_isolated():
    rail = JiuSwarmStreamEventRail()
    ctx_a = await _new_round(rail, sid="sess-a")
    rail.abort("sess-b")
    await rail.before_model_call(ctx_a)  # B 会话的取消不影响 A
    rail.abort("sess-a")
    with pytest.raises(asyncio.CancelledError):
        await rail.before_model_call(ctx_a)


@pytest.mark.asyncio
async def test_epoch_survives_resume_and_reset_for_new_task():
    """resume / reset_for_new_task 不得清世代——暂停中被取消的轮恢复后仍须死."""
    rail = JiuSwarmStreamEventRail()
    ctx = await _new_round(rail)
    rail.abort("sess-a")
    rail.resume("sess-a")
    rail.reset_for_new_task("sess-a")
    with pytest.raises(asyncio.CancelledError):
        await rail.before_model_call(ctx)


@pytest.mark.asyncio
async def test_multiple_aborts_keep_killing_stale_rounds():
    rail = JiuSwarmStreamEventRail()
    ctx = await _new_round(rail)
    for _ in range(3):
        rail.abort("sess-a")  # 多次 cancel（级联顶替形态）
    with pytest.raises(asyncio.CancelledError):
        await rail.before_model_call(ctx)


@pytest.mark.asyncio
async def test_cleanup_holds_epoch_monotonic():
    """cleanup_session 不得把世代清零回绕——否则在跑旧轮对后续 abort 永久免疫."""
    rail = JiuSwarmStreamEventRail()
    ctx = await _new_round(rail)
    for _ in range(3):
        rail.abort("sess-a")  # 世代 → 3
    rail.cleanup_session("sess-a")  # 世代保持 3（不回绕）
    rail.abort("sess-a")  # 世代 → 4（>3，仍能命中旧快照）
    with pytest.raises(asyncio.CancelledError):
        await rail.before_model_call(ctx)


@pytest.mark.asyncio
async def test_snapshot_less_ctx_is_immune_by_design():
    """ctx.extra 缺失快照（回调边界丢 extra）不判定——无法归属世代则不误杀."""
    rail = JiuSwarmStreamEventRail()
    ctx = AgentCallbackContext(agent=object())
    ctx.inputs = InvokeInputs(query="q", conversation_id="sess-a")
    ctx.session = SimpleNamespace()
    # 未走 before_invoke → 无快照
    await rail.before_model_call(ctx)  # 无 abort：通过
    rail.abort("sess-a")
    await rail.before_model_call(ctx)  # 有 abort：仍不判定（无法确认它属于旧世代）
