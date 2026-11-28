# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.foundation.llm.schema.message import AssistantMessage, UserMessage
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _rewind_adapter(messages):
    live_ctx = SimpleNamespace(
        get_messages=MagicMock(return_value=list(messages)),
        context_id=MagicMock(return_value="default_context_id"),
        load_state=MagicMock(),
    )
    ctx_eng = SimpleNamespace(
        create_context=AsyncMock(return_value=live_ctx),
        get_context=MagicMock(return_value=live_ctx),
        save_contexts=AsyncMock(),
    )
    orig_session = SimpleNamespace(update_state=MagicMock())
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(
        react_agent=SimpleNamespace(context_engine=ctx_eng),
        _interaction_session=orig_session,
        _loop_session=None,
        card=MagicMock(),
        save_state=MagicMock(),
    )
    adapter._rewind_session = {}
    return adapter, live_ctx, ctx_eng


@pytest.mark.asyncio
async def test_rewind_without_pointer_reuses_compressed_live_baseline(monkeypatch):
    baseline = [UserMessage(content="compressed summary"), AssistantMessage(content="continued")]
    adapter, _live_ctx, _ctx_eng = _rewind_adapter(baseline)
    presess = SimpleNamespace(
        pre_run=AsyncMock(),
        get_state=MagicMock(return_value=None),
        update_state=MagicMock(),
        _pre_run_done=False,
    )
    monkeypatch.setattr("openjiuwen.core.single_agent.create_agent_session", MagicMock(return_value=presess))
    monkeypatch.setattr(interface_deep, "flush_history_writes_async", AsyncMock())
    monkeypatch.setattr(interface_deep, "load_history_records", MagicMock())
    adapter._write_rewind_context = AsyncMock()

    await adapter._prepare_rewind_session_for_next_invoke("sess-1", reason="stream_error")

    adapter._write_rewind_context.assert_not_awaited()
    assert adapter._rewind_session["sess-1"] is presess
    interface_deep.load_history_records.assert_not_called()
    interface_deep.flush_history_writes_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_rewind_without_pointer_preserves_live_baseline_before_checkpoint_restore(monkeypatch):
    baseline = [
        UserMessage(content="compressed summary"),
        AssistantMessage(content="compressed reply"),
        UserMessage(content="increment 1"),
        AssistantMessage(content="increment 2"),
        UserMessage(content="increment 3"),
        AssistantMessage(content="latest reply"),
    ]
    adapter, live_ctx, ctx_eng = _rewind_adapter(baseline)
    presess = SimpleNamespace(
        pre_run=AsyncMock(),
        get_state=MagicMock(return_value=None),
        update_state=MagicMock(),
        _pre_run_done=False,
    )

    async def _restore_stale_checkpoint(**_kwargs):
        live_ctx.get_messages.return_value = [UserMessage(content="stale checkpoint")]
        return live_ctx

    ctx_eng.create_context = AsyncMock(side_effect=_restore_stale_checkpoint)
    monkeypatch.setattr("openjiuwen.core.single_agent.create_agent_session", MagicMock(return_value=presess))
    monkeypatch.setattr(interface_deep, "flush_history_writes_async", AsyncMock())
    monkeypatch.setattr(interface_deep, "load_history_records", MagicMock())
    adapter._rewind_history_fallback = AsyncMock()
    adapter._write_rewind_context = AsyncMock()
    monkeypatch.setattr(interface_deep.logger, "info", MagicMock())

    await adapter._prepare_rewind_session_for_next_invoke("sess-1", reason="stream_error")

    assert any(
        "复用活窗压缩基线" in str(call.args[0]) and call.args[1:] == ("stream_error", "sess-1", 6)
        for call in interface_deep.logger.info.call_args_list
    )
    assert len(live_ctx.get_messages()) == 1  # 证明 create_context 后读到的已是旧 checkpoint。
    adapter._rewind_history_fallback.assert_not_awaited()
    adapter._write_rewind_context.assert_not_awaited()
    assert adapter._rewind_session["sess-1"] is presess
    assert presess._pre_run_done is True
    interface_deep.load_history_records.assert_not_called()
    interface_deep.flush_history_writes_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_rewind_timestamp_cursor_appends_only_new_history(monkeypatch):
    baseline = [UserMessage(content="compressed summary")]
    adapter, _live_ctx, _ctx_eng = _rewind_adapter(baseline)
    states = {"_last_committed_request_id": "prev", "_last_committed_history_timestamp": 10.0}
    presess = SimpleNamespace(
        pre_run=AsyncMock(), get_state=MagicMock(side_effect=states.get),
        update_state=MagicMock(), _pre_run_done=False,
    )
    records = [
        {"role": "assistant", "event_type": "chat.final", "request_id": "prev", "content": "committed", "timestamp": 9.0},
        {"role": "assistant", "event_type": "chat.final", "request_id": "current", "content": "increment", "timestamp": 11.0},
    ]
    monkeypatch.setattr("openjiuwen.core.single_agent.create_agent_session", MagicMock(return_value=presess))
    monkeypatch.setattr(interface_deep, "flush_history_writes_async", AsyncMock(return_value=True))
    monkeypatch.setattr(interface_deep, "load_history_records", MagicMock(return_value=records))
    adapter._write_rewind_context = AsyncMock()

    await adapter._prepare_rewind_session_for_next_invoke("sess-1", reason="stream_error")

    merged = adapter._write_rewind_context.await_args.kwargs["messages"]
    assert [message.content for message in merged] == ["compressed summary", "increment"]


def test_rewind_skips_compact_transcript_and_boundary_records():
    records = [
        {"role": "user", "event_type": "chat.message", "content": "real question"},
        {"role": "assistant", "event_type": "context.compact_boundary", "content": "Conversation compacted"},
        {
            "role": "assistant", "event_type": "context.compact_summary",
            "content": "<memory_block_dialogue>summary</memory_block_dialogue>",
            "transcript_only": True, "is_compact_summary": True,
        },
        {"role": "assistant", "event_type": "chat.final", "content": "real answer"},
        {"role": "assistant", "event_type": "chat.final", "content": "transcript", "transcript_only": True},
        {"role": "assistant", "event_type": "chat.final", "content": "compact", "is_compact_summary": True},
        {"role": "assistant", "event_type": "context.rewind_summary", "content": "recovery summary"},
    ]

    rebuilt = JiuWenSwarmDeepAdapter._build_rewind_messages_for_model(records)

    assert [message.content for message in rebuilt] == ["real question", "real answer", "recovery summary"]


def test_retry_notice_chat_error_does_not_trigger_rewind():
    source = (Path(interface_deep.__file__).parent / "interface.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    conditions = [
        ast.get_source_segment(source, node.test)
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            and child.func.id == "_schedule_rewind_for_error"
            for child in ast.walk(node)
        )
        and "chat.error" in (ast.get_source_segment(source, node.test) or "")
    ]

    assert conditions
    assert all("not is_retry_notice_payload(data.payload)" in condition for condition in conditions)
