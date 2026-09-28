# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for interrupt handling semantics in interface facade."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface import (
    _is_ask_user_answer_resume,
    _should_record_user_history,
)
from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod


class _InterruptHarness(JiuWenSwarm):
    @property
    def session_manager_for_test(self):
        return getattr(self, "_session_manager")

    async def process_interrupt_for_test(self, request: AgentRequest):
        return await getattr(self, "_process_interrupt")(request)


class _FakeTeamManager:
    def __init__(self, pause_result: bool = True, cancel_result: bool = True) -> None:
        self.pause_result = pause_result
        self.cancel_result = cancel_result
        self.pause_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.cancel_dispositions: list[str | None] = []

    async def pause_session_runtime(self, session_id: str, reason: str = "") -> bool:
        self.pause_calls.append((session_id, reason))
        return self.pause_result

    async def cancel_session_runtime(
        self,
        session_id: str,
        reason: str = "",
        *,
        workflow_disposition: str | None = None,
    ) -> bool:
        self.cancel_calls.append((session_id, reason))
        self.cancel_dispositions.append(workflow_disposition)
        return self.cancel_result


def _build_team_interrupt_request(
    intent: str,
    *,
    mode: str = "team",
    team: bool = True,
    metadata: dict | None = None,
) -> AgentRequest:
    params = {
        "intent": intent,
        "mode": mode,
    }
    if team:
        params["team"] = True
    return AgentRequest(
        request_id=f"req-{intent}",
        channel_id="web",
        session_id="team-session-1",
        req_method=ReqMethod.CHAT_CANCEL,
        params=params,
        metadata=metadata,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "expected_message"),
    [
        ("pause", "团队已暂停"),
        ("cancel", "团队当前执行已结束"),
    ],
)
async def test_team_interrupt_pause_like_intents_use_team_manager(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    expected_message: str,
) -> None:
    claw = _InterruptHarness()
    fake_manager = _FakeTeamManager(pause_result=True)
    cancelled: list[tuple[str, str, float | None]] = []

    def _unexpected_adapter():
        raise AssertionError("team interrupt should not use deep adapter interrupt path")

    async def _fake_cancel_session_task(
        session_id: str,
        reason: str = "",
        wait_timeout: float | None = None,
    ) -> None:
        cancelled.append((session_id, reason, wait_timeout))

    monkeypatch.setattr(claw, "_ensure_adapter", _unexpected_adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id=None: fake_manager,
    )
    monkeypatch.setattr(claw.session_manager_for_test, "cancel_session_task", _fake_cancel_session_task)

    response = await claw.process_interrupt_for_test(_build_team_interrupt_request(intent))

    assert response.payload == {
        "event_type": "chat.interrupt_result",
        "intent": intent,
        "success": True,
        "message": expected_message,
    }
    assert cancelled == [("team-session-1", f"interrupt(intent={intent}): ", 5.0)]
    if intent == "pause":
        assert fake_manager.pause_calls == [("team-session-1", f"interrupt(intent={intent}): ")]
        assert fake_manager.cancel_calls == []
    else:
        assert fake_manager.cancel_calls == [("team-session-1", f"interrupt(intent={intent}): ")]
        assert fake_manager.pause_calls == []


@pytest.mark.asyncio
async def test_team_interrupt_resume_is_ack_only(monkeypatch: pytest.MonkeyPatch) -> None:
    claw = _InterruptHarness()
    fake_manager = _FakeTeamManager(pause_result=True)
    cancelled: list[tuple[str, str, float | None]] = []

    def _unexpected_adapter():
        raise AssertionError("team resume should not use deep adapter interrupt path")

    async def _fake_cancel_session_task(
        session_id: str,
        reason: str = "",
        wait_timeout: float | None = None,
    ) -> None:
        cancelled.append((session_id, reason, wait_timeout))

    monkeypatch.setattr(claw, "_ensure_adapter", _unexpected_adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id=None: fake_manager,
    )
    monkeypatch.setattr(claw.session_manager_for_test, "cancel_session_task", _fake_cancel_session_task)

    response = await claw.process_interrupt_for_test(_build_team_interrupt_request("resume"))

    assert response.payload == {
        "event_type": "chat.interrupt_result",
        "intent": "resume",
        "success": True,
        "message": "团队暂停后，直接发送下一条消息即可继续。",
    }
    assert cancelled == []
    assert fake_manager.pause_calls == []
    assert fake_manager.cancel_calls == []


