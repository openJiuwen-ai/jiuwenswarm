# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""证据收集：信号识别 → 失败窗口 → trace 投影 → 日志辅助。

主证据来自 trajectory store 的 ``raw_json``（与日志级别无关）；日志仅作辅助，
抓 span 覆盖不到的进程级异常（Traceback / ERROR）。
日志过滤一期用 session_id/request_id 正则 + 时间窗模糊匹配（G0 完成后升级为
request_id 精确过滤 + 行号回指）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import DiagnosisEvidence, DiagnosisTrigger, FailureWindow

logger = logging.getLogger(__name__)

# span name / 属性 key 常量（与 agent-core semcov 对齐，避免硬编码散落）
_LLM_CALL_SPAN = "llm.call"
_LLM_REASONING_SPAN = "llm.reasoning"
_CHAT_ERROR_EVENT = "chat.error"
_FORCED_CLOSE_ATTR = "openjiuwen.span.forced_close"
_ATTR_REQUEST_ID = "request.id"
_ATTR_SESSION_ID = "openjiuwen.session.id"
_ATTR_GEN_AI_INPUT = "gen_ai.input.messages"
_ATTR_GEN_AI_SYSTEM = "gen_ai.system_instructions"
_ATTR_GEN_AI_OUTPUT = "gen_ai.output.messages"
_ATTR_TOOL_INPUT = "gen_ai.tool.input"
_ATTR_TOOL_OUTPUT = "gen_ai.tool.output"
_ATTR_TOOL_FAILURE = "tool_failure_reason"
_ATTR_CONTEXT_COMMIT = "context_window_commit"
_ATTR_COMPACTION = "compaction.completed"
_ATTR_USAGE_INPUT = "gen_ai.usage.input_tokens"
_ATTR_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
_EVENT_EXCEPTION = "exception"
_EVENT_EXCEPTION_STACKTRACE = "exception.stacktrace"

# 日志辅助收集的锚点正则（session_id / request_id 通常嵌在消息体里）
_LOG_SESSION_RE = re.compile(r"session[_=]?([0-9a-zA-Z_-]{6,})")
_LOG_REQUEST_RE = re.compile(r"request[_=]?([0-9a-zA-Z_-]{6,})")
_TRACEBACK_HEADER = "Traceback (most recent call last)"
_ERROR_LEVEL_TOKENS = (" ERROR ", "|ERROR|", "ERROR:")


@dataclass
class SpanRecord:
    """一条 store record 解码后的扁平视图（含其下全部 span）。"""

    trace_id: str
    request_id: str | None
    start_time_unix_nano: int
    end_time_unix_nano: int
    has_error: bool
    lifecycle: str
    spans: list[dict] = field(default_factory=list)
    """该 record 下解码出的 OTLP span 原始 dict 列表。"""


@dataclass
class DiagnosisContext:
    """一次诊断的输入参数与 store 读取结果。

    由 G2 端点组装：读 store → 填 records → 调 collect_evidence。
    """

    session_id: str
    trace_id: str
    user_note: str | None
    requested_mode: str | None
    """客户端显式声明的 mode（error/interrupt/unexpected），None 则自动判定。"""

    records: list[SpanRecord] = field(default_factory=list)
    """该 trace 的全部 span record（按 start_time 升序）。"""

    log_dir: Path | None = None
    """本地日志目录（~/.jiuwenswarm/agent/.logs/），None 则不收集本地日志。"""

    uploaded_logs: list[Path] = field(default_factory=list)
    """用户上传的日志文件路径（G5）。非空则替换本地日志收集（不合并）。"""


# ---------------------------------------------------------------------------
# 1. 信号识别（设计 §4.1）
# ---------------------------------------------------------------------------


