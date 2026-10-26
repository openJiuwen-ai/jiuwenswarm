# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Formal diagnosis engine backed by the agent-core attribution pipeline.

The engine adapts the three-stage attribution pipeline of
``openjiuwen.agent_evolving.attribution`` (task parsing, diagnosis, attribution)
to the :class:`DiagnosisEngine` contract. It only reads the snapshot and the
ranges the host passes in and reports progress through ``emit``.

Design notes (see ``CONTEXT_09_new_attr.md``):

- Snapshot conversion: every :class:`TrajectoryRecord` wraps one OTLP span; the
  span is flattened into the core snapshot shape consumed by
  ``build_nodes_from_snapshot``.
- Node ids are assigned with an empty mapping on every run (deterministic per
  run, never persisted); ranges are sliced with the core full-coverage
  semantics, and an empty node pool aborts with ``EMPTY_SCOPE``.
- Every engine event, including the LLM request lines rendered from core
  ``StatusSink`` reports, goes through one queue drained by a single writer
  task, so ``emit`` is awaited strictly in order.
- CPU work (snapshot conversion, node building, slicing) runs on a worker
  thread via ``asyncio.to_thread``.
- A terminal attribution failure (timeout / tokens exceeded / error /
  incomplete) keeps the partial results and is reported through
  ``EngineResult.error``; the host marks the stage failed but completes the
  record.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.agent_evolving.attribution import (
    NodeBackedRuntime,
    TaskParseError,
    assign_node_ids,
    build_nodes_from_snapshot,
    run_attribution,
    run_diagnosis,
    run_task_parse,
    run_trajectory_summary,
    slice_nodes_by_range,
)
from openjiuwen.agent_evolving.attribution.snapshot import SlicedNodes
from openjiuwen.core.single_agent.schema.agent_card import AgentCard

from jiuwenswarm.diagnosis.engine import EmitFn, EngineError, EngineEvent, EngineInputs, EngineResult
from jiuwenswarm.diagnosis.models import (
    DiagnosisError,
    EvolutionJudge,
    ModeProfile,
    RelatedSignalAttribution,
    RewardFailureCause,
    SignalRef,
    StepRef,
)
from jiuwenswarm.diagnosis.node_anchor import anchor_assistant_nodes
from jiuwenswarm.diagnosis.snapshot import TrajectorySnapshot

logger = logging.getLogger(__name__)

#: Resolves the Reward workspace directory of a session.
WorkspaceResolver = Callable[[str], str]
#: Builds the model client for a resolved selection; replaced in tests.
ModelFactory = Callable[[Any], Any]

_REWARD_CARD_ID = "attribution-reward-agent"
_TASK_PARSE_TIMEOUT_S = 180.0

#: Core LLM status stages mapped onto the three zhou stage ids.
_CORE_STAGE_TO_ZHOU = {
    "task_parse": "task_parse",
    "keyword_diag": "diagnose",
    "reward": "diagnose",
    "attribution": "attribute",
}

_EXPERIENCE_SCORE = {"success": 1.0, "partial": 0.5, "fail": 0.0}

