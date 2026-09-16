# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""plain chat 的 clear_interrupt 可 defer_to_runner，避免热路径双读 checkpoint."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "openjiuwen.core.context_engine.schema.config",
    reason="openjiuwen API mismatch in local venv",
)

try:
    from openjiuwen.core.context_engine.schema.config import CompressionRecallConfig  # noqa: F401
except ImportError:
    pytest.skip(
        "openjiuwen lacks CompressionRecallConfig; skip interface_deep import tests",
        allow_module_level=True,
    )

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod


def test_plain_chat_should_clear_skips_answers_and_heartbeat() -> None:
    plain = AgentRequest(
        request_id="r1",
        channel_id="web",
        session_id="s1",
        params={"query": "hello"},
    )
    assert deep_mod.JiuWenSwarmDeepAdapter._plain_chat_should_clear_stale_interrupt(plain)

    with_answers = AgentRequest(
        request_id="r2",
        channel_id="web",
        session_id="s1",
        params={"query": "x", "answers": [{"id": "a"}]},
    )
    assert not deep_mod.JiuWenSwarmDeepAdapter._plain_chat_should_clear_stale_interrupt(
        with_answers
    )

    heartbeat = AgentRequest(
        request_id="r3",
        channel_id="web",
        session_id="heartbeat-1",
        params={"query": "ping"},
    )
    assert not deep_mod.JiuWenSwarmDeepAdapter._plain_chat_should_clear_stale_interrupt(
        heartbeat
    )


@pytest.mark.asyncio
async def test_clear_interrupt_defer_sets_contextvar_only() -> None:
    deep_mod._DEFERRED_INTERRUPT_CLEAR.set(None)
    adapter = MagicMock()
    adapter._instance = object()
    clear = deep_mod.JiuWenSwarmDeepAdapter._clear_session_persisted_interrupt_state

    await clear(
        adapter,
        "sess-1",
        reason="plain_user_message_before_agent_run",
        defer_to_runner=True,
    )

    spec = deep_mod._DEFERRED_INTERRUPT_CLEAR.get()
    assert spec is not None
    assert spec["session_id"] == "sess-1"
    assert spec["reason"] == "plain_user_message_before_agent_run"
    assert "adapter" not in spec
    deep_mod._DEFERRED_INTERRUPT_CLEAR.set(None)


@pytest.mark.asyncio
async def test_flush_deferred_interrupt_on_early_exit_runs_sync(monkeypatch) -> None:
    deep_mod._DEFERRED_INTERRUPT_CLEAR.set(
        {
            "reason": "plain_user_message_before_agent_run",
            "clear_todo_resume_snapshot_pending": False,
            "session_id": "sess-early",
        }
    )
    adapter = MagicMock()
    adapter._instance = object()
    calls: list[dict] = []

    async def _fake_clear(
        self,
        session_id,
        *,
        reason,
        clear_todo_resume_snapshot_pending=False,
        defer_to_runner=False,
    ):
        calls.append(
            {
                "session_id": session_id,
                "reason": reason,
                "defer_to_runner": defer_to_runner,
                "clear_todo_resume_snapshot_pending": clear_todo_resume_snapshot_pending,
            }
        )

    monkeypatch.setattr(
        deep_mod.JiuWenSwarmDeepAdapter,
        "_clear_session_persisted_interrupt_state",
        _fake_clear,
    )

    await deep_mod.JiuWenSwarmDeepAdapter._flush_deferred_interrupt_clear_on_early_exit(
        adapter
    )

    assert deep_mod._DEFERRED_INTERRUPT_CLEAR.get() is None
    assert len(calls) == 1
    assert calls[0]["session_id"] == "sess-early"
    assert calls[0]["defer_to_runner"] is False
    assert calls[0]["reason"].endswith("_early_exit_sync")


def test_clear_request_agent_perf_pops_timings() -> None:
    from jiuwenswarm.server.runtime import agent_perf as ap

    ap.reset_for_tests()
    ap.bind_request("req-early")
    assert "req-early" in ap._TIMINGS
    deep_mod._clear_request_agent_perf("req-early")
    assert "req-early" not in ap._TIMINGS
    ap.reset_for_tests()
