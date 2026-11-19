# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""权限判定（``chat.permission_decision``）暂存 / 下发 / 解析 用例.

覆盖：
- ``permission_decisions`` 暂存/取出（allow/deny、按 tool_call_id、ask 忽略、无 extra 容错）
- ``StreamEventRail._emit_permission_decision`` 的帧形态（类型/载荷；ask 不下发）
- ``after_tool_call`` 消费暂存并在工具结果时刻下发
- ``interface_deep`` 对 ``chat.permission_decision`` 的解析往返
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.permission_decisions import (
    PERMISSION_DECISIONS_EXTRA_KEY,
    pop_permission_decision,
    stash_permission_decision,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class _FakeSession:
    def __init__(self) -> None:
        self.frames: list[OutputSchema] = []

    async def write_stream(self, frame: OutputSchema) -> None:
        self.frames.append(frame)


def _ctx() -> AgentCallbackContext:
    ctx = AgentCallbackContext(agent=object())
    ctx.extra = {}
    return ctx


def test_stash_and_pop_by_tool_call_id() -> None:
    ctx = _ctx()
    tc = SimpleNamespace(id="tc-1", name="read_file")
    stash_permission_decision(ctx, tc, "deny", matched_rule="file_guard:glob", reason="denied")

    assert PERMISSION_DECISIONS_EXTRA_KEY in ctx.extra
    assert pop_permission_decision(ctx, "tc-1") == {
        "tool_name": "read_file",
        "tool_call_id": "tc-1",
        "decision": "deny",
        "matched_rule": "file_guard:glob",
        "reason": "denied",
    }
    # 取出后不再重复下发
    assert pop_permission_decision(ctx, "tc-1") is None


def test_stash_ignores_ask_and_missing_extra() -> None:
    ctx = _ctx()
    stash_permission_decision(ctx, SimpleNamespace(id="t", name="bash"), "ask")
    stash_permission_decision(ctx, SimpleNamespace(id="u", name="bash"), "undetermined")
    assert PERMISSION_DECISIONS_EXTRA_KEY not in ctx.extra

    # 回调边界可能不带 extra：不抛异常
    stash_permission_decision(SimpleNamespace(), SimpleNamespace(id="v", name="x"), "allow")


@pytest.mark.asyncio
async def test_emit_permission_decision_frame() -> None:
    session = _FakeSession()
    ok = await JiuSwarmStreamEventRail._emit_permission_decision(
        session,
        tool_name="read_file",
        tool_call_id="tc-1",
        decision="deny",
        matched_rule="file_guard:glob",
        reason="denied",
    )
    assert ok is True
    assert len(session.frames) == 1
    frame = session.frames[0]
    assert frame.type == "chat.permission_decision"
    assert frame.payload["decision"] == "deny"
    assert frame.payload["tool_name"] == "read_file"
    assert frame.payload["matched_rule"] == "file_guard:glob"


@pytest.mark.asyncio
async def test_emit_permission_decision_skips_ask() -> None:
    session = _FakeSession()
    emitted = await JiuSwarmStreamEventRail._emit_permission_decision(session, decision="ask")
    assert emitted is False
    assert session.frames == []


@pytest.mark.asyncio
async def test_after_tool_call_consumes_and_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    rail = JiuSwarmStreamEventRail()
    session = _FakeSession()
    tc = SimpleNamespace(id="tc-9", name="read_file")
    ctx = _ctx()
    ctx.session = session
    ctx.inputs = ToolCallInputs(
        tool_call=tc,
        tool_name="read_file",
        tool_result="[PERMISSION_DENIED] denied",
    )
    stash_permission_decision(ctx, tc, "deny", matched_rule="file_guard:glob", reason="denied")

    async def _noop(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(rail, "_emit_tool_result", _noop)
    monkeypatch.setattr(rail, "_emit_ask_user_question_if_interrupted", _noop)
    monkeypatch.setattr(rail, "_task_group_action", lambda _ctx: None)
    monkeypatch.setattr(rail._symphony_stream_handler, "reset_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(rail._symphony_stream_handler, "request_force_finish", lambda *_a, **_k: None)

    await rail.after_tool_call(ctx)

    frames = [f for f in session.frames if getattr(f, "type", None) == "chat.permission_decision"]
    assert len(frames) == 1
    assert frames[0].payload["decision"] == "deny"
    assert pop_permission_decision(ctx, "tc-9") is None


def test_parse_permission_decision_roundtrip() -> None:
    frame = OutputSchema(
        type="chat.permission_decision",
        index=0,
        payload={
            "tool_name": "read_file",
            "tool_call_id": "tc-1",
            "decision": "deny",
            "matched_rule": "file_guard:glob",
            "reason": "denied",
        },
    )
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(frame)
    assert parsed is not None
    assert parsed["event_type"] == "chat.permission_decision"
    assert parsed["decision"] == "deny"
    assert parsed["tool_name"] == "read_file"
    assert parsed["matched_rule"] == "file_guard:glob"