_TEXTS: dict[str, dict[str, str]] = {
    "zh": {
        "stage_task_parse": "任务解析",
        "stage_diagnose": "诊断分析",
        "stage_attribute": "归因分析",
        "empty_scope": "选中范围内没有完整可诊断的步骤（未完整覆盖的步骤已被剔除）",
        "task_description": "任务描述：{description}",
        "message_artifact": "消息产物 {node_id}：{text}",
        "file_artifact": "文件产物 {path}（{node_id} · {tool_type}）",
        "task_summary": "提取任务与 {artifacts} 个产物（消息 {messages} · 文件 {files}）",
        "parse_incomplete": "选中范围不包含完整任务，无法提取任务描述",
        "parse_unrelated": "选中范围包含多个互不相关的任务，无法归因",
        "parse_invalid": "任务解析输出无法解析：{reason}",
        "model_failed": "任务解析模型调用失败：{reason}",
        "signal_line": "{signal_id} [{tier}] {source}：{text}（节点 {node_ids}）",
        "reward_total": "Reward 总分 {score}：{comment}",
        "reward_skipped_no_artifacts": "Reward 已跳过：无任务产物",
        "reward_skipped_no_model": "Reward 已跳过：未注入模型",
        "reward_failed": "Reward 失败：{reason}",
        "reward_no_signals": "Reward 未产出有效信号，使用规则信号继续",
        "trajectory_summary_no_signals": "本次没有可归因的信号，无需归因",
        "trajectory_summary_unavailable": "未能生成轨迹摘要（模型调用失败或超时）",
        "attribute_skipped_no_signals": "诊断分析没有产出可归因的信号，已跳过归因",
        "diagnose_summary_reward": "信号 {count} 条（Reward 模式）",
        "diagnose_summary_rule": "信号 {count} 条（规则模式）",
        "diagnose_summary_rule_reason": "信号 {count} 条（规则模式 · {reason}）",
        "perfect_skipped": "已跳过 perfect 信号：{ids}",
        "attribute_entry": "[{attr_id}] {text}（{start} → {end}）",
        "attribute_entry_node": "[{attr_id}] {text}（{node_id}）",
        "attribute_invalid": "标记无效：{signal_id}",
        "attribute_updated_prefix": "（更新）",
        "attribute_mark": "标记 {category}：{signal_id}",
        "attribute_artifact": "已提交产物改进意见（实验路径）",
        "attribute_skill": "已提交 Skill 改进：{key}",
        "attribute_experience": "已提交经验判定：{outcome}",
        "attribute_summary": "轨迹摘要已生成 · 失败原因 {causes} 条 · 演进项 {evolution} 条",
        "attribute_pending": "未收口信号：{ids}",
        "attribute_failed_timeout": "归因未完成：超时",
        "attribute_failed_tokens": "归因未完成：超出 token 预算",
        "attribute_failed_error": "归因未完成：运行出错",
        "attribute_failed_incomplete": "归因未完成：仍有信号未收口",
        "dropped_anchors": "有 {count} 条归因的锚点无法在轨迹中定位，已省略",
        "llm_iter": "iter {iteration}",
        "llm_status_running": "请求中",
        "llm_status_succeeded": "成功",
        "llm_status_failed": "失败",
    },
    "en": {
        "stage_task_parse": "Parse task",
        "stage_diagnose": "Diagnose",
        "stage_attribute": "Attribute causes",
        "empty_scope": "No complete steps to analyse in the selection (partially covered steps were dropped)",
        "task_description": "Task description: {description}",
        "message_artifact": "Message artifact {node_id}: {text}",
        "file_artifact": "File artifact {path} ({node_id} · {tool_type})",
        "task_summary": "Task and {artifacts} artifacts extracted ({messages} messages · {files} files)",
        "parse_incomplete": "The selection does not contain a complete task; no task description could be extracted",
        "parse_unrelated": "The selection contains multiple unrelated tasks; attribution is not possible",
        "parse_invalid": "The task parse output could not be parsed: {reason}",
        "model_failed": "Task parse model call failed: {reason}",
        "signal_line": "{signal_id} [{tier}] {source}: {text} (nodes {node_ids})",
        "reward_total": "Reward total score {score}: {comment}",
        "reward_skipped_no_artifacts": "Reward skipped: no task artifacts",
        "reward_skipped_no_model": "Reward skipped: no model injected",
        "reward_failed": "Reward failed: {reason}",
        "reward_no_signals": "Reward produced no usable signals; rule signals are kept",
        "trajectory_summary_no_signals": "No signals to attribute this run; nothing to attribute",
        "trajectory_summary_unavailable": "Could not generate a trajectory summary (model call failed or timed out)",
        "attribute_skipped_no_signals": "Diagnosis produced no signals to attribute; attribution was skipped",
        "diagnose_summary_reward": "{count} signals (reward mode)",
        "diagnose_summary_rule": "{count} signals (rule mode)",
        "diagnose_summary_rule_reason": "{count} signals (rule mode · {reason})",
        "perfect_skipped": "Perfect signals skipped: {ids}",
        "attribute_entry": "[{attr_id}] {text} ({start} → {end})",
        "attribute_entry_node": "[{attr_id}] {text} ({node_id})",
        "attribute_invalid": "Marked invalid: {signal_id}",
        "attribute_updated_prefix": "(updated) ",
        "attribute_mark": "Marked {category}: {signal_id}",
        "attribute_artifact": "Artifact improvement submitted (experimental path)",
        "attribute_skill": "Skill improvement submitted: {key}",
        "attribute_experience": "Experience verdict submitted: {outcome}",
        "attribute_summary": "Summary ready · {causes} failure causes · {evolution} evolution items",
        "attribute_pending": "Signals not closed: {ids}",
        "attribute_failed_timeout": "Attribution did not finish: timed out",
        "attribute_failed_tokens": "Attribution did not finish: token budget exceeded",
        "attribute_failed_error": "Attribution did not finish: run failed",
        "attribute_failed_incomplete": "Attribution did not finish: signals remain open",
        "dropped_anchors": "{count} attribution entries cannot be anchored in the trajectory and were omitted",
        "llm_iter": "iter {iteration}",
        "llm_status_running": "running",
        "llm_status_succeeded": "succeeded",
        "llm_status_failed": "failed",
    },
}


