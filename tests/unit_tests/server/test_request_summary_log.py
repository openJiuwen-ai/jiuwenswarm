# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Monitoring summary log rendering and classification (issue #6745)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from jiuwenswarm.server import request_summary_log as rsl
from jiuwenswarm.server.request_summary_log import (
    build_summary_payload,
    classify_error_class,
    emit_request_summary,
    resolve_status,
)


@pytest.fixture(autouse=True)
def _reset_toggle_cache():
    """开关/级别 TTL 缓存在每个用例前后清空，避免用例间串味。"""
    rsl.reset_request_summary_cache()
    yield
    rsl.reset_request_summary_cache()


def _fake_request(metadata: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        request_id="req_abc123",
        session_id="s-xyz",
        channel_id="web",
        req_method=SimpleNamespace(value="chat.send"),
        permission_context=SimpleNamespace(
            triggering_user_id="u01", principal_user_id="owner"
        ),
        metadata=metadata if metadata is not None else {},
    )


def _perf_summary() -> dict:
    return {
        "meta": {
            "session_id": "s-xyz",
            "request_id": "req_abc123",
            "channel_id": "web",
            "mode": "agent.plan",
            "trace_id": "4bf92f35",
            "started_at": 1000.0,
            "ended_at": 1001.234,
        },
        "summary": {
            "total_s": 1.234,
            "first_byte_latency_s": 0.858,
            "first_answer_latency_s": 0.862,
            "status": "ok",
            "stats": {
                "llm": {
                    "count": 3,
                    "total_s": 0.890,
                    "fail_count": 0,
                    "max_s": 0.4,
                    "p90_duration_s": 0.4,
                    "model": "glm-5.2",
                    "ttft_ms": 233.1,
                    "tpot_ms": 45.2,
                },
                "tool": {
                    "count": 5,
                    "round_count": 2,
                    "total_s": 0.045,
                    "fail_count": 1,
                    "max_s": 0.022,
                    "p90_duration_s": 0.02,
                    "detail": {
                        "search_web": {
                            "count": 3,
                            "total_ms": 30.1,
                            "max_ms": 12.0,
                            "min_ms": 8.1,
                            "avg_ms": 10.0,
                            "errors": 1,
                        }
                    },
                },
                "task": {"count": 1, "total_s": 0.5, "fail_count": 0},
                "unattributed_s": 0.12,
            },
            "tokens": {
                "input": 4523,
                "output": 812,
                "total": 5335,
                "cache_read": 4096,
                "reasoning": 120,
            },
        },
        "stage_marks": {
            "init": 1000.045,
            "mcp": 1000.048,
            "pre_llm": 1000.856,
            "first_out": 1000.861,
        },
        "stream": {
            "chat_errors": 1,
            "ask_emitted": False,
            "thinking_count": 2,
            "thinking_total_ms": 210.5,
            "thinking_max_ms": 120.0,
            "thinking_segments": {"1": 120.0, "2": 90.5},
            "msg_gen_count": 1,
            "msg_gen_total_ms": 640.0,
            "msg_gen_max_ms": 300.0,
            "stream_gap_total_ms": 45.0,
            "stream_gap_max_ms": 35.0,
        },
        "skills": {"code_review": 2},
        "context_window": {"limit_tokens": 131072, "occupancy_rate": 0.41},
        "chunk_count": 42,
    }


def test_classify_error_class_maps_by_exception_name() -> None:
    assert classify_error_class(None) == "none"
    assert classify_error_class(TimeoutError("x")) == "timeout"

    PolicyError = type("TeamSessionBindingError", (Exception,), {})
    assert classify_error_class(PolicyError("x")) == "policy"

    AuthError = type("AuthTokenExpiredError", (Exception,), {})
    assert classify_error_class(AuthError("x")) == "auth"

    LlmError = type("ModelClientStreamError", (Exception,), {})
    assert classify_error_class(LlmError("x")) == "llm"

    ToolError = type("ToolExecutionError", (Exception,), {})
    assert classify_error_class(ToolError("x")) == "tool"

    assert classify_error_class(RuntimeError("x")) == "internal"