@pytest.mark.asyncio
async def test_code_team_interrupt_uses_team_manager_without_team_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claw = _InterruptHarness()
    fake_manager = _FakeTeamManager(pause_result=True)
    cancelled: list[tuple[str, str, float | None]] = []

    def _unexpected_adapter():
        raise AssertionError("code.team interrupt should not use deep adapter interrupt path")

    async def _fake_cancel_session_task(
        session_id: str,
        reason: str = "",
        wait_timeout: float | None = None,
    ) -> None:
        cancelled.append((session_id, reason, wait_timeout))

    monkeypatch.setattr(claw, "_ensure_adapter", _unexpected_adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id=None: fake_manager,
    )
    monkeypatch.setattr(claw.session_manager_for_test, "cancel_session_task", _fake_cancel_session_task)

    response = await claw.process_interrupt_for_test(
        _build_team_interrupt_request("pause", mode="code.team", team=False)
    )

    assert response.payload == {
        "event_type": "chat.interrupt_result",
        "intent": "pause",
        "success": True,
        "message": "团队已暂停",
    }
    assert cancelled == [("team-session-1", "interrupt(intent=pause): ", 5.0)]
    assert fake_manager.pause_calls == [("team-session-1", "interrupt(intent=pause): ")]
    assert fake_manager.cancel_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "expected_disposition"),
    [
        # 断连兜底（client_disconnect）落 pause 章，可冷启动续跑
        ({E2A_INTERNAL_CANCEL_SOURCE_KEY: E2A_CANCEL_SOURCE_CLIENT_DISCONNECT}, "pause"),
        # 用户主动终止 / 无来源：默认落 seal（stop）
        (None, "stop"),
        ({}, "stop"),
        ({E2A_INTERNAL_CANCEL_SOURCE_KEY: "some_other_source"}, "stop"),
    ],
)
async def test_team_cancel_workflow_disposition_from_cancel_source(
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict | None,
    expected_disposition: str,
) -> None:
    claw = _InterruptHarness()
    fake_manager = _FakeTeamManager(cancel_result=True)

    def _unexpected_adapter():
        raise AssertionError("team interrupt should not use deep adapter interrupt path")

    async def _fake_cancel_session_task(
        session_id: str,
        reason: str = "",
        wait_timeout: float | None = None,
    ) -> None:
        pass

    monkeypatch.setattr(claw, "_ensure_adapter", _unexpected_adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id=None: fake_manager,
    )
    monkeypatch.setattr(claw.session_manager_for_test, "cancel_session_task", _fake_cancel_session_task)

    request = _build_team_interrupt_request("cancel", metadata=metadata)
    response = await claw.process_interrupt_for_test(request)

    assert response.payload["event_type"] == "chat.interrupt_result"
    assert response.payload["intent"] == "cancel"
    assert response.payload["success"] is True
    assert fake_manager.cancel_calls == [("team-session-1", "interrupt(intent=cancel): ")]
    assert fake_manager.cancel_dispositions == [expected_disposition]


def test_is_ask_user_answer_resume_identifies_ask_user_interrupt_answer_payload():
    # 问题澄清答案 resume：source=ask_user_interrupt 且带非空 answers。
    # 这类 payload 被 _should_record_user_history 排除（不写 user 消息），
    # 但答案必须以 chat.ask_user_answer assistant 记录落盘。
    # 该识别函数是流式路径 process_message_stream 里答案落盘分支的守门员——
    # 若它误判，答案永远不落盘，已答问题刷新后又弹成实时交互框（卡在确认位置）。
    resume_params = {
        "source": "ask_user_interrupt",
        "request_id": "req-original-123",
        "answers": [{"question": "用哪种方案?", "selected_options": ["方案A"]}],
        "mode": "agent",
    }
    assert _is_ask_user_answer_resume(resume_params) is True
    # ask_user_interrupt resume 被 _should_record_user_history 排除（不写 user 消息），
    # 但需要走答案落盘分支——两者必须互补，不能都 False。
    assert _should_record_user_history(resume_params) is False

    # 空 answers 列表不识别（无答案可落盘）。
    assert _is_ask_user_answer_resume(
        {"source": "ask_user_interrupt", "request_id": "r", "answers": []}
    ) is False

    # 非 ask_user_interrupt 不识别（permission/confirm/evolution 各有别的落盘路径）。
    assert _is_ask_user_answer_resume(
        {"source": "permission_interrupt", "request_id": "r", "answers": [{"selected_options": ["x"]}]}
    ) is False

    # 缺 source 不识别。
    assert _is_ask_user_answer_resume(
        {"request_id": "r", "answers": [{"selected_options": ["x"]}]}
    ) is False

    # 非 dict 入参不识别。
    assert _is_ask_user_answer_resume(None) is False
    assert _is_ask_user_answer_resume("not-a-dict") is False