def detect_trigger(ctx: DiagnosisContext) -> DiagnosisTrigger:
    """判定诊断场景。客户端显式 mode 优先，否则按 trace 信号自动判定。"""
    if ctx.requested_mode:
        try:
            return DiagnosisTrigger(ctx.requested_mode)
        except ValueError:
            logger.warning("unknown diagnosis mode %r, auto-detecting", ctx.requested_mode)

    has_error = any(r.has_error for r in ctx.records)
    if has_error:
        return DiagnosisTrigger.ERROR
    if _detect_interrupt(ctx):
        return DiagnosisTrigger.INTERRUPT
    return DiagnosisTrigger.UNEXPECTED


def _detect_interrupt(ctx: DiagnosisContext) -> bool:
    """run 未正常收尾：record lifecycle 停在 running 或 run root span forced_close。"""
    for record in ctx.records:
        # store 侧 lifecycle 列承载运行状态：running=仍在跑（历史记录即中断），
        # final=正常结束，provisional=快照中。只有 running 算中断。
        if record.lifecycle == "running":
            return True
        for span in record.spans:
            if _is_run_root(span):
                attrs = _span_attributes(span)
                if _as_text(attrs.get(_FORCED_CLOSE_ATTR)):
                    return True
    return False


def _is_run_root(span: dict) -> bool:
    name = _as_text(span.get("name")) or ""
    return name.startswith("agent.run") or name.startswith("run.")


# ---------------------------------------------------------------------------
# 2. 失败窗口定位（设计 §4.1）
# ---------------------------------------------------------------------------


def locate_failure_window(
    ctx: DiagnosisContext,
    trigger: DiagnosisTrigger,
) -> FailureWindow:
    """窗口覆盖整个所选范围（全局 trace）。

    决策 2：不做时间窗裁剪，LLM 看到的是全局 trace；anchor 仅作元信息记录，
    不再用于裁剪 in_window。无 trace（离线）返回占位 [0,0]。
    """
    if not ctx.records:
        return FailureWindow(0, 0)

    all_times = [t  # pylint: disable=complicate-comprehension
                 for r in ctx.records
                 for t in (r.start_time_unix_nano, r.end_time_unix_nano)
                 if t
                 ]
    if not all_times:
        return FailureWindow(0, 0)

    run_start = min(all_times)
    run_end = max(all_times)

    anchor = _earliest_error_span(ctx) if trigger is DiagnosisTrigger.ERROR else None
    return FailureWindow(
        run_start,
        run_end,
        anchor_span_id=anchor[0] if anchor else None,
        in_window=_spans_in_window(ctx, run_start, run_end),
    )


def _earliest_error_span(ctx: DiagnosisContext) -> tuple[str, int] | None:
    earliest: tuple[str, int] | None = None
    for record in ctx.records:
        if not record.has_error:
            continue
        for span in record.spans:
            if not _span_has_error(span):
                continue
            start = _span_start(span)
            span_id = _as_text(span.get("spanId")) or ""
            if earliest is None or start < earliest[1]:
                earliest = (span_id, start)
    # record 级 has_error 但 span 级未定位到时，退到 record 起点
    if earliest is None:
        for record in ctx.records:
            if record.has_error:
                return ("", record.start_time_unix_nano)
    return earliest


def _spans_in_window(ctx: DiagnosisContext, start: int, end: int) -> tuple[str, ...]:
    ids: list[str] = []
    for record in ctx.records:
        for span in record.spans:
            span_id = _as_text(span.get("spanId")) or ""
            if not span_id:
                continue
            s = _span_start(span)
            if start <= s <= end:
                ids.append(span_id)
    return tuple(ids)


# ---------------------------------------------------------------------------
# 3. trace 证据投影（设计 §4.2）
# ---------------------------------------------------------------------------


