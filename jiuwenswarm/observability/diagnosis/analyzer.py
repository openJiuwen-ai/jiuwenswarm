# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""诊断编排：证据收集（案情摘要）→ 落盘 → 组 Agent 任务书。

分析阶段走 AgentServer 完整 Agent 流程（设计 v2），本模块只负责证据侧：
信号识别 → 失败窗口 → trace 投影 → 日志锚点 → 落盘 → 任务书组装。
LLM 调用与流式消费见 ``agent_client_adapter.py``（Gateway 端点编排）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator

from .agent_client_adapter import DiagnosisAgentRequest, stream_diagnosis_via_agent
from .budget import persist_full_evidence
from .evidence import DiagnosisContext, collect_evidence, collect_log_excerpts, detect_trigger, locate_failure_window
from .prompts import build_agent_prompt, build_history_summary, build_llm_rounds_summary, build_timeline_summary

logger = logging.getLogger(__name__)


async def collect_diagnosis_inputs(
    ctx: DiagnosisContext,
    *,
    diagnosis_dir: Path,
    include_logs: bool = True,
    history_summary_records: int = 20,
) -> dict[str, Any]:
    """证据收集编排：返回组装 Agent 任务书所需的全部输入。

    落盘产物（全量证据包）随本函数持久化，路径在返回值 ``evidence_path``。
    """
    # 决策 4：超长会话只分析最后一个用户请求；普通/单请求会话为 no-op。
    ctx, last_request_id, scoped = _scope_to_last_request(ctx)

    trigger = detect_trigger(ctx)
    window = locate_failure_window(ctx, trigger)

    has_log_source = bool(ctx.uploaded_logs) or bool(ctx.log_dir)
    log_excerpts = (
        collect_log_excerpts(ctx, window) if include_logs and has_log_source else []
    )

    evidence = collect_evidence(ctx, trigger, window, log_excerpts=log_excerpts)
    evidence_path = persist_full_evidence(evidence, diagnosis_dir)
    evidence.evidence_path = evidence_path

    session_start_nano = min(
        (r.start_time_unix_nano for r in ctx.records if r.start_time_unix_nano), default=0
    )

    # 范围描述（决策 2：全局 trace，无时间窗裁剪）
    if ctx.records:
        scope = f"最后请求 {last_request_id}" if scoped else "单请求（全量）"
        window_desc = f"全局 trace：已含所选范围全部 LLM 轮次，未做时间窗裁剪；范围 = {scope}"
    else:
        window_desc = "离线模式占位值 [0,0]（无 trace）"

    summary_blocks = {
        "LLM 调用轮次（全局 trace）": build_llm_rounds_summary(
            evidence.llm_rounds, evidence_path, session_start_nano
        ),
        "trace 时间线（record 级概览）": build_timeline_summary(
            ctx.records, session_start_nano=session_start_nano
        ),
    }
    # history 尾部摘要：trace 存在时证据包 llm_io / llm_rounds 已还原对话，history 与之冗余可省；
    # 无 trace（离线/纯日志）时证据包无 llm_io，history 是对话唯一来源，必须保留。
    if not ctx.records:
        history_records = _load_history_tail(ctx.session_id, history_summary_records)
        if history_records:
            summary_blocks["对话历史尾部（history.json）"] = history_records

    log_dir_for_prompt = str(ctx.log_dir) if ctx.log_dir else (
        str(ctx.uploaded_logs[0].parent) if ctx.uploaded_logs else ""
    )
    prompt = build_agent_prompt(
        evidence_path=evidence_path,
        log_dir=log_dir_for_prompt,
        window=window_desc,
        summary_blocks=summary_blocks,
        user_note=ctx.user_note,
        session_id=ctx.session_id,
    )

    return {
        "prompt": prompt,
        "evidence_path": evidence_path,
        "trigger": trigger.value,
        "window": (window.start_unix_nano, window.end_unix_nano),
    }


