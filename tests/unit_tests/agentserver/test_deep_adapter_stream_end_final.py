# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RC-3 回归：stream 末尾合成 chat.final 必须聚合本轮已可见的 delta 文本。

协议契约依赖"answer chunk 会把整段回复作为 chat.final 重发"；当终态 answer
被解析层丢弃（输出为空/纯空白）或以 chunked 形式降级为 delta 时，该轮没有
真实终态 final，旧实现合成的兜底 final content 恒为空串——客户端从 final
读到"成功但无内容"。修复后合成 final 携带本轮已渲染（未被 output_suppressed）
的 delta 拼接文本。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _adapter_ready(monkeypatch: pytest.MonkeyPatch) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace(  # pylint: disable=protected-access
        get_context_usage=lambda **_kwargs: {},
    )
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _model_name="": True)
    monkeypatch.setattr(adapter, "_bind_runtime_cron_context", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_reset_runtime_cron_context", lambda _tokens: None)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: None)
    monkeypatch.setattr(
        adapter,
        "_apply_model_to_react_agent",
        lambda _model, **_kwargs: None,
    )
    monkeypatch.setattr(adapter, "_mark_session_active", lambda _session_id: None)
    monkeypatch.setattr(adapter, "_register_session_agent_task", lambda _session_id: None)
    monkeypatch.setattr(adapter, "_unregister_session_agent_task", lambda _session_id: None)
    monkeypatch.setattr(adapter, "_unmark_session_active", lambda _session_id, **_kwargs: None)

    async def _noop_update_runtime_config(_runtime_config):
        return None

    monkeypatch.setattr(adapter, "_update_runtime_config", _noop_update_runtime_config)

    from openjiuwen.harness import observability as harness_observability
    from jiuwenswarm.agents.harness import agent_observability as swarm_agent_observability

    monkeypatch.setattr(
        harness_observability,
        "open_agent_run_span",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        harness_observability,
        "close_agent_run_span",
        lambda _handle, **_kwargs: None,
    )
    monkeypatch.setattr(
        swarm_agent_observability,
        "sync_agent_observability",
        lambda **_kwargs: None,
    )
    return adapter


class _FakeInteractionStream:
    """Yields the given chunks, then ends."""

    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk

    async def close(self, *, abort_active_round: bool = False) -> None:
        return None


def _install_stream(adapter: JiuWenSwarmDeepAdapter, chunks: list) -> None:
    adapter._instance.attach_output = AsyncMock(  # pylint: disable=protected-access
        return_value=_FakeInteractionStream(chunks)
    )
    adapter._instance.send_input = AsyncMock()  # pylint: disable=protected-access


def _chat_request() -> AgentRequest:
    return AgentRequest(
        request_id="req-stream-end-final",
        channel_id="web",
        session_id="sess-stream-end-final",
        params={"query": "你好", "mode": "agent"},
        is_stream=True,
    )


def _usage_chunk(output_tokens: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        type="llm_usage",
        payload={"usage_metadata": {
            "input_tokens": 10,
            "output_tokens": output_tokens,
            "total_tokens": 10 + output_tokens,
        }},
    )


async def _run_stream(adapter, chunks: list) -> list[dict]:
    _install_stream(adapter, chunks)
    events = [
        chunk.payload
        async for chunk in adapter.process_message_stream_impl(
            _chat_request(), {"query": "你好"},
        )
        if isinstance(chunk.payload, dict)
    ]
    return events



async def test_dropped_empty_answer_synthesizes_final_with_delta_text(monkeypatch):
    """answer 输出为空被解析层丢弃：合成 final 携带 delta 拼接文本，而非空串。"""
    adapter = _adapter_ready(monkeypatch)
    events = await _run_stream(adapter, [
        SimpleNamespace(type="llm_output", payload={"content": "好的"}),
        SimpleNamespace(type="llm_output", payload={"content": "，没问题"}),
        _usage_chunk(),
        # answer 自身 output 为空 → parse_stream_chunk 返回 None，整块丢弃
        SimpleNamespace(
            type="answer",
            payload={"output": {"output": "", "chunked": False}, "result_type": "answer"},
        ),
    ])

    deltas = [e["content"] for e in events if e.get("event_type") == "chat.delta"]
    assert deltas == ["好的", "，没问题"]
    finals = [e for e in events if e.get("event_type") == "chat.final"]
    assert len(finals) == 1, "只应有 stream 末尾合成的终态 final"
    assert finals[0]["content"] == "好的，没问题"


@pytest.mark.asyncio
async def test_usage_summary_includes_cumulative_session_snapshot_when_total_is_absent(monkeypatch):
    from jiuwenswarm.server.runtime.session import session_token_usage

    snapshot = {
        "schema_version": 1,
        "revision": 4,
        "session_id": "sess-stream-end-final",
        "main": {
            "input_tokens": 40,
            "output_tokens": 8,
            "input_calls": 2,
            "output_calls": 2,
            "calls_total": 2,
        },
        "others": {
            "input_tokens": 12,
            "output_tokens": 3,
            "input_calls": 1,
            "output_calls": 1,
            "calls_total": 1,
        },
    }
    monkeypatch.setattr(session_token_usage, "get_session_token_usage", lambda _session_id: snapshot)
    adapter = _adapter_ready(monkeypatch)
    events = await _run_stream(adapter, [
        SimpleNamespace(type="llm_usage", payload={"usage_metadata": {
            "input_tokens": 10,
            "output_tokens": 4,
        }}),
        SimpleNamespace(type="answer", payload={
            "output": {"output": "done"},
            "result_type": "answer",
        }),
    ])

    summaries = [event for event in events if event.get("event_type") == "chat.usage_summary"]
    assert len(summaries) == 1
    assert summaries[0]["usage"]["input_tokens"] == 10
    assert summaries[0]["usage"]["output_tokens"] == 4
    assert summaries[0]["session_usage"] == snapshot


@pytest.mark.asyncio
async def test_cumulative_usage_is_delivered_when_provider_did_not_report_turn_usage(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import session_history
    from jiuwenswarm.server.runtime.session.session_token_usage import SessionTokenUsageLedger

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    ledger = SessionTokenUsageLedger(tmp_path / "sess-stream-end-final", "sess-stream-end-final")
    ledger.record("unreported-call", "main", None, None)
    events = await _run_stream(_adapter_ready(monkeypatch), [
        SimpleNamespace(type="answer", payload={
            "output": {"output": "done"},
            "result_type": "answer",
        }),
    ])

    summaries = [event for event in events if event.get("event_type") == "chat.usage_summary"]
    assert len(summaries) == 1
    assert summaries[0]["session_usage"]["main"]["calls_total"] == 1
    assert summaries[0]["session_usage"]["main"]["input_tokens"] is None
    assert "usage" not in summaries[0]



async def test_chunked_answer_synthesizes_final_with_delta_text(monkeypatch):
    """chunked answer 被降级为 delta：合成 final 聚合全部可见文本。"""
    adapter = _adapter_ready(monkeypatch)
    events = await _run_stream(adapter, [
        SimpleNamespace(type="llm_output", payload={"content": "第一段"}),
        # chunked answer：解析层转成 chat.delta 而非 final
        SimpleNamespace(
            type="answer",
            payload={"output": {"output": "第二段", "chunked": True}, "result_type": "answer"},
        ),
        _usage_chunk(),
    ])

    deltas = [e["content"] for e in events if e.get("event_type") == "chat.delta"]
    assert deltas == ["第一段", "第二段"]
    finals = [e for e in events if e.get("event_type") == "chat.final"]
    assert len(finals) == 1
    assert finals[0]["content"] == "第一段第二段"



async def test_suppressed_deltas_excluded_from_synthetic_final(monkeypatch):
    """被 output_suppressed 的 delta 不进合成 final（前端没渲染过它们）。"""
    adapter = _adapter_ready(monkeypatch)
    events = await _run_stream(adapter, [
        # 相位开启前的 delta：正常可见
        SimpleNamespace(type="llm_output", payload={"content": "可见文本"}),
        # 两个 steering 输入只消费掉一个 → 相位内仍有 pending，后续输出被抑制
        SimpleNamespace(type="session_input_received", payload={"input_request_id": "in-1"}),
        SimpleNamespace(type="session_input_received", payload={"input_request_id": "in-2"}),
        SimpleNamespace(
            type="session_output_phase",
            payload={"output_phase_id": "phase-1", "applied_input_ids": ["in-1"]},
        ),
        SimpleNamespace(type="llm_output", payload={"content": "被抑制文本"}),
        _usage_chunk(),
    ])

    suppressed = [
        e for e in events
        if e.get("event_type") == "chat.delta" and e.get("output_suppressed")
    ]
    assert len(suppressed) == 1 and suppressed[0]["content"] == "被抑制文本"
    finals = [e for e in events if e.get("event_type") == "chat.final"]
    assert len(finals) == 1
    assert finals[0]["content"] == "可见文本"



async def test_no_visible_deltas_keeps_empty_synthetic_final(monkeypatch):
    """无任何可见输出（如 pause→clear 前无 delta）：合成 final 保持空串（原行为）。"""
    adapter = _adapter_ready(monkeypatch)
    events = await _run_stream(adapter, [
        _usage_chunk(output_tokens=1),
    ])

    finals = [e for e in events if e.get("event_type") == "chat.final"]
    assert len(finals) == 1
    assert finals[0]["content"] == ""



async def test_real_terminal_final_not_duplicated(monkeypatch):
    """正常 answer（重发全文）仍是唯一终态 final，不触发合成（行为不变）。"""
    adapter = _adapter_ready(monkeypatch)
    events = await _run_stream(adapter, [
        SimpleNamespace(type="llm_output", payload={"content": "正常回答"}),
        SimpleNamespace(
            type="answer",
            payload={"output": {"output": "正常回答", "chunked": False}, "result_type": "answer"},
        ),
        _usage_chunk(),
    ])

    finals = [e for e in events if e.get("event_type") == "chat.final"]
    assert len(finals) == 1
    assert finals[0]["content"] == "正常回答"


def test_background_subagent_status_includes_latest_session_usage(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import session_history
    from jiuwenswarm.server.runtime.session.session_token_usage import SessionTokenUsageLedger

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    ledger = SessionTokenUsageLedger(tmp_path / "background_usage", "background_usage")
    ledger.record("main-call", "main", 100, 20)
    projection = {
        "subagent_id": "child-background",
        "parent_session_id": "background_usage",
        "status": "running",
    }
    initial = JiuWenSwarmDeepAdapter.project_subagent_updated_for_web(projection)
    ledger.record("later-child-call", "others", 70, 30)
    final = JiuWenSwarmDeepAdapter.project_subagent_updated_for_web({**projection, "status": "completed"})
    assert initial["event_type"] == final["event_type"] == "chat.subtask_update"
    assert initial["session_usage"]["revision"] == 1
    assert final["session_usage"]["revision"] == 2
    assert final["session_usage"]["others"]["input_tokens"] == 70