def _flatten_span(otlp: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the single span of a one-span OTLP envelope, if any."""
    for resource in otlp.get("resourceSpans") or ():
        if not isinstance(resource, Mapping):
            continue
        for scope in resource.get("scopeSpans") or ():
            if not isinstance(scope, Mapping):
                continue
            spans = scope.get("spans")
            if isinstance(spans, list):
                for span in spans:
                    if isinstance(span, Mapping):
                        return span
    return None


def _core_snapshot(snapshot: TrajectorySnapshot, session_id: str) -> dict[str, Any]:
    """Project a zhou snapshot onto the core snapshot shape.

    Only the keys ``build_nodes_from_snapshot`` consumes are produced; the
    host-side persistence fields (schema version, fingerprint, ...) are omitted
    because core never reads them.
    """
    subjects: list[dict[str, Any]] = []
    for subject in snapshot.subjects:
        records: list[Mapping[str, Any]] = []
        for record in subject.records:
            span = _flatten_span(record.otlp)
            if span is not None:
                records.append(span)
        subjects.append(
            {
                "subject_id": subject.subject_id,
                "display_name": subject.display_name,
                "kind": subject.kind or "subagent",
                "records": records,
            },
        )
    return {"session_id": session_id, "subjects": subjects}


def _prepare_nodes(inputs: EngineInputs) -> dict[str, list[Any]]:
    """Convert, number and slice the snapshot nodes (CPU work, run in a thread)."""
    snapshot = _core_snapshot(inputs.snapshot, inputs.session_id)
    nodes = build_nodes_from_snapshot(snapshot)
    assigned, _mapping = assign_node_ids(nodes, {})
    ranges = [(item.subject_id, item.start_nano, item.end_nano) for item in inputs.ranges]
    sliced = slice_nodes_by_range(assigned, ranges)
    # Root causes and the experience verdict highlight these nodes' spans, so an assistant
    # message must point at the model call that wrote it, not at a later context commit.
    anchored = SlicedNodes(anchor_assistant_nodes(sliced, snapshot))
    # Task parsing reads the whole session and the user's own messages from these two attributes.
    anchored.user_requests = sliced.user_requests
    anchored.whole = sliced.whole
    return anchored


def _render_llm_line(event: Any, text: dict[str, str]) -> str:
    """Render one core LLM request event as a single output line.

    The line always starts with ``<request_id> · `` where the core request id
    looks like ``llm-<n>``; the frontend (``splitStageOutput``) folds the lines
    with that prefix into a "model requests" group, so keep the prefix stable.
    """
    parts = [str(getattr(event, "request_id", None) or "llm"), str(getattr(event, "stage", "") or "")]
    iteration = getattr(event, "iteration", None)
    if iteration is not None:
        parts.append(text["llm_iter"].format(iteration=int(iteration)))
    status = str(getattr(event, "status", "") or "")
    parts.append(text.get(f"llm_status_{status}", status))
    model = getattr(event, "model", None)
    if model:
        parts.append(str(model))
    preview = getattr(event, "response_preview", None)
    if preview:
        parts.append(str(preview))
    error = getattr(event, "error", None)
    if error:
        parts.append(str(error))
    return " · ".join(parts)


def _default_model_factory(model: Any) -> Any:
    return model.build()


class _TaskParseAdapter:
    """Adapt core ``Model.invoke`` to the task-parse pseudo-agent protocol."""

    def __init__(self, model: Any, timeout_s: float = _TASK_PARSE_TIMEOUT_S) -> None:
        self._model = model
        self._timeout_s = timeout_s

    async def invoke(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
        del kwargs
        return await self._model.invoke(messages, tools=tools, temperature=0.0, timeout=self._timeout_s)


def _default_workspace_resolver(session_id: str) -> str:
    """Task deliverable dir as the Reward workspace, with a per-session fallback.

    The Reward sandbox equals its workspace, and extracted file artifacts live
    in the session's bound task directory (metadata ``project_dir``), so the
    workspace points there first; sessions without one fall back to the
    per-session project dir (same rule as the jiuwenswarm-new prototype).
    """
    from jiuwenswarm.common.utils import get_default_project_session_workspace_dir
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    default = str(get_default_project_session_workspace_dir(session_id))
    try:
        metadata = get_session_metadata(session_id, cache_bust=True, enable_writeback=False) or {}
    except Exception:  # noqa: BLE001 - metadata is a hint; keep the default
        return default
    project_dir = metadata.get("project_dir")
    if not isinstance(project_dir, str) or not project_dir.strip():
        return default
    candidate = Path(project_dir).expanduser()
    return str(candidate) if candidate.is_dir() else default


class _QueueStatusSink:
    """Render core LLM request events as text lines into the engine queue."""

    def __init__(self, queue: asyncio.Queue[EngineEvent | None], text: dict[str, str]) -> None:
        self._queue = queue
        self._text = text

    def report(self, event: Any) -> None:
        stage_id = _CORE_STAGE_TO_ZHOU.get(str(getattr(event, "stage", "") or ""))
        if stage_id is None:
            return
        try:
            self._queue.put_nowait(
                EngineEvent(stage_id, "output_delta", text_delta=_render_llm_line(event, self._text)),
            )
        except Exception:  # noqa: BLE001 - status loss must not break the run
            logger.debug("[diagnosis] llm status line dropped", exc_info=True)


#: Longest signal / artifact text written to a stage log line; the full text
#: lives on the cause card and in the Reward block.
_LOG_SIGNAL_LIMIT = 200
_LOG_ENTRY_LIMIT = 120
_SUMMARY_TEXT_LIMIT = 300
_SUGGESTION_LIMIT = 200


def _clip(value: str, limit: int) -> str:
    """Fold whitespace into single spaces and cut ``value`` to ``limit`` characters."""
    folded = " ".join(str(value).split())
    if len(folded) <= limit:
        return folded
    return folded[:limit].rstrip() + "…"


def _suggestion_from_comment(comment: str) -> list[str]:
    """Fallback for an entry without an agent suggestion: first two comment sentences, <=200 chars."""
    folded = " ".join(str(comment or "").split())
    if not folded:
        return []
    sentences = re.findall(r"[^。！？.!?]+[。！？.!?]?", folded)
    picked = "".join(sentences[:2]).strip() or folded
    return [picked[:_SUGGESTION_LIMIT]]


def _attribution_entry_line(entry: Mapping[str, Any], text: dict[str, str]) -> str:
    """Render one attribution entry as a stage log line (long text clipped)."""
    start = str(entry.get("start_node_id") or "")
    end = str(entry.get("end_node_id") or "")
    entry_text = _clip(str(entry.get("text_md") or ""), _LOG_ENTRY_LIMIT)
    attr_id = str(entry.get("attr_id") or "-")
    if end and end != start:
        return text["attribute_entry"].format(attr_id=attr_id, text=entry_text, start=start, end=end)
    return text["attribute_entry_node"].format(attr_id=attr_id, text=entry_text, node_id=start)


def _node_index(nodes: dict[str, list[Any]]) -> dict[str, Any]:
    """Index the nodes of every subject by node id."""
    index: dict[str, Any] = {}
    for subject_nodes in nodes.values():
        for node in subject_nodes:
            index[node.node_id] = node
    return index


def _signal_ref(signal: Any) -> SignalRef:
    """Project a core diagnostic signal onto the persisted signal reference."""
    metadata = getattr(signal, "metadata", None) or {}
    category = metadata.get("category")
    score = getattr(signal, "score", None)
    return SignalRef(
        id=str(signal.signal_id),
        source=str(getattr(signal.source, "value", signal.source)),
        tier=str(getattr(signal, "tier", "") or ""),
        score=float(score) if isinstance(score, (int, float)) else None,
        category=str(category) if category else None,
        node_ids=[str(item) for item in (getattr(signal, "node_ids", None) or [])],
        text=str(getattr(signal, "text", None) or ""),
    )


def _entry_refs(entry: Mapping[str, Any], index: dict[str, Any]) -> list[StepRef]:
    """Turn one attribution entry into the step references it anchors."""
    refs: list[StepRef] = []
    for key in ("start_node_id", "end_node_id"):
        node_id = entry.get(key)
        if not node_id:
            continue
        node = index.get(str(node_id))
        if node is None or not node.trace_id or not node.span_id:
            continue
        try:
            step = StepRef(
                trace_id=node.trace_id,
                span_id=node.span_id,
                subject_id=node.subagent_key,
                node_id=node.node_id,
            )
        except ValueError:
            continue
        refs.append(step)
    return refs


def _merge_refs(groups: list[list[StepRef]]) -> list[StepRef]:
    """Concatenate reference groups, dropping repeats by ``trace_id:span_id``."""
    merged: list[StepRef] = []
    seen: set[str] = set()
    for group in groups:
        for ref in group:
            if ref.key in seen:
                continue
            seen.add(ref.key)
            merged.append(ref)
    return merged


def _related_signal(signal: Any, entries: list[dict[str, Any]], index: dict[str, Any]) -> RelatedSignalAttribution:
    metadata = getattr(signal, "metadata", None) or {}
    category = metadata.get("category")
    score = getattr(signal, "score", None)
    return RelatedSignalAttribution(
        signal_id=str(signal.signal_id),
        source=str(getattr(signal.source, "value", signal.source)),
        tier=str(getattr(signal, "tier", "") or ""),
        category=str(category) if category else None,
        score=float(score) if isinstance(score, (int, float)) else None,
        node_ids=[str(item) for item in (getattr(signal, "node_ids", None) or [])],
        entries=[
            {
                "text_md": str(entry.get("text_md") or ""),
                "refs": [ref.model_dump() for ref in _entry_refs(entry, index)],
            }
            for entry in entries
        ],
    )


def _translate_reward_failure_causes(
    runtime: NodeBackedRuntime,
    signals: list[Any],
    reward_payload: dict[str, Any] | None,
    nodes: dict[str, list[Any]],
) -> tuple[list[RewardFailureCause], int]:
    """Assemble section 4.3; suggestions are the agent's entry suggestions.

    A reward comment only stands in when an entry carries no suggestion (an
    agent build older than the entry field), so a mixed deployment still shows
    something actionable.
    """
    index = _node_index(nodes)
    signals_by_id = {signal.signal_id: signal for signal in signals}
    dimensions = {}
    if isinstance(reward_payload, Mapping):
        for dimension in reward_payload.get("dimensions") or ():
            if isinstance(dimension, Mapping) and dimension.get("requirement"):
                dimensions[str(dimension["requirement"])] = str(dimension.get("comment") or "")
    causes: list[RewardFailureCause] = []
    dropped = 0
    for signal_id, entries in runtime.reward_attributions.items():
        signal = signals_by_id.get(signal_id)
        if signal is None:
            continue
        dimension = str(signal.text).split("\n", 1)[0]
        for entry in entries:
            refs = _entry_refs(entry, index)
            if not refs:
                dropped += 1
                continue
            agent_suggestion = str(entry.get("suggestion") or "").strip()
            suggestion = (
                [agent_suggestion]
                if agent_suggestion
                else _suggestion_from_comment(dimensions.get(dimension, ""))
            )
            causes.append(
                RewardFailureCause(
                    id=f"rfc-{len(causes) + 1}",
                    dimension=dimension,
                    summary=str(entry.get("text_md") or ""),
                    evidence=refs,
                    suggestion=suggestion,
                    signal=_signal_ref(signal),
                ),
            )
    return causes, dropped


def _marked_signal_ids(runtime: NodeBackedRuntime, category: str, *, skill_key: str | None = None) -> list[str]:
    invalid = set(runtime.invalid_signals)
    found: list[str] = []
    for signal_id, categories in runtime.marks.items():
        if signal_id in invalid or category not in categories:
            continue
        if skill_key is not None and categories[category].skill_key != skill_key:
            continue
        found.append(signal_id)
    return found


def _translate_evolution_judge(
    runtime: NodeBackedRuntime,
    signals: list[Any],
    nodes: dict[str, list[Any]],
) -> tuple[list[EvolutionJudge], int]:
    """Assemble section 4.4 from the marked signals and the global advice."""
    index = _node_index(nodes)
    signals_by_id = {signal.signal_id: signal for signal in signals}
    judges: list[EvolutionJudge] = []
    dropped = 0

    for key, payload in runtime.skill_evolutions.items():
        related_ids = [
            signal_id
            for signal_id in _marked_signal_ids(runtime, "skill", skill_key=key)
            if signal_id in runtime.skill_attributions
        ]
        entries = [entry for signal_id in related_ids for entry in runtime.skill_attributions[signal_id]]
        refs = _merge_refs([_entry_refs(entry, index) for entry in entries])
        if not refs:
            dropped += 1
            continue
        summary = _clip(" ".join(str(entry.get("text_md") or "") for entry in entries), _SUMMARY_TEXT_LIMIT)
        judges.append(
            EvolutionJudge(
                id=f"evo-{len(judges) + 1}",
                category="skill",
                summary=summary,
                evidence=refs,
                suggestion=[str(payload.get("md_string") or "")],
                score=float(payload.get("confidence", 0.5)),
                target=key,
                related_signals=[
                    _related_signal(signals_by_id[signal_id], runtime.skill_attributions[signal_id], index)
                    for signal_id in related_ids
                    if signal_id in signals_by_id
                ],
            ),
        )

    if runtime.experience_evolution is not None:
        related_ids = [
            signal_id
            for signal_id in _marked_signal_ids(runtime, "experience")
            if signal_id in runtime.experience_attributions
        ]
        entries = [entry for signal_id in related_ids for entry in runtime.experience_attributions[signal_id]]
        refs = _merge_refs([_entry_refs(entry, index) for entry in entries])
        if not refs:
            dropped += 1
        else:
            outcome = str(runtime.experience_evolution.get("outcome") or "fail")
            summary = _clip(" ".join(str(entry.get("text_md") or "") for entry in entries), _SUMMARY_TEXT_LIMIT)
            judges.append(
                EvolutionJudge(
                    id=f"evo-{len(judges) + 1}",
                    category="experience",
                    summary=summary,
                    evidence=refs,
                    suggestion=[str(item) for item in runtime.experience_evolution.get("suggestions") or []],
                    score=_EXPERIENCE_SCORE.get(outcome, 0.0),
                    outcome=outcome,  # type: ignore[arg-type]
                    related_signals=[
                        _related_signal(signals_by_id[signal_id], runtime.experience_attributions[signal_id], index)
                        for signal_id in related_ids
                        if signal_id in signals_by_id
                    ],
                ),
            )
    return judges, dropped


class _StreamingRuntime(NodeBackedRuntime):
    """Attribution runtime that pushes whole tables and log lines while the agent works.

    The core agent only hands over its results when the run ends; the tool
    callbacks, however, run on the event loop while the agent works, so the
    attribute stage can show each submission live and refresh the two output
    tables as soon as they change. Stored state is unchanged: the final result
    is still built from the runtime stores, which have the last word.
    """

    def __init__(
        self,
        nodes_by_subagent: dict[str, list[Any]],
        language: str,
        queue: asyncio.Queue[EngineEvent | None],
        text: dict[str, str],
        signals: list[Any] | None = None,
        reward_payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(nodes_by_subagent, language)
        self._queue = queue
        self._text = text
        self._signals = list(signals or [])
        self._reward_payload = reward_payload
        self.nodes = nodes_by_subagent

    def _log(self, line: str) -> None:
        try:
            self._queue.put_nowait(EngineEvent("attribute", "output_delta", text_delta=line))
        except Exception:  # noqa: BLE001 - a lost log line must not break the run
            logger.debug("[diagnosis] attribution log line dropped", exc_info=True)

    def _updated(self, status: str) -> str:
        return self._text["attribute_updated_prefix"] if status == "updated" else ""

    def _push_tables(self) -> None:
        """Push the whole reward-cause and evolution tables known so far."""
        try:
            causes, _ = _translate_reward_failure_causes(self, self._signals, self._reward_payload, self.nodes)
            if self._reward_payload is not None:
                self._queue.put_nowait(
                    EngineEvent("attribute", "reward_failure_causes", reward_failure_causes=causes),
                )
            judges, _ = _translate_evolution_judge(self, self._signals, self.nodes)
            self._queue.put_nowait(EngineEvent("attribute", "evolution_judge", evolution_judge=judges))
        except Exception:  # noqa: BLE001 - a lost update must not break the run
            logger.debug("[diagnosis] attribution table update dropped", exc_info=True)

    def _record_cause(self, bucket: str, signal_id: str, entries: list[dict[str, Any]]) -> str:
        store = {
            "reward_attributions": self.reward_attributions,
            "skill_attributions": self.skill_attributions,
            "experience_attributions": self.experience_attributions,
        }[bucket]
        status = super()._record_bucket(store, signal_id, entries)
        prefix = self._updated(status)
        for entry in store.get(signal_id, []):
            self._log(prefix + _attribution_entry_line(entry, self._text))
        self._push_tables()
        return status

    def record_reward_failure(self, signal_id: str, entries: list[dict[str, Any]]) -> str:
        return self._record_cause("reward_attributions", signal_id, entries)

    def record_skill_cause(self, signal_id: str, entries: list[dict[str, Any]]) -> str:
        return self._record_cause("skill_attributions", signal_id, entries)

    def record_experience_cause(self, signal_id: str, entries: list[dict[str, Any]]) -> str:
        return self._record_cause("experience_attributions", signal_id, entries)

    def mark_signal_topic(
        self,
        signal_id: str,
        category: str,
        skill_key: str | None = None,
        relation: str | None = None,
    ) -> str:
        status = super().mark_signal_topic(signal_id, category, skill_key, relation)
        self._log(self._text["attribute_mark"].format(category=category, signal_id=signal_id))
        self._push_tables()
        return status

    def mark_signal_invalid(self, signal_id: str) -> str:
        status = super().mark_signal_invalid(signal_id)
        self._log(self._text["attribute_invalid"].format(signal_id=signal_id))
        return status

    def record_artifact_improvement(self, md: str) -> str:
        status = super().record_artifact_improvement(md)
        self._log(self._updated(status) + self._text["attribute_artifact"])
        return status

    def record_skill_evolution(self, key: str, md: str, confidence: float = 0.5) -> str:
        status = super().record_skill_evolution(key, md, confidence)
        self._log(self._updated(status) + self._text["attribute_skill"].format(key=key))
        self._push_tables()
        return status

    def record_experience_evolution(self, outcome: str, suggestions: list[str]) -> str:
        status = super().record_experience_evolution(outcome, suggestions)
        self._log(self._updated(status) + self._text["attribute_experience"].format(outcome=outcome))
        self._push_tables()
        return status

    def record_trajectory_summary(self, summary: str) -> str:
        status = super().record_trajectory_summary(summary)
        self._log(self._updated(status) + _clip(summary, _LOG_ENTRY_LIMIT))
        return status


@dataclass
class _StageContext:
    """Per-run state shared by the three attribution stages."""

    inputs: EngineInputs
    text: dict[str, str]
    queue: asyncio.Queue[EngineEvent | None]
    model: Any
    model_name: str
    counter: list[int]
    sink: _QueueStatusSink
    nodes: dict[str, list[Any]]
    workspace: str


class AttributionCoreEngine:
    """Diagnosis engine running the agent-core three-stage attribution pipeline.

    Args:
        model_factory: Builds the model client from the resolved selection.
            Defaults to ``ResolvedDiagnosisModel.build``; replaced in tests.
        workspace_resolver: Resolves the Reward workspace directory of a
            session; replaced in tests.
    """

    def __init__(
        self,
        *,
        model_factory: ModelFactory | None = None,
        workspace_resolver: WorkspaceResolver | None = None,
    ) -> None:
        self._model_factory = model_factory or _default_model_factory
        self._workspace_resolver = workspace_resolver or _default_workspace_resolver

    async def run(self, inputs: EngineInputs, emit: EmitFn) -> EngineResult:
        """Run the three stages and return the structured result.

        Args:
            inputs: Snapshot, ranges, resolved model and caller note.
            emit: Progress callback; all events pass through one queue drained
                by a single writer so ``emit`` is awaited strictly in order.

        Returns:
            Results that the host filters and persists.

        Raises:
            EngineError: ``EMPTY_SCOPE``, ``TASK_INCOMPLETE``,
                ``MULTIPLE_UNRELATED_TASKS``, ``MODEL_CALL_FAILED``,
                ``MODEL_OUTPUT_INVALID`` when the pipeline cannot produce a
                diagnosis. A run without any signal is not an error: it ends
                completed with the attribute stage skipped.
        """
        text = _TEXTS[inputs.locale]
        queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
        drainer = asyncio.create_task(self._drain(queue, emit))
        try:
            result = await self._run(inputs, text, queue)
        except asyncio.CancelledError:
            # A cancelled run must not keep emitting; the host marks the
            # remaining stages failed and finishes the record itself.
            queue.put_nowait(None)
            drainer.cancel()
            await asyncio.gather(drainer, return_exceptions=True)
            raise
        except BaseException:
            # A named engine failure still flushes the events produced so far
            # so the host sees the stage it aborted in.
            queue.put_nowait(None)
            await asyncio.gather(drainer, return_exceptions=True)
            raise
        # Successful runs drain the queue to the end so ``emit`` (and thus the
        # host's persistence) is fully settled before the engine returns.
        queue.put_nowait(None)
        await drainer
        return result

    @staticmethod
    async def _drain(queue: asyncio.Queue[EngineEvent | None], emit: EmitFn) -> None:
        while True:
            event = await queue.get()
            if event is None:
                return
            await emit(event)

    async def _run(
        self,
        inputs: EngineInputs,
        text: dict[str, str],
        queue: asyncio.Queue[EngineEvent | None],
    ) -> EngineResult:
        model = self._model_factory(inputs.model)
        model_name = str(inputs.model.info.display_name or inputs.model.info.id or "")
        counter = [0]
        sink = _QueueStatusSink(queue, text)

        workspace = await asyncio.to_thread(self._workspace_resolver, inputs.session_id)
        sliced = await asyncio.to_thread(_prepare_nodes, inputs)
        if not any(sliced.values()):
            raise EngineError("EMPTY_SCOPE", text["empty_scope"])

        context = _StageContext(
            inputs=inputs,
            text=text,
            queue=queue,
            model=model,
            model_name=model_name,
            counter=counter,
            sink=sink,
            nodes=sliced,
            workspace=workspace,
        )
        try:
            task_parse = await self._stage_task_parse(context)
        except EngineError as exc:
            # A valid range must still produce a summary even when the task
            # cannot be parsed; keep the record completed with a warning.
            logger.warning("[diagnosis] task parse failed; summary only: %s", exc.code)
            queue.put_nowait(
                EngineEvent("task_parse", "stage_finished", status="failed", summary=exc.message),
            )
            summary = await self._summary_only(context)
            return EngineResult(
                trajectory_summary=summary,
                error=DiagnosisError(code=exc.code, message=exc.message),
            )
        diagnosis = await self._stage_diagnose(context, task_parse)
        if diagnosis.signals:
            result = await self._stage_attribute(context, task_parse, diagnosis)
        else:
            # Nothing to attribute is a normal outcome, not a failure.
            result = await self._result_without_signals(context, task_parse, diagnosis)
        return result

    @staticmethod
    def _task_hint(context: _StageContext) -> str:
        """Best-effort task hint from the user's own messages when task parsing is unavailable."""
        parts: list[str] = []
        for node in getattr(context.nodes, "user_requests", ()) or ():
            content = str(getattr(node, "content", "") or "").strip()
            if content:
                parts.append(content)
        return " / ".join(parts)[:2000]

    async def _summary_only(self, context: _StageContext, *, task_description: str = "") -> str:
        """A narrative summary for a valid range when the normal attribution agent will not run."""
        hint = task_description.strip() or self._task_hint(context)
        try:
            outcome = await run_trajectory_summary(
                model=context.model,
                nodes_by_subagent=context.nodes,
                language=context.inputs.locale,
                status_sink=context.sink,
                model_name=context.model_name,
                status_counter=context.counter,
                task_hint=hint,
            )
            summary = str(outcome.trajectory_summary or "").strip()
        except Exception:  # noqa: BLE001 - a valid range still needs some summary
            logger.warning("[diagnosis] summary-only run failed", exc_info=True)
            summary = ""
        return summary or context.text["trajectory_summary_unavailable"]

    async def _stage_task_parse(self, context: _StageContext) -> Any:
        inputs = context.inputs
        text = context.text
        queue = context.queue
        queue.put_nowait(EngineEvent("task_parse", "stage_started", title=text["stage_task_parse"]))
        try:
            parsed = await run_task_parse(
                context.nodes,
                inputs.locale,
                _TaskParseAdapter(context.model),
                workspace=context.workspace,
                status_sink=context.sink,
                model_name=context.model_name,
                status_counter=context.counter,
            )
        except TaskParseError as exc:
            if exc.error_type == "multiple_unrelated_tasks":
                raise EngineError("MULTIPLE_UNRELATED_TASKS", text["parse_unrelated"]) from exc
            if exc.error_type == "invalid_response":
                raise EngineError(
                    "MODEL_OUTPUT_INVALID",
                    text["parse_invalid"].format(reason=exc.message),
                ) from exc
            raise EngineError("TASK_INCOMPLETE", text["parse_incomplete"]) from exc
        except Exception as exc:  # noqa: BLE001 - a failed call after retries is a named failure
            raise EngineError(
                "MODEL_CALL_FAILED",
                text["model_failed"].format(reason=str(exc)[:200] or type(exc).__name__),
            ) from exc
        queue.put_nowait(
            EngineEvent(
                "task_parse",
                "output_delta",
                text_delta=text["task_description"].format(description=parsed.task_description),
            ),
        )
        candidates = {path: (node_id, tool_type) for path, node_id, tool_type in parsed.file_candidates}
        for node_id, message in parsed.message_artifacts:
            queue.put_nowait(
                EngineEvent(
                    "task_parse",
                    "output_delta",
                    text_delta=text["message_artifact"].format(
                        node_id=node_id,
                        text=_clip(message, _LOG_SIGNAL_LIMIT),
                    ),
                ),
            )
        for path in parsed.file_artifacts:
            node_id, tool_type = candidates.get(path, ("", ""))
            queue.put_nowait(
                EngineEvent(
                    "task_parse",
                    "output_delta",
                    text_delta=text["file_artifact"].format(
                        path=path,
                        node_id=node_id or "-",
                        tool_type=tool_type or "-",
                    ),
                ),
            )
        summary = text["task_summary"].format(
            artifacts=len(parsed.message_artifacts) + len(parsed.file_artifacts),
            messages=len(parsed.message_artifacts),
            files=len(parsed.file_artifacts),
        )
        queue.put_nowait(EngineEvent("task_parse", "stage_finished", status="completed", summary=summary))
        return parsed

    async def _stage_diagnose(self, context: _StageContext, task_parse: Any) -> Any:
        inputs = context.inputs
        text = context.text
        queue = context.queue
        queue.put_nowait(EngineEvent("diagnose", "stage_started", title=text["stage_diagnose"]))
        diagnosis = await run_diagnosis(
            run_id=f"diag-{time.time_ns()}",
            language=inputs.locale,
            nodes_by_subagent=context.nodes,
            user_attribution=inputs.user_note or "",
            task_parse=task_parse,
            workspace=context.workspace,
            keyword_mode="rule",
            model=context.model,
            reward_card=AgentCard(
                id=_REWARD_CARD_ID,
                name="AttributionRewardAgent",
                description="Task execution reward evaluator / 任务执行 Reward 评价 Agent",
            ),
            status_sink=context.sink,
            model_name=context.model_name,
            status_counter=context.counter,
        )
        for signal in diagnosis.signals:
            queue.put_nowait(
                EngineEvent(
                    "diagnose",
                    "output_delta",
                    text_delta=text["signal_line"].format(
                        signal_id=signal.signal_id,
                        tier=signal.tier,
                        source=signal.source.value,
                        text=_clip(signal.text, _LOG_SIGNAL_LIMIT),
                        node_ids=", ".join(signal.node_ids) or "-",
                    ),
                ),
            )
        reward = diagnosis.run_meta.get("reward") or {}
        reward_note: str | None = None
        if diagnosis.reward_overall is not None:
            queue.put_nowait(
                EngineEvent(
                    "diagnose",
                    "output_delta",
                    text_delta=text["reward_total"].format(
                        score=diagnosis.reward_overall.total_score,
                        comment=diagnosis.reward_overall.comment,
                    ),
                ),
            )
        else:
            # Rule mode: say why Reward did not produce the signals.
            if reward.get("skipped") == "no artifacts":
                reward_note = text["reward_skipped_no_artifacts"]
            elif reward.get("skipped") == "no model injected":
                reward_note = text["reward_skipped_no_model"]
            elif reward.get("failed"):
                reward_note = text["reward_failed"].format(reason=reward.get("error") or reward.get("failed"))
            elif diagnosis.signals:
                reward_note = text["reward_no_signals"]
            if reward_note:
                queue.put_nowait(EngineEvent("diagnose", "output_delta", text_delta=reward_note))
        if diagnosis.signal_mode == "reward":
            summary = text["diagnose_summary_reward"].format(count=len(diagnosis.signals))
        elif reward_note:
            summary = text["diagnose_summary_rule_reason"].format(
                count=len(diagnosis.signals),
                reason=_clip(reward_note, _LOG_ENTRY_LIMIT),
            )
        else:
            summary = text["diagnose_summary_rule"].format(count=len(diagnosis.signals))
        queue.put_nowait(EngineEvent("diagnose", "stage_finished", status="completed", summary=summary))
        return diagnosis

    async def _result_without_signals(
        self,
        context: _StageContext,
        task_parse: Any,
        diagnosis: Any,
    ) -> EngineResult:
        """Finish normally when diagnosis produced no signal: the attribute stage is skipped."""
        text = context.text
        queue = context.queue
        queue.put_nowait(EngineEvent("attribute", "stage_started", title=text["stage_attribute"]))
        queue.put_nowait(
            EngineEvent(
                "attribute",
                "stage_finished",
                status="skipped",
                skip_reason=text["attribute_skipped_no_signals"],
            ),
        )
        summary = await self._summary_only(context, task_description=task_parse.task_description)
        return EngineResult(trajectory_summary=summary, reward=diagnosis.reward_payload)

    async def _stage_attribute(self, context: _StageContext, task_parse: Any, diagnosis: Any) -> EngineResult:
        inputs = context.inputs
        text = context.text
        queue = context.queue
        nodes = context.nodes
        queue.put_nowait(EngineEvent("attribute", "stage_started", title=text["stage_attribute"]))
        language = inputs.locale
        if not any(signal.tier != "perfect" for signal in diagnosis.signals):
            skipped = [signal.signal_id for signal in diagnosis.signals if signal.tier == "perfect"]
            if skipped:
                queue.put_nowait(
                    EngineEvent(
                        "attribute",
                        "output_delta",
                        text_delta=text["perfect_skipped"].format(ids=", ".join(skipped)),
                    ),
                )
            summary = await self._summary_only(context, task_description=task_parse.task_description)
            queue.put_nowait(
                EngineEvent(
                    "attribute",
                    "stage_finished",
                    status="completed",
                    summary=text["attribute_summary"].format(causes=0, evolution=0),
                ),
            )
            return EngineResult(trajectory_summary=summary, reward=diagnosis.reward_payload)
        # Entries are logged and the two tables refreshed as the agent submits them.
        runtime = _StreamingRuntime(nodes, language, queue, text, diagnosis.signals, diagnosis.reward_payload)
        outcome = await run_attribution(
            model=context.model,
            nodes_by_subagent=nodes,
            signals=diagnosis.signals,
            task_description=task_parse.task_description,
            reward_overall=diagnosis.reward_overall,
            language=language,
            swarm_mode=inputs.mode_profile is ModeProfile.TEAM,
            skill_evolution=inputs.skill_evolution,
            ttse_advice=inputs.ttse_advice,
            runtime=runtime,
            status_sink=context.sink,
            model_name=context.model_name,
            status_counter=context.counter,
        )
        if outcome.skipped_signal_ids:
            queue.put_nowait(
                EngineEvent(
                    "attribute",
                    "output_delta",
                    text_delta=text["perfect_skipped"].format(ids=", ".join(outcome.skipped_signal_ids)),
                ),
            )
        if outcome.pending_signal_ids:
            queue.put_nowait(
                EngineEvent(
                    "attribute",
                    "output_delta",
                    text_delta=text["attribute_pending"].format(ids=", ".join(outcome.pending_signal_ids)),
                ),
            )
        causes, dropped_causes = _translate_reward_failure_causes(
            runtime, diagnosis.signals, diagnosis.reward_payload, nodes
        )
        judges, dropped_judges = _translate_evolution_judge(runtime, diagnosis.signals, nodes)
        dropped = dropped_causes + dropped_judges
        if dropped:
            queue.put_nowait(
                EngineEvent(
                    "attribute",
                    "output_delta",
                    text_delta=text["dropped_anchors"].format(count=dropped),
                ),
            )
        result = EngineResult(
            trajectory_summary=outcome.trajectory_summary or text["trajectory_summary_no_signals"],
            reward_failure_causes=causes,
            evolution_judge=judges,
            artifact_improvement=runtime.artifact_improvement,
            reward=diagnosis.reward_payload,
        )
        status = "completed"
        if outcome.status == "failed":
            key = f"attribute_failed_{outcome.failure_kind}" if outcome.failure_kind else "attribute_failed_error"
            message = text.get(key, text["attribute_failed_error"])
            if outcome.error_detail:
                message = f"{message}：{outcome.error_detail}"
            result.error = DiagnosisError(code="ATTRIBUTION_INCOMPLETE", message=message)
            queue.put_nowait(EngineEvent("attribute", "output_delta", text_delta=message))
            status = "failed"
        summary = text["attribute_summary"].format(causes=len(causes), evolution=len(judges))
        queue.put_nowait(EngineEvent("attribute", "stage_finished", status=status, summary=summary))
        return result


__all__ = ["AttributionCoreEngine", "ModelFactory", "WorkspaceResolver"]