def collect_evidence(
    ctx: DiagnosisContext,
    trigger: DiagnosisTrigger,
    window: FailureWindow,
    *,
    log_excerpts: list[dict] | None = None,
    usage_snapshot: dict | None = None,
    evidence_path: str | None = None,
) -> DiagnosisEvidence:
    """组装证据包。trace 投影 + 日志辅助由本函数统一产出。"""
    in_window = set(window.in_window)
    span_summaries: list[dict] = []
    failure_spans: list[dict] = []
    llm_io: list[dict] = []
    context_commits: list[dict] = []
    compaction_events: list[dict] = []

    # 预建索引：span→record、parent→children，供轮次（round）聚合与错误检测。
    span_entries: list[tuple[dict, SpanRecord]] = []
    children: dict[str, list[tuple[dict, SpanRecord]]] = {}
    for record in ctx.records:
        for span in record.spans:
            parent = _as_text(span.get("parentSpanId")) or ""
            if parent:
                children.setdefault(parent, []).append((span, record))
            span_entries.append((span, record))

    for record in ctx.records:
        for span in record.spans:
            span_id = _as_text(span.get("spanId")) or ""
            name = _as_text(span.get("name")) or ""
            summary = _span_summary(span, record, in_window)
            span_summaries.append(summary)

            if span_id not in in_window:
                continue

            attrs = _span_attributes(span)
            events = _span_events(span)

            if _span_has_error(span):
                failure_spans.append(_project_failure_span(span, attrs, events))

            if name == _LLM_CALL_SPAN:
                llm_io.append(_project_llm_io(span, attrs, events))

            if name == _LLM_REASONING_SPAN:
                llm_io.append(_project_llm_reasoning(span, attrs))

            if _as_text(attrs.get(_ATTR_CONTEXT_COMMIT)):
                context_commits.append({"span_id": span_id, "commit": attrs.get(_ATTR_CONTEXT_COMMIT)})

            if _as_text(attrs.get(_ATTR_COMPACTION)):
                compaction_events.append({"span_id": span_id, "event": attrs.get(_ATTR_COMPACTION)})

            # 工具失败逻辑失败（success=False 但无异常）也要进 failure_spans
            tool_failure = _as_text(attrs.get(_ATTR_TOOL_FAILURE))
            if tool_failure and not _span_has_error(span):
                failure_spans.append(_project_tool_logic_failure(span, attrs, tool_failure))

    # 轮次聚合（决策 1/2/3）：每轮 = 一次 llm.call / llm.reasoning span，
    # 错误信号含其所有子孙 span（child tool spans）的失败原因。
    llm_rounds = _build_llm_rounds(span_entries, children)

    return DiagnosisEvidence(
        session_id=ctx.session_id,
        trace_id=ctx.trace_id,
        trigger=trigger.value,
        user_note=ctx.user_note,
        window=(window.start_unix_nano, window.end_unix_nano),
        span_summaries=span_summaries,
        failure_spans=failure_spans,
        llm_io=llm_io,
        llm_rounds=llm_rounds,
        context_commits=context_commits,
        compaction_events=compaction_events,
        usage_snapshot=usage_snapshot or {},
        log_excerpts=log_excerpts or [],
        offline=not ctx.records,
        evidence_path=evidence_path,
    )


_ROUND_SPAN_NAMES = (_LLM_CALL_SPAN, _LLM_REASONING_SPAN)


