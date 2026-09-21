# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""诊断编排单测（v2 Agent 化链路）：mock agent_client，验证
``run_diagnosis_agent`` 的 SSE 事件流 + meta 元事件 + 报告落存。

不实际调 AgentServer / LLM：``agent_client.send_request_stream`` 注入
返回假 chunk 流的 stub。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.observability.diagnosis import DiagnosisContext
from jiuwenswarm.observability.diagnosis.agent_client_adapter import (
    DiagnosisAgentRequest,
    build_diagnosis_envelope,
    map_agent_chunk,
    make_diagnosis_session_id,
)
from jiuwenswarm.observability.diagnosis.analyzer import (
    collect_diagnosis_inputs,
    persist_report,
    run_diagnosis_agent,
)
from jiuwenswarm.observability.diagnosis.evidence import SpanRecord


class _FakeChunk:
    def __init__(self, payload: dict[str, Any], is_complete: bool = False) -> None:
        self.payload = payload
        self.is_complete = is_complete


class _FakeAgentClient:
    """Stub：记录收到的信封，回放预置 chunk 流。"""

    def __init__(self, chunks: list[_FakeChunk]) -> None:
        self._chunks = chunks
        self.envelopes: list[Any] = []

    async def send_request_stream(self, envelope):
        self.envelopes.append(envelope)
        for chunk in self._chunks:
            yield chunk


def _span(span_id: str, name: str, start: int = 1_000_000_000, end: int = 2_000_000_000, **kw) -> dict:
    span: dict = {
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": [],
        "events": [],
    }
    if kw.get("status"):
        span["status"] = {"code": kw["status"]}
    return span


def _ctx(**overrides) -> DiagnosisContext:
    records = [SpanRecord("trace-1", "req-1", 1_000_000_000, 2_000_000_000, True, "ok",
                          [_span("s1", "tool.call", status="ERROR")])]
    defaults = dict(
        session_id="sess-1",
        trace_id="trace-1",
        user_note=None,
        requested_mode=None,
        records=records,
    )
    defaults.update(overrides)
    return DiagnosisContext(**defaults)


# ---------------------------------------------------------------------------
# agent_client_adapter 纯函数
# ---------------------------------------------------------------------------


def test_make_diagnosis_session_id_format() -> None:
    sid = make_diagnosis_session_id(now_ts=1234.5)
    assert sid.startswith("diagnosis_")
    # ts_hex + 6 位随机 hex
    assert len(sid.split("_")) == 3
    assert sid != make_diagnosis_session_id(now_ts=1234.5)


def test_build_diagnosis_envelope_fields() -> None:
    env = build_diagnosis_envelope(
        DiagnosisAgentRequest(prompt="任务书", project_dir="/repo/x", mode="agent"),
        session_id="diagnosis_abc_def",
        now_ts=1000.0,
    )
    assert env.channel == "diagnosis"
    assert env.method == "chat.send"
    assert env.session_id == "diagnosis_abc_def"
    assert env.is_stream is True
    assert env.params["content"] == "任务书"
    assert env.params["query"] == "任务书"
    assert env.params["project_dir"] == "/repo/x"
    # run.kind 必须是 RunKind 合法值（normal/heartbeat/cron/goal），非法值在
    # DeepAgent._normalize_inputs 抛 ValueError → execution.error → 空报告
    assert env.params["run"]["kind"] == "normal"
    assert env.params["run"]["context"]["session_id"] == "diagnosis_abc_def"
    assert env.params["run"]["context"]["extra"] == {"diagnosis": True}


def test_build_diagnosis_envelope_omits_empty_project_dir() -> None:
    env = build_diagnosis_envelope(
        DiagnosisAgentRequest(prompt="p"),
        session_id="diagnosis_x_y",
        now_ts=1000.0,
    )
    assert "project_dir" not in env.params


def test_map_agent_chunk_token() -> None:
    event = map_agent_chunk(_FakeChunk({"event_type": "chat.delta", "content": "根因"}))
    assert event == {"type": "token", "text": "根因"}


def test_map_agent_chunk_tool_update() -> None:
    event = map_agent_chunk(
        _FakeChunk({"event_type": "chat.tool_update", "tool_name": "grep", "status": "running"})
    )
    assert event == {"type": "progress", "stage": "tool", "detail": "grep [running]"}


def test_map_agent_chunk_execution_error() -> None:
    # DeepAgent 交互循环错误：payload 带 code/message（无 error 字段），
    # 漏映射会静默吞掉真实失败原因（空报告假象）
    event = map_agent_chunk(
        _FakeChunk(
            {
                "event_type": "execution.error",
                "code": "execution_error",
                "message": "agent 启动失败",
                "goal": None,
            }
        )
    )
    assert event == {"type": "error", "message": "agent 启动失败"}


def test_map_agent_chunk_final_text() -> None:
    event = map_agent_chunk(_FakeChunk({"event_type": "chat.final", "content": "全文"}))
    assert event == {"type": "final", "text": "全文"}


def test_map_agent_chunk_ignores_other_events() -> None:
    assert map_agent_chunk(_FakeChunk({"event_type": "chat.usage_summary"})) is None
    assert map_agent_chunk(_FakeChunk({"event_type": "chat.notice"})) is None
    assert map_agent_chunk(_FakeChunk("not-a-dict")) is None


