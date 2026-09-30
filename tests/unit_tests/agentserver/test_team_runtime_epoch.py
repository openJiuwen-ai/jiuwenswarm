# coding: utf-8
"""runtime 世代校验（epoch）单测：迟到拆除不得误杀新回合。

场景：cancel/pause 入口捕获世代号后卡在慢 await（如 30 人团队 Runner 清理
~53s），期间 relay-claw gate 超时放行、新"继续"回合完成注册（世代递增）。
迟到的拆除操作必须在破坏性步骤前放弃执行，且不写终态记录——否则新回合的
follow-up waiter 会把正常退出误报为取消/暂停。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.team_manager import Runner, TeamManager


class _Harness(TeamManager):
    def set_active_runtime_for_test(self, session_id: str, team_name: str) -> None:
        self.commit_runtime_ready(session_id, team_name)

    def stub_no_live_team_agent(self) -> None:
        # cancel/pause 前置的 best-effort settle 在 team_agent=None 时直接跳过，
        # 避免单测触碰 Runner pool / checkpoint。
        self._resolve_live_team_agent = AsyncMock(return_value=None)  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _non_distributed_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """单测统一走非分布式分支，prepare_runtime_activation 同步注册 pending。"""
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {},
    )


def _manager() -> _Harness:
    mgr = _Harness()
    mgr.stub_no_live_team_agent()
    return mgr


async def _register_round(mgr: _Harness, session_id: str, team_name: str) -> asyncio.Task:
    """模拟一个新回合的注册序列：prepare（bump epoch）→ 注册 stream task。"""
    await mgr.prepare_runtime_activation(session_id, team_name)
    task = asyncio.ensure_future(asyncio.sleep(3600))
    mgr.register_stream_task(session_id, task)
    return task


async def _cancel_round_tasks(*tasks: asyncio.Task) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_epoch_bumps_once_per_round() -> None:
    """prepare 每回合递增一次；commit/register_stream_task 不再递增。"""
    mgr = _manager()
    assert mgr.get_session_runtime_epoch("sess-1") == 0
    await mgr.prepare_runtime_activation("sess-1", "team_t")
    assert mgr.get_session_runtime_epoch("sess-1") == 1
    mgr.commit_runtime_ready("sess-1", "team_t")
    task = asyncio.ensure_future(asyncio.sleep(0))
    mgr.register_stream_task("sess-1", task)
    assert mgr.get_session_runtime_epoch("sess-1") == 1
    await mgr.prepare_runtime_activation("sess-1", "team_t")
    assert mgr.get_session_runtime_epoch("sess-1") == 2
    await _cancel_round_tasks(task)


@pytest.mark.asyncio
async def test_late_cancel_superseded_keeps_new_round() -> None:
    """核心用例：cancel 卡在慢 settle 期间新回合注册 → cancel 放弃拆除。

    生产事故时序：停止请求 → cancel 入口 → 卡在 HITL/Runner settle
    （数十秒）→ gate 超时放行 → 用户"继续"注册新回合 → cancel 迟到醒来。
    """
    mgr = _manager()
    mgr._settle_persisted_cancelled_permission_interrupt = AsyncMock()  # type: ignore[method-assign]

    settle_entered = asyncio.Event()
    release_settle = asyncio.Event()

    async def slow_settle(*_args: object, **_kwargs: object) -> None:
        settle_entered.set()
        await release_settle.wait()

    mgr._settle_live_cancelled_permission_interrupt = slow_settle  # type: ignore[method-assign]

    runner_stop_calls: list[str] = []

    async def fake_stop_runner(session_id: str, team_name: str, caller: str) -> bool:
        runner_stop_calls.append(caller)
        return True

    mgr._stop_runner_team_runtime = fake_stop_runner  # type: ignore[method-assign]

    old_task = await _register_round(mgr, "sess-1", "team_t")

    cancel_task = asyncio.ensure_future(
        mgr.cancel_session_runtime("sess-1", reason="test")
    )
    await asyncio.wait_for(settle_entered.wait(), timeout=1.0)

    # 慢 settle 期间：拆除在途可见（follow-up 归一化据此等待收尾）
    assert mgr.has_pending_cancel("sess-1") is True

    # 慢 settle 期间：新回合注册（relay-claw gate 放行后的"继续"）
    new_task = await _register_round(mgr, "sess-1", "team_t")

    release_settle.set()
    assert await asyncio.wait_for(cancel_task, timeout=1.0) is False

    # 迟到 cancel 放弃拆除后：拆除在途状态必须解除（wrapper finally 兜底），
    # 否则后续 follow-up 会被已死的清理窗口永久拖延
    assert mgr.has_pending_cancel("sess-1") is False

    # 新回合存活：stream task 未被动过、无终态记录（waiter 不误报）
    assert new_task.done() is False
    assert new_task.cancelled() is False
    assert mgr.has_stream_task("sess-1") is True
    assert mgr.get_session_terminal_state("sess-1") is None
    # 未发生任何拆除：forced stop / lock 内拆除均未执行
    assert runner_stop_calls == []
    # 放弃路径必须清掉 cancel_requested，否则后续 pause 会被永久抑制
    assert "sess-1" not in mgr._cancel_requested
    mgr._settle_persisted_cancelled_permission_interrupt.assert_not_called()

    await _cancel_round_tasks(old_task, new_task)


@pytest.mark.asyncio
async def test_late_cancel_superseded_inside_lifecycle_lock() -> None:
    """校验点 2：cancel 等 lifecycle lock 期间新回合注册 → 进锁后放弃。"""
    mgr = _manager()
    mgr._settle_persisted_cancelled_permission_interrupt = AsyncMock()  # type: ignore[method-assign]
    mgr.set_active_runtime_for_test("sess-1", "team_t")

    holder_locked = asyncio.Event()
    cancel_parked = asyncio.Event()
    new_round_registered = asyncio.Event()

    async def hold_lock() -> None:
        async with mgr._get_lifecycle_lock("sess-1"):
            holder_locked.set()
            await cancel_parked.wait()
            # cancel 已在 forced stop / 等锁途中：此刻注册新回合（bump epoch）
            await mgr.prepare_runtime_activation("sess-1", "team_t")
            new_task = asyncio.ensure_future(asyncio.sleep(3600))
            mgr.register_stream_task("sess-1", new_task)
            new_round_registered.set()

    async def fake_stop_runner(session_id: str, team_name: str, caller: str) -> bool:
        assert caller == "cancel: forced"
        cancel_parked.set()
        await new_round_registered.wait()
        return True

    mgr._stop_runner_team_runtime = fake_stop_runner  # type: ignore[method-assign]

    holder = asyncio.ensure_future(hold_lock())
    await asyncio.wait_for(holder_locked.wait(), timeout=1.0)

    cancel_task = asyncio.ensure_future(
        mgr.cancel_session_runtime("sess-1", reason="test")
    )
    assert await asyncio.wait_for(cancel_task, timeout=1.0) is False
    await asyncio.wait_for(holder, timeout=1.0)

    # 迟到 cancel 进锁后放弃：不写终态记录，新回合 stream task 存活
    assert mgr.get_session_terminal_state("sess-1") is None
    assert mgr.has_stream_task("sess-1") is True
    stream_task = mgr._stream_tasks["sess-1"]
    assert stream_task.done() is False
    assert "sess-1" not in mgr._cancel_requested
    mgr._settle_persisted_cancelled_permission_interrupt.assert_not_called()

    await _cancel_round_tasks(stream_task)


@pytest.mark.asyncio
async def test_late_pause_superseded_keeps_new_round() -> None:
    """pause 等 lifecycle lock 期间新回合注册 → pause 放弃拆除、无终态记录。"""
    mgr = _manager()
    mgr.set_active_runtime_for_test("sess-1", "team_t")

    holder_locked = asyncio.Event()
    allow_holder_exit = asyncio.Event()

    async def hold_lock() -> asyncio.Task:
        async with mgr._get_lifecycle_lock("sess-1"):
            holder_locked.set()
            await allow_holder_exit.wait()
            # pause 已在等锁途中：注册新回合（bump epoch）
            return await _register_round(mgr, "sess-1", "team_t")

    runner_stop_calls: list[str] = []

    async def fake_stop_runner(session_id: str, team_name: str, caller: str) -> bool:
        runner_stop_calls.append(caller)
        return True

    mgr._stop_runner_team_runtime = fake_stop_runner  # type: ignore[method-assign]

    holder = asyncio.ensure_future(hold_lock())
    await asyncio.wait_for(holder_locked.wait(), timeout=1.0)

    pause_task = asyncio.ensure_future(
        mgr.pause_session_runtime("sess-1", reason="test")
    )
    # 让 pause 运行到等锁挂起点
    await asyncio.sleep(0)
    allow_holder_exit.set()
    new_task = await asyncio.wait_for(holder, timeout=1.0)

    assert await asyncio.wait_for(pause_task, timeout=1.0) is False

    # 新回合存活、无 "paused" 终态记录、未触发任何拆除
    assert new_task.done() is False
    assert mgr.has_stream_task("sess-1") is True
    assert mgr.get_session_terminal_state("sess-1") is None
    assert runner_stop_calls == []

    await _cancel_round_tasks(new_task)


@pytest.mark.asyncio
async def test_pause_abort_skips_teardown_when_new_round_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pause 被 cancel 抢占：若新回合已注册，让路拆除同样放弃（由 cancel 决定）。"""
    mgr = _manager()
    mgr._settle_persisted_cancelled_permission_interrupt = AsyncMock()  # type: ignore[method-assign]

    pause_entered = asyncio.Event()

    async def fake_pause_agent_team(**_kwargs: object) -> bool:
        pause_entered.set()
        await asyncio.sleep(3600)
        return True

    monkeypatch.setattr(
        Runner,
        "pause_agent_team",
        fake_pause_agent_team,
    )

    runner_stop_calls: list[str] = []

    async def fake_stop_runner(session_id: str, team_name: str, caller: str) -> bool:
        runner_stop_calls.append(caller)
        return True

    mgr._stop_runner_team_runtime = fake_stop_runner  # type: ignore[method-assign]

    assert await mgr.prepare_runtime_activation("sess-1", "team_t") is None
    mgr.commit_runtime_ready("sess-1", "team_t")
    pause_task = asyncio.ensure_future(
        mgr.pause_session_runtime("sess-1", reason="test")
    )
    await asyncio.wait_for(pause_entered.wait(), timeout=1.0)

    # Runner.pause 等待期间新回合注册（不拿 lifecycle lock，与生产时序一致）
    new_task = await _register_round(mgr, "sess-1", "team_t")

    # cancel 到达：其入口捕获的已是新回合世代，preempt pause
    cancel_task = asyncio.ensure_future(
        mgr.cancel_session_runtime("sess-1", reason="test")
    )
    assert await asyncio.wait_for(cancel_task, timeout=1.0) is True
    assert await asyncio.wait_for(pause_task, timeout=1.0) is False

    # pause 的让路拆除被世代校验拦下（无 "pause aborted" stop）；
    # cancel 作用于其到达时刻的目标（新回合），正常拆除并如实记录终态。
    assert "pause aborted" not in runner_stop_calls
    assert "cancel" in runner_stop_calls
    assert mgr.get_session_terminal_state("sess-1") == "cancelled"

    mgr._settle_persisted_cancelled_permission_interrupt.assert_awaited_once()
    await _cancel_round_tasks(new_task, pause_task)


