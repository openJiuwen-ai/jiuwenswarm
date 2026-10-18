# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic diagnosis engine for tests that must not call a model."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from jiuwenswarm.diagnosis.engine import EmitFn, EngineError, EngineEvent, EngineInputs, EngineResult
from jiuwenswarm.diagnosis.models import (
    ArtifactEval,
    ArtifactFix,
    DiagnosisSuggestions,
    ModeProfile,
    RootCause,
    RootCauseCategory,
    SkillSuggestion,
    StepRef,
)
from jiuwenswarm.diagnosis.spans import RuleFinding, SpanFact, scan_rules, spans_from_record

_TEXTS: dict[str, dict[str, str]] = {
    "zh": {
        "stage_evidence": "收集证据",
        "stage_attribution": "归因分析",
        "stage_suggestions": "生成建议",
        "evidence_summary": "范围内共 {spans} 个步骤，规则扫描 {alerts} 条告警",
        "attribution_error": "定位到失败步骤：{message}",
        "attribution_none": "范围内没有显式失败，以最后一步为关注点",
        "root_cause_none": "未发现显式失败，建议人工核对最后一步的输出是否满足需求",
        "headline_error": "发现 {count} 处执行异常",
        "headline_none": "未发现显式失败",
        "suggestions_summary": "生成 {count} 条优化建议",
        "artifact_fix": "请回顾上一轮执行中出现的问题（{summary}），修正后重新交付结果。",
        "skill_change": "补充对该类失败的处理说明",
        "skill_message": "请检查并优化与该问题相关的 Skill：{summary}",
        "ttse_reason": "这段执行出现了问题（{summary}），值得沉淀为经验",
        "stub_skip": "占位引擎不评估产物",
        "empty_scope": "选区内没有可分析的步骤",
    },
    "en": {
        "stage_evidence": "Collect evidence",
        "stage_attribution": "Attribute causes",
        "stage_suggestions": "Draft suggestions",
        "evidence_summary": "{spans} steps in scope, {alerts} rule alerts",
        "attribution_error": "Located a failing step: {message}",
        "attribution_none": "No explicit failure in scope; focusing on the last step",
        "root_cause_none": "No explicit failure found; verify that the last step's output meets the request",
        "headline_error": "Found {count} execution errors",
        "headline_none": "No explicit failure found",
        "suggestions_summary": "Produced {count} suggestions",
        "artifact_fix": "Review the problem seen in the previous run ({summary}), fix it and deliver the result again.",
        "skill_change": "Add handling guidance for this kind of failure",
        "skill_message": "Please review and improve the Skill related to this problem: {summary}",
        "ttse_reason": "This stretch of the run went wrong ({summary}); worth learning an experience from",
        "stub_skip": "The stub engine does not evaluate artifacts",
        "empty_scope": "There are no steps to analyse in the selected scope",
    },
}


def _select_spans(inputs: EngineInputs) -> tuple[list[SpanFact], dict[str, str]]:
    """Return the spans inside the ranges plus a ``span key -> node_id`` map."""
    windows = {item.subject_id: item for item in inputs.ranges}
    selected: list[SpanFact] = []
    node_ids: dict[str, str] = {}
    for subject in inputs.snapshot.subjects:
        facts: list[SpanFact] = []
        for record in subject.records:
            envelope = {"trace_id": record.trace_id, "span_id": record.span_id, "lifecycle": "final"}
            facts.extend(spans_from_record(envelope, record.otlp, subject.subject_id))
        facts.sort(key=lambda fact: (fact.start_nano, fact.ref.span_id))
        window = windows.get(subject.subject_id)
        for index, fact in enumerate(facts):
            node_ids[f"{subject.subject_id}|{fact.ref.key}"] = f"{subject.subject_id}:{index}"
            if window is not None and window.start_nano <= fact.start_nano <= window.end_nano:
                selected.append(fact)
    selected.sort(key=lambda fact: (fact.start_nano, fact.ref.span_id))
    return selected, node_ids


def _anchor(ref: StepRef, node_ids: dict[str, str]) -> StepRef:
    node_id = node_ids.get(f"{ref.subject_id}|{ref.key}")
    return ref.model_copy(update={"node_id": node_id}) if node_id else ref


def _with_anchor(span: SpanFact, node_ids: dict[str, str]) -> SpanFact:
    return replace(span, ref=_anchor(span.ref, node_ids))


