# coding: utf-8
"""team follow-up waiter 中断终态语义（方案 A）单测。

cancel/pause 拆除 runtime 时必须留下终态记录（"cancelled"/"paused"），
follow-up waiter 据此区分"被中断退出"与"正常 team.completed"，不再假报完成。
终态在下一轮 stream task 注册时清除。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
    _follow_up_waiter_interrupt_error,
)


class _Harness(TeamManager):
    def set_active_runtime_for_test(self, session_id: str, team_name: str) -> None:
        self.commit_runtime_ready(session_id, team_name)

    def stub_no_live_team_agent(self) -> None:
        # cancel/pause 前置的 best-effort settle 在 team_agent=None 时直接跳过，
        # 避免单测触碰 Runner pool / checkpoint。
        self._resolve_live_team_agent = AsyncMock(return_value=None)  # type: ignore[method-assign]


def _manager() -> _Harness:
    mgr = _Harness()
    mgr.stub_no_live_team_agent()
    return mgr


def test_record_get_clear_lifecycle() -> None:
    mgr = _manager()
    assert mgr.get_session_terminal_state("sess-1") is None
    mgr.record_session_terminal_state("sess-1", "cancelled")
    assert mgr.get_session_terminal_state("sess-1") == "cancelled"
    mgr.clear_session_terminal_state("sess-1")
    assert mgr.get_session_terminal_state("sess-1") is None


@pytest.mark.asyncio
async def test_register_stream_task_clears_terminal_state() -> None:
    """新回合注册 stream task 时必须清除上一轮的终态记录。"""
    mgr = _manager()
    mgr.record_session_terminal_state("sess-1", "cancelled")
    task = asyncio.ensure_future(asyncio.sleep(0))
    await task
    mgr.register_stream_task("sess-1", task)
    assert mgr.get_session_terminal_state("sess-1") is None


@pytest.mark.asyncio
async def test_cancel_session_runtime_records_cancelled() -> None:
    """cancel 拆除 runtime → 终态记录 "cancelled"，waiter 查询可感知。"""
    mgr = _manager()
    mgr.set_active_runtime_for_test("sess-1", "team_t")
    assert await mgr.cancel_session_runtime("sess-1", reason="test") is True
    assert mgr.get_session_terminal_state("sess-1") == "cancelled"


@pytest.mark.asyncio
async def test_pause_session_runtime_records_paused() -> None:
    """pause 拆除流任务 → 终态记录 "paused"，waiter 查询可感知。"""
    mgr = _manager()
    mgr.set_active_runtime_for_test("sess-2", "team_t")
    assert await mgr.pause_session_runtime("sess-2", reason="test") is True
    assert mgr.get_session_terminal_state("sess-2") == "paused"


@pytest.mark.asyncio
async def test_cancel_without_runtime_records_nothing() -> None:
    """无 runtime 时 cancel 返回 False，不留终态记录。"""
    mgr = _manager()
    assert await mgr.cancel_session_runtime("sess-empty", reason="test") is False
    assert mgr.get_session_terminal_state("sess-empty") is None


def test_follow_up_waiter_interrupt_error_mapping() -> None:
    """终态 → chat.error payload 映射：cancelled/paused 如实上报，其余保持完成语义。"""
    cancelled = _follow_up_waiter_interrupt_error("cancelled")
    assert cancelled is not None
    assert cancelled["event_type"] == "chat.error"
    assert cancelled["code"] == "team_cancelled"

    paused = _follow_up_waiter_interrupt_error("paused")
    assert paused is not None
    assert paused["code"] == "team_paused"

    # 终态缺失（正常完成/未知退出）→ None，waiter 走既有 is_complete 收尾
    assert _follow_up_waiter_interrupt_error(None) is None
    assert _follow_up_waiter_interrupt_error("unknown_state") is None
