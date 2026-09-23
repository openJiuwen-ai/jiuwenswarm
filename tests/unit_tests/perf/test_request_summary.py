# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.perf.accumulator import RequestMeta, RequestSummaryAccumulator
from jiuwenswarm.perf.collector import PerfCollector
from jiuwenswarm.perf.config import init_perf_summary_config
from jiuwenswarm.perf.events import LlmPerfEvent, TaskPerfEvent, ToolPerfEvent
from jiuwenswarm.perf.extract import tool_status_from_result
from jiuwenswarm.perf.stats import ms_to_s, percentile_s
from jiuwenswarm.perf.writer import append_request_summary, request_summaries_file


def test_perf_usage_snapshot_recovers_tokens_when_stream_usage_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.perf import interface_hooks

    accumulator = SimpleNamespace(
        input_tokens=42_462,
        output_tokens=355,
        cache_read_tokens=1_024,
    )
    collector = SimpleNamespace(get_accumulator=lambda request_id: accumulator)
    monkeypatch.setattr(interface_hooks, "get_perf_collector", lambda: collector)

    snapshot = interface_hooks.snapshot_perf_summary_usage("req-interrupted")
    stream_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_tokens": 0,
    }

    recovered = interface_hooks.merge_perf_summary_usage_fallback(
        stream_usage,
        snapshot,
    )

    assert recovered is True
    assert stream_usage == {
        "input_tokens": 42_462,
        "output_tokens": 355,
        "total_tokens": 42_817,
        "cache_tokens": 1_024,
    }


def test_perf_usage_fallback_does_not_duplicate_complete_stream_usage() -> None:
    from jiuwenswarm.perf.interface_hooks import merge_perf_summary_usage_fallback

    stream_usage = {
        "input_tokens": 1_000,
        "output_tokens": 100,
        "total_tokens": 1_100,
        "cache_tokens": 200,
    }

    recovered = merge_perf_summary_usage_fallback(
        stream_usage,
        {
            "input_tokens": 1_000,
            "output_tokens": 100,
            "total_tokens": 1_100,
            "cache_tokens": 200,
        },
    )

    assert recovered is False
    assert stream_usage["total_tokens"] == 1_100


def test_external_token_usage_hook_routes_sdk_snapshot_to_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.perf import interface_hooks

    calls: list[tuple[tuple, dict]] = []
    collector = SimpleNamespace(
        record_external_token_usage=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(interface_hooks, "get_perf_collector", lambda: collector)

    interface_hooks.record_deepresearch_sdk_token_usage(
        "request-1",
        "conversation-1",
        {"input_tokens": 500, "output_tokens": 80, "total_tokens": 580},
    )

    assert calls == [
        (
            ("request-1",),
            {
                "source": "deepresearch_sdk",
                "usage_id": "conversation-1",
                "input_tokens": 500,
                "output_tokens": 80,
                "total_tokens": 580,
            },
        )
    ]


def test_request_summary_rail_imports_required_symbols() -> None:
    """Regression: ensure request_summary_rail imports all runtime dependencies."""
    import ast

    src = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "perf" / "request_summary_rail.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("jiuwenswarm.perf"):
            for alias in node.names:
                imported.add(alias.name)
    for required in ("get_perf_collector", "get_perf_summary_config"):
        assert required in imported, f"missing import: {required}"


def test_tool_status_uses_success_and_status_fields() -> None:
    assert tool_status_from_result({"success": True, "result": "done"}) == "ok"
    assert tool_status_from_result({"success": False, "error": "spawn failed"}) == "error"
    assert tool_status_from_result({"status": "ok", "note": "no error occurred"}) == "ok"
    assert tool_status_from_result({"message": "Successfully updated 2 task(s)"}) == "ok"
    assert tool_status_from_result("Successfully created 9 task(s)") == "ok"
    # Regression: Chinese skip text must stay valid UTF-8 success marker.
    assert tool_status_from_result("跳过大纲确认") == "ok"
    assert tool_status_from_result(
        "shell operation execution error, reason: execution timeout after 180 seconds"
    ) == "error"
    assert tool_status_from_result("Traceback (most recent call last):\nValueError") == "error"


def test_percentile_s_basic() -> None:
    assert percentile_s([10000, 20000, 30000, 40000, 100000], 0.90) == 100.0
    assert percentile_s([5000], 0.99) == 5.0
    assert ms_to_s(1500) == 1.5


def test_bottleneck_top_n_zero_disables_tracking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"perf": {"summary": {"bottleneck_top_n": 0}}},
    )
    from jiuwenswarm.perf.config import load_perf_summary_config

    cfg = load_perf_summary_config()
    assert cfg.bottleneck_top_n == 0