@pytest.mark.asyncio
async def test_deliver_control_input_persists_ask_user_answer_with_original_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 问题澄清答案 resume 走的是 control-input 路径（Runtime.session_work_kind 判定为
    # CONTROL_INPUT → _deliver_control → JiuWenSwarm.deliver_control_input），**不经过**
    # process_message_stream。所以答案落盘必须在 deliver_control_input 里，否则刷新后
    # chat.ask_user_question 找不到配对的 chat.ask_user_answer，已答问题又弹成实时
    # 交互框（卡在确认位置）。
    #
    # 本测试用 monkeypatch 把 deliver_control_input 的所有外部依赖替身掉，只验证它确实
    # 调了 append_history_record，且 request_id 用的是 params.request_id（原问题那一轮
    # 的 rid），而非本轮信封 id（request.request_id 是前端为这次 resume 新生成的 req_xxx）。
    claw = _InterruptHarness()

    # 替身 _ensure_adapter：deliver_control_input 第一步就会调它，返回一个哨兵即可。
    sentinel_adapter = object()
    monkeypatch.setattr(claw, "_ensure_adapter", lambda mode=None: sentinel_adapter)

    # 替身 session_manager.get_session_id：直接透传，便于断言落盘用的 session_id。
    fake_session_manager = type("SM", (), {})()
    fake_session_manager.get_session_id = lambda sid: sid or "web-sess-1"
    monkeypatch.setattr(claw, "_session_manager", fake_session_manager)

    # 替身 _build_inputs：同步返回最小三元组，不真正解析。
    def _fake_build_inputs(_request):
        return ({}, "local", None)
    monkeypatch.setattr(claw, "_build_inputs", _fake_build_inputs)

    # 替身 reconcile_session_mcp：no-op。
    async def _fake_reconcile(_sid, _mcp, *, model_name=None, history_before_request_id=None):
        return None
    monkeypatch.setattr(claw, "reconcile_session_mcp", _fake_reconcile)

    # 替身 adapter.process_message_stream_impl：产出一个 chat.final chunk（模拟
    # resume 后 LLM 的回复），验证它也被落盘。注意它会被当成 bound method 调用
    # （adapter.process_message_stream_impl 形式），所以第一个形参收 self。
    async def _fake_stream_impl(_self, _request, _inputs):
        yield AgentResponseChunk(
            request_id="req-resume-new-456",
            channel_id="web",
            payload={"event_type": "chat.final", "content": "你选择了方案A"},
            is_complete=True,
        )
    class _FakeAdapter:
        process_message_stream_impl = _fake_stream_impl
    monkeypatch.setattr(claw, "_ensure_adapter", lambda mode=None: _FakeAdapter())

    # 捕获 append_history_record 调用（经 _run_history_io 透传）。
    captured: list[dict] = []

    def _fake_append(**kwargs):
        captured.append(kwargs)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface.append_history_record",
        _fake_append,
    )

    # resume 请求：本轮信封 id = req-resume-new-456，原问题 rid = req-original-123。
    resume_request = AgentRequest(
        request_id="req-resume-new-456",
        channel_id="web",
        session_id="web-sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "source": "ask_user_interrupt",
            "request_id": "req-original-123",
            "answers": [{"question": "用哪种方案?", "selected_options": ["方案A"]}],
            "mode": "agent",
        },
    )

    # 消费完 generator，触发落盘分支。
    async for _ in claw.deliver_control_input(resume_request):
        pass

    # 必须落盘 2 条 assistant 记录：
    # 1) chat.ask_user_answer（答案，用原问题 rid）
    # 2) chat.final（LLM 回复，用本轮信封 rid）
    assert len(captured) == 2, f"expected 2 persists, got {len(captured)}: {captured}"

    answer_record = next(r for r in captured if r["event_type"] == "chat.ask_user_answer")
    assert answer_record["session_id"] == "web-sess-1"
    assert answer_record["role"] == "assistant"
    assert answer_record["content"] == ""
    # 关键：chat.ask_user_answer 的 request_id 必须用原问题那一轮的 rid，而非本轮信封 id。
    assert answer_record["request_id"] == "req-original-123"
    assert answer_record["extra"]["request_id"] == "req-original-123"
    assert answer_record["extra"]["source"] == "ask_user_interrupt"
    assert answer_record["extra"]["answers"] == [
        {"question": "用哪种方案?", "selected_options": ["方案A"]},
    ]

    final_record = next(r for r in captured if r["event_type"] == "chat.final")
    assert final_record["session_id"] == "web-sess-1"
    assert final_record["role"] == "assistant"
    assert final_record["content"] == "你选择了方案A"
    # chat.final 用本轮信封 rid（与 process_message_stream 一致：这轮 LLM 回复归这轮）。
    assert final_record["request_id"] == "req-resume-new-456"


