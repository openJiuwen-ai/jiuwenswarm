"""历史气泡与实时气泡必须一一对应（Goal 场景回归）。

Goal 执行中，attempt 边界的 ``chat.final`` 会被降级成 ``chat.delta``
（``goal_intermediate``，见 interface_deep._adapt_goal_intermediate_final），
真正收尾的是流末尾一个 **空正文** 的 ``chat.final``。前端拿到空 final 只是
关掉气泡光标、正文保留已流式的 delta；历史侧过去在空 final 上直接把
``durable_pending_final_chunks`` 丢掉，于是整段 Goal 回答不落盘——重新打开
历史记录时这条气泡凭空消失，和实时看到的完全不是一回事。

Goal 仍 active 时流结束更极端：连那个空 final 都不发（见
interface_deep._should_emit_stream_end_chat_final），气泡里的正文彻底没人落盘。

这里按 facade 消费循环的真实事件序列钉住五件事：
1. Goal 段（delta + 空 final）必须落一条 chat.final 历史；
2. 普通问答轮 → Goal 轮的拆气泡边界（空 final）要落成两条独立历史记录；
3. Goal 流没有任何 final 就结束时，收尾要补落一条；
4. 非 Goal 流没有 final 就结束仍然什么都不落（该改动只涉及 Goal）；
5. 普通对话（delta + 带正文 final）仍然只落一条，用 final 的正文，不重复。
"""
from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


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
        # Session-level MCP reconcile is a no-op for this scripted adapter
        # (goal-history tests don't exercise MCP selection).
        return None

    async def handle_user_answer(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def handle_heartbeat(self, *_args: Any, **_kwargs: Any) -> Any:
        return None


async def _run_stream(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, Any]],
    *, include_users: bool = False,
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
        request_id="req-goal-history",
        channel_id="web",
        session_id="goal_history_sess",
        params={"query": "hello", "mode": "agent"},
    )
    async for _chunk in facade.process_message_stream(request):
        pass
    return recorded if include_users else [r for r in recorded if r.get("role") == "assistant"]


def _final_contents(records: List[dict[str, Any]]) -> List[str]:
    return [
        str(r.get("content") or "")
        for r in records
        if r.get("event_type") == "chat.final" and str(r.get("content") or "").strip()
    ]


@pytest.mark.asyncio
async def test_goal_segment_text_is_persisted_when_final_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = await _run_stream(
        monkeypatch,
        [
            {"event_type": "chat.delta", "content": "先看一下测试。"},
            {"event_type": "chat.delta", "content": "已经全部通过。"},
            # attempt 边界 final 被降级：前端当增量、不拆气泡
            {
                "event_type": "chat.delta",
                "content": "本轮小结：测试通过。",
                "goal_intermediate": True,
            },
            # Goal 完成后流末尾的兜底 final（无正文）
            {"event_type": "chat.final", "content": ""},
        ],
    )

    assert _final_contents(records) == ["先看一下测试。已经全部通过。本轮小结：测试通过。"]


@pytest.mark.asyncio
async def test_user_and_goal_segments_persist_as_two_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = await _run_stream(
        monkeypatch,
        [
            {"event_type": "chat.delta", "content": "普通问答的回答。"},
            # 拆气泡边界：用户轮气泡收尾，Goal 段随后新起一个气泡
            {"event_type": "chat.final", "content": ""},
            {"event_type": "chat.delta", "content": "Goal 段的回答。"},
            {"event_type": "chat.final", "content": ""},
        ],
    )

    assert _final_contents(records) == ["普通问答的回答。", "Goal 段的回答。"]


@pytest.mark.asyncio
async def test_goal_stream_without_any_final_still_persists_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Goal 还在跑（record 仍 active），adapter 故意不发收尾 final：这条流让出输出权
    # 之后 Goal 会在别的流上继续，但它已经推给前端的正文必须留在历史里。
    records = await _run_stream(
        monkeypatch,
        [
            {"event_type": "goal.updated", "goal": {"status": "active"}},
            {"event_type": "chat.delta", "content": "Goal 正在执行："},
            {"event_type": "chat.delta", "content": "已经改完第一处。"},
        ],
    )

    assert _final_contents(records) == ["Goal 正在执行：已经改完第一处。"]