def test_accumulator_finalize_schema() -> None:
    acc = RequestSummaryAccumulator(
        meta=RequestMeta(
            session_id="sess-1",
            request_id="req-1",
            channel_id="web",
            mode="plan",
            trace_id=None,
            started_at=1000.0,
        ),
        _bottleneck_top_n=3,
    )
    acc.record_llm(
        LlmPerfEvent(
            llm_call_id="llm-1",
            duration_ms=1000.0,
            model="deepseek-chat",
            iteration=1,
            input_tokens=100,
            output_tokens=50,
            status="ok",
            agent_id="main_agent",
            task_id="todo:1",
        )
    )
    acc.record_tool(
        ToolPerfEvent(
            tool_call_id="tool-1",
            name="WebSearch",
            duration_ms=500.0,
            status="ok",
            agent_id="main_agent",
            task_id="todo:1",
            iteration=1,
        )
    )
    summary = acc.finalize(status="ok", ended_at=1002.5)

    assert summary["schema_version"] == 1
    assert summary["meta"]["request_id"] == "req-1"
    assert summary["summary"]["total_s"] == 2.5
    assert summary["summary"]["stats"]["llm"]["count"] == 1
    assert summary["summary"]["stats"]["llm"]["p90_duration_s"] == 1.0
    assert summary["summary"]["stats"]["tool"]["max_s"] == 0.5
    assert summary["summary"]["stats"]["tool"]["list"] == [{"name": "WebSearch", "count": 1}]
    assert "subagent" not in summary["summary"]["stats"]
    assert "by_agent" not in summary["summary"]["stats"]
    assert summary["summary"]["first_answer_latency_s"] == 0.0
    assert "llm_call_id" not in summary["bottleneck"]["llm"][0]
    assert summary["bottleneck"]["tool"][0]["name"] == "WebSearch"
    assert "errors" not in summary


def test_accumulator_adds_external_token_usage_once_without_fake_llm_call() -> None:
    acc = RequestSummaryAccumulator(
        meta=RequestMeta(
            session_id="sess-1",
            request_id="req-1",
            channel_id="web",
            mode="plan",
            trace_id=None,
            started_at=1000.0,
        )
    )
    acc.record_llm(
        LlmPerfEvent(
            llm_call_id="llm-1",
            duration_ms=1000.0,
            model="deepseek-chat",
            iteration=1,
            input_tokens=100,
            output_tokens=50,
            status="ok",
        )
    )

    assert acc.record_external_token_usage(
        source="deepresearch_sdk",
        usage_id="conversation-1",
        input_tokens=500,
        output_tokens=80,
        total_tokens=580,
    ) is True
    assert acc.record_external_token_usage(
        source="deepresearch_sdk",
        usage_id="conversation-1",
        input_tokens=500,
        output_tokens=80,
        total_tokens=580,
    ) is False

    summary = acc.finalize(status="ok", ended_at=1002.0)
    assert summary["summary"]["tokens"] == {
        "input": 600,
        "output": 130,
        "total": 730,
        "cache_read": 0,
        "reasoning": 0,
    }
    assert summary["summary"]["stats"]["llm"]["count"] == 1


def test_accumulator_sums_reasoning_tokens_into_summary_and_bottleneck() -> None:
    acc = RequestSummaryAccumulator(
        meta=RequestMeta(
            session_id="sess-1",
            request_id="req-1",
            channel_id="web",
            mode="plan",
            trace_id=None,
            started_at=1000.0,
        )
    )
    acc.record_llm(
        LlmPerfEvent(
            llm_call_id="llm-1",
            duration_ms=1000.0,
            model="glm-5.2",
            iteration=1,
            input_tokens=100,
            output_tokens=80,
            status="ok",
            reasoning_tokens=55,
        )
    )
    acc.record_llm(
        LlmPerfEvent(
            llm_call_id="llm-2",
            duration_ms=500.0,
            model="glm-5.2",
            iteration=2,
            input_tokens=120,
            output_tokens=40,
            status="ok",
            reasoning_tokens=12,
        )
    )
    acc.cache_read_tokens = 64

    summary = acc.finalize(status="ok", ended_at=1002.0)
    assert summary["summary"]["tokens"] == {
        "input": 220,
        "output": 120,
        "total": 340,
        "cache_read": 64,
        "reasoning": 67,
    }
    assert summary["bottleneck"]["llm"][0]["reasoning_tokens"] == 55