def _build_llm_rounds(
    span_entries: list[tuple[dict, SpanRecord]],
    children: dict[str, list[tuple[dict, SpanRecord]]],
) -> list[dict]:
    """按 LLM 调用轮次聚合：每轮含时间、错误信号（含该轮内所有 span 的失败原因）、紧凑内容预览。

    轮次边界 = 相邻两次 llm.call/llm.reasoning span 的起始时间：一次 LLM 调用与它触发的
    工具调用、推理等 span 构成一个轮次。注意工具 span 在 trace 中与 llm.call 是**兄弟**
    （都挂在 agent/iteration span 下），按 parentSpanId 遍历子孙拿不到，故改用"时间窗包含"
    判定轮内 span：属于 [本轮起始, 下一轮起始) 的所有 span 都视为该轮成员，确保轮内任何报错
    （不局限于工具、也不局限于已显式标记）都能把该轮 has_error 标为 True。
    """
    _ = children  # 保留参数，轮内 span 改用时间窗判定

    # 先收集所有轮次锚点（llm.call/llm.reasoning span 的起始时间），用于推断每轮结束边界。
    anchors: list[tuple[dict, SpanRecord, int]] = []
    for span, record in span_entries:
        if _as_text(span.get("name")) in _ROUND_SPAN_NAMES:
            anchors.append((span, record, _span_start(span)))
    if not anchors:
        return []
    anchors.sort(key=lambda t: t[2])

    rounds: list[dict] = []
    n = len(anchors)
    for i, (span, record, start) in enumerate(anchors):
        # 本轮回合窗：[start, next_start)；最后一轮延伸到 trace 末尾。
        end_boundary = anchors[i + 1][2] if i + 1 < n else None
        name = _as_text(span.get("name")) or ""
        span_id = _as_text(span.get("spanId")) or ""
        end = _span_end(span)
        attrs = _span_attributes(span)
        events = _span_events(span)

        error_messages: list[str] = []

        # 轮内成员 = 起始时间落在本轮回合窗 [start, end_boundary) 的所有 span
        # （含 llm.call 自身及其触发的工具/子 span；工具 span 与 llm.call 是兄弟，
        # 只能靠时间窗识别）。任一成员有报错即把本轮标记 has_error=True。
        for s, _rec in span_entries:
            s_start = _span_start(s)
            if s_start < start:
                continue
            if end_boundary is not None and s_start >= end_boundary:
                continue
            sname = _as_text(s.get("name")) or ""
            sattrs = _span_attributes(s)
            sevents = _span_events(s)
            if _span_has_error(s):
                st = _extract_stacktrace(sevents)
                if st:
                    error_messages.append(f"[{sname}] {st}")
                else:
                    detail = _span_status(s)  # "ERROR:reason" 或 "ERROR"
                    if detail and detail != "ERROR":
                        error_messages.append(f"[{sname}] {detail}")
                    else:
                        # status=ERROR 但无详情：用工具输出文本兜底，避免丢失失败原因
                        out = _compact_text(sattrs.get(_ATTR_TOOL_OUTPUT))
                        error_messages.append(f"[{sname}] {out}" if out else f"[{sname}] (status=ERROR)")
            tf = _as_text(sattrs.get(_ATTR_TOOL_FAILURE))
            if tf:
                error_messages.append(f"[{sname}] tool_failure: {tf}")
            else:
                # 兜底：工具以 "[ERROR]: ..." 约定串返回失败（runtime 未抛异常、未显式
                # success=False、也未写入 tool_failure_reason 属性）时，仍视作该轮报错，
                # 使轮次 has_error 对"轮内任何报错"生效。
                out_text = _compact_text(sattrs.get(_ATTR_TOOL_OUTPUT))
                if out_text and out_text.lstrip().upper().startswith("[ERROR]"):
                    error_messages.append(f"[{sname}] {out_text}")

        preview = _build_round_content_preview(span, name, attrs, events)
        # 把该轮内的 span 输出也带进预览（每条 [:300] 截断），否则工具返回的错误串
        # （如 web_search 的 "[ERROR]: ..."）只在全量文件里、摘要预览完全看不到。
        tool_outputs = []
        for s, _rec in span_entries:
            s_start = _span_start(s)
            if s_start < start:
                continue
            if end_boundary is not None and s_start >= end_boundary:
                continue
            sname = _as_text(s.get("name")) or ""
            sattrs = _span_attributes(s)
            tool_outputs.append(
                {"tool": sname, "output": _compact_text(sattrs.get(_ATTR_TOOL_OUTPUT))}
            )
        if tool_outputs:
            preview["tool_outputs"] = tool_outputs

        rounds.append(
            {
                "round_index": i + 1,
                "span_id": span_id,
                "request_id": record.request_id,
                "start_unix_nano": start,
                "end_unix_nano": end,
                "has_error": bool(error_messages),
                "error_messages": error_messages,
                "error_message": "\n".join(error_messages) if error_messages else None,
                "content_preview": preview,
            }
        )
    return rounds


