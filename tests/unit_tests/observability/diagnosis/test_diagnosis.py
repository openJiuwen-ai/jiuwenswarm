# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""诊断包单测：信号识别 / 窗口定位 / 证据投影 / 异常栈 / 序列化。

覆盖 G1 骨架核心路径。OTLP 构造参照 test_trajectory_projection.py 的风格。
"""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.observability.diagnosis import (
    DiagnosisContext,
    DiagnosisEvidence,
    DiagnosisTrigger,
)
from jiuwenswarm.observability.diagnosis.evidence import (
    SpanRecord,
    collect_evidence,
    decode_span_records,
    detect_trigger,
    locate_failure_window,
)
from jiuwenswarm.observability.diagnosis.budget import cleanup_expired_evidence, persist_full_evidence
from jiuwenswarm.observability.config import load_diagnosis_settings


def _attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def _otlp(spans: list[dict]) -> dict:
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def _span(
    span_id: str,
    name: str,
    *,
    start: int = 1000,
    end: int = 2000,
    parent: str = "",
    attributes: list[dict] | None = None,
    status: str | None = None,
    events: list[dict] | None = None,
) -> dict:
    span: dict = {
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": attributes or [],
        "events": events or [],
    }
    if parent:
        span["parentSpanId"] = parent
    if status:
        span["status"] = {"code": status}
    return span


def _ctx(records: list[SpanRecord], **kw) -> DiagnosisContext:
    defaults = dict(
        session_id="sess-1",
        trace_id="trace-1",
        user_note=None,
        requested_mode=None,
        records=records,
    )
    defaults.update(kw)
    return DiagnosisContext(**defaults)


# ---------------------------------------------------------------------------
# 信号识别
# ---------------------------------------------------------------------------


def test_detect_trigger_error_from_has_error_span() -> None:
    records = [SpanRecord("trace-1", "req-1", 1000, 2000, True, "ok",
                          [_span("s1", "tool.call", status="ERROR")])]
    assert detect_trigger(_ctx(records)) is DiagnosisTrigger.ERROR


def test_detect_trigger_unexpected_when_no_signal() -> None:
    records = [SpanRecord("trace-1", "req-1", 1000, 2000, False, "ok",
                          [_span("s1", "llm.call")])]
    assert detect_trigger(_ctx(records)) is DiagnosisTrigger.UNEXPECTED


def test_detect_trigger_interrupt_from_running_lifecycle() -> None:
    """record lifecycle 非 final（如 running）→ INTERRUPT。"""
    records = [SpanRecord("trace-1", "req-1", 1000, 2000, False, "running",
                          [_span("s1", "agent.run", start=1000, end=2000)])]
    assert detect_trigger(_ctx(records)) is DiagnosisTrigger.INTERRUPT


def test_detect_trigger_interrupt_from_forced_close() -> None:
    """run root span forced_close 属性 → INTERRUPT。"""
    forced = [_attr("openjiuwen.span.forced_close", "true")]
    spans = [_span("s1", "agent.run", attributes=forced)]
    records = [SpanRecord("trace-1", "req-1", 1000, 2000, False, "final", spans)]
    assert detect_trigger(_ctx(records)) is DiagnosisTrigger.INTERRUPT


def test_detect_trigger_respects_explicit_mode() -> None:
    records = [SpanRecord("trace-1", "req-1", 1000, 2000, True, "ok",
                          [_span("s1", "tool.call", status="ERROR")])]
    ctx = _ctx(records, requested_mode="unexpected")
    assert detect_trigger(ctx) is DiagnosisTrigger.UNEXPECTED


# ---------------------------------------------------------------------------
# 窗口定位
# ---------------------------------------------------------------------------


def test_window_anchors_on_earliest_error_span() -> None:
    # 决策 2：窗口覆盖全局 trace（[run_start, run_end]），不做时间窗裁剪；
    # anchor 仍记录最早错误 span（s2），仅作元信息，不再用于裁剪 in_window。
    spans = [
        _span("s1", "llm.call", start=1000, end=2000),
        _span("s2", "tool.call", start=3000, end=4000, status="ERROR"),
        _span("s3", "llm.call", start=5000, end=6000),
    ]
    records = [SpanRecord("trace-1", "req-1", 1000, 6000, True, "ok", spans)]
    window = locate_failure_window(_ctx(records), DiagnosisTrigger.ERROR)
    assert window.anchor_span_id == "s2"
    assert window.start_unix_nano == 1000  # 全局窗口起于 run_start，非 anchor 起点
    assert window.end_unix_nano == 6000
    # 全局窗口：全部 span 都在窗口内
    assert set(window.in_window) == {"s1", "s2", "s3"}


def test_window_unexpected_takes_tail() -> None:
    spans = [
        _span("s1", "llm.call", start=1000, end=2000),
        _span("s2", "llm.call", start=5000, end=6000),
    ]
    records = [SpanRecord("trace-1", "req-1", 1000, 6000, False, "ok", spans)]
    window = locate_failure_window(_ctx(records), DiagnosisTrigger.UNEXPECTED)
    assert window.start_unix_nano == 1000  # tail 1-2 turn 起点取最小
    assert window.end_unix_nano == 6000


# ---------------------------------------------------------------------------
# 证据投影 + 异常栈提取
# ---------------------------------------------------------------------------


def test_collect_evidence_extracts_llm_io_and_stacktrace() -> None:
    # 纳秒级时间戳（真实 OTLP 语义）：start=1_000_000_000ns, event=1_500_000_000ns
    spans = [
        _span(
            "s1",
            "llm.call",
            start=1_000_000_000,
            end=2_000_000_000,
            status="ERROR",
            attributes=[
                _attr("gen_ai.input.messages", "prompt-1"),
                _attr("gen_ai.output.messages", "resp-1"),
                _attr("gen_ai.usage.input_tokens", "100"),
            ],
            events=[
                {
                    "name": "exception",
                    "timeUnixNano": "1500000000",
                    "attributes": [
                        _attr("exception.stacktrace", "file.py:10 in foo"),
                    ],
                }
            ],
        ),
    ]
    records = [SpanRecord("trace-1", "req-1", 1_000_000_000, 2_000_000_000, True, "ok", spans)]
    window = locate_failure_window(_ctx(records), DiagnosisTrigger.ERROR)
    evidence = collect_evidence(_ctx(records), DiagnosisTrigger.ERROR, window)

    assert len(evidence.llm_io) == 1
    assert evidence.llm_io[0]["input_messages"] == "prompt-1"
    assert evidence.llm_io[0]["output_messages"] == "resp-1"
    assert evidence.llm_io[0]["ttft_ms"] == 500  # (1.5e9-1e9)//1e6 = 500ms
    assert evidence.llm_io[0]["stacktrace"] == "file.py:10 in foo"
    assert len(evidence.failure_spans) == 1
    assert evidence.failure_spans[0]["stacktrace"] == "file.py:10 in foo"


def test_collect_evidence_context_commit_and_compaction() -> None:
    spans = [
        _span("s1", "llm.call", start=1000, end=2000,
              attributes=[_attr("context_window_commit", "commit-1")]),
        _span("s2", "llm.call", start=3000, end=4000,
              attributes=[_attr("compaction.completed", "compact-1")]),
    ]
    records = [SpanRecord("trace-1", "req-1", 1000, 4000, False, "ok", spans)]
    window = locate_failure_window(_ctx(records), DiagnosisTrigger.UNEXPECTED)
    evidence = collect_evidence(_ctx(records), DiagnosisTrigger.UNEXPECTED, window)

    assert len(evidence.context_commits) == 1
    assert evidence.context_commits[0]["commit"] == "commit-1"
    assert len(evidence.compaction_events) == 1


def test_global_window_keeps_all_spans() -> None:
    """决策 2：全局窗口下所有 span 都在窗口内，llm.call span 进 llm_io。"""
    spans = [
        _span("s1", "llm.call", start=1000, end=2000,
              attributes=[_attr("gen_ai.output.messages", "out-1")]),
        _span("s2", "tool.call", start=3000, end=4000, status="ERROR",
              attributes=[_attr("tool_failure_reason", "boom")]),
    ]
    records = [SpanRecord("trace-1", "req-1", 1000, 4000, True, "ok", spans)]
    window = locate_failure_window(_ctx(records), DiagnosisTrigger.ERROR)
    # 全局窗口：s1、s2 都在窗口内（不再按时间窗裁剪）
    assert set(window.in_window) == {"s1", "s2"}
    evidence = collect_evidence(_ctx(records), DiagnosisTrigger.ERROR, window)

    summary_ids = {s["span_id"] for s in evidence.span_summaries}
    assert "s1" in summary_ids and "s2" in summary_ids
    # s1 是 llm.call 且在窗口内 → 进 llm_io（不再有"窗口外"概念）
    llm_ids = {item["span_id"] for item in evidence.llm_io}
    assert "s1" in llm_ids


# ---------------------------------------------------------------------------
# decode_span_records
# ---------------------------------------------------------------------------


def test_decode_span_records_skips_missing_otlp() -> None:
    raw = [
        {"trace_id": "t1", "request_id": "r1", "otlp": _otlp([_span("s1", "llm.call")]),
         "start_time_unix_nano": "1000", "end_time_unix_nano": "2000",
         "has_error": 0, "lifecycle": "ok"},
        {"trace_id": "t2", "request_id": None, "otlp": None},  # 跳过
    ]
    records = decode_span_records(raw)
    assert len(records) == 1
    assert records[0].trace_id == "t1"
    assert records[0].request_id == "r1"


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def test_evidence_offline_flag_persisted(tmp_path) -> None:
    """离线（无 records）→ offline=true 标记随证据包落盘（prompt 据此豁免时间窗约束）。"""
    ctx = _ctx([], trace_id="")
    window = locate_failure_window(ctx, DiagnosisTrigger.ERROR)
    evidence = collect_evidence(ctx, DiagnosisTrigger.ERROR, window)
    assert evidence.offline is True
    path = persist_full_evidence(evidence, tmp_path / "diag")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["offline"] is True


# ---------------------------------------------------------------------------
# prompt 组装容错
# ---------------------------------------------------------------------------


def test_build_agent_prompt_tolerates_unusual_user_note() -> None:
    """user_note 非字符串 / 纯空白 → 不抛异常，按无 note 处理（fallback 标题）。"""
    from jiuwenswarm.observability.diagnosis.prompts import build_agent_prompt

    for unusual in (None, "", "   ", "\n \n", 123, ["not", "a", "string"]):
        prompt = build_agent_prompt(
            evidence_path="/tmp/evidence.json",
            log_dir="/tmp/logs",
            window="0 ~ 0 (unix nano)",
            user_note=unusual,
            session_id="sess-1",
        )
        # 非 str 一律视为无 note：不出现「用户补充描述」块
        if not isinstance(unusual, str) or not unusual.strip():
            assert "用户补充描述" not in prompt


def test_build_agent_prompt_note_title_and_fallback() -> None:
    from jiuwenswarm.observability.diagnosis.prompts import build_agent_prompt

    prompt = build_agent_prompt(
        evidence_path="/tmp/evidence.json",
        log_dir="",
        window="0 ~ 0 (unix nano)",
        user_note="多行\n描述",
        session_id="sess-1",
    )
    assert "## 关于「多行」的诊断报告" in prompt

    prompt = build_agent_prompt(
        evidence_path="/tmp/evidence.json",
        log_dir="",
        window="0 ~ 0 (unix nano)",
        user_note=None,
        session_id="sess-abcdef123456",
    )
    assert "## 诊断报告（abcdef123456）" in prompt


# ---------------------------------------------------------------------------
# 配置加载与启动校验
# ---------------------------------------------------------------------------


def test_load_diagnosis_settings_disabled_by_default() -> None:
    settings = load_diagnosis_settings({"trajectory_ui": {"enabled": False}})
    assert settings.enabled is False


def test_load_diagnosis_settings_allows_trajectory_ui_disabled() -> None:
    """diagnosis.enabled=true 但 trajectory_ui 未开启 → 不再 raise（离线纯日志诊断不依赖 trajectory store）。

    离线模式（offline=true）只需日志、不需 trace；在线诊断由端点查 trace 时自行 404。
    """
    cfg = {"diagnosis": {"enabled": True}, "trajectory_ui": {"enabled": False}}
    settings = load_diagnosis_settings(cfg)
    assert settings.enabled is True


def test_load_diagnosis_settings_ok_when_trajectory_enabled() -> None:
    cfg = {
        "diagnosis": {"enabled": True},
        "trajectory_ui": {"enabled": True},
    }
    settings = load_diagnosis_settings(cfg)
    assert settings.enabled is True


def test_cleanup_expired_evidence_removes_old_files(tmp_path) -> None:
    """过期文件清理：超过 keep_days 的 evidence/report/uploads 被删。"""
    import os
    import time as _time

    session_dir = tmp_path / "sess-1"
    session_dir.mkdir()
    uploads = session_dir / "uploads"
    uploads.mkdir()

    # 过期文件（mtime 设为 10 天前）
    old_evidence = session_dir / "evidence-old.json"
    old_evidence.write_text("{}", encoding="utf-8")
    old_report = session_dir / "report-old.md"
    old_report.write_text("old", encoding="utf-8")
    old_upload = uploads / "old.log"
    old_upload.write_text("old log", encoding="utf-8")
    old_ts = _time.time() - 10 * 86400
    for p in (old_evidence, old_report, old_upload):
        os.utime(p, (old_ts, old_ts))

    # 新文件（当前时间）
    new_evidence = session_dir / "evidence-new.json"
    new_evidence.write_text("{}", encoding="utf-8")

    removed = cleanup_expired_evidence(tmp_path, keep_days=7)
    assert removed == 3
    assert not old_evidence.exists()
    assert not old_report.exists()
    assert not old_upload.exists()
    assert new_evidence.exists()


def test_diagnosis_session_prefix_filters() -> None:
    """diagnosis_ 前缀三处隔离：oneshot 回收 / 会话列表隐藏 / trace sink drop。"""
    # 1) oneshot 回收（session_manager）
    from jiuwenswarm.server.runtime.session.session_manager import SessionManager

    assert SessionManager._is_oneshot_session("diagnosis_4d2_074e6c") is True
    assert SessionManager._is_oneshot_session("heartbeat_1_2") is True
    assert SessionManager._is_oneshot_session("cron_1_job") is True
    assert SessionManager._is_oneshot_session("sess-normal") is False

    # 2) 会话列表不可见（session_metadata）
    from jiuwenswarm.server.runtime.session.session_metadata import _is_internal_session_dir

    assert _is_internal_session_dir("diagnosis_4d2_074e6c") is True
    assert _is_internal_session_dir("sess-normal") is False


def test_build_history_summary_tail_and_truncation() -> None:
    """history 尾部摘要：取最后 N 条 + content 截断 + 无法识别结构原样投影。"""
    from jiuwenswarm.observability.diagnosis.prompts import build_history_summary

    records = [
        {"role": "user", "content": f"消息{i}"}
        for i in range(30)
    ]
    summary = build_history_summary(records, max_records=5)
    # 尾部 5 条
    assert "消息25" in summary and "消息29" in summary
    assert "消息0" not in summary and "消息20" not in summary

    # 超长 content 截断
    long_records = [{"role": "assistant", "content": "x" * 500}]
    summary = build_history_summary(long_records, max_records=1)
    assert len(summary) < 500

    # 空 history → 占位说明（摘要层照常拼 prompt，Agent 不误读为缺证据）
    assert "无对话历史" in build_history_summary([], max_records=5)