def test_extract_usage_tokens_reads_reasoning_from_usage_metadata() -> None:
    from jiuwenswarm.perf.extract import extract_usage_tokens

    direct = extract_usage_tokens(
        {
            "usage_metadata": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_tokens": 4,
                "reasoning_tokens": 7,
            }
        }
    )
    nested = extract_usage_tokens(
        SimpleNamespace(
            usage_metadata=SimpleNamespace(
                input_tokens=1,
                output_tokens=2,
                cache_read_input_tokens=0,
                cache_read_tokens=0,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=9),
                reasoning_tokens=0,
            )
        )
    )

    assert direct == (10, 20, 4, 7)
    assert nested == (1, 2, 0, 9)
    acc = RequestSummaryAccumulator(
        meta=RequestMeta(
            session_id="sess-1",
            request_id="req-1",
            channel_id="web",
            mode="plan",
            trace_id=None,
            started_at=1000.0,
        ),
        _include_by_agent=True,
    )
    acc.record_llm(
        LlmPerfEvent(
            llm_call_id="llm-1",
            duration_ms=1000.0,
            model="deepseek-chat",
            iteration=1,
            input_tokens=100,
            output_tokens=50,
            status="ok",
            agent_id="main_agent",
            task_id="todo:1",
        )
    )
    summary = acc.finalize(status="ok", ended_at=1001.0)
    assert summary["summary"]["stats"]["by_agent"]["main_agent"]["llm"]["count"] == 1


def test_accumulator_omits_errors_when_disabled() -> None:
    acc = RequestSummaryAccumulator(
        meta=RequestMeta(
            session_id="sess-1",
            request_id="req-1",
            channel_id="web",
            mode="plan",
            trace_id=None,
            started_at=1000.0,
        ),
        _include_errors=False,
    )
    acc.record_tool(
        ToolPerfEvent(
            tool_call_id="call-err",
            name="bash",
            duration_ms=100.0,
            status="error",
            error_message="command failed",
        )
    )

    summary = acc.finalize(status="ok", ended_at=1001.0)

    assert "errors" not in summary
    assert summary["summary"]["stats"]["tool"]["fail_count"] == 1


def test_accumulator_merges_subagent_into_total_stats() -> None:
    acc = RequestSummaryAccumulator(
        meta=RequestMeta(
            session_id="sess-1",
            request_id="req-1",
            channel_id="web",
            mode="plan",
            trace_id=None,
            started_at=1000.0,
        ),
        _include_errors=True,
    )
    iteration = 3
    acc.record_tool(
        ToolPerfEvent(
            tool_call_id="call-a",
            name="web_search",
            duration_ms=100.0,
            status="ok",
            iteration=iteration,
        )
    )
    acc.record_tool(
        ToolPerfEvent(
            tool_call_id="call-b",
            name="bash",
            duration_ms=200.0,
            status="error",
            iteration=iteration,
            error_message="command failed",
            agent_id="subagent_abc",
        )
    )
    acc.record_llm(
        LlmPerfEvent(
            llm_call_id="llm-err",
            duration_ms=50.0,
            model="glm",
            iteration=2,
            input_tokens=1,
            output_tokens=0,
            status="error",
            error_message="rate limited",
            agent_id="subagent_abc",
        )
    )

    summary = acc.finalize(status="ok", ended_at=1002.0)
    tool_stats = summary["summary"]["stats"]["tool"]
    llm_stats = summary["summary"]["stats"]["llm"]

    assert "subagent" not in summary["summary"]["stats"]
    assert tool_stats["count"] == 2
    assert tool_stats["round_count"] == 1
    assert tool_stats["list"] == [{"name": "bash", "count": 1}, {"name": "web_search", "count": 1}]
    assert llm_stats["count"] == 1
    assert len(summary["errors"]) == 2
    assert all(err["status"] == "error" for err in summary["errors"])
    assert "call_id" not in summary["errors"][1]
    assert summary["errors"][0]["duration_s"] == 0.2