def _iter_descendants(span_id: str, children: dict[str, list[tuple[dict, SpanRecord]]]):
    """DFS 遍历某 span 的所有子孙 span（含隔代），避免重复。"""
    stack = list(children.get(span_id, []))
    seen: set[str] = set()
    while stack:
        s, rec = stack.pop()
        sid = _as_text(s.get("spanId")) or ""
        if not sid or sid in seen:
            continue
        seen.add(sid)
        yield s, rec
        stack.extend(children.get(sid, []))


def _build_round_content_preview(span: dict, name: str, attrs: dict, events: list[dict]) -> dict:
    """紧凑内容预览：每条消息 [:300] 截断；完整内容在证据包 JSON 中按需检索。"""
    if name == _LLM_REASONING_SPAN:
        reasoning = _as_text(attrs.get("gen_ai.reasoning.content") or attrs.get("gen_ai.thinking")) or ""
        if len(reasoning) > 300:
            reasoning = reasoning[:300] + "…"
        return {"reasoning": reasoning, "ttft_ms": _span_ttft(span)}

    inputs = _coerce_list(attrs.get(_ATTR_GEN_AI_INPUT))
    outputs = _coerce_list(attrs.get(_ATTR_GEN_AI_OUTPUT))
    messages = []
    for m in inputs:
        if not isinstance(m, dict):
            continue
        role = _as_text(m.get("role")) or "?"
        messages.append({"role": role, "content": _compact_text(m.get("content"))})
    out_text = ""
    if isinstance(outputs, list) and outputs:
        last = outputs[-1]
        if isinstance(last, dict):
            out_text = _compact_text(last.get("content"))
        elif isinstance(last, str):
            out_text = _compact_text(last)
    return {
        "messages": messages,
        "output": out_text,
        "usage_input_tokens": attrs.get(_ATTR_USAGE_INPUT),
        "usage_output_tokens": attrs.get(_ATTR_USAGE_OUTPUT),
        "ttft_ms": _span_ttft(span),
    }


def _compact_text(content) -> str:
    """把消息 content 压成 [:300] 字符串（多模态 content parts 取 text）。"""
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(_as_text(p.get("text")) or "")
        content = " ".join(p for p in parts if p)
    if not isinstance(content, str):
        content = str(content)
    if len(content) > 300:
        content = content[:300] + "…"
    return content