class StubDiagnosisEngine:
    """Deterministic engine that exercises the whole pipeline without a model.

    It reports three stages, attributes the first rule-scan alert (or the last
    step when there is none) and emits one suggestion per outlet that the mode
    profile supports.
    """

    def __init__(self, *, step_delay_s: float = 0.05) -> None:
        self._step_delay_s = max(0.0, step_delay_s)

    async def _pause(self) -> None:
        if self._step_delay_s > 0:
            await asyncio.sleep(self._step_delay_s)

    async def run(self, inputs: EngineInputs, emit: EmitFn) -> EngineResult:
        """Run the three stages and return their output."""
        text = _TEXTS[inputs.locale]
        spans, node_ids = _select_spans(inputs)
        if not spans:
            raise EngineError("TASK_INCOMPLETE", text["empty_scope"])
        findings = [
            RuleFinding(code=item.code, message=item.message, ref=_anchor(item.ref, node_ids))
            for item in scan_rules(spans)
        ]
        spans = [_with_anchor(span, node_ids) for span in spans]
        result = EngineResult()

        await emit(EngineEvent("evidence", "stage_started", title=text["stage_evidence"]))
        summary = text["evidence_summary"].format(spans=len(spans), alerts=len(findings))
        await emit(EngineEvent("evidence", "output_delta", text_delta=summary))
        await self._pause()
        result.artifact_eval = ArtifactEval(applied=False, skipped_reason=text["stub_skip"])
        await emit(EngineEvent("evidence", "stage_finished", status="completed", summary=summary))

        await emit(EngineEvent("attribution", "stage_started", title=text["stage_attribution"]))
        root_cause = self._attribute(findings, spans, text)
        result.root_causes.append(root_cause)
        if inputs.mode_profile is ModeProfile.AGENT_WORK:
            result.root_causes.append(self._experience(root_cause, text))
        message = (
            text["attribution_error"].format(message=root_cause.summary)
            if findings
            else text["attribution_none"]
        )
        await emit(EngineEvent("attribution", "output_delta", text_delta=message))
        await self._pause()
        await emit(EngineEvent("attribution", "stage_finished", status="completed", summary=message))
        result.headline = (
            text["headline_error"].format(count=len(findings))
            if findings
            else text["headline_none"]
        )

        await emit(EngineEvent("suggestions", "stage_started", title=text["stage_suggestions"]))
        result.suggestions = self._suggest(inputs, root_cause, text)
        count = (1 if result.suggestions.artifact_fix else 0) + len(result.suggestions.skills)
        done = text["suggestions_summary"].format(count=count)
        await emit(EngineEvent("suggestions", "output_delta", text_delta=done))
        await self._pause()
        await emit(EngineEvent("suggestions", "stage_finished", status="completed", summary=done))
        return result

    @staticmethod
    def _attribute(
        findings: list[RuleFinding],
        spans: list[SpanFact],
        text: dict[str, str],
    ) -> RootCause:
        if findings:
            finding = findings[0]
            return RootCause(
                id="rc-1",
                category=RootCauseCategory.TOOL_OR_ENV,
                subject_id=finding.ref.subject_id,
                refs=[finding.ref],
                summary=finding.message,
                evidence=finding.code,
                confidence=0.6,
            )
        last = spans[-1]
        return RootCause(
            id="rc-1",
            category=RootCauseCategory.OTHER,
            subject_id=last.ref.subject_id,
            refs=[last.ref],
            summary=text["root_cause_none"],
            evidence="",
            confidence=0.2,
        )

    @staticmethod
    def _suggest(
        inputs: EngineInputs,
        root_cause: RootCause,
        text: dict[str, str],
    ) -> DiagnosisSuggestions:
        summary = root_cause.summary
        suggestions = DiagnosisSuggestions(
            artifact_fix=ArtifactFix(
                instruction=text["artifact_fix"].format(summary=summary),
                root_cause_ids=[root_cause.id],
            ),
        )
        if inputs.mode_profile in {ModeProfile.AGENT_WORK, ModeProfile.TEAM}:
            suggestions.skills.append(
                SkillSuggestion(
                    id="sk-1",
                    skill_name="general",
                    change_summary=text["skill_change"],
                    rationale=summary,
                    message=text["skill_message"].format(summary=summary),
                    root_cause_ids=[root_cause.id],
                    confidence=0.3,
                ),
            )
        return suggestions

    @staticmethod
    def _experience(root_cause: RootCause, text: dict[str, str]) -> RootCause:
        failed = root_cause.category is RootCauseCategory.TOOL_OR_ENV
        return RootCause(
            id="tt-1",
            category=RootCauseCategory.TTSE_FAIL if failed else RootCauseCategory.TTSE_PARTIAL,
            subject_id=root_cause.subject_id,
            refs=list(root_cause.refs),
            summary=text["ttse_reason"].format(summary=root_cause.summary),
        )


__all__ = ["StubDiagnosisEngine"]