def test_parallel_tool_timing_dict() -> None:
    from jiuwenswarm.perf.context import pop_tool_start, set_tool_start

    set_tool_start("call-1", "tool_a", 10.0)
    set_tool_start("call-2", "tool_b", 20.0)
    first = pop_tool_start("call-1")
    second = pop_tool_start("call-2")
    assert first == ("tool_a", 10.0)
    assert second == ("tool_b", 20.0)


def test_parallel_tool_timing_no_false_anon_match() -> None:
    from jiuwenswarm.perf.context import pop_tool_start, set_tool_start

    set_tool_start("", "bash", 10.0)
    set_tool_start("", "bash", 20.0)
    assert pop_tool_start("", "bash") is None

    set_tool_start("call-1", "tool_a", 10.0)
    set_tool_start("call-2", "tool_b", 20.0)
    assert pop_tool_start("", "tool_a") is None


def test_request_summary_rail_supports_record_only() -> None:
    """DeepAdapter rail wiring is deferred; package API must support record_only."""
    from jiuwenswarm.perf.request_summary_rail import RequestSummaryRail

    rail = RequestSummaryRail(record_only=True)
    assert rail._record_only is True


def test_extract_react_iteration_from_inputs() -> None:
    from types import SimpleNamespace

    from jiuwenswarm.perf.context import increment_react_iteration, reset_react_iteration
    from jiuwenswarm.perf.extract import extract_react_iteration

    reset_react_iteration()
    increment_react_iteration()
    increment_react_iteration()
    assert extract_react_iteration(SimpleNamespace(inputs=None)) == 2

    reset_react_iteration()
    ctx = SimpleNamespace(inputs=SimpleNamespace(iteration=3))
    assert extract_react_iteration(ctx) == 3
    assert extract_react_iteration(SimpleNamespace(inputs={"step": 5})) == 5


def test_extract_agent_id_prefers_subagent_and_card() -> None:
    from types import SimpleNamespace

    from jiuwenswarm.perf.extract import extract_agent_id

    card = SimpleNamespace(id="spawn_researcher", name="spawn_researcher")
    deep = SimpleNamespace(card=card)
    inner = SimpleNamespace(id="262923614bdb45709ece791ffb3d3409", card=None)

    assert extract_agent_id(inner, deep_agent=deep) == "spawn_researcher"
    assert extract_agent_id(inner, deep_agent=None) == ""