def _build_control_input_harness(claw: _InterruptHarness, monkeypatch: pytest.MonkeyPatch):
    """装配 deliver_control_input 的最小依赖替身（adapter / session_manager / inputs / mcp）。

    返回 (captured, set_fake_stream) —— set_fake_stream(fn) 让测试自定义 fake stream 产出，
    以复现 chat.delta / 空 chat.final / 无 final 等场景。
    """
    monkeypatch.setattr(claw, "_build_inputs", lambda _request: ({}, "local", None))

    async def _fake_reconcile(_sid, _mcp, *, model_name=None, history_before_request_id=None):
        return None
    monkeypatch.setattr(claw, "reconcile_session_mcp", _fake_reconcile)

    fake_session_manager = type("SM", (), {})()
    fake_session_manager.get_session_id = lambda sid: sid or "web-sess-1"
    monkeypatch.setattr(claw, "_session_manager", fake_session_manager)

    captured: list[dict] = []

    def _fake_append(**kwargs):
        captured.append(kwargs)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface.append_history_record",
        _fake_append,
    )

    def set_fake_stream(fn):
        class _FakeAdapter:
            process_message_stream_impl = fn
        monkeypatch.setattr(claw, "_ensure_adapter", lambda mode=None: _FakeAdapter())

    return captured, set_fake_stream


def _ask_user_answer_resume_request(answers: list[dict] | None = None) -> AgentRequest:
    return AgentRequest(
        request_id="req-resume-new-456",
        channel_id="web",
        session_id="web-sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "source": "ask_user_interrupt",
            "request_id": "req-original-123",
            "answers": answers if answers is not None else [
                {"question": "用哪种方案?", "selected_options": ["方案A"]},
            ],
            "mode": "agent",
        },
    )


@pytest.mark.asyncio
async def test_deliver_control_input_delta_chunks_are_not_persisted_but_merged_into_empty_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 复现 HIGH bug：control-input 路径若误落盘每个 chat.delta，空 chat.final 场景
    # 正文只在 delta 里，刷新后 delta 被过滤 + 空 final 被空壳规则丢弃 → 整段回复消失。
    # 期望：chat.delta 不落盘，累积文本在空 final 时合并成一条 chat.final 落盘。
    claw = _InterruptHarness()
    captured, set_fake_stream = _build_control_input_harness(claw, monkeypatch)

    async def _fake_stream_impl(_self, _request, _inputs):
        # 模拟 LLM 流式回复：3 个 delta + 1 个空 final（正文全在 delta 里）。
        for piece in ("你好", "，", "我选方案A"):
            yield AgentResponseChunk(
                request_id="req-resume-new-456",
                channel_id="web",
                payload={"event_type": "chat.delta", "content": piece},
                is_complete=False,
            )
        yield AgentResponseChunk(
            request_id="req-resume-new-456",
            channel_id="web",
            payload={"event_type": "chat.final", "content": ""},
            is_complete=True,
        )
    set_fake_stream(_fake_stream_impl)

    async for _ in claw.deliver_control_input(_ask_user_answer_resume_request()):
        pass

    # 1) chat.ask_user_answer（答案，原问题 rid）
    # 2) chat.final（合并 delta 正文，本轮信封 rid）
    assert len(captured) == 2, f"expected 2 persists, got {len(captured)}: {captured}"

    answer_record = next(r for r in captured if r["event_type"] == "chat.ask_user_answer")
    assert answer_record["request_id"] == "req-original-123"

    final_record = next(r for r in captured if r["event_type"] == "chat.final")
    # 关键：3 个 delta 文本被合并成一条 final 正文，而非落盘成 3 条 delta 记录。
    assert final_record["content"] == "你好，我选方案A"
    assert final_record["request_id"] == "req-resume-new-456"
    # 确保没有任何 chat.delta 被落盘。
    assert not any(r["event_type"] == "chat.delta" for r in captured)