def _coerce_list(value) -> list:
    """把属性值规整为 list：已是 list 直接返回；JSON 字符串尝试解析；否则返回空。

    不同 trace 后端对 gen_ai.input/output.messages 的序列化不一致（有时是已解码
    list，有时是 JSON 字符串），统一规整避免预览为空。
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _span_summary(span: dict, record: SpanRecord, in_window: set[str]) -> dict:
    span_id = _as_text(span.get("spanId")) or ""
    return {
        "span_id": span_id,
        "parent_span_id": _as_text(span.get("parentSpanId")) or "",
        "name": _as_text(span.get("name")) or "",
        "start_time_unix_nano": _span_start(span),
        "end_time_unix_nano": _span_end(span),
        "status": _span_status(span),
        "request_id": record.request_id,
        "in_window": span_id in in_window,
    }


def _project_failure_span(span: dict, attrs: dict, events: list[dict]) -> dict:
    """错误 span 投影：含工具参数/结果/失败原因/异常栈（代码定位层 1）。"""
    span_id = _as_text(span.get("spanId")) or ""
    return {
        "span_id": span_id,
        "name": _as_text(span.get("name")) or "",
        "tool_input": attrs.get(_ATTR_TOOL_INPUT),
        "tool_output": attrs.get(_ATTR_TOOL_OUTPUT),
        "tool_failure_reason": _as_text(attrs.get(_ATTR_TOOL_FAILURE)),
        "stacktrace": _extract_stacktrace(events),
    }


def _project_tool_logic_failure(span: dict, attrs: dict, tool_failure: str) -> dict:
    span_id = _as_text(span.get("spanId")) or ""
    return {
        "span_id": span_id,
        "name": _as_text(span.get("name")) or "",
        "tool_input": attrs.get(_ATTR_TOOL_INPUT),
        "tool_output": attrs.get(_ATTR_TOOL_OUTPUT),
        "tool_failure_reason": tool_failure,
        "stacktrace": None,
        "kind": "logic_failure",
    }


def _project_llm_io(span: dict, attrs: dict, events: list[dict]) -> dict:
    span_id = _as_text(span.get("spanId")) or ""
    return {
        "span_id": span_id,
        "name": _LLM_CALL_SPAN,
        "input_messages": attrs.get(_ATTR_GEN_AI_INPUT),
        "system_instructions": attrs.get(_ATTR_GEN_AI_SYSTEM),
        "output_messages": attrs.get(_ATTR_GEN_AI_OUTPUT),
        "usage_input_tokens": attrs.get(_ATTR_USAGE_INPUT),
        "usage_output_tokens": attrs.get(_ATTR_USAGE_OUTPUT),
        "ttft_ms": _span_ttft(span),
        "stacktrace": _extract_stacktrace(events),
    }


def _project_llm_reasoning(span: dict, attrs: dict) -> dict:
    span_id = _as_text(span.get("spanId")) or ""
    return {
        "span_id": span_id,
        "name": _LLM_REASONING_SPAN,
        "reasoning": attrs.get("gen_ai.reasoning.content") or attrs.get("gen_ai.thinking"),
    }


# ---------------------------------------------------------------------------
# 4. 日志辅助收集（设计 §4.2，降级为补充证据）
# ---------------------------------------------------------------------------


def collect_log_excerpts(
    ctx: DiagnosisContext,
    window: FailureWindow,
    *,
    max_lines: int = 200,
) -> list[dict]:
    """收集日志辅助证据。上传日志优先（替换本地），无上传则用本地。

    每条证据标注 (source_file, line_no, matched_key, source)。
    source: "local" 或 "uploaded:<文件名>"。
    上传日志锚点零命中时降级为无锚点 ERROR/Traceback 提取，并标注降级。
    本地日志按 session_id/request_id + ERROR/Traceback 过滤；
    上传日志同规则，但强制过脱敏（原始文件未过服务端 filter）。
    """
    if ctx.uploaded_logs:
        return _collect_from_files(
            ctx.uploaded_logs,
            ctx,
            source_prefix="uploaded",
            force_sanitize=True,
            max_lines=max_lines,
        )
    if ctx.log_dir is None or not ctx.log_dir.exists():
        return []
    return _collect_from_files(
        list(_iter_log_files(ctx.log_dir)),
        ctx,
        source_prefix="local",
        force_sanitize=False,
        max_lines=max_lines,
    )


def _collect_from_files(
    files: list[Path],
    ctx: DiagnosisContext,
    *,
    source_prefix: str,
    force_sanitize: bool,
    max_lines: int,
) -> list[dict]:
    """从给定文件列表收集证据。source_prefix 区分 local/uploaded。"""
    anchors = _log_anchors(ctx)
    sanitize = _get_sanitize_fn() if force_sanitize else None
    excerpts: list[dict] = []
    anchor_hit = False
    for log_file in files:
        lines = _read_log_lines(log_file)
        if lines is None:
            continue
        file_anchor_hit = False
        for line_no, line in enumerate(lines, start=1):
            if len(excerpts) >= max_lines:
                break
            matched = _match_log_line(line, anchors)
            if matched is None:
                continue
            if matched.startswith("session_id:") or matched.startswith("request_id:"):
                anchor_hit = True
                file_anchor_hit = True
            line_text = sanitize(line) if sanitize else line
            source = (
                f"{source_prefix}:{log_file.name}"
                if source_prefix == "uploaded"
                else source_prefix
            )
            excerpts.append(
                {
                    "source_file": str(log_file.name),
                    "line_no": line_no,
                    "matched_key": matched,
                    "line": line_text,
                    "source": source,
                }
            )
        # 上传日志锚点零命中：主循环已无条件抓 ERROR/Traceback，这里不重复抓，
        # 只在首条插入降级提示让 LLM 知置信级别（避免双收同一条）。
        if source_prefix == "uploaded" and not file_anchor_hit:
            # 该文件无锚点命中：标注其后续 ERROR/Traceback 为低置信证据
            for e in excerpts:
                if (
                    e.get("source_file") == log_file.name
                    and e.get("matched_key") in ("error_level", "traceback")
                ):
                    e["matched_key"] = "degraded:no_anchor"
    if source_prefix == "uploaded" and excerpts and not anchor_hit:
        excerpts.insert(
            0,
            {
                "source_file": "(meta)",
                "line_no": 0,
                "matched_key": "degraded:no_anchor",
                "line": "[锚点匹配失败，以下为无锚点提取，证据置信级别较低]",
                "source": "uploaded:meta",
            },
        )
    return excerpts


def _read_log_lines(log_file: Path) -> list[str] | None:
    """读取日志行，UTF-8 失败则 GBK fallback（Windows 日志大概率 GBK）。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return log_file.read_text(encoding=encoding).splitlines()
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _get_sanitize_fn():
    """获取 _sanitize_log_text（上传日志唯一脱敏防线）。"""
    try:
        from jiuwenswarm.common.utils import _sanitize_log_text

        return _sanitize_log_text
    except Exception:  # pragma: no cover
        return None