def test_extract_agent_id_prefers_deep_agent_over_contextvar(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from jiuwenswarm.perf.extract import extract_agent_id

    monkeypatch.setattr(
        "jiuwenswarm.perf.extract.resolve_subagent_id",
        lambda: "general-purpose",
    )
    parent = SimpleNamespace(card=SimpleNamespace(id="jiuwenswarm", name="jiuwenswarm"))
    assert extract_agent_id(None, deep_agent=parent) == "jiuwenswarm"


def test_get_request_context_returns_none_without_binding() -> None:
    from jiuwenswarm.perf.context import clear_request_context, get_request_context

    clear_request_context()
    assert get_request_context() is None


def test_accumulator_record_task_with_attributed_stats() -> None:
    acc = RequestSummaryAccumulator(
        meta=RequestMeta(
            session_id="sess-1",
            request_id="req-1",
            channel_id="web",
            mode="plan",
            trace_id=None,
            started_at=1000.0,
        ),
    )
    task_id = "todo:abc"
    acc.record_llm(
        LlmPerfEvent(
            llm_call_id="llm-1",
            duration_ms=100.0,
            model="glm",
            iteration=1,
            input_tokens=10,
            output_tokens=5,
            status="ok",
            task_id=task_id,
        )
    )
    acc.record_tool(
        ToolPerfEvent(
            tool_call_id="call-1",
            name="web_search",
            duration_ms=50.0,
            status="ok",
            task_id=task_id,
        )
    )
    acc.record_task(
        TaskPerfEvent(
            task_id=task_id,
            task_content="Stage 1",
            source="todo",
            started_at=1000.0,
            ended_at=1001.0,
            duration_ms=1000.0,
            status="succeeded",
        )
    )

    summary = acc.finalize(status="ok", ended_at=1002.0)
    assert summary["summary"]["stats"]["task"]["count"] == 1
    assert summary["summary"]["stats"]["unattributed_s"] == 0.0
    assert summary["tasks"][0]["stats"]["llm"]["count"] == 1
    assert summary["tasks"][0]["duration_s"] == 1.0


def test_collector_restores_accumulator_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_perf_summary_config()
    collector = PerfCollector()
    collector.begin_request(
        session_id="sess-z",
        request_id="req-z",
        channel_id="web",
        mode="agent.plan",
        started_at=1000.0,
    )

    write_attempts = {"count": 0}

    def _append_once_fail(*_args, **_kwargs) -> None:
        write_attempts["count"] += 1
        if write_attempts["count"] == 1:
            raise OSError("disk full")

    monkeypatch.setattr("jiuwenswarm.perf.collector.append_request_summary", _append_once_fail)
    monkeypatch.setattr("jiuwenswarm.perf.guard.logger.warning", lambda *_a, **_k: None)
    collector.finalize_request("req-z", status="ok", ended_at=1001.0)

    restored = collector.get_accumulator("req-z")
    assert restored is not None
    assert restored.flushed is False

    collector.finalize_request("req-z", status="ok", ended_at=1001.0)
    assert write_attempts["count"] == 2
    assert collector.get_accumulator("req-z") is None


def test_collector_does_not_prune_active_long_running_request() -> None:
    init_perf_summary_config()
    collector = PerfCollector()
    stale_started_at = time.time() - PerfCollector._STALE_ORPHAN_AGE_S - 60.0

    collector.begin_request(
        session_id="sess-long",
        request_id="req-long",
        channel_id="web",
        mode="agent.plan",
        started_at=stale_started_at,
    )
    collector.record_llm(
        "req-long",
        LlmPerfEvent(
            llm_call_id="llm-long",
            duration_ms=100.0,
            model="gpt-4",
            iteration=1,
            input_tokens=1,
            output_tokens=1,
            status="ok",
        ),
    )

    collector.begin_request(
        session_id="sess-other",
        request_id="req-other",
        channel_id="web",
        mode="agent.plan",
    )

    assert collector.get_accumulator("req-long") is not None
    assert "req-long" in collector._active_request_ids

    summary = collector.get_accumulator("req-long").finalize(status="ok")
    assert summary["summary"]["stats"]["llm"]["count"] == 1


def test_collector_prunes_inactive_orphan_accumulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_perf_summary_config()
    collector = PerfCollector()
    stale_started_at = time.time() - PerfCollector._STALE_ORPHAN_AGE_S - 60.0
    monkeypatch.setattr("jiuwenswarm.perf.collector.logger.warning", lambda *_a, **_k: None)

    collector.begin_request(
        session_id="sess-orphan",
        request_id="req-orphan",
        channel_id="web",
        mode="agent.plan",
        started_at=stale_started_at,
    )
    collector._active_request_ids.discard("req-orphan")

    collector.begin_request(
        session_id="sess-trigger",
        request_id="req-trigger",
        channel_id="web",
        mode="agent.plan",
    )

    assert collector.get_accumulator("req-orphan") is None
    assert collector.get_accumulator("req-trigger") is not None


def test_collector_finalize_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_perf_summary_config()
    monkeypatch.setattr(
        "jiuwenswarm.perf.writer.get_agent_sessions_dir",
        lambda: tmp_path,
    )

    collector = PerfCollector()
    collector.begin_request(
        session_id="sess-x",
        request_id="req-x",
        channel_id="web",
        mode="agent.plan",
        started_at=1000.0,
    )
    collector.record_llm(
        "req-x",
        LlmPerfEvent(
            llm_call_id="llm-x",
            duration_ms=800.0,
            model="gpt-4",
            iteration=1,
            input_tokens=10,
            output_tokens=5,
            status="ok",
        ),
    )
    collector.finalize_request("req-x", status="ok", ended_at=1001.0)
    from jiuwenswarm.perf.writer import flush_request_summary_writer

    flush_request_summary_writer()

    out_path = request_summaries_file("sess-x", str(tmp_path))
    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert payload["schema_version"] == 1
    assert payload["summary"]["stats"]["llm"]["total_s"] == 0.8


def test_collector_finalize_writes_tenant_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_perf_summary_config()
    monkeypatch.setattr(
        "jiuwenswarm.perf.writer.get_agent_sessions_dir",
        lambda: tmp_path,
    )

    collector = PerfCollector()
    collector.begin_request(
        session_id="sess-office",
        request_id="req-office",
        channel_id="officeclaw",
        mode="agent.plan",
        started_at=1000.0,
        service_id="default",
        agent_id="office",
    )
    collector.finalize_request("req-office", status="ok", ended_at=1001.0)
    from jiuwenswarm.perf.writer import flush_request_summary_writer

    flush_request_summary_writer()

    out_path = tmp_path / "sess-office" / "request_summaries.jsonl"
    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert payload["meta"]["request_id"] == "req-office"


def test_append_request_summary_sync_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.perf.writer.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    append_request_summary("sess-y", {"schema_version": 5, "meta": {"request_id": "req-y"}})
    from jiuwenswarm.perf.writer import flush_request_summary_writer

    flush_request_summary_writer()
    out_path = request_summaries_file("sess-y", str(tmp_path))
    assert "req-y" in out_path.read_text(encoding="utf-8")


def test_set_perf_summary_context_works_without_rail() -> None:
    """Rail build failure must not skip begin_request / ContextVar bind."""
    from jiuwenswarm.perf.collector import get_perf_collector
    from jiuwenswarm.perf.context import clear_request_context, get_request_context
    from jiuwenswarm.perf.interface_hooks import (
        clear_perf_summary_context,
        finalize_perf_summary_request,
        set_perf_summary_context,
    )

    clear_request_context()
    set_perf_summary_context(
        None,
        channel_id="web",
        session_id="sess-rail-none",
        request_id="req-rail-none",
        mode="agent",
    )
    assert get_request_context() is not None
    assert get_perf_collector().get_accumulator("req-rail-none") is not None
    finalize_perf_summary_request("req-rail-none", status="ok")
    clear_perf_summary_context()
    assert get_request_context() is None


def test_maybe_mark_first_byte_and_first_answer() -> None:
    from jiuwenswarm.perf.collector import get_perf_collector
    from jiuwenswarm.perf.context import clear_request_context, set_request_context
    from jiuwenswarm.perf.interface_hooks import maybe_mark_answer_first_byte

    clear_request_context()
    set_request_context(
        session_id="sess-fb",
        request_id="req-fb",
        channel_id="web",
        mode="agent",
    )
    acc = get_perf_collector().get_accumulator("req-fb")
    assert acc is not None

    maybe_mark_answer_first_byte({"event_type": "chat.tool_call", "tool_call": {}})
    assert acc.first_byte_latency_ms is not None
    assert acc.first_answer_latency_ms is None

    maybe_mark_answer_first_byte({"event_type": "chat.delta", "content": "hi"})
    assert acc.first_answer_latency_ms is not None
    clear_request_context()


def test_load_perf_summary_config_defaults_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERF_SUMMARY_ENABLED", raising=False)
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    from jiuwenswarm.perf import config as perf_config

    perf_config._CONFIG = None
    cfg = perf_config.load_perf_summary_config()
    assert cfg.enabled is True


def test_request_context_survives_without_contextvar() -> None:
    """Interaction-round Tasks do not inherit stream-handler ContextVar."""
    from contextvars import Context

    from jiuwenswarm.perf.context import (
        clear_request_context,
        get_request_context,
        normalize_session_key,
        set_request_context,
    )
    from jiuwenswarm.perf.request_summary_rail import RequestSummaryRail

    clear_request_context()
    set_request_context(
        session_id="web_parent",
        request_id="req-cross-task",
        channel_id="web",
        mode="agent",
    )
    rail = RequestSummaryRail(record_only=True)
    rail.bind_request_context(get_request_context())

    class _Session:
        def get_session_id(self) -> str:
            return "web_parent_sub_explore_abcd"

    class _Cb:
        session = _Session()

    def _via_rail_bind() -> str | None:
        # Empty Context: no task-local ContextVar.
        assert get_request_context() is None
        ctx = rail._active_request_context(_Cb())
        assert ctx is not None
        return str(ctx["request_id"])

    def _via_session_registry() -> str | None:
        assert get_request_context() is None
        unbound = RequestSummaryRail(record_only=True)
        ctx = unbound._active_request_context(_Cb())
        assert ctx is not None
        assert normalize_session_key("web_parent_sub_explore_abcd") == "web_parent"
        return str(ctx["request_id"])

    assert Context().run(_via_rail_bind) == "req-cross-task"
    assert Context().run(_via_session_registry) == "req-cross-task"
    clear_request_context(session_id="web_parent", request_id="req-cross-task")
    rail.clear_bound_request_context()
    assert get_request_context(session_id="web_parent") is None
