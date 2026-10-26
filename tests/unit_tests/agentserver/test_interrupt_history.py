# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""人工干预（HITL interrupt）时 history.jsonl 记录回归。

三个子问题：
1. before_tool_call 被 interrupt rail 中断后，agent-core 的 rail 装饰器仍会触发
   after_tool_call；修复后中断态不再发射空 tool_result chunk。
2. 中断边界（chat.ask_user_question / harness.activate_interaction）结束本轮
   输出且没有收尾 chat.final；修复前 consumer 只在 chat.tool_call 上冲刷
   pending 正文，中断前流出的正文整段不落盘。
3. resume 重放完整 rail 周期，同一 tool_call_id 再次进入 before_tool_call；
   修复前会重复发射 tool_call/tool_update chunk，history.jsonl 出现重复记录。
"""
from __future__ import annotations

import json
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, List

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.session import session_history
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
    _extract_tool_interrupt,
)
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.session.interaction.base import AgentInterrupt
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.ability_manager import AbilityExecutionError
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import InvokeInputs, ToolCallInputs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StreamSession:
    def __init__(self) -> None:
        self.chunks: List[Any] = []

    async def write_stream(self, output: Any) -> None:
        self.chunks.append(output)

    def get_state(self, _key: str) -> Any:
        return None


def _make_interrupt(tool_call_id: str) -> ToolInterruptException:
    return ToolInterruptException(
        request=InterruptRequest(
            message="need confirmation",
            metadata={"tool_call_id": tool_call_id},
        ),
        tool_call=SimpleNamespace(id=tool_call_id, name="ask_user", arguments={}),
    )


def _ctx(
    session: _StreamSession,
    tool_name: str,
    tool_call_id: str = "call-1",
    tool_result: Any = None,
    exception: BaseException | None = None,
    extra: dict[str, Any] | None = None,
):
    tool_call = SimpleNamespace(id=tool_call_id, name=tool_name, arguments={})
    return SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args={},
            tool_result=tool_result,
        ),
        extra=extra if extra is not None else {},
        exception=exception,
    )


def _chunk_types(session: _StreamSession) -> List[str]:
    return [str(getattr(c, "type", "")) for c in session.chunks]


# ---------------------------------------------------------------------------
# Fix 1: after_tool_call 中断时不发空 tool_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_tool_call_skips_tool_result_when_interrupted_via_exception():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _ctx(
        session,
        "ask_user",
        tool_result=None,
        exception=AbortError(reason="interrupted", cause=_make_interrupt("call-1")),
    )

    await rail.after_tool_call(ctx)

    # 无 tool_result chunk；ask_user 的中断问题卡片仍然要发。
    assert "tool_result" not in _chunk_types(session)
    assert "chat.ask_user_question" in _chunk_types(session)


@pytest.mark.asyncio
async def test_after_tool_call_skips_tool_result_when_interrupt_via_result():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    # 有些路径把 ToolInterruptException 作为 result 对象返回。
    ctx = _ctx(session, "ask_user", tool_result=_make_interrupt("call-2"))

    await rail.after_tool_call(ctx)

    assert "tool_result" not in _chunk_types(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, "", False, 0])
async def test_completed_falsy_result_is_not_treated_as_interruption(result: Any):
    session = _StreamSession()
    await JiuSwarmStreamEventRail().after_tool_call(_ctx(session, "tool", tool_result=result))
    assert _chunk_types(session) == ["tool_result"]


@pytest.mark.asyncio
async def test_terminal_execution_error_with_interrupt_cause_still_emits_result():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    error = AbilityExecutionError(
        StatusCode.AGENT_TOOL_EXECUTION_ERROR,
        msg="Tool execution error",
        tool_message=ToolMessage(content="Tool execution error", tool_call_id="call-1"),
    )
    error.__cause__ = AgentInterrupt("confirmation failed")
    ctx = _ctx(session, "interactive_tool", exception=error)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)
    await rail.before_tool_call(_ctx(session, "interactive_tool"))

    assert _chunk_types(session) == [
        "tool_call", "tool_update", "tool_result", "tool_call", "tool_update",
    ]
    result = session.chunks[2].payload["tool_result"]
    assert result["rendered_result"] == "Tool execution error"


@pytest.mark.asyncio
async def test_after_tool_call_still_emits_tool_result_on_normal_completion():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _ctx(session, "list_files", tool_result={"success": True, "files": []})

    await rail.after_tool_call(ctx)

    assert "tool_result" in _chunk_types(session)


@pytest.mark.asyncio
async def test_extract_tool_interrupt_finds_nested_cause():
    interrupt = _make_interrupt("call-3")
    wrapped = AbortError(reason="x", cause=interrupt)
    assert _extract_tool_interrupt(wrapped) is interrupt
    assert _extract_tool_interrupt(None) is None


# ---------------------------------------------------------------------------
# Fix 3: before_tool_call 对同一 tool_call_id 只发射一次
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_tool_call_dedupes_on_resume_replay():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _ctx(session, "web_search", tool_call_id="dup-call")

    await rail.before_tool_call(ctx)
    # resume 重放：同一 tool_call_id 再进一次 before_tool_call。
    await rail.before_tool_call(ctx)

    tool_calls = [c for c in session.chunks if str(c.type) == "tool_call"]
    tool_updates = [c for c in session.chunks if str(c.type) == "tool_update"]
    assert len(tool_calls) == 1
    assert len(tool_updates) == 1


@pytest.mark.asyncio
async def test_before_tool_call_emits_for_distinct_tool_call_ids():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()

    await rail.before_tool_call(_ctx(session, "web_search", tool_call_id="a"))
    await rail.before_tool_call(_ctx(session, "read_file", tool_call_id="b"))

    tool_calls = [c for c in session.chunks if str(c.type) == "tool_call"]
    assert len(tool_calls) == 2


@pytest.mark.asyncio
async def test_cleanup_session_releases_dedup_latch():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()

    await rail.before_tool_call(_ctx(session, "web_search", tool_call_id="c"))
    rail.cleanup_session("default")
    # 会话销毁后同一 id 重新出现（新会话）→ 重新发射。
    await rail.before_tool_call(_ctx(session, "web_search", tool_call_id="c"))

    tool_calls = [c for c in session.chunks if str(c.type) == "tool_call"]
    assert len(tool_calls) == 2


@pytest.mark.asyncio
async def test_stream_cleanup_keeps_inflight_results_for_stop_collection():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    await rail.before_tool_call(_ctx(session, "web_search"))

    rail.cleanup_session("default")
    rail.collect_cancelled_tool_updates("default")

    results = rail.get_cancelled_tool_results("default")
    assert len(results) == 1
    assert results[0]["tool_call_id"] == "call-1"
    assert results[0]["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["suspended", "completed", "unreadable", "aborted", "other_session"])
async def test_adapter_stream_cleanup_releases_session_references(completion: str):
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    other_session = _StreamSession()
    session_ref = weakref.ref(session)
    for sid, current in (("first", session), ("second", other_session)):
        rail._main_sessions[sid] = current
        rail._conversation_ids[sid] = sid
        await rail.before_tool_call(_ctx(current, "web_search", extra={rail._SID_KEY: sid}))
        rail.pause(sid)
        rail._cancelled_tool_results[sid] = [{"tool_call_id": "previous-call"}]
    pause_event = rail._pause_events["first"]
    if completion == "aborted":
        rail._abort_requested["first"] = True

    state_reads: list[str] = []

    def read_state(key: str) -> Any:
        state_reads.append(key)
        if completion in {"unreadable", "aborted"}:
            raise RuntimeError("interruption state unavailable")
        if completion == "completed":
            return None
        return SimpleNamespace(interrupted_tools={"call-1": object()})

    loop_session = SimpleNamespace(
        get_session_id=lambda: "second" if completion == "other_session" else "first",
        get_state=read_state,
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(loop_session=loop_session, _loop_session=loop_session)
    adapter._stream_event_rail = rail
    adapter._active_session_ids = {"first": 1, "second": 1}

    adapter._unmark_session_active("first")

    assert adapter._active_session_ids == {"second": 1}
    assert rail._main_sessions == {"second": other_session}
    assert rail._conversation_ids == {"second": "second"}
    assert set(rail._pause_events) == {"second"}
    assert not rail._pause_events["second"].is_set()
    assert pause_event.is_set()
    assert not rail.is_abort_requested("first")
    assert rail.get_cancelled_tool_results("first") == []
    assert rail.get_cancelled_tool_results("second") == [{"tool_call_id": "previous-call"}]
    assert bool(state_reads) == (completion not in {"aborted", "other_session"})
    assert set(rail._inflight_tool_calls) == {("first", "call-1"), ("second", "call-1")}

    await rail.before_tool_call(_ctx(session, "web_search", extra={rail._SID_KEY: "first"}))
    assert _chunk_types(session).count("tool_call") == (
        1 if completion in {"suspended", "unreadable"} else 2
    )
    del session
    assert session_ref() is None
    rail.collect_cancelled_tool_updates("first")
    assert [result["tool_call_id"] for result in rail.get_cancelled_tool_results("first")] == ["call-1"]
    assert ("second", "call-1") in rail._inflight_tool_calls
    assert ("second", "call-1") in rail._emitted_tool_call_ids


@pytest.mark.asyncio
async def test_reset_for_new_task_keeps_dedup_latch_for_resume():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()

    await rail.before_tool_call(_ctx(session, "ask_user", tool_call_id="d"))
    # interrupt cancel/supplement 走 reset_for_new_task；latch 必须存活，
    # 否则随后的 resume 重放又会重复发射。
    rail.reset_for_new_task("default")
    await rail.before_tool_call(_ctx(session, "ask_user", tool_call_id="d"))

    tool_calls = [c for c in session.chunks if str(c.type) == "tool_call"]
    assert len(tool_calls) == 1


@pytest.mark.asyncio
async def test_normal_completion_releases_dedup_latch():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()

    # 第一轮：发射 → 正常完成（非中断）。
    ctx1 = _ctx(session, "web_search", tool_call_id="seq-0", tool_result={"ok": True})
    await rail.before_tool_call(ctx1)
    await rail.after_tool_call(ctx1)
    # 第二轮：部分提供商的 tool_call_id 按响应重置（call_0...），
    # 同 id 是新的合法调用，必须重新发射而不是被残留 latch 吞掉。
    ctx2 = _ctx(session, "web_search", tool_call_id="seq-0", tool_result={"ok": True})
    await rail.before_tool_call(ctx2)

    tool_calls = [c for c in session.chunks if str(c.type) == "tool_call"]
    assert len(tool_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("other_action", ["pending", "complete", "cleanup"])
async def test_resume_dedup_is_isolated_for_same_tool_id_in_different_sessions(other_action: str):
    rail = JiuSwarmStreamEventRail()
    first = _StreamSession()
    second = _StreamSession()
    first_ctx = _ctx(first, "web_search", extra={rail._SID_KEY: "first"})
    second_ctx = _ctx(second, "web_search", tool_result="done", extra={rail._SID_KEY: "second"})

    await rail.before_tool_call(first_ctx)
    await rail.before_tool_call(second_ctx)
    if other_action == "complete":
        await rail.after_tool_call(second_ctx)
    elif other_action == "cleanup":
        rail.cleanup_session("second")
    await rail.before_tool_call(first_ctx)

    assert _chunk_types(first) == ["tool_call", "tool_update"]
    assert _chunk_types(second).count("tool_call") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("other_action", ["pending", "complete", "interrupt", "cancel"])
async def test_cancelled_results_are_isolated_for_same_tool_id_in_different_sessions(other_action: str):
    rail = JiuSwarmStreamEventRail()
    first = _StreamSession()
    second = _StreamSession()
    first_ctx = _ctx(first, "first_tool", extra={rail._SID_KEY: "first"})
    second_ctx = _ctx(second, "second_tool", tool_result="done", extra={rail._SID_KEY: "second"})
    await rail.before_tool_call(first_ctx)
    await rail.before_tool_call(second_ctx)

    if other_action == "complete":
        await rail.after_tool_call(second_ctx)
    elif other_action == "interrupt":
        second_ctx.inputs.tool_result = None
        second_ctx.exception = _make_interrupt("call-1")
        await rail.after_tool_call(second_ctx)
    elif other_action == "cancel":
        rail.collect_cancelled_tool_updates("second")
    rail.collect_cancelled_tool_updates("first")

    first_results = rail.get_cancelled_tool_results("first")
    assert len(first_results) == 1
    assert first_results[0]["tool_call_id"] == "call-1"
    assert first_results[0]["tool_name"] == "first_tool"
    if other_action == "pending":
        rail.collect_cancelled_tool_updates("second")
    second_results = rail.get_cancelled_tool_results("second")
    assert [result["tool_name"] for result in second_results] == (
        ["second_tool"] if other_action in {"pending", "cancel"} else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("next_action", ["resume", "cancel", "abandon"])
@pytest.mark.parametrize("raw_task", [False, True])
async def test_interrupt_dedup_survives_adapter_stream_cleanup(next_action: str, raw_task: bool):
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    state = {"interrupt": SimpleNamespace(interrupted_tools={"call-1": object()})}
    loop_session = SimpleNamespace(
        get_session_id=lambda: "session-1",
        get_state=lambda _key: state["interrupt"],
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(loop_session=loop_session, _loop_session=loop_session)
    adapter._stream_event_rail = rail
    adapter._active_session_ids = {"session-1": 1}
    extra = {rail._SID_KEY: "session-1"}
    interrupted = _ctx(session, "web_search", exception=_make_interrupt("call-1"), extra=extra)

    await rail.before_tool_call(interrupted)
    await rail.after_tool_call(interrupted)
    adapter._unmark_session_active("session-1")
    assert not adapter._active_session_ids

    if next_action == "resume":
        resume_invoke = SimpleNamespace(
            session=session,
            inputs=InvokeInputs(query=InteractiveInput(), conversation_id="session-1"),
            extra={},
        )
        await rail.before_invoke(resume_invoke)
        try:
            resumed = _ctx(session, "web_search", tool_result="done", extra=resume_invoke.extra)
            await rail.before_tool_call(resumed)
            assert _chunk_types(session) == ["tool_call", "tool_update"]
            await rail.after_tool_call(resumed)
        finally:
            await rail.after_invoke(resume_invoke)
        state["interrupt"] = None
        adapter._unmark_session_active("session-1")
    elif next_action == "cancel":
        rail.abort("session-1")
        rail.collect_cancelled_tool_updates("session-1")
        rail.reset_for_new_task("session-1")
        rail.reset_abort("session-1")
    else:
        state["interrupt"] = None

    invoke = SimpleNamespace(
        session=session,
        inputs=InvokeInputs(
            query=InteractiveInput("new task") if raw_task else "new task",
            conversation_id="session-1",
        ),
        extra={},
    )
    await rail.before_invoke(invoke)
    try:
        await rail.before_tool_call(_ctx(session, "web_search", extra=invoke.extra))
    finally:
        await rail.after_invoke(invoke)
    assert _chunk_types(session).count("tool_call") == 2


# ---------------------------------------------------------------------------
# Fix 2: 中断边界冲刷 pending 正文（facade 消费循环）
# ---------------------------------------------------------------------------


class _ScriptedAdapter:
    """把预设 payload 列表当作 adapter 输出流回放。"""

    def __init__(self, payloads: list[dict[str, Any]], *, as_dict: bool = False) -> None:
        self._payloads = payloads
        self._as_dict = as_dict

    async def create_instance(self, config: dict[str, Any] | None = None) -> None:
        return None

    async def reload_agent_config(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def process_message_impl(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def process_message_stream_impl(
        self, request: AgentRequest, _inputs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        for payload in self._payloads:
            if self._as_dict:
                yield dict(payload)
                continue
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=dict(payload),
                is_complete=False,
            )

    async def process_interrupt(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def reconcile_session_mcp(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def handle_user_answer(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def handle_heartbeat(self, *_args: Any, **_kwargs: Any) -> None:
        return None


async def _run_stream(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, Any]],
    *,
    as_dict: bool = False,
    history_root: Path | None = None,
) -> List[dict[str, Any]]:
    facade = JiuWenSwarm()
    recorded: List[dict[str, Any]] = []
    monkeypatch.setattr(facade, "_adapter", _ScriptedAdapter(payloads, as_dict=as_dict))
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(
        interface_module, "append_history_record", lambda **kwargs: recorded.append(kwargs)
    )
    if history_root is not None:
        from jiuwenswarm.server.runtime.session import lifecycle, session_metadata

        for module in (session_history, session_metadata, lifecycle):
            monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: history_root)
        monkeypatch.setattr(interface_module, "append_history_record", session_history.append_history_record)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)

    request = AgentRequest(
        request_id="req-interrupt-history",
        channel_id="web",
        session_id="interrupt_history_sess",
        params={"query": "hello", "mode": "agent"},
    )
    async for _chunk in facade.process_message_stream(request):
        pass
    if history_root is not None:
        assert session_history.flush_pending_writes()
        path = history_root / request.session_id / "history.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in recorded if r.get("role") == "assistant"]


def _final_contents(records: List[dict[str, Any]]) -> List[str]:
    return [
        str(r.get("content") or "")
        for r in records
        if r.get("event_type") == "chat.final" and str(r.get("content") or "").strip()
    ]


def _ask_user_payload() -> dict[str, Any]:
    return {
        "event_type": "chat.ask_user_question",
        "request_id": "req-interrupt-history",
        "questions": [{"id": "q1", "question": "继续吗？"}],
    }


@pytest.mark.asyncio
async def test_ask_user_interrupt_flushes_pending_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = await _run_stream(
        monkeypatch,
        [
            {"event_type": "chat.delta", "content": "我需要先确认一下"},
            {"event_type": "chat.delta", "content": "你的意图。"},
            _ask_user_payload(),
        ],
    )

    finals = _final_contents(records)
    assert finals == ["我需要先确认一下你的意图。"]


@pytest.mark.asyncio
async def test_activate_interaction_flushes_pending_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = await _run_stream(
        monkeypatch,
        [
            {"event_type": "chat.delta", "content": "准备执行操作"},
            {
                "event_type": "harness.activate_interaction",
                "interaction_type": "activate_confirm",
                "interaction_id": "i-1",
                "options": ["accept", "reject"],
            },
        ],
    )

    finals = _final_contents(records)
    assert finals == ["准备执行操作"]


@pytest.mark.asyncio
async def test_tool_call_still_flushes_pending_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = await _run_stream(
        monkeypatch,
        [
            {"event_type": "chat.delta", "content": "先搜索一下"},
            {
                "event_type": "chat.tool_call",
                "tool_call": {"name": "web_search", "tool_call_id": "t1", "arguments": {}},
            },
        ],
    )

    finals = _final_contents(records)
    assert finals == ["先搜索一下"]


# ---------------------------------------------------------------------------
# 既有契约回归：falsy 终态值（success=False 等）仍是有效 tool_result
# （220452a61 约定），rail 层守卫不得影响真实错误结果的落盘。
# ---------------------------------------------------------------------------


def test_false_success_flag_still_counts_as_terminal():
    assert (
        session_history._has_persistable_assistant_payload(
            content_text="",
            event_type="chat.tool_result",
            extra={"tool_call_id": "call_x", "tool_name": "bash", "success": False},
        )
        is True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
@pytest.mark.parametrize("with_text", [False, True])
@pytest.mark.parametrize("goal", [False, True])
@pytest.mark.parametrize("boundary", [
    "chat.ask_user_question", "harness.activate_interaction", "plan.approval_required",
])
async def test_interaction_persists_reasoning_and_text_before_stream_ends(
    monkeypatch: pytest.MonkeyPatch,
    as_dict: bool,
    with_text: bool,
    goal: bool,
    boundary: str,
) -> None:
    payloads = [{"event_type": "goal.updated", "goal": {"status": "active"}}] if goal else []
    payloads.append({"event_type": "chat.reasoning", "content": "需要确认执行范围。"})
    if with_text:
        payloads.append({"event_type": "chat.delta", "content": "请确认执行范围。"})
    payloads.extend([
        {"event_type": boundary, "request_id": "confirm-1"},
        {"event_type": "chat.reasoning", "content": "用户已确认。"},
        {"event_type": "chat.final", "content": "继续执行。"},
    ])

    records = await _run_stream(monkeypatch, payloads, as_dict=as_dict)

    reasoning_records = [r for r in records if (r.get("extra") or {}).get("reasoning_content")]
    assert [r["extra"]["reasoning_content"] for r in reasoning_records] == [
        "需要确认执行范围。", "用户已确认。",
    ]
    assert reasoning_records[0]["event_type"] == ("chat.final" if with_text else "chat.reasoning")
    assert _final_contents(records) == (["请确认执行范围。"] if with_text else []) + ["继续执行。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_text", [False, True])
async def test_goal_pause_round_trips_reasoning_and_text_in_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    with_text: bool,
) -> None:
    payloads = [
        {"event_type": "goal.updated", "goal": {"status": "active"}},
        {"event_type": "chat.reasoning", "content": "需要确认范围。"},
    ]
    if with_text:
        payloads.append({"event_type": "chat.delta", "content": "请确认范围。"})
    payloads.append(_ask_user_payload())

    records = await _run_stream(monkeypatch, payloads, history_root=tmp_path)

    reasoning_records = [r for r in records if r.get("reasoning_content")]
    assert len(reasoning_records) == 1
    assert reasoning_records[0]["reasoning_content"] == "需要确认范围。"
    assert reasoning_records[0]["reasoning_updated_at"] > 0
    if not with_text:
        assert reasoning_records[0]["reasoning_updated_at"] >= reasoning_records[0]["timestamp"] * 1000
    assert _final_contents(records) == (["请确认范围。"] if with_text else [])
    assert records.index(reasoning_records[0]) < next(
        i for i, record in enumerate(records) if record.get("event_type") == "chat.ask_user_question"
    )