def test_resolve_status_more_severe_side_wins() -> None:
    assert resolve_status("ok", "ok") == "ok"
    assert resolve_status("ok", "error") == "failed"
    assert resolve_status("ok", "cancelled") == "cancelled"
    assert resolve_status("cancelled", "ok") == "cancelled"
    assert resolve_status("client_gone", "error") == "client_gone"
    assert resolve_status("failed", "ok") == "failed"


def test_build_summary_payload_full_render() -> None:
    request = _fake_request(metadata={"ext": {"source": "app"}})
    # t0 = recv; marks are absolute monotonic values relative to a fake origin.
    payload = build_summary_payload(
        request=request,
        pipeline_status="ok",
        pipeline_error=None,
        error_class="none",
        t0_monotonic=999.99,
        t_end_monotonic=1001.25,
        t0_wall=999.998,
        perf=_perf_summary(),
    )

    assert payload["v"] == 1
    assert payload["rid"] == "req_abc123"
    assert payload["sid"] == "s-xyz"
    assert payload["uid"] == "u01"
    assert payload["channel"] == "web"
    assert payload["source"] == "app"
    assert payload["method"] == "chat.send"
    assert payload["mode"] == "agent.plan"
    assert payload["trace_id"] == "4bf92f35"
    assert payload["status"] == "ok"
    assert payload["error_class"] == "none"
    assert payload["total_ms"] == payload["DurationMs"] == 1260.0
    # prep = adapter wall start (1000.0) - pipeline wall start (999.998)
    assert payload["prep_ms"] == 2.0
    # stage offsets measured from t0_monotonic (999.99): init 55ms, mcp 58ms,
    # pre_llm 866ms, first_out 871ms; clamped to the 1260ms window.
    assert payload["t_init_ms"] == 55.0
    assert payload["t_mcp_ms"] == 58.0
    assert payload["t_pre_llm_ms"] == 866.0
    assert payload["t_first_out_ms"] == 871.0
    assert payload["first_byte_ms"] == 858.0
    assert payload["first_answer_ms"] == 862.0

    assert payload["llm_calls"] == 3
    assert payload["llm_fail"] == 0
    assert payload["llm_ms"] == 890.0
    assert payload["llm_ttft_ms"] == 233.1
    assert payload["llm_tpot_ms"] == 45.2
    assert payload["model"] == "glm-5.2"

    assert payload["tok_in"] == 4523
    assert payload["tok_out"] == 812
    assert payload["tok_total"] == 5335
    assert payload["tok_cache"] == 4096
    assert payload["tok_reason"] == 120
    assert payload["ctx_window_tokens"] == 131072
    assert payload["ctx_occupancy"] == 0.41

    assert payload["tool_calls"] == 5
    assert payload["tool_fail"] == 1
    assert payload["tool_ms"] == 45.0
    assert payload["tool_max_ms"] == 22.0
    assert payload["tool_rounds"] == 2
    assert payload["tool_stats"]["search_web"]["errors"] == 1

    assert payload["thinking_total_ms"] == 210.5
    assert payload["thinking_max_ms"] == 120.0
    assert payload["thinking_stats"] == {"1": 120.0, "2": 90.5}
    assert payload["msg_gen_total_ms"] == 640.0
    assert payload["msg_gen_max_ms"] == 300.0
    # gap = total - llm - tool（1260 - 890 - 45），与 perf unattributed_s（0.12
    # → 120ms）无关 —— unattributed 是"未归属到 task"，会混入主 agent 时长。
    assert payload["gap_total_ms"] == 325.0
    assert payload["gap_max_ms"] == 35.0

    assert payload["skill_stats"] == {"code_review": 2}
    assert payload["chat_errors"] == 1
    assert payload["tasks"] == 1
    assert payload["task_fail"] == 0
    assert payload["chunks"] == 42
    assert "hitl_wait_total_ms" not in payload


def test_build_summary_payload_thin_line_without_perf() -> None:
    payload = build_summary_payload(
        request=_fake_request(),
        pipeline_status="failed",
        pipeline_error=RuntimeError("boom"),
        error_class="internal",
        t0_monotonic=10.0,
        t_end_monotonic=10.5,
        t0_wall=10.0,
        perf=None,
    )
    assert payload["status"] == "failed"
    assert payload["error_class"] == "internal"
    assert payload["total_ms"] == 500.0
    assert "llm_calls" in payload and payload["llm_calls"] == 0
    assert "mode" not in payload
    # uid comes from the request itself, not from perf.
    assert payload["uid"] == "u01"
    assert "chunks" not in payload


