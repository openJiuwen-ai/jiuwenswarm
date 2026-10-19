"""Session trace parser for experience distillation."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from jiuwenswarm.common.utils import get_agent_sessions_dir

from ..models import TraceRecord

LOGGER = logging.getLogger(__name__)

_SESSION_DIR = get_agent_sessions_dir()


def list_session_ids() -> list[str]:
    """Return all session IDs from the sessions directory, excluding heartbeat directories."""
    if not _SESSION_DIR.exists() or not _SESSION_DIR.is_dir():
        return []
    return [
        d.name
        for d in _SESSION_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("heartbeat_")
    ]


def _read_json(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        LOGGER.error("Failed to read trace file")
        return None


def _extract_text(content) -> str:
    """Extract plain text from content which may be None, str, or a list of content blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "".join(texts)
    return str(content)


def _extract_skills_from_tool_call(tool_call: dict) -> list[str]:
    """Extract skill names from a tool_call record.

    Recognizes several patterns:
    - ``skill_tool``: arguments contain ``skill_name``
    - ``symphony_compose_score``: arguments contain ``candidate_skill_ids``
      or the result's ``raw_output.plan.steps[].skill_id``
    """
    name = tool_call.get("name", "")
    args_raw = tool_call.get("arguments", "{}")
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except json.JSONDecodeError:
        args = {}

    if name == "skill_tool":
        skill_name = args.get("skill_name", "")
        return [skill_name] if skill_name else []

    if name == "symphony_compose_score":
        # Primary: candidate_skill_ids from arguments
        candidate_ids = args.get("candidate_skill_ids", [])
        if isinstance(candidate_ids, list) and candidate_ids:
            return [s for s in candidate_ids if isinstance(s, str) and s]
        # Fallback: skill_ids from the result's raw_output.plan.steps
        result_raw = tool_call.get("result")
        raw_output = tool_call.get("raw_output")
        # result may be a string representation of a dict
        if raw_output is None and result_raw is not None:
            if isinstance(result_raw, str):
                try:
                    raw_output = json.loads(result_raw)
                except json.JSONDecodeError:
                    # Python-style repr (single quotes); try ast.literal_eval
                    try:
                        import ast
                        raw_output = ast.literal_eval(result_raw)
                    except Exception:
                        raw_output = None
            elif isinstance(result_raw, dict):
                raw_output = result_raw
        if isinstance(raw_output, dict):
            plan = raw_output.get("plan")
            if isinstance(plan, dict):
                steps = plan.get("steps", [])
                if isinstance(steps, list):
                    skill_ids = []
                    for step in steps:
                        if isinstance(step, dict):
                            sid = step.get("skill_id") or step.get("name")
                            if sid and isinstance(sid, str) and sid not in skill_ids:
                                skill_ids.append(sid)
                    if skill_ids:
                        return skill_ids

    return []


def _extract_skills_from_raw_output(raw_output: dict) -> list[str]:
    """Extract skill names from a tool_result's raw_output.

    Used for ``symphony_compose_score`` results where the plan.steps
    contain the selected skill_ids.
    """
    skill_ids: list[str] = []
    plan = raw_output.get("plan")
    if isinstance(plan, dict):
        steps = plan.get("steps", [])
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    sid = step.get("skill_id") or step.get("name")
                    if sid and isinstance(sid, str) and sid not in skill_ids:
                        skill_ids.append(sid)
    return skill_ids


def parse_session(session_id: str) -> list[TraceRecord]:
    """Parse a single session directory into TraceRecords.

    Uses request_id as traceId. A new request_id indicates a new user query segment.
    Skill names are extracted from ``skill_tool`` and ``symphony_compose_score`` calls.
    """
    session_dir = _SESSION_DIR / session_id
    if not session_dir.is_dir():
        return []

    metadata = _read_json(session_dir / "metadata.json")
    if metadata is None or not isinstance(metadata, dict):
        return []

    history_data = _read_json(session_dir / "history.json")
    if history_data is None:
        history_data = []
    if not isinstance(history_data, list):
        history_data = []

    current_request_id = ""
    query = ""
    reasoning = ""
    assistant_content = ""
    skills = []
    trace_records = []
    messages = []

    def _flush_record():
        # Commit accumulated assistant_content/reasoning before flushing
        if assistant_content:
            messages.append({"role": "assistant", "content": assistant_content, "reasoning": reasoning})
        if not query or not current_request_id:
            return
        # result from the last assistant message, not tool result
        result = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                result = _extract_text(msg.get("content", ""))
                break
        trace_records.append(TraceRecord(
            trace_id=current_request_id,
            query=query,
            skills=skills,
            messages=messages,
            result=result,
        ))

    for record in history_data:
        if not isinstance(record, dict):
            continue
        request_id = record.get("request_id", "")
        if request_id and request_id != current_request_id:
            _flush_record()
            current_request_id = request_id
            query = ""
            messages = []
            skills = []
            reasoning = ""
            assistant_content = ""
        if record.get("role") == "user":
            query = _extract_text(record.get("content")).strip()
        elif record.get("role") == "assistant":
            event_type = record.get("event_type", "")
            if event_type == "chat.reasoning":
                reasoning += _extract_text(record.get("content")).strip()
            elif event_type == "chat.delta":
                assistant_content += _extract_text(record.get("content")).strip()
            elif event_type == "chat.final":
                assistant_content = _extract_text(record.get("content")).strip()
            elif event_type == "chat.tool_call":
                tool_call = record.get("tool_call") or {}
                if isinstance(tool_call, dict):
                    extracted = _extract_skills_from_tool_call(tool_call)
                    for s in extracted:
                        if s and s not in skills:
                            skills.append(s)
                messages.append({"role": "assistant", "content": assistant_content, "reasoning": reasoning,
                                 "tool_call": tool_call})
                assistant_content = ""
                reasoning = ""
            elif event_type == "chat.tool_result":
                tool_result = _extract_text(record.get("result")).strip()
                messages.append({"role": "tool", "content": tool_result})
                # Extract skills from raw_output (e.g. symphony_compose_score plan)
                raw_output = record.get("raw_output")
                if isinstance(raw_output, dict):
                    tool_name = record.get("tool_name", "")
                    if tool_name == "symphony_compose_score":
                        extracted = _extract_skills_from_raw_output(raw_output)
                        for s in extracted:
                            if s and s not in skills:
                                skills.append(s)
            elif event_type == "chat.error":
                messages.append({"role": "assistant", "content": _extract_text(record.get("content")).strip()})
    _flush_record()
    return trace_records


def parse_all_sessions() -> list[TraceRecord]:
    """Parse all sessions into TraceRecords, skipping unparseable ones."""
    records: list[TraceRecord] = []
    for session_id in list_session_ids():
        trace_records = parse_session(session_id)
        if trace_records:
            records.extend(trace_records)
    return records