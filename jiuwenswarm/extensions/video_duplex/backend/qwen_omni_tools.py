"""Qwen Omni Realtime tool definitions and request validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


QWEN_OMNI_DELEGATE_TOOL_NAME = "jiuwen_delegate"
QWEN_OMNI_RESEARCH_TOOL_NAME = "jiuwen_research"
_MAX_CALL_ID_CHARS = 200
_MAX_TASK_CHARS = 2_000
_DELEGATE_ARGUMENT_NAMES = ("task", "query", "instruction", "request")


@dataclass(frozen=True)
class QwenOmniToolCall:
    name: str
    call_id: str
    arguments: dict[str, Any]
    task: str

    @property
    def query(self) -> str:
        """Compatibility alias for existing video search job fields."""
        return self.task


def qwen_omni_tools() -> list[dict[str, Any]]:
    """Return fresh Qwen-compatible tool definitions for each session."""
    return [
        {
            "type": "function",
            "function": {
                "name": QWEN_OMNI_DELEGATE_TOOL_NAME,
                "description": (
                    "Delegate any request that cannot be completed directly from the current "
                    "audio, video, and conversation to the full Jiuwen Core Agent. Jiuwen may "
                    "use all of its available capabilities, including research, files, document "
                    "processing, calculation, code execution, and computer or browser tools."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": (
                                "A complete, self-contained task preserving the user's requested "
                                "action, target, path or name, output format, and constraints."
                            ),
                        },
                    },
                    "required": ["task"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _normalize_delegate_arguments(raw: Any, *, legacy: bool) -> dict[str, Any]:
    """Normalize a bounded, unambiguous provider envelope with exactly one task."""
    if isinstance(raw, str):
        if len(raw) > 65_536:
            raise ValueError("arguments too long")
        candidate = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate, re.IGNORECASE)
        if fenced:
            candidate = fenced.group(1).strip()
        try:
            raw = json.loads(candidate)
        except (ValueError, RecursionError) as exc:
            # Broken structured data must not become an executable task string.
            if fenced or not candidate or candidate[0] in "{[\"'`" or not any(c.isalpha() for c in candidate):
                raise ValueError("arguments must be valid JSON or a task string") from exc
            raw = {"query" if legacy else "task": candidate}
    if not isinstance(raw, dict):
        raise ValueError("arguments must be a JSON object")
    aliases = set(_DELEGATE_ARGUMENT_NAMES) | {"prompt", "input", "content", "text"}
    wrappers = {"arguments", "parameters", "payload", "input"}
    tasks = []
    normalized = {}

    def visit(record, depth):
        if depth > 8 or not isinstance(record, dict):
            raise ValueError("Invalid argument wrapper")
        for key, item in record.items():
            if key in wrappers and isinstance(item, dict):
                visit(item, depth + 1)
            elif key in aliases:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError("task must be a nonempty string")
                tasks.append((key, item.strip()))
            elif key not in {"reason", "priority"}:
                raise ValueError("Unsupported delegation argument")

    visit(raw, 0)
    if len(tasks) != 1:
        raise ValueError("arguments must contain exactly one task field")
    key, task = tasks[0]
    key = "query" if legacy else key if key in _DELEGATE_ARGUMENT_NAMES else "task"
    normalized[key] = task
    return normalized


def parse_qwen_omni_tool_call(value: Any) -> QwenOmniToolCall:
    """Validate the current delegation tool and legacy research calls."""
    if not isinstance(value, dict):
        raise ValueError("tool call must be an object")

    name = str(value.get("name") or "").strip()
    if name not in {QWEN_OMNI_DELEGATE_TOOL_NAME, QWEN_OMNI_RESEARCH_TOOL_NAME}:
        raise ValueError(f"unsupported Qwen tool: {name or '<empty>'}")

    call_id = str(value.get("call_id") or "").strip()
    if not call_id or len(call_id) > _MAX_CALL_ID_CHARS:
        raise ValueError(f"call_id must contain 1-{_MAX_CALL_ID_CHARS} characters")

    raw_arguments = value.get("arguments")
    arguments = _normalize_delegate_arguments(
        raw_arguments, legacy=name == QWEN_OMNI_RESEARCH_TOOL_NAME,
    )
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    if len(arguments) != 1:
        raise ValueError("arguments must contain exactly one task field")
    if name == QWEN_OMNI_DELEGATE_TOOL_NAME:
        argument_name = next(
            (key for key in _DELEGATE_ARGUMENT_NAMES if key in arguments), None
        )
    else:
        argument_name = "query" if "query" in arguments else None
    if argument_name is None:
        raise ValueError("arguments must contain a supported task field")

    raw_task = arguments.get(argument_name)
    if not isinstance(raw_task, str):
        raise ValueError(f"{argument_name} must be a string")
    task = raw_task.strip()
    if not task or len(task) > _MAX_TASK_CHARS:
        raise ValueError(f"{argument_name} must contain 1-{_MAX_TASK_CHARS} characters")
    return QwenOmniToolCall(
        name=name,
        call_id=call_id,
        arguments=arguments,
        task=task,
    )
