# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn the model's JSON replies into diagnosis results.

Parsers never raise on content problems. They keep every usable item, drop the
rest and return a list of problems, which the engine feeds back to the model
for one corrected reply. The host filters the final result again, so a step id
that slips through here is still rejected there.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.diagnosis.llm_engine.nodes import Node
from jiuwenswarm.diagnosis.llm_engine.prompts import MAX_ROOT_CAUSES, Outlets
from jiuwenswarm.diagnosis.models import (
    ArtifactEval,
    ArtifactFix,
    ArtifactIssue,
    DiagnosisSuggestions,
    FocusRange,
    RootCause,
    RootCauseCategory,
    SkillSuggestion,
    StepRef,
    TtseSignal,
    TtseSignalOutcome,
)

_TASK_OUTCOMES = frozenset({"success", "partial", "fail", "unclear"})
_VERDICTS = frozenset({"pass", "partial", "fail"})
_MAX_ITEMS = 8
_MAX_TEXT_CHARS = 2000


@dataclass(slots=True)
class Attribution:
    """Parsed attribution reply."""

    task_outcome: str = "unclear"
    headline: str | None = None
    artifact_eval: ArtifactEval = field(default_factory=ArtifactEval)
    root_causes: list[RootCause] = field(default_factory=list)
    focus_ranges: list[FocusRange] = field(default_factory=list)


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the JSON object embedded in a model reply.

    Args:
        text: Raw reply, possibly wrapped in prose or a code fence.

    Returns:
        The decoded object.

    Raises:
        ValueError: When the reply holds no JSON object.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("the reply contains no JSON object")
    try:
        value = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ValueError(f"the reply is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("the reply must be a JSON object")
    return value


def _items(value: Any) -> list[Any]:
    return value[:_MAX_ITEMS] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value).strip()[:_MAX_TEXT_CHARS] if isinstance(value, str | int | float) else ""


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _refs(raw: Any, nodes: Mapping[str, Node], where: str, problems: list[str]) -> list[StepRef]:
    refs: list[StepRef] = []
    seen: set[str] = set()
    for item in _items(raw):
        step_id = _text(item)
        node = nodes.get(step_id)
        if node is None:
            problems.append(f"{where}: step id {step_id!r} is not in the transcript")
            continue
        if step_id not in seen:
            seen.add(step_id)
            refs.append(node.ref)
    return refs


def _window(nodes: list[Node]) -> tuple[int, int]:
    start = min(node.fact.start_nano for node in nodes)
    end = max(max(node.fact.end_nano, node.fact.start_nano) for node in nodes)
    return start, end


def _focus(refs: list[StepRef], nodes: Mapping[str, Node], reason: str) -> FocusRange | None:
    if not refs:
        return None
    subject_id = refs[0].subject_id
    same = [ref for ref in refs if ref.subject_id == subject_id]
    start, end = _window([nodes[ref.node_id] for ref in same if ref.node_id in nodes])
    return FocusRange(
        subject_id=subject_id,
        refs=same,
        start_unix_nano=str(start),
        end_unix_nano=str(end),
        reason=reason or None,
    )


def _artifact_eval(raw: Any, nodes: Mapping[str, Node], problems: list[str]) -> ArtifactEval:
    if not isinstance(raw, Mapping):
        return ArtifactEval(applied=False)
    verdict = _text(raw.get("verdict")).lower()
    if verdict not in _VERDICTS:
        return ArtifactEval(applied=False)
    issues: list[ArtifactIssue] = []
    for index, item in enumerate(_items(raw.get("issues"))):
        if not isinstance(item, Mapping):
            continue
        description = _text(item.get("description"))
        if description:
            issues.append(
                ArtifactIssue(
                    description=description,
                    refs=_refs(item.get("step_ids"), nodes, f"artifact_eval.issues[{index}]", problems),
                ),
            )
    return ArtifactEval(
        applied=True,
        verdict=verdict,  # type: ignore[arg-type]
        score=_number(raw.get("score"), 1.0 if verdict == "pass" else 0.5),
        summary=_text(raw.get("summary")) or None,
        issues=issues,
    )


def parse_attribution(
    data: Mapping[str, Any],
    nodes: Mapping[str, Node],
    allowed: frozenset[RootCauseCategory],
) -> tuple[Attribution, list[str]]:
    """Parse the attribution reply.

    Args:
        data: Decoded reply object.
        nodes: Citable nodes by id.
        allowed: Categories the mode profile allows.

    Returns:
        The usable attribution and the problems found in the reply.
    """
    problems: list[str] = []
    outcome = _text(data.get("task_outcome")).lower()
    result = Attribution(
        task_outcome=outcome if outcome in _TASK_OUTCOMES else "unclear",
        headline=_text(data.get("headline")) or None,
        artifact_eval=_artifact_eval(data.get("artifact_eval"), nodes, problems),
    )
    for index, item in enumerate(_items(data.get("root_causes"))[:MAX_ROOT_CAUSES]):
        if not isinstance(item, Mapping):
            continue
        where = f"root_causes[{index}]"
        refs = _refs(item.get("step_ids"), nodes, where, problems)
        summary = _text(item.get("summary"))
        if not refs or not summary:
            problems.append(f"{where}: needs a summary and at least one known step id")
            continue
        try:
            category = RootCauseCategory(_text(item.get("category")).lower())
        except ValueError:
            category = RootCauseCategory.OTHER
        # A category the mode does not report is downgraded instead of retried.
        if category not in allowed:
            category = RootCauseCategory.OTHER
        result.root_causes.append(
            RootCause(
                id=f"rc-{len(result.root_causes) + 1}",
                category=category,
                subject_id=refs[0].subject_id,
                refs=refs,
                summary=summary,
                evidence=_text(item.get("evidence")),
                confidence=_number(item.get("confidence"), 0.5),
            ),
        )
    for index, item in enumerate(_items(data.get("focus"))):
        if not isinstance(item, Mapping):
            continue
        focus = _focus(
            _refs(item.get("step_ids"), nodes, f"focus[{index}]", problems),
            nodes,
            _text(item.get("reason")),
        )
        if focus is not None:
            result.focus_ranges.append(focus)
    if not result.focus_ranges:
        for cause in result.root_causes:
            focus = _focus(cause.refs, nodes, cause.summary)
            if focus is not None:
                result.focus_ranges.append(focus)
    return result, problems


