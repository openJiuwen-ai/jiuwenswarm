# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Diagnosis engine contract and its simple LLM implementation.

The engine reads the trajectory snapshot and per-subject ranges that the host
passes in; it never touches the trajectory store, the Skill library or the TTSE
bank. It reports progress through an ``emit`` callback and returns structured
results; the host (``service.py``) filters those results against the scope and
mode profile before persisting. Failures that the engine can name are raised as
:class:`EngineError`.

This file has two parts. The contract (:class:`DiagnosisEngine` and the
dataclasses around it) is what the host depends on. :class:`LlmDiagnosisEngine`
is the simple implementation the host uses until the formal engine is plugged
in: re-implement ``LlmDiagnosisEngine.run`` (or return another engine from
``build_diagnosis_engine`` in ``server/diagnosis/host.py``) and keep the
contract unchanged. Its helpers live in ``jiuwenswarm/diagnosis/llm_engine/``
and can be deleted together with it.

``LlmDiagnosisEngine`` calls the session's own model twice instead of running
an agent loop:

1. ``evidence``: number the steps in scope, render the transcript and run the
   deterministic rule scan. No model call.
2. ``attribution``: one model call returns the task outcome, the artifact
   evaluation, root causes and the stretches to look at.
3. ``suggestions``: one model call turns the root causes into an
   artifact-fix request, Skill suggestions and TTSE signals. It is skipped when
   the run needs no follow-up.

Replies are checked against the transcript (known step ids, mode-allowed
categories, Skill names seen in the run). A reply with problems is sent back
once for correction; the usable remainder is kept when the correction fails.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from openjiuwen.core.foundation.llm.schema.message import SystemMessage, UserMessage

from jiuwenswarm.diagnosis.llm_engine.nodes import Node, render_transcript, select_nodes
from jiuwenswarm.diagnosis.llm_engine.parsing import (
    Attribution,
    extract_json_object,
    parse_attribution,
    parse_suggestions,
)
from jiuwenswarm.diagnosis.llm_engine.prompts import (
    ATTRIBUTION_SYSTEM,
    SUGGESTION_SYSTEM,
    Outlets,
    build_attribution_prompt,
    build_suggestion_prompt,
    retry_feedback,
)
from jiuwenswarm.diagnosis.llm_engine.progress import (
    attribution_text,
    calling_attribution_text,
    calling_suggestions_text,
    evidence_text,
    retry_text,
    skipped_suggestions_text,
    suggestions_text,
)
from jiuwenswarm.diagnosis.model_resolver import ResolvedDiagnosisModel
from jiuwenswarm.diagnosis.models import (
    ALLOWED_ROOT_CAUSE_CATEGORIES,
    ArtifactEval,
    DiagnosisError,
    DiagnosisSuggestions,
    FocusRange,
    ModeProfile,
    RootCause,
)
from jiuwenswarm.diagnosis.snapshot import SubjectRange, TrajectorySnapshot
from jiuwenswarm.diagnosis.spans import RuleFinding, scan_rules

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

EngineEventKind = Literal["stage_started", "output_delta", "stage_finished"]


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """One progress notification produced by an engine."""

    stage_id: str
    kind: EngineEventKind
    title: str | None = None
    text_delta: str | None = None
    status: Literal["completed", "failed", "skipped"] | None = None
    summary: str | None = None
    skip_reason: str | None = None


EmitFn = Callable[[EngineEvent], Awaitable[None]]


