# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""流被取消时消费循环补发 cancelled 终止帧的回归测试。

契约：每个流式请求恰有一个 is_complete=True 终止帧。chat.interrupt 等
取消场景下，原实现只留痕+log+raise，流静默死亡，客户端只能靠超时收尾。
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.common.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


def _request(request_id: str) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="tui",
        session_id=f"sess-{request_id}",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )


def _delta(request_id: str, content: str) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request_id,
        channel_id="tui",
        payload={"event_type": "chat.delta", "content": content},
        is_complete=False,
    )


def _terminal(request_id: str) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request_id,
        channel_id="tui",
        payload={"is_complete": True},
        is_complete=True,
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch, adapter) -> None:
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh", "memory": {"mode": "disabled"}},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(
        interface_module, "_schedule_symphony_session_feedback", lambda *_args: None
    )


def _cancelled_chunks(chunks) -> list:
    return [
        c for c in chunks
        if isinstance(c.payload, dict) and c.payload.get("code") == "cancelled"
    ]


@pytest.mark.asyncio
async def test_consumer_cancellation_emits_cancelled_terminal_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流静默期被取消：消费方在 CancelledError 前收到唯一 cancelled 终止帧。"""
    started = asyncio.Event()
    hang_forever = asyncio.Event()

    class HangingAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield _delta("req-cancel", "x")
            yield _delta("req-cancel", "y")
            started.set()
            await hang_forever.wait()

    _patch_common(monkeypatch, HangingAdapter)
    swarm = interface_module.JiuWenSwarm()
    chunks: list[AgentResponseChunk] = []

    async def consume() -> None:
        async for chunk in swarm.process_message_stream(_request("req-cancel")):
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    finals = [c for c in chunks if c.is_complete]
    cancelled = _cancelled_chunks(chunks)
    assert len(finals) == 1
    assert finals[0] is cancelled[0]
    assert cancelled[0].payload["event_type"] == "chat.error"
    assert [c.payload.get("event_type") for c in chunks[:2]] == ["chat.delta", "chat.delta"]


@pytest.mark.asyncio
async def test_cancellation_after_terminal_does_not_double_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终止帧已发出后再取消：不得补发第二个终止帧。"""
    started = asyncio.Event()
    hang_forever = asyncio.Event()

    class TerminalThenHangingAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield _delta("req-nodouble", "x")
            yield _terminal("req-nodouble")
            started.set()
            await hang_forever.wait()

    _patch_common(monkeypatch, TerminalThenHangingAdapter)
    swarm = interface_module.JiuWenSwarm()
    chunks: list[AgentResponseChunk] = []

    async def consume() -> None:
        async for chunk in swarm.process_message_stream(_request("req-nodouble")):
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len([c for c in chunks if c.is_complete]) == 1
    assert _cancelled_chunks(chunks) == []


@pytest.mark.asyncio
async def test_normal_completion_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常完成路径不出现 cancelled 帧。"""

    class CompletingAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield _delta("req-normal", "answer")
            yield AgentResponseChunk(
                request_id="req-normal",
                channel_id="tui",
                payload={"event_type": "chat.final", "content": "answer"},
                is_complete=False,
            )
            yield _terminal("req-normal")

    _patch_common(monkeypatch, CompletingAdapter)
    swarm = interface_module.JiuWenSwarm()
    chunks = [
        chunk async for chunk in swarm.process_message_stream(_request("req-normal"))
    ]

    assert chunks[-1].is_complete is True
    assert _cancelled_chunks(chunks) == []


def test_cancelled_terminal_chunk_encodes_as_final_error_envelope() -> None:
    """补发帧经现成信封映射成为合法终止帧（is_final + status=failed + e2a.error）。"""
    chunk = AgentResponseChunk(
        request_id="req-env",
        channel_id="tui",
        payload={"event_type": "chat.error", "code": "cancelled", "error": "任务已取消"},
        is_complete=True,
    )
    wire = encode_agent_chunk_for_wire(chunk, response_id="req-env", sequence=2)

    assert wire["is_final"] is True
    assert wire["status"] == "failed"
    assert wire["response_kind"] == "e2a.error"
    assert wire["body"]["details"]["code"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("already_flushed", [False, True])
@pytest.mark.parametrize("cancel_producer", [False, True])
async def test_cancel_persists_stop_without_pending_text(
    monkeypatch, already_flushed, cancel_producer,
) -> None:
    """工具阶段/零正文取消也要留下原轮停止记录，覆盖两种取消来源。"""
    ready = asyncio.Event()
    producer = None
    rid = "req-stop-history"

    class Adapter:
        @staticmethod
        async def process_message_stream_impl(*_args):
            nonlocal producer
            producer = asyncio.current_task()
            if already_flushed:
                yield AgentResponseChunk(request_id=rid, channel_id="tui", payload={
                    "event_type": "chat.final", "content": "正在识别图片"
                })
            yield AgentResponseChunk(request_id=rid, channel_id="tui", payload={
                "event_type": "chat.tool_call", "tool_call": {"name": "bash", "tool_call_id": "call-picture"}
            })
            await asyncio.Event().wait()

    _patch_common(monkeypatch, Adapter)
    records = []
    monkeypatch.setattr(interface_module, "append_history_record", lambda **kwargs: records.append(kwargs))
    swarm = interface_module.JiuWenSwarm()
    chunks = []

    async def consume():
        async for chunk in swarm.process_message_stream(_request(rid)):
            chunks.append(chunk)
            if chunk.payload.get("event_type") == "chat.tool_call":
                ready.set()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(ready.wait(), timeout=5)
    (producer if cancel_producer else consumer).cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer, timeout=5)
    stopped = [r for r in records if (r.get("extra") or {}).get("aborted")]
    assert len(stopped) == 1
    assert stopped[0]["request_id"] == rid
    assert stopped[0]["event_type"] == "chat.final"
    assert len(_cancelled_chunks(chunks)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_sent", [False, True])
async def test_close_while_downstream_is_sending_preserves_cancel_history(monkeypatch, terminal_sent):
    """消费者停在 yield 外侧时，aclose 必须留痕且不能再 yield。"""
    rid = "req-close-history"

    class Adapter:
        @staticmethod
        async def process_message_stream_impl(*_args):
            yield _delta(rid, "图片分析了一部分")
            if terminal_sent:
                yield _terminal(rid)
            await asyncio.Event().wait()

    _patch_common(monkeypatch, Adapter)
    records = []
    monkeypatch.setattr(interface_module, "append_history_record", lambda **kwargs: records.append(kwargs))
    stream = interface_module.JiuWenSwarm().process_message_stream(_request(rid))
    await anext(stream)
    if terminal_sent:
        await anext(stream)
    await stream.aclose()
    stopped = [r for r in records if (r.get("extra") or {}).get("aborted")]
    if terminal_sent:
        assert stopped == []
    else:
        assert len(stopped) == 1
        assert stopped[0]["request_id"] == rid
        assert stopped[0]["content"] == "图片分析了一部分"