def _cause_ids(raw: Any, known: frozenset[str], where: str, problems: list[str]) -> list[str]:
    ids = [item for item in (_text(value) for value in _items(raw)) if item in known]
    if not ids:
        problems.append(f"{where}: root_cause_ids must name one of {sorted(known)}")
    return ids


def parse_suggestions(
    data: Mapping[str, Any],
    nodes: Mapping[str, Node],
    root_cause_ids: frozenset[str],
    outlets: Outlets,
    transcript: str,
) -> tuple[DiagnosisSuggestions, list[str]]:
    """Parse the suggestion reply.

    Args:
        data: Decoded reply object.
        nodes: Citable nodes by id.
        root_cause_ids: Ids of the validated root causes.
        outlets: Outlets the mode profile supports; others are ignored.
        transcript: Transcript the model saw, used to check Skill names.

    Returns:
        The usable suggestions and the problems found in the reply.
    """
    problems: list[str] = []
    suggestions = DiagnosisSuggestions()
    lowered = transcript.lower()

    fix = data.get("artifact_fix")
    if isinstance(fix, Mapping) and _text(fix.get("instruction")):
        ids = [item for item in (_text(v) for v in _items(fix.get("root_cause_ids"))) if item in root_cause_ids]
        suggestions.artifact_fix = ArtifactFix(
            instruction=_text(fix.get("instruction")),
            root_cause_ids=ids or sorted(root_cause_ids),
        )

    if outlets.skills:
        for index, item in enumerate(_items(data.get("skills"))):
            if not isinstance(item, Mapping):
                continue
            where = f"skills[{index}]"
            name = _text(item.get("skill_name"))
            message = _text(item.get("message"))
            ids = _cause_ids(item.get("root_cause_ids"), root_cause_ids, where, problems)
            if not name or name.lower() not in lowered:
                problems.append(f"{where}: skill_name must be a Skill that appears in the transcript")
                continue
            if not message or not ids:
                problems.append(f"{where}: needs a message and root_cause_ids")
                continue
            suggestions.skills.append(
                SkillSuggestion(
                    id=f"sk-{len(suggestions.skills) + 1}",
                    skill_name=name,
                    change_summary=_text(item.get("change_summary")),
                    rationale=_text(item.get("rationale")),
                    message=message,
                    root_cause_ids=ids,
                    confidence=_number(item.get("confidence"), 0.5),
                ),
            )

    if outlets.ttse:
        for index, item in enumerate(_items(data.get("ttse"))):
            if not isinstance(item, Mapping):
                continue
            signal = _ttse_signal(item, nodes, root_cause_ids, len(suggestions.ttse) + 1, f"ttse[{index}]", problems)
            if signal is not None:
                suggestions.ttse.append(signal)
    return suggestions, problems


def _ttse_signal(
    item: Mapping[str, Any],
    nodes: Mapping[str, Node],
    root_cause_ids: frozenset[str],
    number: int,
    where: str,
    problems: list[str],
) -> TtseSignal | None:
    first = nodes.get(_text(item.get("start_step_id")))
    last = nodes.get(_text(item.get("end_step_id")))
    if first is None or last is None or first.ref.subject_id != last.ref.subject_id:
        problems.append(f"{where}: start_step_id and end_step_id must be step ids of the same subject")
        return None
    ids = _cause_ids(item.get("root_cause_ids"), root_cause_ids, where, problems)
    reason = _text(item.get("reason"))
    if not ids or not reason:
        problems.append(f"{where}: needs a reason and root_cause_ids")
        return None
    outcome = TtseSignalOutcome.FAIL if _text(item.get("outcome")).lower() == "fail" else TtseSignalOutcome.PARTIAL
    start, end = _window([first, last])
    return TtseSignal(
        id=f"tt-{number}",
        outcome=outcome,
        score=0.0 if outcome is TtseSignalOutcome.FAIL else _number(item.get("score"), 0.5),
        reason=reason,
        subject_id=first.ref.subject_id,
        start_unix_nano=str(start),
        end_unix_nano=str(end),
        root_cause_ids=ids,
        confidence=_number(item.get("confidence"), 0.5),
    )


__all__ = ["Attribution", "extract_json_object", "parse_attribution", "parse_suggestions"]
