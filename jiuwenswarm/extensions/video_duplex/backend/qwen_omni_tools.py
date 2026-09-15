"""Qwen Omni Realtime tool definitions and request validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
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
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("arguments must be valid JSON") from exc
    elif isinstance(raw_arguments, dict):
        arguments = dict(raw_arguments)
    else:
        raise ValueError("arguments must be a JSON object")
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
