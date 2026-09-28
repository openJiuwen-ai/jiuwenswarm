# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the chat.error settle edge.

Drives ``team_helpers._consume_stream_with_query`` with a teammate chat.error
chunk and verifies:

* settled board → processing_status(is_complete=True) broadcast + stream break;
* unsettled board → pure no-op (stream keeps consuming);
* flag disabled (TeamSpec.enable_round_failed_recovery=False) → settle
  evaluation skipped entirely (emergency rollback path).
"""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import team_helpers


class _RecoveryTestManager:
    """TeamManager double covering both the consume-loop and settle hooks."""

    def __init__(self) -> None:
        self.round_complete_claims: list[str] = []

    def reset_seen_team_events(self, session_id: str) -> None:
        pass

    def reset_round_complete(self, session_id: str) -> None:
        pass

    def reset_workflow_completed(self, session_id: str) -> None:
        pass

    def get_monitor_handler(self, session_id: str):
        return None

    def clear_pending_runtime(self, session_id: str) -> None:
        pass

    def clear_active_runtime(self, session_id: str) -> None:
        pass

    def pop_stream_task(self, session_id: str):
        pass

    def claim_round_complete(self, session_id: str) -> bool:
        self.round_complete_claims.append(session_id)
        return True


def _error_chunk() -> SimpleNamespace:
    # role 必须显式为非 leader：_is_leader_output 对 role=None 默认按 leader 处理。
    return SimpleNamespace(type="llm_output", payload={}, role="member")


def _delta_chunk() -> SimpleNamespace:
    return SimpleNamespace(type="llm_output", payload={}, role="member")


def _error_parse(chunk):
    if chunk is None:
        return None
    return {"event_type": "chat.error", "error": "model call failed", "session_id": "s"}


def _mixed_parse(error_chunk):
    def _parse(chunk):
        if chunk is error_chunk:
            return _error_parse(chunk)
        return {"event_type": "chat.delta", "content": "后续输出"}

    return _parse


async def _run_consume(monkeypatch, chunks, parse_fn) -> list[dict]:
    broadcasts: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda c, s, e: broadcasts.append(e)
    )
    monkeypatch.setattr(team_helpers, "_ASK_USER_RESEND_INTERVAL_S", 0.05)
    monkeypatch.setattr(team_helpers, "_ASK_USER_RESEND_MAX", 5)

    async def _fake_stream(**kwargs):
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(team_helpers, "_run_agent_team_streaming", _fake_stream)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _RecoveryTestManager())
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", parse_fn)

    await team_helpers._consume_stream_with_query(
        "web",
        "sess-recovery",
        SimpleNamespace(team_name="spec-team"),
        "请为一款定价299的产品做营销",
        round_id=1,
    )
    return broadcasts


@pytest.mark.asyncio
async def test_teammate_chat_error_settles_terminal_board(monkeypatch) -> None:
    """失败任务已被熔断收敛（全员 settled + 全任务终态）：chat.error 关流并推送 is_complete。"""
    error_chunk = _error_chunk()
    trailing_chunk = _delta_chunk()

    async def _settled(channel_id, session_id) -> bool:
        return True

    monkeypatch.setattr(team_helpers, "_team_round_settled", _settled)

    broadcasts = await _run_consume(
        monkeypatch,
        [error_chunk, trailing_chunk],
        _mixed_parse(error_chunk),
    )

    kinds = [e.get("event_type") for e in broadcasts]
    assert "chat.error" in kinds
    # 轮次起始帧恒存在（is_processing=True），只认 is_complete=True 的收尾帧。
    complete = [
        e for e in broadcasts
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
    ]
    assert len(complete) == 1
    # settle 后流必须 break：后续 chunk 不再广播；teammate 路径无 chat.final。
    assert "chat.delta" not in kinds
    assert "chat.final" not in kinds


@pytest.mark.asyncio
async def test_teammate_chat_error_is_noop_when_board_not_settled(monkeypatch) -> None:
    """任务仍在途（回池待重派）：settle 评估 no-op，流继续消费后续 chunk。"""
    error_chunk = _error_chunk()
    trailing_chunk = _delta_chunk()

    async def _settled(channel_id, session_id) -> bool:
        return False

    monkeypatch.setattr(team_helpers, "_team_round_settled", _settled)

    broadcasts = await _run_consume(
        monkeypatch,
        [error_chunk, trailing_chunk],
        _mixed_parse(error_chunk),
    )

    kinds = [e.get("event_type") for e in broadcasts]
    assert "chat.error" in kinds
    assert "chat.delta" in kinds  # 未关流，后续输出照常广播
    assert not any(
        e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
        for e in broadcasts
    )


@pytest.mark.asyncio
async def test_chat_error_skips_settle_when_recovery_disabled(monkeypatch) -> None:
    """旗标关闭（回滚路径）：完全不触发 settle 评估，行为与修复前一致。"""
    error_chunk = _error_chunk()
    trailing_chunk = _delta_chunk()

    settle_calls: list[str] = []

    async def _spy_finish(channel_id, session_id, round_id, *, reason):
        settle_calls.append(reason)
        return False

    monkeypatch.setattr(team_helpers, "_round_failed_recovery_enabled", lambda s: False)
    monkeypatch.setattr(team_helpers, "_finish_round_if_settled", _spy_finish)

    broadcasts = await _run_consume(
        monkeypatch,
        [error_chunk, trailing_chunk],
        _mixed_parse(error_chunk),
    )

    assert settle_calls == []
    kinds = [e.get("event_type") for e in broadcasts]
    assert "chat.error" in kinds
    assert "chat.delta" in kinds
    assert not any(
        e.get("event_type") == "chat.processing_status" and e.get("is_complete") is True
        for e in broadcasts
    )


@pytest.mark.asyncio
async def test_recovery_flag_accessor_defaults_true(monkeypatch) -> None:
    """访问器默认放行：monitor 缺失 / spec 缺失 / 访问异常时一律 True。"""
    monkeypatch.setattr(
        team_helpers, "get_team_manager", lambda channel_id: _RecoveryTestManager()
    )
    assert team_helpers._round_failed_recovery_enabled("any-session") is True

    def _boom(channel_id):
        raise RuntimeError("team manager not ready")

    monkeypatch.setattr(team_helpers, "get_team_manager", _boom)
    assert team_helpers._round_failed_recovery_enabled("any-session") is True