@pytest.mark.asyncio
async def test_cancel_in_flight_tracked_through_slow_settle() -> None:
    """拆除在途全生命周期（方案 A 闭环缺口 3）：settle→stop→finalize 全程可见。

    生产事故时序：cancel 卡在慢 settle（~37s）期间"继续"到达——follow-up
    归一化靠 has_pending_cancel 识别清理窗口并 wait_for_cancel_settled 等收尾；
    时限内未收尾返回 False（回退 interact 路径），收尾后返回 True。
    """
    mgr = _manager()
    mgr._settle_persisted_cancelled_permission_interrupt = AsyncMock()  # type: ignore[method-assign]

    settle_entered = asyncio.Event()
    release_settle = asyncio.Event()

    async def slow_settle(*_args: object, **_kwargs: object) -> None:
        settle_entered.set()
        await release_settle.wait()

    mgr._settle_live_cancelled_permission_interrupt = slow_settle  # type: ignore[method-assign]

    async def fake_stop_runner(session_id: str, team_name: str, caller: str) -> bool:
        return True

    mgr._stop_runner_team_runtime = fake_stop_runner  # type: ignore[method-assign]

    task = await _register_round(mgr, "sess-1", "team_t")
    assert mgr.has_pending_cancel("sess-1") is False

    cancel_task = asyncio.ensure_future(
        mgr.cancel_session_runtime("sess-1", reason="test")
    )
    await asyncio.wait_for(settle_entered.wait(), timeout=1.0)

    # 慢 settle 期间：拆除在途可见；短时限等待返回 False（仍在途）
    assert mgr.has_pending_cancel("sess-1") is True
    assert await mgr.wait_for_cancel_settled("sess-1", timeout_sec=0.01) is False

    # 挂起的等待者：收尾信号唤醒后返回 True
    waiter_task = asyncio.ensure_future(
        mgr.wait_for_cancel_settled("sess-1", timeout_sec=5.0)
    )
    await asyncio.sleep(0)
    assert waiter_task.done() is False

    release_settle.set()
    assert await asyncio.wait_for(cancel_task, timeout=1.0) is True
    assert await asyncio.wait_for(waiter_task, timeout=1.0) is True
    assert mgr.has_pending_cancel("sess-1") is False

    await _cancel_round_tasks(task)


@pytest.mark.asyncio
async def test_normal_cancel_still_tears_down() -> None:
    """回归：无新回合时世代校验不拦截，cancel 照常拆除并记录终态。"""
    mgr = _manager()
    mgr._settle_persisted_cancelled_permission_interrupt = AsyncMock()  # type: ignore[method-assign]
    mgr.set_active_runtime_for_test("sess-1", "team_t")

    async def fake_stop_runner(session_id: str, team_name: str, caller: str) -> bool:
        return True

    mgr._stop_runner_team_runtime = fake_stop_runner  # type: ignore[method-assign]

    assert await mgr.cancel_session_runtime("sess-1", reason="test") is True
    assert mgr.get_session_terminal_state("sess-1") == "cancelled"


@pytest.mark.asyncio
async def test_normal_pause_still_tears_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：无新回合时 pause 照常拆除并记录终态。"""
    mgr = _manager()
    mgr.set_active_runtime_for_test("sess-2", "team_t")

    async def fake_pause_agent_team(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(Runner, "pause_agent_team", fake_pause_agent_team)
    assert await mgr.pause_session_runtime("sess-2", reason="test") is True
    assert mgr.get_session_terminal_state("sess-2") == "paused"
