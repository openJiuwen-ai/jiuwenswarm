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

from types import SimpleNamespace
from typing import Any, AsyncIterator, List

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.session import session_history
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
    _extract_tool_interrupt,
)
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StreamSession:
    def __init__(self) -> None:
        self.chunks: List[Any] = []

    async def write_stream(self, output: Any) -> None:
        self.chunks.append(output)


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


# ---------------------------------------------------------------------------
# Fix 2: 中断边界冲刷 pending 正文（facade 消费循环）
# ---------------------------------------------------------------------------


class _ScriptedAdapter:
    """把预设 payload 列表当作 adapter 输出流回放。"""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads

    async def create_instance(self, config: dict[str, Any] | None = None) -> None:
        return None

    async def reload_agent_config(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def process_message_impl(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def process_message_stream_impl(
        self, request: AgentRequest, _inputs: dict[str, Any]
    ) -> AsyncIterator[AgentResponseChunk]:
        for payload in self._payloads:
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
) -> List[dict[str, Any]]:
    facade = JiuWenSwarm()
    recorded: List[dict[str, Any]] = []
    monkeypatch.setattr(facade, "_adapter", _ScriptedAdapter(payloads))
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(
        interface_module, "append_history_record", lambda **kwargs: recorded.append(kwargs)
    )
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