def _log_anchors(ctx: DiagnosisContext) -> dict[str, str]:
    """从 ctx 提取日志锚点：session_id + request_id（若 records 里有）。"""
    anchors: dict[str, str] = {}
    if ctx.session_id:
        anchors["session_id"] = ctx.session_id
    request_ids = {r.request_id for r in ctx.records if r.request_id}
    if request_ids:
        # 取第一个非空 request_id 作锚点（同一 trace 通常同 request）
        anchors["request_id"] = next(iter(request_ids))
    return anchors


def _iter_log_files(log_dir: Path):
    for name in ("agent_server.log", "gateway.log", "channel.log", "full.log"):
        candidate = log_dir / name
        if candidate.exists():
            yield candidate


def _match_log_line(line: str, anchors: dict[str, str]) -> str | None:
    """命中规则：含 session_id 锚点 OR request_id 锚点 OR Traceback OR ERROR 级。"""
    if _TRACEBACK_HEADER in line:
        return "traceback"
    if _is_error_level_line(line):
        return "error_level"
    sid = anchors.get("session_id")
    if sid and sid in line:
        return f"session_id:{sid}"
    rid = anchors.get("request_id")
    if rid and rid in line:
        return f"request_id:{rid}"
    return None


def _is_error_level_line(line: str) -> bool:
    """行首 ERROR 或含 ERROR 级 token（兼容 core 默认后端的 `| ERROR |`）。"""
    stripped = line.lstrip()
    if stripped.startswith("ERROR"):
        return True
    return any(tok in line for tok in _ERROR_LEVEL_TOKENS)


# ---------------------------------------------------------------------------
# 5. OTLP span 解析小工具（内聚在本包，不外泄）
# ---------------------------------------------------------------------------


def decode_span_records(raw_records: Sequence[Mapping[str, Any]]) -> list[SpanRecord]:
    """从 store 的 archive records（含 otlp 字段）解码出 SpanRecord 列表。

    ``raw_records`` 每项形如 ``_archive_record_from_row`` 的输出：含 ``otlp``
    （已解码的 OTLP payload）、``trace_id``、``request_id``、
    ``start_time_unix_nano``、``end_time_unix_nano``、``has_error``、``lifecycle``。
    """
    out: list[SpanRecord] = []
    for item in raw_records:
        otlp = item.get("otlp")
        if not isinstance(otlp, Mapping):
            continue
        spans = _extract_spans(otlp)
        if not spans:
            continue
        out.append(
            SpanRecord(
                trace_id=str(item.get("trace_id") or ""),
                request_id=_as_text(item.get("request_id")),
                start_time_unix_nano=int(item.get("start_time_unix_nano") or 0),
                end_time_unix_nano=int(item.get("end_time_unix_nano") or 0),
                has_error=bool(item.get("has_error")),
                lifecycle=str(item.get("lifecycle") or ""),
                spans=spans,
            )
        )
    out.sort(key=lambda r: r.start_time_unix_nano)
    return out


