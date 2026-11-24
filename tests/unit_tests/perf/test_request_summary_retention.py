# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PerfCollector retention + monitoring-summary extensions (issue #6745)."""

from __future__ import annotations

import pytest

from jiuwenswarm.perf.collector import PerfCollector
from jiuwenswarm.perf.events import LlmPerfEvent, ToolPerfEvent


@pytest.fixture()
def collector(monkeypatch: pytest.MonkeyPatch) -> PerfCollector:
    """A fresh collector whose jsonl writer is a no-op."""
    from jiuwenswarm.perf import collector as collector_module

    monkeypatch.setattr(
        collector_module, "append_request_summary", lambda *args, **kwargs: None
    )
    return PerfCollector()


def _begin(collector: PerfCollector, request_id: str = "req-1"):
    return collector.begin_request(
        session_id="s-1",
        request_id=request_id,
        channel_id="web",
        mode="agent.plan",
    )


def test_finalize_retains_summary_and_pop_consumes_once(collector: PerfCollector) -> None:
    _begin(collector)
    collector.finalize_request("req-1", status="ok")

    summary = collector.pop_finalized_summary("req-1")
    assert summary is not None
    assert summary["meta"]["request_id"] == "req-1"
    assert summary["summary"]["status"] == "ok"

    # Consumed exactly once — the pipeline finally runs after adapter finalize.
    assert collector.pop_finalized_summary("req-1") is None


