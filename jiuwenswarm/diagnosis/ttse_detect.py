# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Experience detection of a diagnosed stretch, reproduced from TTSE.

TTSE decides whether a run is worth learning an experience from with its
``SignalBasedSuccessDetector``: a tool-failure scan, a user-feedback check, a
tool-call gate and a reply Judge, and the first rule that matches wins. This
module runs the same rules, in the same order and with the same building blocks
from agent-core, over the main-agent nodes of the selected stretch, but it also
remembers which nodes made a rule match so that the verdict can be anchored in
the trajectory like any other root cause.

The result is one root cause whose category is ``ttse_success``,
``ttse_partial`` or ``ttse_fail``; a ``skip`` verdict yields no root cause.
The detection needs neither a ``TTSERail`` nor an enabled TTSE.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from openjiuwen.agent_evolving.attribution import TrajectoryNode
from openjiuwen.agent_evolving.optimizer.llm_resilience import invoke_text_with_retry
from openjiuwen.agent_evolving.signal import ConversationSignalDetector, is_tool_execution_failure
from openjiuwen.agent_evolving.ttse import TTSEConfig
from openjiuwen.agent_evolving.ttse.prompts import detect_judge_prompt
from openjiuwen.agent_evolving.ttse.trajectory_adapter import count_tool_calls, extract_final_reply

from jiuwenswarm.diagnosis.models import RootCause, RootCauseCategory, StepRef

logger = logging.getLogger(__name__)

Locale = Literal["zh", "en"]
Outcome = Literal["success", "partial", "fail"]

#: Roles TTSE reads; system rows are prompt scaffolding, not behaviour.
_MESSAGE_ROLES = frozenset({"user", "assistant", "tool"})
_JUDGE_OUTCOMES = frozenset({"success", "partial", "fail"})
_JUDGE_TEMPERATURE = 0.0
#: Step references one experience root cause carries at most.
_MAX_REFS = 5
#: Evidence lines of one kind (tool failures, Judge goals) shown at most.
_MAX_EVIDENCE_LINES = 5
#: Characters of a user quote or Judge reason kept in the headline and evidence.
_EXCERPT_CHARS = 160
#: Characters kept before and after the matched error keyword of a tool output.
_SNIPPET_BEFORE = 60
_SNIPPET_AFTER = 100
#: Characters of the first failing output kept in the headline.
_SUMMARY_SNIPPET_CHARS = 120
_ROOT_CAUSE_ID = "ttse-experience"

_CATEGORY_BY_OUTCOME: dict[str, RootCauseCategory] = {
    "success": RootCauseCategory.TTSE_SUCCESS,
    "partial": RootCauseCategory.TTSE_PARTIAL,
    "fail": RootCauseCategory.TTSE_FAIL,
}

