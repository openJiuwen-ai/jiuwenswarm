# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Formal diagnosis engine backed by the agent-core attribution pipeline.

The engine adapts the three-stage attribution pipeline of
``openjiuwen.agent_evolving.attribution`` (task parsing, diagnosis, attribution)
to the :class:`DiagnosisEngine` contract. It only reads the snapshot and the
ranges the host passes in and reports progress through ``emit``.

Design notes (see ``CYR/CONTEXT_03_adapt_to_zhou.md``):

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
import time
import uuid
from collections.abc import Callable, Mapping
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
    slice_nodes_by_range,
)
from openjiuwen.core.single_agent.schema.agent_card import AgentCard

from jiuwenswarm.diagnosis.engine import EmitFn, EngineError, EngineEvent, EngineInputs, EngineResult
from jiuwenswarm.diagnosis.models import (
    TTSE_ROOT_CAUSE_CATEGORIES,
    ArtifactEval,
    ArtifactFix,
    DiagnosisError,
    DiagnosisSuggestions,
    ModeProfile,
    RootCause,
    RootCauseCategory,
    SkillSuggestion,
    StepRef,
)
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
        "no_signals": "诊断后没有可归因的信号",
        "diagnose_summary_reward": "信号 {count} 条（Reward 模式）",
        "diagnose_summary_rule": "信号 {count} 条（规则模式）",
        "headline_score": "总分 {score} · 待归因 {count} 项",
        "headline_plain": "待归因 {count} 项",
        "perfect_skipped": "已跳过 perfect 信号：{ids}",
        "attribute_entry": "[{attr_id}] {text}（{start} → {end}）",
        "attribute_entry_node": "[{attr_id}] {text}（{node_id}）",
        "attribute_invalid": "标记无效：{signal_id}",
        "attribute_pending": "未收口信号：{ids}",
        "attribute_failed_timeout": "归因未完成：超时",
        "attribute_failed_tokens": "归因未完成：超出 token 预算",
        "attribute_failed_error": "归因未完成：运行出错",
        "attribute_failed_incomplete": "归因未完成：仍有信号未收口",
        "attribute_summary": "归因 {attributed} 条 · 标无效 {invalid} 条 · 改进意见 1 条",
        "attribute_summary_no_fix": "归因 {attributed} 条 · 标无效 {invalid} 条",
        "dropped_anchors": "有 {count} 条归因的锚点无法在轨迹中定位，其根因已从列表剔除",
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
        "no_signals": "No signals to attribute after diagnosis",
        "diagnose_summary_reward": "{count} signals (reward mode)",
        "diagnose_summary_rule": "{count} signals (rule mode)",
        "headline_score": "Score {score} · {count} to attribute",
        "headline_plain": "{count} to attribute",
        "perfect_skipped": "Perfect signals skipped: {ids}",
        "attribute_entry": "[{attr_id}] {text} ({start} → {end})",
        "attribute_entry_node": "[{attr_id}] {text} ({node_id})",
        "attribute_invalid": "Marked invalid: {signal_id}",
        "attribute_pending": "Signals not closed: {ids}",
        "attribute_failed_timeout": "Attribution did not finish: timed out",
        "attribute_failed_tokens": "Attribution did not finish: token budget exceeded",
        "attribute_failed_error": "Attribution did not finish: run failed",
        "attribute_failed_incomplete": "Attribution did not finish: signals remain open",
        "attribute_summary": "Attributed {attributed} · invalid {invalid} · 1 improvement",
        "attribute_summary_no_fix": "Attributed {attributed} · invalid {invalid}",
        "dropped_anchors": (
            "{count} attribution entries have anchors that cannot be located in the trajectory; "
            "their root causes were dropped"
        ),
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
    return slice_nodes_by_range(assigned, ranges)


def _render_llm_line(event: Any, text: dict[str, str]) -> str:
    """Render one core LLM request event as a single output line."""
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