# ---------------------------------------------------------------------------
# collect_diagnosis_inputs：证据收集 + 任务书
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_diagnosis_inputs_returns_prompt_and_evidence(tmp_path: Path) -> None:
    inputs = await collect_diagnosis_inputs(
        _ctx(user_note="something weird"),
        diagnosis_dir=tmp_path / "diag",
        include_logs=False,
    )
    assert inputs["trigger"] == "error"
    evidence_path = Path(inputs["evidence_path"])
    assert evidence_path.exists()
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["session_id"] == "sess-1"
    assert payload["user_note"] == "something weird"
    # 任务书嵌证据包绝对路径与时间线摘要
    assert str(evidence_path) in inputs["prompt"]
    assert "trace 时间线" in inputs["prompt"]
    # 在线模式（有 records）窗口为全局 trace 描述（区别于离线占位值）
    assert "全局 trace" in inputs["prompt"]
    assert inputs["window"] == (1_000_000_000, 2_000_000_000)


@pytest.mark.asyncio
async def test_collect_diagnosis_inputs_offline_window_placeholder(tmp_path: Path) -> None:
    inputs = await collect_diagnosis_inputs(
        _ctx(records=[], trace_id=""),
        diagnosis_dir=tmp_path / "diag",
        include_logs=False,
    )
    # 离线模式窗口占位说明进任务书（约束 A 例外条款）
    assert "离线模式占位值" in inputs["prompt"]


# ---------------------------------------------------------------------------
# run_diagnosis_agent：事件流 + 信封 + 上抛
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_diagnosis_agent_event_sequence(tmp_path: Path) -> None:
    client = _FakeAgentClient([
        _FakeChunk({"event_type": "chat.tool_update", "tool_name": "grep", "status": "running"}),
        _FakeChunk({"event_type": "chat.delta", "content": "## 诊断报告\n"}),
        _FakeChunk({"event_type": "chat.delta", "content": "根因：工具异常"}),
        _FakeChunk({"event_type": "chat.final", "content": ""}, is_complete=True),
    ])
    events = []
    async for event in run_diagnosis_agent(
        client,
        _ctx(),
        diagnosis_dir=tmp_path / "diag",
        include_logs=False,
        project_dir="/repo/proj",
        session_id_factory=lambda: "diagnosis_fixed_1",
    ):
        events.append(event)

    types = [e["type"] for e in events]
    assert types[0] == "progress" and events[0]["stage"] == "collect_evidence"
    assert "meta" in types
    assert "token" in types
    assert "final" not in types  # 空 final 被映射层跳过

    # meta 事件携带落盘所需元数据
    meta = next(e for e in events if e["type"] == "meta")
    assert meta["diag_session_id"] == "diagnosis_fixed_1"
    assert meta["window"] == [1_000_000_000, 2_000_000_000]
    assert Path(meta["evidence_path"]).exists()

    # 工具动态映射为 progress
    tool_progress = [e for e in events if e.get("stage") == "tool"]
    assert tool_progress and tool_progress[0]["detail"] == "grep [running]"

    # 发往 AgentServer 的信封：channel/project_dir/mode 口径
    assert len(client.envelopes) == 1
    env = client.envelopes[0]
    assert env.channel == "diagnosis"
    assert env.session_id == "diagnosis_fixed_1"
    assert env.params["project_dir"] == "/repo/proj"
    assert env.params["mode"] == "agent"
    assert env.params["run"]["kind"] == "normal"


@pytest.mark.asyncio
async def test_run_diagnosis_agent_propagates_error_event(tmp_path: Path) -> None:
    client = _FakeAgentClient([
        _FakeChunk({"event_type": "chat.error", "error": "agent 内部失败"}),
    ])
    events = []
    async for event in run_diagnosis_agent(
        client, _ctx(), diagnosis_dir=tmp_path / "diag", include_logs=False,
    ):
        events.append(event)
    assert events[-1] == {"type": "error", "message": "agent 内部失败"}


@pytest.mark.asyncio
async def test_run_diagnosis_agent_stream_failure_raises(tmp_path: Path) -> None:
    class _BrokenClient:
        async def send_request_stream(self, envelope):
            raise RuntimeError("AgentServer connection closed")
            yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="AgentServer connection closed"):
        async for _ in run_diagnosis_agent(
            _BrokenClient(), _ctx(), diagnosis_dir=tmp_path / "diag", include_logs=False,
        ):
            pass


# ---------------------------------------------------------------------------
# persist_report：沿用 v1 格式（历史报告端点 _parse_report_meta 依赖）
# ---------------------------------------------------------------------------


def test_persist_report_header_format(tmp_path: Path) -> None:
    path = persist_report(
        tmp_path / "diag",
        "sess-1",
        "# 诊断报告\n\n根因：xxx",
        evidence_path="/tmp/evidence-1.json",
        trace_id="trace-1",
        window=(100, 200),
    )
    text = Path(path).read_text(encoding="utf-8")
    assert "> session: sess-1" in text
    assert "> trace: trace-1" in text
    assert "> window: 100 - 200" in text
    assert "> evidence: /tmp/evidence-1.json" in text
    assert "> generated_at:" in text
    assert "# 诊断报告" in text