_TEXTS: dict[str, dict[str, str]] = {
    "zh": {
        "unknown_tool": "未知工具",
        "summary_tool_error": "工具 {tool} 输出报错：{snippet}",
        "summary_tool_error_more": "（另有 {count} 处工具报错）",
        "summary_user_feedback": "用户纠正或表达不满：{excerpt}",
        "summary_judge": "最终回复评判为「{outcome}」：{reason}",
        "summary_judge_plain": "最终回复评判为「{outcome}」",
        "basis_tool_error": "判定依据：运行中有工具输出报错，按 TTSE 的规则记为部分完成，不再继续检测",
        "basis_user_feedback": "判定依据：用户对结果表达了纠正或不满，按 TTSE 的规则记为部分完成，不再继续检测",
        "basis_judge": "判定依据：工具调用 {count} 次（不少于 {minimum} 次），没有工具报错和用户纠正，由 Judge 评判最终回复",
        "evidence_tool_error": "工具 {tool} 的输出匹配到报错关键词「{keyword}」：{snippet}",
        "evidence_tool_error_plain": "工具 {tool} 的输出疑似报错：{snippet}",
        "evidence_user_feedback": "用户原话：{excerpt}",
        "outcome_success": "完成",
        "outcome_partial": "部分完成",
        "outcome_fail": "未完成",
        "goal": "目标[{verdict}] {goal}：{reason}",
        "goal_satisfied": "满足",
        "goal_unsatisfied": "未满足",
    },
    "en": {
        "unknown_tool": "unknown tool",
        "summary_tool_error": "Tool {tool} reported an error: {snippet}",
        "summary_tool_error_more": " ({count} more tool errors)",
        "summary_user_feedback": "The user corrected or objected to the result: {excerpt}",
        "summary_judge": "Final reply judged as \"{outcome}\": {reason}",
        "summary_judge_plain": "Final reply judged as \"{outcome}\"",
        "basis_tool_error": (
            "Basis: a tool output reports an error; TTSE's rules count such a run as partly completed "
            "and stop checking"
        ),
        "basis_user_feedback": (
            "Basis: the user corrected or objected to the result; TTSE's rules count such a run as partly "
            "completed and stop checking"
        ),
        "basis_judge": (
            "Basis: {count} tool calls (at least {minimum}), no tool error and no user correction, so the "
            "Judge rated the final reply"
        ),
        "evidence_tool_error": "Output of tool {tool} matches the error keyword \"{keyword}\": {snippet}",
        "evidence_tool_error_plain": "Output of tool {tool} looks like an error: {snippet}",
        "evidence_user_feedback": "The user said: {excerpt}",
        "outcome_success": "completed",
        "outcome_partial": "partly completed",
        "outcome_fail": "not completed",
        "goal": "Goal [{verdict}] {goal}: {reason}",
        "goal_satisfied": "satisfied",
        "goal_unsatisfied": "unsatisfied",
    },
}


@dataclass(slots=True)
class _Verdict:
    """Detection outcome with the nodes and the text that explain it.

    Attributes:
        outcome: ``success``, ``partial`` or ``fail``.
        anchors: Nodes that made the rule match.
        summary: One-line concrete reason, shown as the root cause headline.
        evidence: Lines that show which rule fired and what it saw.
    """

    outcome: Outcome
    anchors: list[TrajectoryNode]
    summary: str
    evidence: list[str] = field(default_factory=list)


def _to_messages(nodes: Sequence[TrajectoryNode]) -> tuple[list[dict[str, Any]], list[TrajectoryNode]]:
    """Project nodes onto chat messages, keeping the node behind every message.

    Args:
        nodes: Message and tool rows of one subject in trajectory order.

    Returns:
        The messages and, at the same index, the node each one came from.
    """
    messages: list[dict[str, Any]] = []
    kept: list[TrajectoryNode] = []
    for node in nodes:
        role = str(node.role or "")
        if role not in _MESSAGE_ROLES:
            continue
        message: dict[str, Any] = {"role": role, "content": node.content or ""}
        if node.tool_calls:
            message["tool_calls"] = [dict(call) for call in node.tool_calls]
        if role == "tool":
            if node.name:
                message["name"] = node.name
            if node.tool_call_id:
                message["tool_call_id"] = node.tool_call_id
        messages.append(message)
        kept.append(node)
    return messages, kept


def _call_id(call: Mapping[str, Any]) -> str:
    return str(call.get("id") or call.get("tool_call_id") or "")