def _translate_attribution(
    nodes: dict[str, list[Any]],
    runtime: NodeBackedRuntime,
    signals: list[Any],
) -> tuple[list[RootCause], DiagnosisSuggestions, int]:
    """Map core attribution output onto zhou root causes and suggestions.

    An entry whose anchors carry no highlightable span identity (hash fallback
    nodes) yields no step reference; the host filter drops such root causes, so
    they are counted and reported by the caller.

    Returns:
        The root causes, the suggestions and the number of root causes that
        cannot be anchored in the trajectory.
    """
    index: dict[str, Any] = {}
    for subject_nodes in nodes.values():
        for node in subject_nodes:
            index[node.node_id] = node
    signals_by_id = {signal.signal_id: signal for signal in signals}
    root_causes: list[RootCause] = []
    dropped = 0
    for signal_id, entries in runtime.attributions.items():
        evidence = signals_by_id.get(signal_id)
        for entry in entries:
            refs: list[StepRef] = []
            for node_id in (entry.get("start_node_id"), entry.get("end_node_id")):
                if not node_id:
                    continue
                node = index.get(str(node_id))
                if node is None or not node.trace_id or not node.span_id:
                    continue
                try:
                    refs.append(
                        StepRef(
                            trace_id=node.trace_id,
                            span_id=node.span_id,
                            subject_id=node.subagent_key,
                            node_id=node.node_id,
                        ),
                    )
                except ValueError:
                    continue
            subject_id = str(entry.get("start_node_id") or "").rpartition(":")[0]
            if not subject_id and refs:
                subject_id = refs[0].subject_id
            root_causes.append(
                RootCause(
                    id=str(entry.get("attr_id") or f"attr-{uuid.uuid4().hex[:8]}"),
                    category=RootCauseCategory.OTHER,
                    subject_id=subject_id,
                    refs=refs,
                    summary=str(entry.get("text_md") or ""),
                    evidence=evidence.text if evidence is not None else "",
                    confidence=None,
                ),
            )
            if not refs:
                dropped += 1
    suggestions = DiagnosisSuggestions()
    if runtime.artifact_improvement:
        suggestions.artifact_fix = ArtifactFix(
            instruction=runtime.artifact_improvement,
            root_cause_ids=[cause.id for cause in root_causes],
        )
    for key, markdown in runtime.skill_improvements.items():
        suggestions.skills.append(
            SkillSuggestion(
                id=f"skill-{uuid.uuid4().hex[:8]}",
                skill_name=key,
                change_summary=markdown,
                rationale=markdown,
                message=markdown,
                root_cause_ids=[cause.id for cause in root_causes],
            ),
        )
    return root_causes, suggestions, dropped


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
                ``MODEL_OUTPUT_INVALID`` or ``NO_SIGNALS`` when the pipeline
                cannot produce a diagnosis.
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

        task_parse = await self._stage_task_parse(inputs, text, queue, model, model_name, counter, sink, sliced, workspace)
        diagnosis, headline = await self._stage_diagnose(
            inputs,
            text,
            queue,
            model,
            model_name,
            counter,
            sink,
            sliced,
            task_parse,
            workspace,
        )
        result = await self._stage_attribute(
            inputs,
            text,
            queue,
            model,
            model_name,
            counter,
            sink,
            sliced,
            task_parse,
            diagnosis,
            headline,
        )
        await self._append_experience_cause(inputs, model, model_name, sliced, result)
        return result

    @staticmethod
    async def _append_experience_cause(
        inputs: EngineInputs,
        model: Any,
        model_name: str,
        sliced: dict[str, list[Any]],
        result: EngineResult,
    ) -> None:
        """Append the TTSE experience root cause of the main agent's stretch.

        Only agent work is judged, at most one such cause is reported, and a
        detection failure is logged without touching the rest of the result.
        """
        if inputs.mode_profile is not ModeProfile.AGENT_WORK:
            return
        if any(cause.category in TTSE_ROOT_CAUSE_CATEGORIES for cause in result.root_causes):
            return
        subject_id = next((item.subject_id for item in inputs.snapshot.subjects if item.kind == "main_agent"), None)
        nodes = sliced.get(subject_id) if subject_id else None
        if not nodes:
            return
        try:
            from jiuwenswarm.diagnosis.ttse_detect import detect_experience_cause

            cause = await detect_experience_cause(
                nodes,
                model=model,
                model_name=model_name,
                locale=inputs.locale,
            )
        except Exception:  # noqa: BLE001 - experience detection must not fail the diagnosis
            logger.warning("[diagnosis] experience detection failed", exc_info=True)
            return
        if cause is not None:
            result.root_causes.append(cause)

    async def _stage_task_parse(
        self,
        inputs: EngineInputs,
        text: dict[str, str],
        queue: asyncio.Queue[EngineEvent | None],
        model: Any,
        model_name: str,
        counter: list[int],
        sink: _QueueStatusSink,
        nodes: dict[str, list[Any]],
        workspace: str,
    ) -> Any:
        queue.put_nowait(EngineEvent("task_parse", "stage_started", title=text["stage_task_parse"]))
        try:
            parsed = await run_task_parse(
                nodes,
                inputs.locale,
                _TaskParseAdapter(model),
                workspace=workspace,
                status_sink=sink,
                model_name=model_name,
                status_counter=counter,
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
                    text_delta=text["message_artifact"].format(node_id=node_id, text=message),
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

    async def _stage_diagnose(
        self,
        inputs: EngineInputs,
        text: dict[str, str],
        queue: asyncio.Queue[EngineEvent | None],
        model: Any,
        model_name: str,
        counter: list[int],
        sink: _QueueStatusSink,
        nodes: dict[str, list[Any]],
        task_parse: Any,
        workspace: str,
    ) -> tuple[Any, str]:
        queue.put_nowait(EngineEvent("diagnose", "stage_started", title=text["stage_diagnose"]))
        diagnosis = await run_diagnosis(
            run_id=f"diag-{time.time_ns()}",
            language=inputs.locale,
            nodes_by_subagent=nodes,
            user_attribution=inputs.user_note or "",
            task_parse=task_parse,
            workspace=workspace,
            keyword_mode="rule",
            model=model,
            reward_card=AgentCard(
                id=_REWARD_CARD_ID,
                name="AttributionRewardAgent",
                description="Task execution reward evaluator / 任务执行 Reward 评价 Agent",
            ),
            status_sink=sink,
            model_name=model_name,
            status_counter=counter,
        )
        if not diagnosis.signals:
            raise EngineError("NO_SIGNALS", text["no_signals"])
        for signal in diagnosis.signals:
            queue.put_nowait(
                EngineEvent(
                    "diagnose",
                    "output_delta",
                    text_delta=text["signal_line"].format(
                        signal_id=signal.signal_id,
                        tier=signal.tier,
                        source=signal.source.value,
                        text=signal.text,
                        node_ids=", ".join(signal.node_ids) or "-",
                    ),
                ),
            )
        reward = diagnosis.run_meta.get("reward") or {}
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
        elif reward.get("skipped") == "no artifacts":
            queue.put_nowait(EngineEvent("diagnose", "output_delta", text_delta=text["reward_skipped_no_artifacts"]))
        elif reward.get("skipped") == "no model injected":
            queue.put_nowait(EngineEvent("diagnose", "output_delta", text_delta=text["reward_skipped_no_model"]))
        elif reward.get("failed"):
            queue.put_nowait(
                EngineEvent(
                    "diagnose",
                    "output_delta",
                    text_delta=text["reward_failed"].format(reason=reward.get("error") or reward.get("failed")),
                ),
            )
        else:
            queue.put_nowait(EngineEvent("diagnose", "output_delta", text_delta=text["reward_no_signals"]))
        pending = sum(1 for signal in diagnosis.signals if signal.tier != "perfect")
        if diagnosis.reward_overall is not None:
            headline = text["headline_score"].format(score=diagnosis.reward_overall.total_score, count=pending)
        else:
            headline = text["headline_plain"].format(count=pending)
        summary = (
            text["diagnose_summary_reward"].format(count=len(diagnosis.signals))
            if diagnosis.signal_mode == "reward"
            else text["diagnose_summary_rule"].format(count=len(diagnosis.signals))
        )
        queue.put_nowait(EngineEvent("diagnose", "stage_finished", status="completed", summary=summary))
        return diagnosis, headline

    async def _stage_attribute(
        self,
        inputs: EngineInputs,
        text: dict[str, str],
        queue: asyncio.Queue[EngineEvent | None],
        model: Any,
        model_name: str,
        counter: list[int],
        sink: _QueueStatusSink,
        nodes: dict[str, list[Any]],
        task_parse: Any,
        diagnosis: Any,
        headline: str,
    ) -> EngineResult:
        queue.put_nowait(EngineEvent("attribute", "stage_started", title=text["stage_attribute"]))
        language = inputs.locale
        runtime = NodeBackedRuntime(nodes, language)
        outcome = await run_attribution(
            model=model,
            nodes_by_subagent=nodes,
            signals=diagnosis.signals,
            task_description=task_parse.task_description,
            reward_overall=diagnosis.reward_overall,
            language=language,
            swarm_mode=inputs.mode_profile is ModeProfile.TEAM,
            runtime=runtime,
            status_sink=sink,
            model_name=model_name,
            status_counter=counter,
        )
        if outcome.skipped_signal_ids:
            queue.put_nowait(
                EngineEvent(
                    "attribute",
                    "output_delta",
                    text_delta=text["perfect_skipped"].format(ids=", ".join(outcome.skipped_signal_ids)),
                ),
            )
        for entries in runtime.attributions.values():
            for entry in entries:
                start = str(entry.get("start_node_id") or "")
                end = str(entry.get("end_node_id") or "")
                entry_text = str(entry.get("text_md") or "")
                attr_id = str(entry.get("attr_id") or "-")
                if end and end != start:
                    queue.put_nowait(
                        EngineEvent(
                            "attribute",
                            "output_delta",
                            text_delta=text["attribute_entry"].format(
                                attr_id=attr_id,
                                text=entry_text,
                                start=start,
                                end=end,
                            ),
                        ),
                    )
                else:
                    queue.put_nowait(
                        EngineEvent(
                            "attribute",
                            "output_delta",
                            text_delta=text["attribute_entry_node"].format(
                                attr_id=attr_id,
                                text=entry_text,
                                node_id=start,
                            ),
                        ),
                    )
        for signal_id in sorted(runtime.invalid_signals):
            queue.put_nowait(
                EngineEvent("attribute", "output_delta", text_delta=text["attribute_invalid"].format(signal_id=signal_id)),
            )
        if outcome.pending_signal_ids:
            queue.put_nowait(
                EngineEvent(
                    "attribute",
                    "output_delta",
                    text_delta=text["attribute_pending"].format(ids=", ".join(outcome.pending_signal_ids)),
                ),
            )
        root_causes, suggestions, dropped = _translate_attribution(nodes, runtime, diagnosis.signals)
        if dropped:
            queue.put_nowait(
                EngineEvent(
                    "attribute",
                    "output_delta",
                    text_delta=text["dropped_anchors"].format(count=dropped),
                ),
            )
        result = EngineResult(
            headline=headline,
            artifact_eval=ArtifactEval(applied=False, skipped_reason="not_produced"),
            root_causes=root_causes,
            suggestions=suggestions,
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
        attributed = len(runtime.attributions)
        invalid = len(runtime.invalid_signals)
        if runtime.artifact_improvement:
            summary = text["attribute_summary"].format(attributed=attributed, invalid=invalid)
        else:
            summary = text["attribute_summary_no_fix"].format(attributed=attributed, invalid=invalid)
        queue.put_nowait(EngineEvent("attribute", "stage_finished", status=status, summary=summary))
        return result


__all__ = ["AttributionCoreEngine", "ModelFactory", "WorkspaceResolver"]
