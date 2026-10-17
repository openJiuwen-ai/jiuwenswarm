# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Readable progress text the engine streams to the diagnosis panel.

Each function returns text for one ``output_delta`` event; lines end with a
newline so consecutive chunks stay on separate lines in the panel.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from jiuwenswarm.diagnosis.llm_engine.parsing import Attribution
from jiuwenswarm.diagnosis.models import DiagnosisSuggestions
from jiuwenswarm.diagnosis.spans import RuleFinding

Locale = Literal["zh", "en"]

_MAX_ALERT_LINES = 8
_MAX_PROBLEM_LINES = 3

_TEXTS: dict[str, dict[str, str]] = {
    "zh": {
        "evidence": "范围内共 {nodes} 个步骤，规则扫描 {alerts} 条告警",
        "alert_more": "  …… 另有 {count} 条告警未列出",
        "calling_attribution": "正在调用模型分析 {nodes} 个步骤……",
        "calling_suggestions": "正在根据 {count} 个根因生成优化建议……",
        "no_suggestions": "没有需要优化的问题，跳过建议生成",
        "retry": "模型回复有 {count} 处问题，已反馈给模型重新生成（第 {attempt}/{total} 次）：",
        "outcome": "任务结果：{outcome}",
        "outcome_success": "已完成",
        "outcome_partial": "部分完成",
        "outcome_fail": "未完成",
        "outcome_unclear": "无法判断",
        "artifact": "产物评估：{verdict}（得分 {score}）{summary}",
        "artifact_none": "产物评估：未执行",
        "verdict_pass": "通过",
        "verdict_partial": "部分满足",
        "verdict_fail": "未通过",
        "issue": "  - 问题：{description}{steps}",
        "root_cause": "根因 {number}【{category}】{summary}",
        "root_cause_detail": "  步骤：{steps}；置信度 {confidence}%",
        "evidence_line": "  依据：{evidence}",
        "no_root_cause": "未发现需要处理的根因",
        "steps": "（步骤 {steps}）",
        "artifact_fix": "产物修复请求：{instruction}",
        "skill": "Skill 建议「{name}」：{change}",
        "ttse": "TTSE 触发信号（{outcome}，主体 {subject}）：{reason}",
        "ttse_fail": "失败",
        "ttse_partial": "部分完成",
        "cat_skill_defect": "Skill 缺陷",
        "cat_ttse_gap": "经验缺口",
        "cat_tool_or_env": "工具或环境",
        "cat_model_capability": "模型能力",
        "cat_requirement_unclear": "需求不明确",
        "cat_other": "其他",
    },
    "en": {
        "evidence": "{nodes} steps in scope, {alerts} rule alerts",
        "alert_more": "  ... {count} more alerts not listed",
        "calling_attribution": "Asking the model to analyse {nodes} steps...",
        "calling_suggestions": "Drafting optimization suggestions for {count} root causes...",
        "no_suggestions": "Nothing to optimize; skipping suggestions",
        "retry": "The model reply had {count} problems; asking it again (attempt {attempt}/{total}):",
        "outcome": "Task outcome: {outcome}",
        "outcome_success": "completed",
        "outcome_partial": "partly completed",
        "outcome_fail": "not completed",
        "outcome_unclear": "unclear",
        "artifact": "Artifact evaluation: {verdict} (score {score}) {summary}",
        "artifact_none": "Artifact evaluation: not performed",
        "verdict_pass": "pass",
        "verdict_partial": "partial",
        "verdict_fail": "fail",
        "issue": "  - Issue: {description}{steps}",
        "root_cause": "Root cause {number} [{category}] {summary}",
        "root_cause_detail": "  Steps: {steps}; confidence {confidence}%",
        "evidence_line": "  Evidence: {evidence}",
        "no_root_cause": "No root cause needs attention",
        "steps": " (steps {steps})",
        "artifact_fix": "Artifact fix request: {instruction}",
        "skill": "Skill suggestion '{name}': {change}",
        "ttse": "TTSE trigger signal ({outcome}, subject {subject}): {reason}",
        "ttse_fail": "fail",
        "ttse_partial": "partial",
        "cat_skill_defect": "Skill defect",
        "cat_ttse_gap": "Experience gap",
        "cat_tool_or_env": "Tool or environment",
        "cat_model_capability": "Model capability",
        "cat_requirement_unclear": "Unclear requirement",
        "cat_other": "Other",
    },
}