def _call_name(call: Mapping[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, Mapping) and function.get("name"):
        return str(function["name"])
    return str(call.get("name") or "")


def _squash(value: str) -> str:
    """Collapse every whitespace run, newlines included, into one space."""
    return " ".join(value.split())


def _failure_pattern() -> re.Pattern[str] | None:
    """Return TTSE's error-keyword pattern, or ``None`` when agent-core hides it.

    Only used to show which keyword matched; the verdict itself always comes
    from ``is_tool_execution_failure``.
    """
    try:
        from openjiuwen.agent_evolving.signal.from_conv import _FAILURE_KEYWORDS
    except ImportError:
        return None
    return _FAILURE_KEYWORDS if isinstance(_FAILURE_KEYWORDS, re.Pattern) else None


def _failure_detail(content: str) -> tuple[str, str]:
    """Return the matched error keyword and a one-line snippet around it.

    Args:
        content: Output of a tool row that matched the failure rules.

    Returns:
        The keyword (empty when it cannot be located) and the snippet.
    """
    pattern = _failure_pattern()
    match = pattern.search(content) if pattern is not None else None
    if match is None:
        head = _squash(content)
        limit = _SNIPPET_BEFORE + _SNIPPET_AFTER
        return "", head[:limit] + ("..." if len(head) > limit else "")
    start = max(0, match.start() - _SNIPPET_BEFORE)
    end = min(len(content), match.end() + _SNIPPET_AFTER)
    snippet = _squash(content[start:end])
    return match.group(0), ("..." if start > 0 else "") + snippet + ("..." if end < len(content) else "")


def _failed_tool_rows(
    messages: Sequence[Mapping[str, Any]],
    kept: Sequence[TrajectoryNode],
) -> list[tuple[TrajectoryNode, str, str]]:
    """Find the tool rows whose output matches TTSE's execution-failure rules.

    Returns:
        ``(node, tool name, output)`` of every failing tool row.
    """
    names: dict[str, str] = {}
    failures: list[tuple[TrajectoryNode, str, str]] = []
    for message, node in zip(messages, kept):
        role = message["role"]
        if role == "assistant":
            for call in message.get("tool_calls") or ():
                identity, name = _call_id(call), _call_name(call)
                if identity and name:
                    names[identity] = name
            continue
        if role != "tool":
            continue
        tool = str(message.get("name") or names.get(str(message.get("tool_call_id") or ""), ""))
        output = str(message.get("content") or "")
        if is_tool_execution_failure(output, tool):
            failures.append((node, tool, output))
    return failures


async def _user_feedback_row(
    messages: Sequence[dict[str, Any]],
    kept: Sequence[TrajectoryNode],
    *,
    model: Any,
    model_name: str,
    locale: Locale,
) -> tuple[TrajectoryNode | None, str] | None:
    """Ask TTSE's user-feedback detector whether the user objected to the run.

    Returns:
        ``None`` without feedback; otherwise the user node the feedback comes
        from (the last user row when the excerpt cannot be matched) and the
        excerpt.
    """
    detector = ConversationSignalDetector().bind_llm(
        llm=model,
        model=model_name,
        language="cn" if locale == "zh" else "en",
    )
    try:
        signals = await detector.detect_user_intent(list(messages), allow_skillless=True)
    except Exception:  # noqa: BLE001 - detection failures count as "no feedback", as in TTSE
        logger.warning("[diagnosis] user feedback detection failed", exc_info=True)
        return None
    if not signals:
        return None
    excerpt = str(signals[0].excerpt or "").strip()
    user_rows = [node for message, node in zip(messages, kept) if message["role"] == "user"]
    matched = next((node for node in reversed(user_rows) if excerpt and excerpt in (node.content or "")), None)
    return matched or (user_rows[-1] if user_rows else None), excerpt


def _parse_judge_json(raw: str) -> dict[str, Any] | None:
    """Parse the Judge reply, tolerating code fences and surrounding prose."""
    text = (raw or "").strip()
    if not text:
        return None
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    braced = re.search(r"\{[\s\S]*\}", text)
    if braced:
        candidates.append(braced.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _final_reply_row(
    messages: Sequence[Mapping[str, Any]],
    kept: Sequence[TrajectoryNode],
) -> TrajectoryNode | None:
    """Return the last assistant row that answers without calling a tool."""
    for message, node in zip(reversed(messages), reversed(kept)):
        if message["role"] != "assistant" or message.get("tool_calls"):
            continue
        if str(message.get("content") or "").strip():
            return node
    return None


def _goal_lines(goals: Any, text: dict[str, str]) -> list[str]:
    lines: list[str] = []
    if not isinstance(goals, list):
        return lines
    for item in goals[:_MAX_EVIDENCE_LINES]:
        if not isinstance(item, Mapping):
            continue
        satisfied = str(item.get("verdict") or "").strip().upper() == "SATISFIED"
        lines.append(
            text["goal"].format(
                verdict=text["goal_satisfied"] if satisfied else text["goal_unsatisfied"],
                goal=str(item.get("goal") or "").strip(),
                reason=str(item.get("reason") or "").strip(),
            ),
        )
    return lines


async def _judge(
    messages: Sequence[dict[str, Any]],
    kept: Sequence[TrajectoryNode],
    *,
    config: TTSEConfig,
    model: Any,
    model_name: str,
    text: dict[str, str],
) -> _Verdict | None:
    """Judge the final reply against the task, as TTSE does.

    Returns:
        ``None`` when the Judge fails or answers with something unusable,
        which TTSE treats as ``skip``.
    """
    query = next((str(message["content"]) for message in messages if message["role"] == "user"), "")
    final_reply = extract_final_reply(messages, max_chars=config.detect_final_reply_chars)
    prompt = detect_judge_prompt(query, final_reply)
    try:
        raw = await invoke_text_with_retry(
            model,
            model_name,
            prompt,
            policy=config.detect_llm_policy,
            temperature=_JUDGE_TEMPERATURE,
        )
    except Exception:  # noqa: BLE001 - a failed Judge means skip
        logger.warning("[diagnosis] experience Judge call failed", exc_info=True)
        return None
    data = _parse_judge_json(raw)
    if data is None:
        logger.warning("[diagnosis] experience Judge reply is not JSON")
        return None
    outcome = str(data.get("outcome") or "").strip().lower()
    if outcome not in _JUDGE_OUTCOMES:
        logger.warning("[diagnosis] experience Judge outcome is invalid: %s", outcome)
        return None
    reason = _squash(str(data.get("reason") or ""))
    label = text[f"outcome_{outcome}"]
    summary = (
        text["summary_judge"].format(outcome=label, reason=_clip(reason, _EXCERPT_CHARS))
        if reason
        else text["summary_judge_plain"].format(outcome=label)
    )
    evidence = [
        text["basis_judge"].format(count=count_tool_calls(messages), minimum=config.detect_min_tool_calls),
        *_goal_lines(data.get("goals"), text),
    ]
    anchor = _final_reply_row(messages, kept)
    return _Verdict(
        outcome=outcome,  # type: ignore[arg-type]
        anchors=[anchor] if anchor is not None else [],
        summary=summary,
        evidence=evidence,
    )


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "..."


def _inline_code(value: str) -> str:
    """Quote raw text as Markdown inline code.

    The summary is rendered as Markdown like every other root cause; tool output and user
    words carry ``_``, ``|`` and brackets that Markdown would otherwise read as syntax.
    """
    if not value:
        return value
    safe = value.replace("`", "'")
    return f"`{safe}`"


def _tool_error_verdict(failures: Sequence[tuple[TrajectoryNode, str, str]], text: dict[str, str]) -> _Verdict:
    """Build the ``partial`` verdict of failing tool rows, naming the first failure.

    Args:
        failures: ``(node, tool name, output)`` of every failing tool row.
        text: Localized texts.
    """
    evidence = [text["basis_tool_error"]]
    for _node, tool, output in failures[:_MAX_EVIDENCE_LINES]:
        keyword, snippet = _failure_detail(output)
        name = tool or text["unknown_tool"]
        key = "evidence_tool_error" if keyword else "evidence_tool_error_plain"
        evidence.append(text[key].format(tool=name, keyword=keyword, snippet=snippet))
    if len(failures) > _MAX_EVIDENCE_LINES:
        evidence.append(text["summary_tool_error_more"].format(count=len(failures) - _MAX_EVIDENCE_LINES).strip())
    first_tool = failures[0][1] or text["unknown_tool"]
    snippet = _inline_code(_clip(_failure_detail(failures[0][2])[1], _SUMMARY_SNIPPET_CHARS))
    summary = text["summary_tool_error"].format(tool=first_tool, snippet=snippet)
    if len(failures) > 1:
        summary += text["summary_tool_error_more"].format(count=len(failures) - 1)
    return _Verdict(
        outcome="partial",
        anchors=[node for node, _tool, _output in failures],
        summary=summary,
        evidence=evidence,
    )


async def _detect(
    nodes: Sequence[TrajectoryNode],
    *,
    model: Any,
    model_name: str,
    locale: Locale,
) -> tuple[_Verdict | None, list[TrajectoryNode]]:
    """Run the TTSE rules in order; the first match decides.

    Returns:
        The verdict (``None`` for skip) and the nodes it may fall back to for
        anchoring.
    """
    config = TTSEConfig()
    text = _TEXTS[locale]
    messages, kept = _to_messages(nodes)
    if not messages:
        return None, kept

    failures = _failed_tool_rows(messages, kept)
    if failures:
        return _tool_error_verdict(failures, text), kept

    feedback = await _user_feedback_row(messages, kept, model=model, model_name=model_name, locale=locale)
    if feedback is not None:
        node, excerpt = feedback
        quote = _clip(_squash(excerpt), _EXCERPT_CHARS)
        verdict = _Verdict(
            outcome="partial",
            anchors=[node] if node is not None else [],
            summary=text["summary_user_feedback"].format(excerpt=_inline_code(quote)),
            evidence=[text["basis_user_feedback"], text["evidence_user_feedback"].format(excerpt=quote)],
        )
        return verdict, kept

    if count_tool_calls(messages) < config.detect_min_tool_calls:
        return None, kept

    verdict = await _judge(messages, kept, config=config, model=model, model_name=model_name, text=text)
    return verdict, kept


def _step_refs(anchors: Sequence[TrajectoryNode], fallback: Sequence[TrajectoryNode]) -> list[StepRef]:
    """Build step references from nodes that carry a span identity.

    Args:
        anchors: Nodes the verdict rests on.
        fallback: Nodes to take the last anchorable one from when none of the
            anchors has a span identity.

    Returns:
        At most ``_MAX_REFS`` references without duplicates.
    """
    refs: list[StepRef] = []
    seen: set[str] = set()
    for node in anchors:
        if not node.trace_id or not node.span_id or node.node_id in seen:
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
        seen.add(node.node_id)
        if len(refs) >= _MAX_REFS:
            break
    if refs:
        return refs
    for node in reversed(fallback):
        if not node.trace_id or not node.span_id:
            continue
        try:
            return [
                StepRef(
                    trace_id=node.trace_id,
                    span_id=node.span_id,
                    subject_id=node.subagent_key,
                    node_id=node.node_id,
                ),
            ]
        except ValueError:
            continue
    return []


async def detect_experience_cause(
    nodes: Sequence[TrajectoryNode],
    *,
    model: Any,
    model_name: str,
    locale: Locale,
) -> RootCause | None:
    """Detect whether the selected stretch is worth learning an experience from.

    Args:
        nodes: Message and tool rows of the main agent inside the selection.
        model: Model client used by the user-feedback check and the Judge.
        model_name: Name passed to the model calls.
        locale: Language of the user-facing text.

    Returns:
        A root cause of category ``ttse_success``, ``ttse_partial`` or
        ``ttse_fail``, or ``None`` when TTSE would skip the stretch or the
        verdict cannot be anchored in the trajectory.
    """
    verdict, kept = await _detect(nodes, model=model, model_name=model_name, locale=locale)
    if verdict is None:
        return None
    refs = _step_refs(verdict.anchors, kept)
    if not refs:
        logger.info("[diagnosis] experience verdict %s has no anchorable step", verdict.outcome)
        return None
    return RootCause(
        id=_ROOT_CAUSE_ID,
        category=_CATEGORY_BY_OUTCOME[verdict.outcome],
        subject_id=refs[0].subject_id,
        refs=refs,
        summary=verdict.summary,
        evidence="\n".join(verdict.evidence),
        confidence=None,
    )


__all__ = ["detect_experience_cause"]
