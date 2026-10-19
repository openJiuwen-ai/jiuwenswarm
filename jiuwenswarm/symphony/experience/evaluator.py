"""LLM-based trace evaluator: judge whether the skills used in a trace were correctly selected."""

from __future__ import annotations

import json
import logging
from typing import Any

from .models import TraceRecord

LOGGER = logging.getLogger(__name__)

_SKILL_CORRECTNESS_PROMPT = """\
You are a judge that evaluates whether the skills used in a conversation were correctly selected for the user's task.

User query: {query}
Assistant result: {result}

Determine if the skills used were appropriate for this task. Output ONLY valid JSON with this schema:
{{"success": true/false, "error_type": "<type or null>", "error_detail": "<reason or null>"}}

A skill selection is "correct" (success=true) when the chosen skills directly address the user's need, their capabilities match what the task requires, and there isn't a clearly better alternative skill available.

A skill selection is "incorrect" (success=false) when:
- "wrong_skill": The skill doesn't match the task requirements or a different skill would have been much more appropriate.
- "skill_error": The skill was the right choice but encountered an execution error (tool failure, exception, etc).
- "incomplete": The skill started but did not finish the task.
- "refusal": The skill refused to perform the task.
- "empty": The skill returned an empty or meaningless result.

For incorrect selections, provide a brief error_detail explaining why the skill choice was wrong or what went wrong.
"""

_SKILL_CORRECTNESS_PROMPT_ENHANCED = """\
You are a judge that evaluates whether the skills used in a conversation were correctly selected for the user's task, based on the full interaction process.

User query: {query}
Skills used: {skills_used}
Interaction process: {interaction_summary}
Assistant result: {result}

Determine if the skills used were the RIGHT choice for this task. Consider:
1. Whether each skill's capabilities match what the task requires.
2. Whether there is a clearly better alternative skill that should have been used instead.
3. Whether the skill selection directly addresses the user's need.

Output ONLY valid JSON with this schema:
{{"success": true/false, "error_type": "<type or null>", "error_detail": "<reason or null>"}}

A skill selection is "correct" (success=true) when the chosen skills directly address the user's need, their capabilities match what the task requires, and there isn't a clearly better alternative skill available.

A skill selection is "incorrect" (success=false) when:
- "wrong_skill": The skill doesn't match the task requirements or a different skill would have been much more appropriate.
- "skill_error": The skill was the right choice but encountered an execution error (tool failure, exception, etc).
- "incomplete": The skill started but did not finish the task.
- "refusal": The skill refused to perform the task.
- "empty": The skill returned an empty or meaningless result.

For incorrect selections, provide a brief error_detail explaining why the skill choice was wrong or what went wrong.
"""


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _format_skills(skills: list[str]) -> str:
    if not skills:
        return "(no skills used)"
    return ", ".join(skills)