def _clip(text: str, limit: int = 300) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "..."


def evidence_text(node_count: int, findings: Sequence[RuleFinding], locale: Locale) -> str:
    """Describe the scope and list the rule alerts."""
    text = _TEXTS[locale]
    lines = [text["evidence"].format(nodes=node_count, alerts=len(findings))]
    lines.extend(f"  - [{item.ref.node_id}] {_clip(item.message, 200)}" for item in findings[:_MAX_ALERT_LINES])
    if len(findings) > _MAX_ALERT_LINES:
        lines.append(text["alert_more"].format(count=len(findings) - _MAX_ALERT_LINES))
    return "\n".join(lines) + "\n"


def calling_attribution_text(node_count: int, locale: Locale) -> str:
    """Announce the attribution model call."""
    return _TEXTS[locale]["calling_attribution"].format(nodes=node_count) + "\n"


def calling_suggestions_text(root_cause_count: int, locale: Locale) -> str:
    """Announce the suggestion model call."""
    return _TEXTS[locale]["calling_suggestions"].format(count=root_cause_count) + "\n"


def skipped_suggestions_text(locale: Locale) -> str:
    """Explain why the suggestion call is skipped."""
    return _TEXTS[locale]["no_suggestions"] + "\n"


def retry_text(problems: Sequence[str], attempt: int, total: int, locale: Locale) -> str:
    """Tell the user a model reply was sent back for correction."""
    text = _TEXTS[locale]
    lines = [text["retry"].format(count=len(problems), attempt=attempt, total=total)]
    lines.extend(f"  - {_clip(item, 160)}" for item in problems[:_MAX_PROBLEM_LINES])
    return "\n".join(lines) + "\n"


def _steps(refs_node_ids: Sequence[str | None], locale: Locale) -> str:
    ids = [item for item in refs_node_ids if item]
    return _TEXTS[locale]["steps"].format(steps=", ".join(ids)) if ids else ""


def attribution_text(attribution: Attribution, locale: Locale) -> str:
    """Describe the attribution result."""
    text = _TEXTS[locale]
    lines = [text["outcome"].format(outcome=text[f"outcome_{attribution.task_outcome}"])]
    evaluation = attribution.artifact_eval
    if evaluation.applied and evaluation.verdict is not None:
        score = "-" if evaluation.score is None else f"{evaluation.score:.2f}"
        summary = _clip(evaluation.summary or "")
        lines.append(
            text["artifact"].format(verdict=text[f"verdict_{evaluation.verdict}"], score=score, summary=summary).rstrip(),
        )
        lines.extend(
            text["issue"].format(
                description=_clip(issue.description, 200),
                steps=_steps([ref.node_id for ref in issue.refs], locale),
            )
            for issue in evaluation.issues
        )
    else:
        lines.append(text["artifact_none"])
    if not attribution.root_causes:
        lines.append(text["no_root_cause"])
    for number, cause in enumerate(attribution.root_causes, start=1):
        lines.append(
            text["root_cause"].format(
                number=number,
                category=text[f"cat_{cause.category.value}"],
                summary=_clip(cause.summary, 300),
            ),
        )
        lines.append(
            text["root_cause_detail"].format(
                steps=", ".join(ref.node_id or "" for ref in cause.refs),
                confidence=round(cause.confidence * 100),
            ),
        )
        if cause.evidence:
            lines.append(text["evidence_line"].format(evidence=_clip(cause.evidence)))
    return "\n".join(lines) + "\n"


def suggestions_text(suggestions: DiagnosisSuggestions, locale: Locale) -> str:
    """Describe the suggestions."""
    text = _TEXTS[locale]
    lines: list[str] = []
    if suggestions.artifact_fix is not None:
        lines.append(text["artifact_fix"].format(instruction=_clip(suggestions.artifact_fix.instruction)))
    lines.extend(
        text["skill"].format(name=item.skill_name, change=_clip(item.change_summary))
        for item in suggestions.skills
    )
    lines.extend(
        text["ttse"].format(
            outcome=text[f"ttse_{item.outcome.value}"],
            subject=item.subject_id,
            reason=_clip(item.reason),
        )
        for item in suggestions.ttse
    )
    return "\n".join(lines) + "\n" if lines else ""


__all__ = [
    "attribution_text",
    "calling_attribution_text",
    "calling_suggestions_text",
    "evidence_text",
    "retry_text",
    "skipped_suggestions_text",
    "suggestions_text",
]
