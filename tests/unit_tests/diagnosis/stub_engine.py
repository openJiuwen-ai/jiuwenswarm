# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic diagnosis engine for tests that must not call a model."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from jiuwenswarm.diagnosis.engine import EmitFn, EngineError, EngineEvent, EngineInputs, EngineResult
from jiuwenswarm.diagnosis.models import (
    EvolutionJudge,
    ModeProfile,
    RewardFailureCause,
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
        "trajectory_summary_error": "本次执行出现 {count} 处执行异常，详见诊断发现。",
        "trajectory_summary_none": "本次执行未发现显式失败。",
        "suggestions_summary": "生成 {count} 条优化建议",
        "artifact_fix": "请回顾上一轮执行中出现的问题（{summary}），修正后重新交付结果。",
        "skill_change": "补充对该类失败的处理说明",
        "experience_tip": "遇到同类问题时先核对执行环境",
        "ttse_reason": "这段执行出现了问题（{summary}），值得沉淀为经验",
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
        "trajectory_summary_error": "This run showed {count} execution errors; see the findings.",
        "trajectory_summary_none": "This run showed no explicit failure.",
        "suggestions_summary": "Produced {count} suggestions",
        "artifact_fix": "Review the problem seen in the previous run ({summary}), fix it and deliver the result again.",
        "skill_change": "Add handling guidance for this kind of failure",
        "experience_tip": "Check the execution environment first when the same problem recurs",
        "ttse_reason": "This stretch of the run went wrong ({summary}); worth learning an experience from",
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

    It reports three stages, one deliverable failure cause (from the first
    rule-scan alert, or the last step), and the evolution items the mode
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
        await emit(EngineEvent("evidence", "stage_finished", status="completed", summary=summary))

        await emit(EngineEvent("attribution", "stage_started", title=text["stage_attribution"]))
        ref = findings[0].ref if findings else spans[-1].ref
        cause = self._cause(findings, spans, text)
        result.reward_failure_causes.append(cause)
        result.trajectory_summary = (
            text["trajectory_summary_error"].format(count=len(findings))
            if findings
            else text["trajectory_summary_none"]
        )
        result.reward = {
            "schema_version": "attribution.reward.v1",
            "overall": {"comment": "stub", "total_score": 80},
            "categories": [],
            "dimensions": [
                {"category": "stub", "requirement": "stub", "comment": "fix it", "score": 80},
            ],
        }
        await emit(EngineEvent("attribution", "reward_failure_causes", reward_failure_causes=[cause]))
        if inputs.mode_profile in {ModeProfile.AGENT_WORK, ModeProfile.TEAM}:
            result.evolution_judge.append(self._skill(ref, text))
        if inputs.mode_profile is ModeProfile.AGENT_WORK:
            result.evolution_judge.append(self._experience(ref, cause, text))
        if result.evolution_judge:
            await emit(EngineEvent("attribution", "evolution_judge", evolution_judge=list(result.evolution_judge)))
        message = (
            text["attribution_error"].format(message=cause.summary)
            if findings
            else text["attribution_none"]
        )
        await emit(EngineEvent("attribution", "output_delta", text_delta=message))
        await self._pause()
        await emit(EngineEvent("attribution", "stage_finished", status="completed", summary=message))

        await emit(EngineEvent("suggestions", "stage_started", title=text["stage_suggestions"]))
        done = text["suggestions_summary"].format(count=len(result.evolution_judge) + 1)
        await emit(EngineEvent("suggestions", "output_delta", text_delta=done))
        await self._pause()
        await emit(EngineEvent("suggestions", "stage_finished", status="completed", summary=done))
        return result

    @staticmethod
    def _cause(findings: list[RuleFinding], spans: list[SpanFact], text: dict[str, str]) -> RewardFailureCause:
        ref = findings[0].ref if findings else spans[-1].ref
        summary = findings[0].message if findings else text["root_cause_none"]
        return RewardFailureCause(
            id="rfc-1",
            dimension="stub",
            summary=summary,
            evidence=[ref],
            suggestion=[text["artifact_fix"].format(summary=summary)],
        )

    @staticmethod
    def _skill(ref: StepRef, text: dict[str, str]) -> EvolutionJudge:
        return EvolutionJudge(
            id="evo-1",
            category="skill",
            summary=text["skill_change"],
            evidence=[ref],
            suggestion=[text["skill_change"]],
            score=0.3,
            target="general",
        )

    @staticmethod
    def _experience(ref: StepRef, cause: RewardFailureCause, text: dict[str, str]) -> EvolutionJudge:
        return EvolutionJudge(
            id="evo-2",
            category="experience",
            summary=text["ttse_reason"].format(summary=cause.summary),
            evidence=[ref],
            suggestion=[text["experience_tip"]],
            score=0.5,
            outcome="partial",
        )


__all__ = ["StubDiagnosisEngine"]