@pytest.mark.asyncio
async def test_deliver_control_input_non_empty_final_clears_pending_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 非空 chat.final：正文由 final 承载，前面累积的 delta 不再合并落盘（避免重复）。
    # 同时验证 timestamp 语义对齐 pms：非空 final 传 segment_started_at（首个 delta 时刻），
    # _resolve_final_record_timestamp 据此把 completed_at 存入 extra、timestamp 用起始时刻。
    # 用单调递增 time.time 序列确保 segment_started_at < completed_at（否则该函数走
    # >= 早退分支、不 setdefault completed_at，断言失效）。
    import jiuwenswarm.server.runtime.agent_adapter.interface as _iface
    _t = iter([1000.0, 1000.1, 1000.2, 1000.3, 1000.4, 1000.5, 1000.6, 1000.7])
    monkeypatch.setattr(_iface.time, "time", lambda: next(_t))

    claw = _InterruptHarness()
    captured, set_fake_stream = _build_control_input_harness(claw, monkeypatch)

    async def _fake_stream_impl(_self, _request, _inputs):
        yield AgentResponseChunk(
            request_id="req-resume-new-456", channel_id="web",
            payload={"event_type": "chat.delta", "content": "这段会被丢弃"},
            is_complete=False,
        )
        yield AgentResponseChunk(
            request_id="req-resume-new-456", channel_id="web",
            payload={"event_type": "chat.final", "content": "最终回复正文"},
            is_complete=True,
        )
    set_fake_stream(_fake_stream_impl)

    async for _ in claw.deliver_control_input(_ask_user_answer_resume_request()):
        pass

    assert len(captured) == 2, f"got {len(captured)}: {captured}"
    final_record = next(r for r in captured if r["event_type"] == "chat.final")
    # 非空 final 落盘自身正文，累积的 delta 被清空、不合并落盘。
    assert final_record["content"] == "最终回复正文"
    assert not any(r["event_type"] == "chat.delta" for r in captured)
    # timestamp 语义：segment_started_at 被透传 → completed_at 存入 extra。
    extra = final_record.get("extra") or {}
    assert "completed_at" in extra, (
        f"non-empty final missing completed_at (segment_started_at not propagated): {extra}"
    )


@pytest.mark.asyncio
async def test_deliver_control_input_pending_deltas_flushed_at_stream_end_without_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 不发 chat.final 的场景（如 Goal 中间态）：流结束时把累积 delta 兜底合并落盘。
    claw = _InterruptHarness()
    captured, set_fake_stream = _build_control_input_harness(claw, monkeypatch)

    async def _fake_stream_impl(_self, _request, _inputs):
        yield AgentResponseChunk(
            request_id="req-resume-new-456", channel_id="web",
            payload={"event_type": "chat.delta", "content": "只发delta"},
            is_complete=False,
        )
        # 流直接结束，不发 chat.final。
    set_fake_stream(_fake_stream_impl)

    async for _ in claw.deliver_control_input(_ask_user_answer_resume_request()):
        pass

    assert len(captured) == 2, f"got {len(captured)}: {captured}"
    final_record = next(r for r in captured if r["event_type"] == "chat.final")
    # 流末兜底：累积的 delta 合并成一条 chat.final 落盘。
    assert final_record["content"] == "只发delta"
    assert not any(r["event_type"] == "chat.delta" for r in captured)