def _summarize_messages(messages: list[dict], max_len: int = 3000) -> str:
    """Extract a concise interaction summary from the message history."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            text = content if isinstance(content, str) else str(content)
            if text.strip():
                parts.append(f"[User] {text.strip()}")

        elif role == "assistant":
            reasoning = msg.get("reasoning", "")
            tool_call = msg.get("tool_call")
            if reasoning:
                parts.append(f"[Reasoning] {_truncate(reasoning.strip(), 200)}")
            if tool_call and isinstance(tool_call, dict):
                tc_name = tool_call.get("name", "")
                if tc_name == "skill_tool":
                    try:
                        raw_args = tool_call.get("arguments", {})
                        args = (
                            json.loads(raw_args)
                            if isinstance(raw_args, str)
                            else raw_args
                        )
                        skill_name = args.get("skill_name", "")
                        parts.append(f"[Skill Call: {skill_name}]")
                    except (json.JSONDecodeError, TypeError):
                        parts.append(f"[Skill Call: skill_tool]")
                else:
                    parts.append(f"[Tool Call: {tc_name}]")
            if content and isinstance(content, str) and not tool_call:
                if len(content) > 50:
                    parts.append(f"[Assistant] {_truncate(content.strip(), 200)}")

        elif role == "tool":
            text = content if isinstance(content, str) else str(content)
            if text.strip():
                parts.append(f"[Tool Result] {_truncate(text.strip(), 300)}")

    summary = "\n".join(parts)
    if len(summary) > max_len:
        summary = _truncate(summary, max_len)
    return summary


class TraceEvaluator:
    """Judge whether the skills used in each TraceRecord were correctly selected via LLM.

    When ``messages`` are available, uses an enhanced prompt that includes
    the full interaction process and skill selection context. Otherwise, falls back
    to the simple query+result prompt.

    ``success=True`` means the skill selection was correct — these traces are
    used to build experience patterns. ``success=False`` means the skills were
    incorrectly chosen or the execution failed — these traces are excluded from
    the experience bank.

    Usage::

        evaluator = TraceEvaluator(llm_client=openai_client, llm_model="qwen3-32b")
        records = parse_session("session_abc")
        evaluator.evaluate(records)
        # records[0].success / .error_type / .error_detail are now filled
    """

    def __init__(
        self,
        llm_client: Any | None = None,
        llm_model: str = "",
    ) -> None:
        self._llm_model = str(llm_model or "").strip()
        self._llm = llm_client if llm_client is not None else None

    def evaluate(self, records: list[TraceRecord]) -> list[TraceRecord]:
        """Judge skill correctness for each record, skip those with empty skills, return a new processed list."""
        result = []
        for record in records:
            if not record.skills:
                continue
            self._judge_one(record)
            result.append(record)
        return result

    def _judge_one(self, record: TraceRecord) -> None:
        if not self._llm or not self._llm_model:
            self._fallback(record)
            return

        query = record.query or ""
        result = record.result or ""

        if not query:
            record.success = False
            record.error_type = "empty"
            record.error_detail = "No user query found"
            return

        if not result:
            record.success = False
            record.error_type = "empty"
            record.error_detail = "Skill returned empty result"
            return

        # Use enhanced prompt when messages are available
        if record.messages:
            prompt = _SKILL_CORRECTNESS_PROMPT_ENHANCED.format(
                query=query,
                skills_used=_format_skills(record.skills),
                interaction_summary=_summarize_messages(record.messages),
                result=result,
            )
            system_msg = (
                "You evaluate whether the skills used were correctly selected for the user's task. "
                "Output only valid JSON.")
        else:
            prompt = _SKILL_CORRECTNESS_PROMPT.format(query=query, result=result)
            system_msg = (
                "You evaluate whether the skills used were correctly selected for the user's task. "
                "Output only valid JSON."
            )
        try:
            response = self._llm.chat.completions.create(
                model=self._llm_model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=256,
                stream=False,
                extra_body={"enable_thinking": False, "thinking": {"type": "disabled"}},
            )
            content = response.choices[0].message.content
            if content:
                self._parse_judge_response(content, record)
            else:
                self._fallback(record)
        except Exception as exc:
            LOGGER.warning("TraceEvaluator: LLM call failed for trace %s: %s", record.trace_id, exc)
            self._fallback(record)

    @staticmethod
    def _parse_judge_response(response: str, record: TraceRecord) -> None:
        raw = response.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("TraceEvaluator: failed to parse JSON for trace %s", record.trace_id)
            TraceEvaluator._fallback(record)
            return

        success = bool(data.get("success", False))
        record.success = success
        if success:
            record.error_type = None
            record.error_detail = None
        else:
            record.error_type = data.get("error_type") or "error"
            record.error_detail = data.get("error_detail") or ""

    @staticmethod
    def _fallback(record: TraceRecord) -> None:
        """Conservative fallback when LLM evaluation is unavailable or fails.

        Without the LLM we cannot reliably judge whether the skill selection
        was correct, so the safe choice is to NOT treat a record as a positive
        sample: default to ``success=False`` with ``error_type="unevaluated"``
        so it is excluded from the experience bank rather than polluting it
        with unverified successes.

        Only clearly-failing cases (no query, no result, or an execution error
        already flagged upstream) are given a more specific ``error_type``;
        everything else is conservatively marked ``unevaluated``. Result
        length is deliberately not used as a correctness signal.
        """
        query = (record.query or "").strip()
        result = record.result or ""

        # Defensive: result should already be a str (parser normalizes it),
        # but the fallback must not assume that.
        if not isinstance(result, str):
            result = "" if result is None else str(result)

        if not query:
            record.success = False
            record.error_type = "empty"
            record.error_detail = "No user query found"
            return

        if not result.strip():
            record.success = False
            record.error_type = "empty"
            record.error_detail = "Skill returned empty result"
            return

        # Upstream already flagged an execution error (e.g. skill raised) —
        # keep that classification, it is more precise than "unevaluated".
        if record.error_type:
            record.success = False
            if not record.error_detail:
                record.error_detail = "Upstream-flagged error, not LLM-evaluated"
            return

        # Cannot judge without the LLM — conservatively exclude from the KB.
        record.success = False
        record.error_type = "unevaluated"
        record.error_detail = "LLM evaluation unavailable; conservatively excluded"


__all__ = ["TraceEvaluator"]