def test_pipeline_error_downgrades_to_none_for_cancel() -> None:
    payload = build_summary_payload(
        request=_fake_request(),
        pipeline_status="cancelled",
        pipeline_error=None,
        error_class="none",
        t0_monotonic=10.0,
        t_end_monotonic=10.5,
        t0_wall=10.0,
        perf=None,
    )
    assert payload["status"] == "cancelled"
    assert payload["error_class"] == "none"


def test_perf_error_status_promotes_pipeline_ok_to_failed() -> None:
    perf = _perf_summary()
    perf["summary"]["status"] = "error"
    payload = build_summary_payload(
        request=_fake_request(),
        pipeline_status="ok",
        pipeline_error=None,
        error_class="none",
        t0_monotonic=999.99,
        t_end_monotonic=1001.25,
        t0_wall=999.998,
        perf=perf,
    )
    assert payload["status"] == "failed"
    # errors[] is config-gated in production; the fixture has none, so the
    # fail-counter fallback classifies by tool.fail_count=1.
    assert payload["error_class"] == "tool"

    perf_with_errors = _perf_summary()
    perf_with_errors["summary"]["status"] = "error"
    perf_with_errors["errors"] = [{"kind": "llm", "error": "boom"}]
    payload = build_summary_payload(
        request=_fake_request(),
        pipeline_status="ok",
        pipeline_error=None,
        error_class="none",
        t0_monotonic=999.99,
        t_end_monotonic=1001.25,
        t0_wall=999.998,
        perf=perf_with_errors,
    )
    assert payload["error_class"] == "llm"


def test_emit_request_summary_is_valid_single_line_json(caplog) -> None:
    payload = build_summary_payload(
        request=_fake_request(),
        pipeline_status="ok",
        pipeline_error=None,
        error_class="none",
        t0_monotonic=10.0,
        t_end_monotonic=10.5,
        t0_wall=10.0,
        perf=None,
    )
    with caplog.at_level(logging.INFO, logger="jiuwenswarm.server.request_summary"):
        emit_request_summary(payload)

    assert caplog.records, "summary record should be emitted"
    record = caplog.records[-1]
    # Text mode: message itself is compact single-line JSON.
    assert "\n" not in record.message
    assert json.loads(record.message) == payload
    # JSON mode: flat payload merged into record dict via extra=.
    for key, value in payload.items():
        assert getattr(record, key) == value