@pytest.mark.asyncio
async def test_deliver_control_input_interleaved_delta_tool_call_and_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 交错流：delta → tool_call（边界）→ delta → 空 final。
    # 验证：① 边界处把前段 delta 合并落盘成一条 final；② tool_call 本身也落盘
    # （边界合并后不 return，继续落盘事件本身——对齐 pms）；③ 边界后新的 delta
    # 在空 final 时再合并落盘成第二条 final；④ 全程无 chat.delta 落盘。
    claw = _InterruptHarness()
    captured, set_fake_stream = _build_control_input_harness(claw, monkeypatch)

    async def _fake_stream_impl(_self, _request, _inputs):
        # 前段 delta（应在 tool_call 边界合并落盘）
        yield AgentResponseChunk(
            request_id="req-resume-new-456", channel_id="web",
            payload={"event_type": "chat.delta", "content": "前段回答"},
            is_complete=False,
        )
        # tool_call 边界：触发前段 delta 合并落盘，且 tool_call 本身也落盘
        yield AgentResponseChunk(
            request_id="req-resume-new-456", channel_id="web",
            payload={"event_type": "chat.tool_call", "content": "",
                     "tool_call": {"name": "search", "input": {"q": "x"}}},
            is_complete=False,
        )
        # 后段 delta（应在空 final 时合并落盘）
        yield AgentResponseChunk(
            request_id="req-resume-new-456", channel_id="web",
            payload={"event_type": "chat.delta", "content": "后段回答"},
            is_complete=False,
        )
        # 空 final：触发后段 delta 合并落盘
        yield AgentResponseChunk(
            request_id="req-resume-new-456", channel_id="web",
            payload={"event_type": "chat.final", "content": ""},
            is_complete=True,
        )
    set_fake_stream(_fake_stream_impl)

    async for _ in claw.deliver_control_input(_ask_user_answer_resume_request()):
        pass

    # 预期落盘 4 条：
    # 1) chat.ask_user_answer（答案，原问题 rid）
    # 2) chat.final（前段 delta 合并）
    # 3) chat.tool_call（边界事件本身）
    # 4) chat.final（后段 delta 合并）
    assert len(captured) == 4, f"expected 4 persists, got {len(captured)}: {captured}"

    answer = next(r for r in captured if r["event_type"] == "chat.ask_user_answer")
    assert answer["request_id"] == "req-original-123"

    finals = [r for r in captured if r["event_type"] == "chat.final"]
    assert len(finals) == 2, f"expected 2 finals, got {len(finals)}"
    # 按落盘顺序：前段 final 在 tool_call 之前，后段 final 在 tool_call 之后
    assert finals[0]["content"] == "前段回答"
    assert finals[1]["content"] == "后段回答"

    tool_call = next(r for r in captured if r["event_type"] == "chat.tool_call")
    assert tool_call["request_id"] == "req-resume-new-456"
    # tool_call 载荷在 extra 里（payload 除 event_type/content 外的字段进 extra）
    assert tool_call["extra"]["tool_call"] == {"name": "search", "input": {"q": "x"}}

    # 全程无 chat.delta 落盘
    assert not any(r["event_type"] == "chat.delta" for r in captured)


@pytest.mark.asyncio
async def test_deliver_control_input_merged_final_does_not_leak_ask_user_interrupt_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 合并落盘的 chat.final 不应透传 source=ask_user_interrupt（resume 信封参数）。
    # pms 的 source 透传面向 proactive_recommendation 等正常来源；control 路径的
    # source 是中断答案语义标签，泄到回复气泡会导致前端误判来源。
    claw = _InterruptHarness()
    captured, set_fake_stream = _build_control_input_harness(claw, monkeypatch)

    async def _fake_stream_impl(_self, _request, _inputs):
        yield AgentResponseChunk(
            request_id="req-resume-new-456", channel_id="web",
            payload={"event_type": "chat.delta", "content": "回复正文"},
            is_complete=False,
        )
        yield AgentResponseChunk(
            request_id="req-resume-new-456", channel_id="web",
            payload={"event_type": "chat.final", "content": ""},
            is_complete=True,
        )
    set_fake_stream(_fake_stream_impl)

    async for _ in claw.deliver_control_input(_ask_user_answer_resume_request()):
        pass

    final_record = next(r for r in captured if r["event_type"] == "chat.final")
    extra = final_record.get("extra") or {}
    # source 不应泄到合并 final 的 extra
    assert extra.get("source") != "ask_user_interrupt", (
        f"merged final leaked ask_user_interrupt source: {extra}"
    )