def test_retained_summary_survives_jsonl_write_failure(
    collector: PerfCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.perf import collector as collector_module

    _begin(collector)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(collector_module, "append_request_summary", _boom)
    collector.finalize_request("req-1", status="ok")

    assert collector.pop_finalized_summary("req-1") is not None


def test_mark_stage_first_mark_wins(collector: PerfCollector) -> None:
    _begin(collector)
    collector.mark_stage("req-1", "init")
    first = collector.get_accumulator("req-1").stage_marks["init"]
    collector.mark_stage("req-1", "init")
    collector.mark_stage("req-1", "pre_llm")

    acc = collector.get_accumulator("req-1")
    assert acc.stage_marks["init"] == first
    assert set(acc.stage_marks) == {"init", "pre_llm"}


def test_llm_stream_timing_last_call_wins(collector: PerfCollector) -> None:
    _begin(collector)
    collector.record_llm_stream_timing(
        "req-1", ttft_ms=100.0, total_ms=300.0, chunk_count=5, status="ok"
    )
    collector.record_llm_stream_timing(
        "req-1", ttft_ms=200.0, total_ms=600.0, chunk_count=9, status="ok"
    )

    acc = collector.get_accumulator("req-1")
    assert acc.llm_ttft_ms == 200.0
    assert acc.llm_tpot_ms == round((600.0 - 200.0) / 8, 1)

    _begin(collector, request_id="req-2")
    collector.record_llm_stream_timing(
        "req-2", ttft_ms=None, total_ms=100.0, chunk_count=1, status="error"
    )
    acc = collector.get_accumulator("req-2")
    assert acc.llm_ttft_ms is None
    assert acc.llm_tpot_ms is None


def test_tool_detail_accumulates_per_name_durations(collector: PerfCollector) -> None:
    _begin(collector)
    for duration, status in ((10.0, "ok"), (20.0, "error"), (30.0, "ok")):
        collector.record_tool(
            "req-1",
            ToolPerfEvent(
                tool_call_id=f"t-{duration}",
                name="search_web",
                duration_ms=duration,
                status=status,
            ),
        )
    collector.record_tool(
        "req-1",
        ToolPerfEvent(tool_call_id="t-x", name="bash", duration_ms=5.0, status="ok"),
    )

    acc = collector.get_accumulator("req-1")
    detail = acc.tool_stats.to_detail_dict()
    assert detail["search_web"] == {
        "count": 3,
        "total_ms": 60.0,
        "max_ms": 30.0,
        "min_ms": 10.0,
        "avg_ms": 20.0,
        "errors": 1,
    }
    # jsonl-facing to_dict stays count-only (backward compatible), count-desc order.
    assert acc.tool_stats.to_dict()["list"] == [
        {"name": "search_web", "count": 3},
        {"name": "bash", "count": 1},
    ]


def test_stream_metrics_and_context_usage(collector: PerfCollector) -> None:
    _begin(collector)
    collector.record_thinking_segment("req-1", 120.0)
    collector.record_thinking_segment("req-1", 90.5)
    collector.record_message_gen("req-1", 300.0)
    collector.record_message_gen("req-1", 640.0)
    collector.record_stream_gap("req-1", 35.0)
    collector.record_stream_gap("req-1", 10.0)
    collector.record_chat_error("req-1")
    collector.mark_ask_emitted("req-1")
    collector.record_skill_usage("req-1", "code_review")
    collector.record_skill_usage("req-1", "code_review")
    collector.record_context_usage(
        "req-1", limit_tokens=131072, occupancy_rate=0.41
    )
    collector.record_chunk_sent("req-1")
    collector.record_chunk_sent("req-1")

    acc = collector.get_accumulator("req-1")
    assert acc.thinking_segments == {"1": 120.0, "2": 90.5}
    assert acc.thinking_max_ms == 120.0
    assert acc.msg_gen_max_ms == 640.0
    assert acc.stream_gap_max_ms == 35.0
    assert acc.chat_error_count == 1
    assert acc.ask_emitted is True
    assert acc.skill_stats == {"code_review": 2}
    assert acc.ctx_window_tokens == 131072
    assert acc.ctx_occupancy == 0.41
    assert acc.chunk_count == 2

    collector.finalize_request("req-1", status="ok")
    summary = collector.pop_finalized_summary("req-1")
    assert summary["stream"]["thinking_total_ms"] == 210.5
    assert summary["skills"] == {"code_review": 2}
    assert summary["context_window"] == {
        "limit_tokens": 131072,
        "occupancy_rate": 0.41,
    }
    assert summary["chunk_count"] == 2


def test_hitl_ask_registered_on_ask_and_consumed_on_resume(
    collector: PerfCollector,
) -> None:
    _begin(collector)
    collector.mark_ask_emitted("req-1")
    collector.finalize_request("req-1", status="ok")

    ask = collector.pop_hitl_ask("s-1")
    assert ask is not None
    assert ask[0] == "req-1"

    # Consumed once; a request without ask registers nothing.
    assert collector.pop_hitl_ask("s-1") is None
    _begin(collector, request_id="req-2")
    collector.finalize_request("req-2", status="ok")
    assert collector.pop_hitl_ask("s-1") is None


def test_finalize_summary_carries_llm_model_and_stage_marks(
    collector: PerfCollector,
) -> None:
    _begin(collector)
    collector.mark_stage("req-1", "init")
    collector.record_llm(
        "req-1",
        LlmPerfEvent(
            llm_call_id="c1",
            duration_ms=1234.0,
            model="glm-5.2",
            iteration=1,
            input_tokens=100,
            output_tokens=50,
            status="ok",
        ),
    )
    collector.finalize_request("req-1", status="ok")

    summary = collector.pop_finalized_summary("req-1")
    stats_llm = summary["summary"]["stats"]["llm"]
    assert stats_llm["model"] == "glm-5.2"
    assert "init" in summary["stage_marks"]



def test_resolve_stream_request_id_falls_back_to_session_registry(
    collector: PerfCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型客户端流跑在不继承 ContextVar 的任务上时，经 openjiuwen 会话回溯 rid。"""
    import openjiuwen.core.common.logging as oj_logging

    import jiuwenswarm.perf.context as perf_context
    from jiuwenswarm.perf.interface_hooks import resolve_stream_request_id

    # 注册表登记活动请求（set_request_context），随后模拟跨任务不可见：
    # 本任务的 perf ContextVar 为空，仅共享注册表 + runtime 会话上下文可用。
    perf_context.set_request_context(
        session_id="s-1",
        request_id="req-stream",
        channel_id="web",
        mode="flash",
        begin_perf_collection=False,
    )
    collector.begin_request(
        session_id="s-1", request_id="req-stream", channel_id="web", mode="flash"
    )
    perf_context._request_context.set(None)
    monkeypatch.setattr(oj_logging, "get_session_id", lambda: "s-1")

    try:
        assert resolve_stream_request_id() == "req-stream"

        # 计时经该 rid 落进 accumulator（last-call-wins）。
        collector.record_llm_stream_timing(
            "req-stream", ttft_ms=350.0, total_ms=900.0, chunk_count=40, status="ok"
        )
        collector.finalize_request("req-stream", status="ok")
        summary = collector.pop_finalized_summary("req-stream")
        assert summary["summary"]["stats"]["llm"]["ttft_ms"] == 350.0
        assert summary["summary"]["stats"]["llm"]["tpot_ms"] == round((900.0 - 350.0) / 39, 1)
    finally:
        perf_context.clear_request_context(request_id="req-stream")
        perf_context._request_context.set(None)


def test_resolve_stream_request_id_empty_with_default_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runtime 会话为默认占位时不得误挂到 default 注册表，应解析为空。"""
    import openjiuwen.core.common.logging as oj_logging

    import jiuwenswarm.perf.context as perf_context
    from jiuwenswarm.perf.interface_hooks import resolve_stream_request_id

    perf_context._request_context.set(None)
    monkeypatch.setattr(oj_logging, "get_session_id", lambda: "default_trace_id")
    assert resolve_stream_request_id() == ""