class EngineError(Exception):
    """Failure an engine can name; the host stores it as the diagnosis error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class EngineInputs:
    """Everything an engine may read.

    Attributes:
        session_id: Session being diagnosed.
        mode_profile: Mode tier deciding which outputs are meaningful.
        locale: Language of user-facing text.
        user_note: Optional note from the caller.
        snapshot: Finished records of every subject of the session.
        ranges: At most one closed time range per diagnosed subject.
        model: Resolved model the engine should call.
    """

    session_id: str
    mode_profile: ModeProfile
    locale: Literal["zh", "en"]
    user_note: str | None
    snapshot: TrajectorySnapshot
    ranges: list[SubjectRange]
    model: ResolvedDiagnosisModel


@dataclass(slots=True)
class EngineResult:
    """Structured engine output, before host-side filtering.

    ``error`` carries a terminal failure the engine wants the host to keep
    alongside the partial results (for example an attribution that ran out of
    budget after reporting some causes): the host stores it on the record and
    marks the failed stage, but keeps the record completed.
    """

    headline: str | None = None
    artifact_eval: ArtifactEval | None = None
    focus_ranges: list[FocusRange] = field(default_factory=list)
    root_causes: list[RootCause] = field(default_factory=list)
    suggestions: DiagnosisSuggestions = field(default_factory=DiagnosisSuggestions)
    error: DiagnosisError | None = None


class DiagnosisEngine(Protocol):
    """Contract between the host and any diagnosis engine."""

    async def run(self, inputs: EngineInputs, emit: EmitFn) -> EngineResult:
        """Analyse the scope and return structured results.

        Args:
            inputs: Trajectory snapshot, per-subject ranges, resolved model and
                the caller's note. Node building, artifact evaluation and
                model calls stay inside the engine.
            emit: Progress callback; must be awaited in order.

        Returns:
            Structured results that the host filters and persists.

        Raises:
            EngineError: When the engine cannot produce a diagnosis for a
                reason it can name (for example ``TASK_INCOMPLETE``).
        """
        ...


#: Sampling temperature; diagnosis should be as repeatable as a model allows.
_TEMPERATURE = 0.1

#: Builds the model client for a resolved selection; replaced in tests.
ModelFactory = Callable[[ResolvedDiagnosisModel], Any]

_TEXTS: dict[str, dict[str, str]] = {
    "zh": {
        "stage_evidence": "收集证据",
        "stage_attribution": "归因分析",
        "stage_suggestions": "生成建议",
        "evidence_summary": "范围内共 {nodes} 个步骤，规则扫描 {alerts} 条告警",
        "empty_scope": "选区内没有可分析的步骤",
        "attribution_summary": "定位到 {count} 个根因",
        "attribution_none": "未发现需要处理的问题",
        "suggestions_summary": "生成 {count} 条优化建议",
        "suggestions_none": "无需优化建议",
        "model_failed": "诊断模型调用失败：{reason}",
        "model_timeout": "诊断模型调用超时",
        "model_invalid": "诊断模型的输出无法解析：{reason}",
        "outcome_success": "任务已完成",
        "outcome_partial": "任务部分完成",
        "outcome_fail": "任务未完成",
        "outcome_unclear": "无法判断任务是否完成",
    },
    "en": {
        "stage_evidence": "Collect evidence",
        "stage_attribution": "Attribute causes",
        "stage_suggestions": "Draft suggestions",
        "evidence_summary": "{nodes} steps in scope, {alerts} rule alerts",
        "empty_scope": "There are no steps to analyse in the selection",
        "attribution_summary": "Found {count} root causes",
        "attribution_none": "Nothing needs attention",
        "suggestions_summary": "Produced {count} suggestions",
        "suggestions_none": "No suggestions needed",
        "model_failed": "Diagnosis model call failed: {reason}",
        "model_timeout": "Diagnosis model call timed out",
        "model_invalid": "The diagnosis model's output could not be parsed: {reason}",
        "outcome_success": "Task completed",
        "outcome_partial": "Task partly completed",
        "outcome_fail": "Task not completed",
        "outcome_unclear": "Cannot tell whether the task completed",
    },
}


def _content_text(value: Any) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _default_model_factory(model: ResolvedDiagnosisModel) -> Any:
    return model.build()


class LlmDiagnosisEngine:
    """Two-call diagnosis engine that reuses the diagnosed session's model.

    Args:
        model_factory: Builds the model client from the resolved selection.
            Defaults to ``ResolvedDiagnosisModel.build``.
        max_attempts: Calls per stage, counting the first one; a reply with
            problems is sent back for correction until this limit.
        call_timeout_s: Timeout of one model call, in seconds.
    """

    def __init__(
        self,
        *,
        model_factory: ModelFactory | None = None,
        max_attempts: int = 2,
        call_timeout_s: float = 180.0,
    ) -> None:
        self._model_factory = model_factory or _default_model_factory
        self._max_attempts = max(1, max_attempts)
        self._call_timeout_s = call_timeout_s

    async def run(self, inputs: EngineInputs, emit: EmitFn) -> EngineResult:
        """Run the three stages and return the structured result.

        Args:
            inputs: Snapshot, ranges, resolved model and caller note.
            emit: Progress callback.

        Returns:
            Results that the host filters and persists.

        Raises:
            EngineError: ``TASK_INCOMPLETE`` when no step is in scope,
                ``MODEL_CALL_FAILED`` or ``MODEL_OUTPUT_INVALID`` when the
                model cannot deliver a usable reply.
        """
        text = _TEXTS[inputs.locale]
        nodes = select_nodes(inputs.snapshot, inputs.ranges)
        if not nodes:
            raise EngineError("TASK_INCOMPLETE", text["empty_scope"])
        by_id = {node.node_id: node for node in nodes}
        transcript = render_transcript(nodes)
        findings = _anchored_findings(nodes)

        await emit(EngineEvent("evidence", "stage_started", title=text["stage_evidence"]))
        summary = text["evidence_summary"].format(nodes=len(nodes), alerts=len(findings))
        await emit(
            EngineEvent("evidence", "output_delta", text_delta=evidence_text(len(nodes), findings, inputs.locale)),
        )
        await emit(EngineEvent("evidence", "stage_finished", status="completed", summary=summary))

        model = self._model_factory(inputs.model)
        allowed = ALLOWED_ROOT_CAUSE_CATEGORIES[inputs.mode_profile]
        outlets = Outlets.for_profile(inputs.mode_profile)

        await emit(EngineEvent("attribution", "stage_started", title=text["stage_attribution"]))
        prompt = build_attribution_prompt(
            transcript=transcript,
            findings=findings,
            user_note=inputs.user_note,
            categories=allowed,
            locale=inputs.locale,
        )

        async def attribution_note(note: str) -> None:
            await emit(EngineEvent("attribution", "output_delta", text_delta=note))

        await attribution_note(calling_attribution_text(len(nodes), inputs.locale))
        attribution = await self._ask(
            model,
            ATTRIBUTION_SYSTEM,
            prompt,
            lambda data: parse_attribution(data, by_id, allowed),
            text,
            (attribution_note, inputs.locale),
        )
        await attribution_note(attribution_text(attribution, inputs.locale))
        await emit(
            EngineEvent(
                "attribution",
                "stage_finished",
                status="completed",
                summary=_attribution_summary(attribution, text),
            ),
        )

        result = EngineResult(
            headline=attribution.headline or text[f"outcome_{attribution.task_outcome}"],
            artifact_eval=attribution.artifact_eval,
            focus_ranges=attribution.focus_ranges,
            root_causes=attribution.root_causes,
        )

        await emit(EngineEvent("suggestions", "stage_started", title=text["stage_suggestions"]))

        async def suggestions_note(note: str) -> None:
            await emit(EngineEvent("suggestions", "output_delta", text_delta=note))

        if not _needs_suggestions(attribution):
            await suggestions_note(skipped_suggestions_text(inputs.locale))
        else:
            await suggestions_note(calling_suggestions_text(len(attribution.root_causes), inputs.locale))
            suggest_prompt = build_suggestion_prompt(
                transcript=transcript,
                root_causes=attribution.root_causes,
                outlets=outlets,
                user_note=inputs.user_note,
                locale=inputs.locale,
            )
            known = frozenset(item.id for item in attribution.root_causes)
            result.suggestions = await self._ask(
                model,
                SUGGESTION_SYSTEM,
                suggest_prompt,
                lambda data: parse_suggestions(data, by_id, known, outlets, transcript),
                text,
                (suggestions_note, inputs.locale),
            )
            await suggestions_note(suggestions_text(result.suggestions, inputs.locale))
        count = _suggestion_count(result.suggestions)
        done = text["suggestions_summary"].format(count=count) if count else text["suggestions_none"]
        await emit(EngineEvent("suggestions", "stage_finished", status="completed", summary=done))
        return result

    async def _ask(
        self,
        model: Any,
        system: str,
        prompt: str,
        parse: Callable[[dict[str, Any]], tuple[_T, list[str]]],
        text: dict[str, str],
        progress: tuple[Callable[[str], Awaitable[None]], Literal["zh", "en"]],
    ) -> _T:
        """Call the model until a reply parses without problems.

        The best parsed reply wins; a reply that cannot be parsed at all counts
        as having no result. Each correction round is reported through
        ``progress`` (a callback and the locale of its text).
        """
        note, locale = progress
        best: _T | None = None
        best_problems = 0
        current = prompt
        last_error = ""
        for attempt in range(1, self._max_attempts + 1):
            reply = await self._invoke(model, system, current, text)
            try:
                parsed, problems = parse(extract_json_object(reply))
            except ValueError as exc:
                last_error = str(exc)
                problems = [last_error]
                parsed = None
            if parsed is not None and (best is None or len(problems) < best_problems):
                best, best_problems = parsed, len(problems)
            if parsed is not None and not problems:
                return parsed
            logger.info("[diagnosis] reply rejected: attempt=%d problems=%s", attempt, problems[:5])
            if attempt < self._max_attempts:
                await note(retry_text(problems, attempt + 1, self._max_attempts, locale))
            current = prompt + retry_feedback(problems, reply)
        if best is None:
            raise EngineError("MODEL_OUTPUT_INVALID", text["model_invalid"].format(reason=last_error))
        return best

    async def _invoke(self, model: Any, system: str, prompt: str, text: dict[str, str]) -> str:
        messages = [SystemMessage(content=system), UserMessage(content=prompt)]
        try:
            response = await asyncio.wait_for(
                model.invoke(messages, temperature=_TEMPERATURE),
                timeout=self._call_timeout_s,
            )
        except TimeoutError as exc:
            raise EngineError("MODEL_CALL_FAILED", text["model_timeout"]) from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("[diagnosis] model call failed: %s", type(exc).__name__)
            raise EngineError(
                "MODEL_CALL_FAILED",
                text["model_failed"].format(reason=str(exc) or type(exc).__name__),
            ) from exc
        return _content_text(response)


def _anchored_findings(nodes: list[Node]) -> list[RuleFinding]:
    """Run the rule scan and attach node ids to its findings."""
    by_key = {node.fact.ref.key: node for node in nodes}
    findings: list[RuleFinding] = []
    for item in scan_rules([node.fact for node in nodes]):
        node = by_key.get(item.ref.key)
        if node is not None:
            findings.append(RuleFinding(code=item.code, message=item.message, ref=node.ref))
    return findings


def _needs_suggestions(attribution: Attribution) -> bool:
    if attribution.root_causes:
        return True
    return attribution.artifact_eval.applied and attribution.artifact_eval.verdict != "pass"


def _suggestion_count(suggestions: DiagnosisSuggestions) -> int:
    return (1 if suggestions.artifact_fix else 0) + len(suggestions.skills) + len(suggestions.ttse)


def _attribution_summary(attribution: Attribution, text: dict[str, str]) -> str:
    if not attribution.root_causes:
        return text["attribution_none"]
    return text["attribution_summary"].format(count=len(attribution.root_causes))

__all__ = [
    "DiagnosisEngine",
    "EmitFn",
    "EngineError",
    "EngineEvent",
    "EngineInputs",
    "EngineResult",
    "LlmDiagnosisEngine",
    "ModelFactory",
]