def _extract_spans(otlp: Mapping[str, Any]) -> list[dict]:
    spans: list[dict] = []
    resource_spans = otlp.get("resourceSpans")
    if not isinstance(resource_spans, list):
        return spans
    for resource_span in resource_spans:
        if not isinstance(resource_span, Mapping):
            continue
        scope_spans = resource_span.get("scopeSpans")
        if not isinstance(scope_spans, list):
            continue
        for scope_span in scope_spans:
            if not isinstance(scope_span, Mapping):
                continue
            raw_spans = scope_span.get("spans")
            if not isinstance(raw_spans, list):
                continue
            for span in raw_spans:
                if isinstance(span, Mapping):
                    spans.append(dict(span))
    return spans


def _span_attributes(span: Mapping[str, Any]) -> dict[str, Any]:
    return _decode_attributes(span.get("attributes"))


def _decode_attributes(raw_attributes: Any) -> dict[str, Any]:
    """解码 OTLP attributes（list of {key, value:{stringValue...}}）为 dict。

    与 ``observability.projection`` 同算法，内联在本包以保持内聚、不依赖其私有符号。
    """
    if not isinstance(raw_attributes, list):
        return {}
    decoded: dict[str, Any] = {}
    for item in raw_attributes:
        if not isinstance(item, Mapping):
            continue
        key = _as_text(item.get("key"))
        value = item.get("value")
        if key is not None and isinstance(value, Mapping):
            decoded[key] = _decode_otlp_value(value)
    return decoded


def _decode_otlp_value(value: Mapping[str, Any]) -> Any:
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        if key in value:
            return value[key]
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _span_events(span: Mapping[str, Any]) -> list[dict]:
    events = span.get("events")
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, Mapping)]


def _span_has_error(span: Mapping[str, Any]) -> bool:
    status = span.get("status")
    if isinstance(status, Mapping):
        code = _as_text(status.get("code"))
        if code and code.upper() in ("ERROR", "2"):
            return True
    for event in _span_events(span):
        name = _as_text(event.get("name"))
        if name == _EVENT_EXCEPTION:
            return True
    return False


def _span_start(span: Mapping[str, Any]) -> int:
    return int(span.get("startTimeUnixNano") or 0)


def _span_end(span: Mapping[str, Any]) -> int:
    return int(span.get("endTimeUnixNano") or 0)


def _span_status(span: Mapping[str, Any]) -> str:
    status = span.get("status")
    if isinstance(status, Mapping):
        code = _as_text(status.get("code"))
        message = _as_text(status.get("message"))
        if code:
            return f"{code}:{message}" if message else code
    return "ok"


def _span_ttft(span: Mapping[str, Any]) -> int | None:
    """TTFT = 第一个事件时间 - span 起始；无事件则 None。"""
    start = _span_start(span)
    if not start:
        return None
    events = _span_events(span)
    if not events:
        return None
    first_ts = min((int(e.get("timeUnixNano") or 0) for e in events), default=0)
    if not first_ts:
        return None
    return (first_ts - start) // 1_000_000  # ns → ms


def _extract_stacktrace(events: list[dict]) -> str | None:
    for event in events:
        if _as_text(event.get("name")) != _EVENT_EXCEPTION:
            continue
        attrs = _decode_attributes(event.get("attributes"))
        stack = _as_text(attrs.get(_EVENT_EXCEPTION_STACKTRACE))
        if stack:
            return stack
    return None