def _scope_to_last_request(
    ctx: DiagnosisContext,
) -> tuple[DiagnosisContext, str | None, bool]:
    """决策 4：超长会话只分析最后一个用户请求。

    按 request_id 分组；若存在多个不同 request_id，则只保留 start_time 最大的那一个
    请求对应的 record（即最后一次用户请求）。单请求 / 无 request_id 时原样返回。
    """
    request_ids = [r.request_id for r in ctx.records if r.request_id]
    distinct = set(request_ids)
    if len(distinct) <= 1:
        return ctx, (request_ids[-1] if request_ids else None), False

    last_rid: str | None = None
    last_start = -1
    for r in ctx.records:
        if r.request_id and r.start_time_unix_nano > last_start:
            last_start = r.start_time_unix_nano
            last_rid = r.request_id
    scoped_records = [r for r in ctx.records if r.request_id == last_rid]
    return replace(ctx, records=scoped_records), last_rid, True


def _load_history_tail(session_id: str, max_records: int) -> str:
    """读被诊断会话 history 尾部 N 条 → 摘要文本；读不到返回空串。

    history 文件在 AgentServer 侧数据目录；Gateway 分离部署时若不共享
    data_dir 则读不到，返回空串（Agent 仍可从证据包的 llm_io 还原对话）。
    """
    if max_records <= 0:
        return ""
    try:
        from jiuwenswarm.server.runtime.session.session_history import load_history_records

        records = load_history_records(session_id)
        return build_history_summary(records, max_records=max_records)
    except Exception:
        logger.debug("diagnosis history tail load failed: session=%s", session_id, exc_info=True)
        return ""


async def run_diagnosis_agent(
    agent_client: Any,
    ctx: DiagnosisContext,
    *,
    diagnosis_dir: Path,
    include_logs: bool = True,
    history_summary_records: int = 20,
    project_dir: str = "",
    mode: str = "agent",
    session_id_factory=None,
) -> AsyncIterator[dict]:
    """完整诊断流（端点直接消费）：证据收集 → Agent 任务书 → AgentServer 流式分析。

    yield 事件口径（与 v1 SSE 对齐 + 一个内部元事件）：
      {"type": "meta", "evidence_path", "window", "diag_session_id"}  内部元数据，
          端点截获不透传（报告落盘/取消中断要用，不进前端）
      {"type": "progress", "stage": ...[, "detail": ...]}
      {"type": "token", "text": ...}
      {"type": "final", "text": ...}     chat.final 兜底全文（端点决定是否采用）
      {"type": "error", "message": ...}

    ``send_request_stream`` 抛错（AgentServer 不可达/断连）原样上抛，由端点
    转 AGENT_UNREACHABLE —— 无降级路径，错误必须显式可见。
    ``session_id_factory`` 可注入（测试），默认 ``make_diagnosis_session_id``。
    """
    from .agent_client_adapter import make_diagnosis_session_id

    yield {"type": "progress", "stage": "collect_evidence"}
    inputs = await collect_diagnosis_inputs(
        ctx,
        diagnosis_dir=diagnosis_dir,
        include_logs=include_logs,
        history_summary_records=history_summary_records,
    )

    diag_session = (
        session_id_factory() if session_id_factory is not None else make_diagnosis_session_id()
    )
    yield {
        "type": "meta",
        "evidence_path": str(inputs["evidence_path"]),
        "window": [int(inputs["window"][0]), int(inputs["window"][1])],
        "diag_session_id": diag_session,
    }

    request = DiagnosisAgentRequest(
        prompt=inputs["prompt"],
        project_dir=project_dir,
        mode=mode,
    )

    async for event in stream_diagnosis_via_agent(
        agent_client,
        request,
        session_id=diag_session,
    ):
        yield event


# ---------------------------------------------------------------------------
# 报告落存（沿用 v1 格式：metadata 头 + 正文，历史报告端点依赖该格式）
# ---------------------------------------------------------------------------


def persist_report(
    diagnosis_dir: Path,
    session_id: str,
    report_text: str,
    *,
    evidence_path: str,
    trace_id: str,
    window: tuple[int, int],
) -> str:
    """报告落存 markdown，头部嵌证据包路径 + trace_id + 时间窗。"""
    session_dir = diagnosis_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    path = session_dir / f"report-{ts}.md"
    header = (
        f"> 诊断报告\n"
        f"> session: {session_id}\n"
        f"> trace: {trace_id}\n"
        f"> window: {window[0]} - {window[1]}\n"
        f"> evidence: {evidence_path}\n"
        f"> generated_at: {ts}\n\n"
    )
    path.write_text(header + report_text, encoding="utf-8")
    return str(path)