@pytest.mark.asyncio
async def test_plain_stream_without_any_final_persists_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 没有 Goal 事件的普通流保持原样：不因为上面的兜底而多落一条历史。
    records = await _run_stream(
        monkeypatch,
        [
            {"event_type": "chat.delta", "content": "普通回答被打断了"},
        ],
    )

    assert _final_contents(records) == []


@pytest.mark.asyncio
async def test_plain_turn_still_persists_single_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = await _run_stream(
        monkeypatch,
        [
            {"event_type": "chat.delta", "content": "回答"},
            {"event_type": "chat.delta", "content": "正文"},
            {"event_type": "chat.final", "content": "回答正文"},
        ],
    )

    assert _final_contents(records) == ["回答正文"]


@pytest.mark.asyncio
async def test_final_record_uses_first_delta_timestamp_and_completed_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.final 历史用首包时刻排序，收尾另存 completed_at（耗时不缩水）。"""
    clock = {"t": 1000.0}

    def fake_time() -> float:
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(interface_module.time, "time", fake_time)
    records = await _run_stream(
        monkeypatch,
        [
            {"event_type": "chat.delta", "content": "先"},
            {"event_type": "chat.delta", "content": "后"},
            {"event_type": "chat.final", "content": "先后"},
        ],
    )
    finals = [r for r in records if r.get("event_type") == "chat.final"]
    assert len(finals) == 1
    final = finals[0]
    # 首包 delta 时刻早于收尾；completed_at 为收尾
    assert float(final["timestamp"]) < float(final["extra"]["completed_at"])
    assert float(final["extra"]["completed_at"]) - float(final["timestamp"]) >= 1.0


@pytest.mark.asyncio
async def test_supplement_boundary_persists_visible_prefix_before_user_and_hides_old_tail(monkeypatch):
    sequence = 0

    def event(kind, phase="first", **fields):
        nonlocal sequence
        sequence += 1
        return {"event_type": kind, "output_phase_id": phase,
                "output_order": {"request_id": "original", "sequence": sequence},
                "timestamp": 1800000000000 + sequence, **fields}

    records = await _run_stream(monkeypatch, [
        event("chat.output_phase", applied_input_ids=[]),
        event("chat.reasoning", content="first thought"),
        event("chat.delta", content="already visible"),
        event("chat.input_received", input_request_id="input-1", content="new instruction"),
        event("chat.delta", content="old tail", output_suppressed=True),
        event("chat.reasoning", content="old thought", output_suppressed=True),
        event("chat.output_phase", "second", applied_input_ids=["input-1"]),
        event("chat.reasoning", "second", content="second thought"),
        event("chat.delta", "second", content="new answer"),
        event("chat.final", "second", content="new answer"),
    ], include_users=True)
    visible = [r for r in records if r.get("content") and not (r.get("extra") or {}).get("output_suppressed")]
    assert [(r["role"], r["content"]) for r in visible] == [
        ("user", "hello"), ("assistant", "already visible"),
        ("user", "new instruction"), ("assistant", "new answer"),
    ]
    prefix, user, answer = visible[1:]
    assert prefix["extra"]["reasoning_content"] == "first thought"
    assert prefix["extra"]["output_order"]["sequence"] == 3
    assert user["request_id"] == "input-1"
    assert user["extra"]["is_supplemental_input"] is True
    assert "supplemental_input" not in user["extra"], "the ordinary request_id already identifies the supplement"
    assert prefix["extra"]["completed_at"] == user["timestamp"]
    assert user["extra"]["output_order"]["sequence"] == 4
    assert answer["extra"]["output_order"]["sequence"] == 9
    assert answer["extra"]["reasoning_content"] == "second thought"
    assert answer["extra"]["reasoning_output_order"]["sequence"] == 8
    assert "timestamp" not in answer["extra"], "stream milliseconds must not overwrite the history seconds timestamp"
    assert answer["timestamp"] < answer["extra"]["completed_at"]
    assert answer["extra"]["completed_at"] == (1800000000000 + 10) / 1000
    hidden = [r for r in records if (r.get("extra") or {}).get("output_suppressed")]
    assert any(r.get("content") == "old tail" for r in hidden)
    assert all("old" not in (r.get("extra") or {}).get("reasoning_content", "") for r in visible)


@pytest.mark.asyncio
@pytest.mark.parametrize("supplement", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
async def test_goal_automatic_phases_keep_one_visible_history_segment(
    monkeypatch, supplement, terminal,
):
    payloads = [{"event_type": "goal.updated", "goal": {"status": "active"}}]

    def emit(kind, phase, **fields):
        sequence = len(payloads) + 1
        payloads.append({
            "event_type": kind, "output_phase_id": phase,
            "output_order": {"request_id": "original", "sequence": sequence},
            "timestamp": 1800000000000 + sequence, **fields,
        })

    emit("chat.output_phase", "initial", applied_input_ids=[])
    if supplement:
        emit("chat.delta", "initial", content="visible before supplement")
        emit("chat.input_received", "initial", input_request_id="input-1", content="new instruction")
        emit("chat.delta", "initial", content="hidden tail", output_suppressed=True)
        emit("chat.output_phase", "first", applied_input_ids=["input-1"])
    emit("chat.reasoning", "first" if supplement else "initial", content="thought one. ")
    emit("chat.delta", "first" if supplement else "initial", content="attempt one. ")
    emit("chat.output_phase", "second", applied_input_ids=[])
    emit("chat.reasoning", "second", content="thought two.")
    emit("chat.delta", "second", content="attempt two.")
    if terminal:
        emit("chat.final", "second", content="")

    records = await _run_stream(monkeypatch, payloads, include_users=True)
    visible = [r for r in records if r.get("content") and not (r.get("extra") or {}).get("output_suppressed")]
    expected = [("user", "hello")]
    if supplement:
        expected += [("assistant", "visible before supplement"), ("user", "new instruction")]
    assert [(r["role"], r["content"]) for r in visible] == expected + [
        ("assistant", "attempt one. attempt two."),
    ]
    assert visible[-1]["extra"]["reasoning_content"] == "thought one. thought two."


@pytest.mark.asyncio
@pytest.mark.parametrize("old_final", [False, True])
async def test_goal_stream_end_does_not_reveal_unconsumed_steering_tail(monkeypatch, old_final):
    payloads = []

    def emit(kind, **fields):
        sequence = len(payloads) + 1
        payloads.append({
            "event_type": kind, "output_phase_id": "initial",
            "output_order": {"request_id": "original", "sequence": sequence},
            "timestamp": 1800000000000 + sequence, **fields,
        })

    emit("chat.output_phase", applied_input_ids=[])
    emit("chat.delta", content="visible prefix")
    emit("chat.input_received", input_request_id="input-1", content="new instruction")
    emit("chat.delta", content="hidden tail", output_suppressed=True)
    emit("chat.reasoning", content="hidden thought", output_suppressed=True)
    if old_final:
        emit("chat.final", content="hidden tail", output_suppressed=True)
    emit("chat.final", content="")

    records = await _run_stream(monkeypatch, payloads, include_users=True)
    visible = [r for r in records if not (r.get("extra") or {}).get("output_suppressed")]
    assert [(r["role"], r["content"]) for r in visible if r.get("content")] == [
        ("user", "hello"), ("assistant", "visible prefix"), ("user", "new instruction"),
    ]
    assert all("hidden" not in (r.get("extra") or {}).get("reasoning_content", "") for r in visible)
    hidden = [r for r in records if (r.get("extra") or {}).get("output_suppressed")]
    assert any(r.get("content") == "hidden tail" for r in hidden)