def test_emit_request_summary_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("logger closed")

    monkeypatch.setattr(rsl.logger, "log", _boom)
    with caplog.at_level(logging.WARNING):
        emit_request_summary({"rid": "req-1", "total_ms": 1.0})
    assert any("request_summary" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 开关与级别（env > config，TTL 缓存）：emit 必须真正被 _summary_enabled 关住。
# ---------------------------------------------------------------------------


def _disable_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.gateway.config.logging import access as logging_access

    monkeypatch.setattr(
        logging_access,
        "get_logging_config_section_sync",
        lambda: {"request_summary": {"enabled": False}},
        raising=True,
    )


def test_emit_request_summary_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setenv(rsl._ENV_ENABLED, "0")
    with caplog.at_level(logging.INFO, logger="jiuwenswarm.server.request_summary"):
        emit_request_summary({"rid": "req-off", "total_ms": 1.0})
    assert not caplog.records


def test_emit_request_summary_disabled_by_config(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.delenv(rsl._ENV_ENABLED, raising=False)
    _disable_by_config(monkeypatch)
    with caplog.at_level(logging.INFO, logger="jiuwenswarm.server.request_summary"):
        emit_request_summary({"rid": "req-off", "total_ms": 1.0})
    assert not caplog.records


def test_emit_request_summary_env_true_overrides_config(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setenv(rsl._ENV_ENABLED, "1")
    _disable_by_config(monkeypatch)
    with caplog.at_level(logging.INFO, logger="jiuwenswarm.server.request_summary"):
        emit_request_summary({"rid": "req-on", "total_ms": 1.0})
    assert caplog.records


def test_summary_level_prefers_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rsl._ENV_LEVEL, "warning")
    assert rsl._summary_level() == logging.WARNING


def test_toggle_cache_resolves_once_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.gateway.config.logging import access as logging_access

    calls: list[int] = []

    def _fake_section() -> dict:
        calls.append(1)
        return {"request_summary": {"enabled": False}}

    monkeypatch.delenv(rsl._ENV_ENABLED, raising=False)
    monkeypatch.setattr(
        logging_access,
        "get_logging_config_section_sync",
        _fake_section,
        raising=True,
    )
    assert rsl._summary_enabled() is False
    assert rsl._summary_enabled() is False
    # TTL 内只解析一次，避免逐条汇总都走同步配置读取（临时线程）。
    assert calls == [1]
    rsl.reset_request_summary_cache()
    assert rsl._summary_enabled() is False
    assert calls == [1, 1]


# ---------------------------------------------------------------------------
# pipeline._emit_monitoring_summary：HITL 弹窗等待归账（flash/officeclaw 的
# 恢复以新的 chat.send 到达，等待记在第二条 chat 行上）。
# ---------------------------------------------------------------------------


def _chat_request(rid: str, sid: str, method: str = "chat.send") -> SimpleNamespace:
    from jiuwenswarm.common.schema.message import ReqMethod

    return SimpleNamespace(
        request_id=rid,
        session_id=sid,
        channel_id="officeclaw",
        req_method=ReqMethod(method),
        permission_context=SimpleNamespace(
            triggering_user_id="u01", principal_user_id="owner"
        ),
        metadata={},
    )


def _capture_summary(monkeypatch: pytest.MonkeyPatch, collector) -> list[dict]:
    from jiuwenswarm.server import pipeline

    monkeypatch.setattr(pipeline, "get_perf_collector", lambda: collector)
    captured: list[dict] = []
    monkeypatch.setattr(pipeline, "emit_request_summary", captured.append)
    return captured


def _emit(request: SimpleNamespace, *, t0_wall: float) -> None:
    from jiuwenswarm.server import pipeline

    pipeline._emit_monitoring_summary(
        request,
        t0_monotonic=100.0,
        t0_wall=t0_wall,
        pipeline_status="ok",
        pipeline_error=None,
        error_class="none",
    )


def test_hitl_wait_attributed_to_followup_chat_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.perf.collector import PerfCollector

    collector = PerfCollector()
    collector._register_hitl_ask_locked("rid-ask", "sess-1", 1000.0)
    captured = _capture_summary(monkeypatch, collector)

    # 弹窗后的恢复请求：flash/officeclaw 下仍是 chat.send，等 2.0s。
    _emit(_chat_request("rid-resume", "sess-1"), t0_wall=1002.0)
    assert collector._hitl_asks == {}
    payload = captured[-1]
    assert payload["hitl_wait_total_ms"] == 2000.0
    assert payload["hitl_wait_count"] == 1


def test_ask_line_does_not_consume_own_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.perf.collector import PerfCollector

    collector = PerfCollector()
    collector._register_hitl_ask_locked("rid-ask", "sess-2", 1000.0)
    captured = _capture_summary(monkeypatch, collector)

    # ask 行自身：exclude 生效 —— 不记等待，登记保留给恢复行。
    _emit(_chat_request("rid-ask", "sess-2"), t0_wall=1001.5)
    payload = captured[-1]
    assert "hitl_wait_total_ms" not in payload
    assert "hitl_wait_count" not in payload
    assert collector._hitl_asks["sess-2"] == ("rid-ask", 1000.0)


def test_non_chat_request_leaves_hitl_registration_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.perf.collector import PerfCollector

    collector = PerfCollector()
    collector._register_hitl_ask_locked("rid-ask", "sess-3", 1000.0)
    captured = _capture_summary(monkeypatch, collector)

    _emit(_chat_request("rid-rpc", "sess-3", method="mcp.server.list"), t0_wall=1005.0)
    assert collector._hitl_asks["sess-3"] == ("rid-ask", 1000.0)
    assert "hitl_wait_total_ms" not in captured[-1]
