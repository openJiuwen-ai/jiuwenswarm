# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified ``skill_step`` facade tool.

The facade collapses the previous ``skill_step_create / _complete /
_complete_batch / _insert / _remove / _list`` surface into a single
``skill_step`` tool that the model sees, with an ``action`` discriminator
selecting the underlying operation.

The class still inherits from :class:`TodoToolkit` and writes to
``skill_step.md`` under the session directory, so existing call sites that
rely on ``load_tasks`` / ``clear_tasks`` / ``resolve_todo_path`` (e.g.
``SkillComplianceRail``) keep working unchanged. Only :meth:`get_tools`
is overridden to expose the unified facade tool instead of the six
``skill_step_*`` tools the parent class would emit.
"""

from __future__ import annotations

import ast
import json
from typing import Annotated, Any, ClassVar, List, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _coerce_list_like(value: Any, *, want: Literal["str", "int"]) -> Any:
    """Best-effort normalization for list-like tool inputs.

    Accepted forms:
    - native list/tuple
    - JSON-encoded list string: ``'["a","b"]'`` / ``"[1,2]"``
    - Python literal list/tuple string: ``"['a','b']"`` / ``"(1,2)"``
    - comma-separated string: ``"a,b,c"`` / ``"1,2,3"``
    """
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value

        parsed: Any = None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None

        if not isinstance(parsed, list):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, tuple):
                parsed = list(parsed)

        if isinstance(parsed, list):
            return parsed

        # Final fallback: tolerate "a,b,c" / "1,2,3" style values.
        if "," in text:
            parts = [p.strip() for p in text.split(",") if p.strip()]
            if parts:
                if want == "int":
                    try:
                        return [int(p) for p in parts]
                    except ValueError:
                        return value
                return parts

    return value


def _coerce_list_str(value: Any) -> Any:
    return _coerce_list_like(value, want="str")


def _coerce_list_int(value: Any) -> Any:
    return _coerce_list_like(value, want="int")


_JsonListStr = Annotated[Optional[List[str]], BeforeValidator(_coerce_list_str)]
_JsonListInt = Annotated[Optional[List[int]], BeforeValidator(_coerce_list_int)]

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenclaw.agentserver.tools.todo_toolkits import TodoToolkit


_REQUIRED_BY_ACTION: dict[str, tuple[str, ...]] = {
    "create": ("tasks",),
    "complete": ("idx",),
    "complete_batch": ("indices",),
    "insert": ("idx", "tasks"),
    "remove": ("idx",),
    "list": (),
}

_FIELD_HINTS: dict[str, str] = {
    "tasks": 'list[str], e.g. ["step 1", "step 2"]',
    "indices": "list[int], 1-based, strictly ascending and contiguous",
    "idx": "int >= 1 (1-based index)",
}


class SkillStepInput(BaseModel):
    """Flat input schema for the unified ``skill_step`` tool.

    A single object schema is exposed to the LLM (rather than a
    discriminated union of six branch schemas) so that every field's
    type — notably ``tasks: list[str]`` and ``indices: list[int]`` —
    is visible at the top level of the tool's JSON schema. Per-action
    required-field rules are enforced by :meth:`_check_required`, which
    produces error messages that name the action, missing field, and
    expected shape.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "complete", "complete_batch", "insert", "remove", "list"] = Field(
        ...,
        description=(
            "Operation selector. Required fields by action: "
            'create -> tasks (list[str]); '
            "complete -> idx (int); "
            "complete_batch -> indices (list[int]), optional results (list[str]); "
            "insert -> idx (int), tasks (list[str]); "
            "remove -> idx (int); "
            "list -> (no extra fields). "
            "IMPORTANT: pass arrays as native JSON arrays, never as quoted JSON strings."
        ),
    )
    tasks: _JsonListStr = Field(
        default=None,
        min_length=1,
        description=(
            "Step descriptions. REQUIRED list[str] when action is 'create' or "
            "'insert'. Must be a JSON array of strings, not a single string. "
            'GOOD: tasks=["step 1","step 2"]; BAD: tasks="[\"step 1\",\"step 2\"]".'
        ),
    )
    idx: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "1-based step index. REQUIRED int when action is 'complete', "
            "'insert', or 'remove'."
        ),
    )
    result: str = Field(
        default="",
        description="Brief outcome string for action='complete'. Optional.",
    )
    indices: _JsonListInt = Field(
        default=None,
        min_length=1,
        description=(
            "1-based completed-step indices for action='complete_batch'; must "
            "be strictly ascending, contiguous, and start at the first open "
            "step. REQUIRED list[int] for that action. "
            'GOOD: indices=[1,2,3]; BAD: indices="[1,2,3]".'
        ),
    )
    results: _JsonListStr = Field(
        default=None,
        description=(
            "Per-step brief outcomes for action='complete_batch', aligned 1:1 "
            "with indices. Optional. "
            'GOOD: results=["ok 1","ok 2"]; BAD: results="[\"ok 1\",\"ok 2\"]".'
        ),
    )

    @model_validator(mode="after")
    def _check_required(self) -> "SkillStepInput":
        missing: list[str] = []
        for name in _REQUIRED_BY_ACTION.get(self.action, ()):
            if getattr(self, name) is None:
                missing.append(name)
        if missing:
            details = ", ".join(f"{n} ({_FIELD_HINTS.get(n, 'value')})" for n in missing)
            raise ValueError(
                f"skill_step(action={self.action!r}) is missing required field(s): {details}. "
                f"Pass them as native JSON values, not as JSON-encoded strings."
            )
        return self


_BACKEND_NAME_REWRITES = {
    "skill_step_complete_batch": 'skill_step(action="complete_batch")',
    "skill_step_complete": 'skill_step(action="complete")',
    "skill_step_create": 'skill_step(action="create")',
    "skill_step_insert": 'skill_step(action="insert")',
    "skill_step_remove": 'skill_step(action="remove")',
    "skill_step_list": 'skill_step(action="list")',
}


class SkillStepToolkit(TodoToolkit):
    """Skill step toolkit with a unified ``skill_step`` facade tool.

    Inherits all storage / locking / TodoOpResult publishing from
    :class:`TodoToolkit`, only changing the persisted filename and tool
    prefix. Overrides :meth:`get_tools` to expose a single ``skill_step``
    tool instead of one tool per action, so the agent sees one tool card
    rather than six.

    The instance methods (``todo_create`` / ``todo_complete`` / etc.) are
    kept available because :class:`SkillComplianceRail`, tests, and a few
    other call sites use them directly to seed or inspect the on-disk
    plan; only the LLM-facing tool surface is collapsed.
    """

    TODO_FILENAME: ClassVar[str] = "skill_step.md"
    TOOL_PREFIX: ClassVar[str] = "skill_step"
    EXPOSE_START: ClassVar[bool] = False
    EXPOSE_COMPLETE_BATCH: ClassVar[bool] = True

    TOOL_NAME: ClassVar[str] = "skill_step"

    async def skill_step(self, **inputs: Any) -> str:
        """Dispatch a skill step action to the underlying backend tool."""
        action = inputs.get("action")
        backend_tool_name, backend_inputs = self._build_backend_call(action, inputs)
        for tool in super().get_tools():
            if tool.card.name == backend_tool_name:
                result = await tool.invoke(
                    backend_inputs,
                    skip_none_value=True,
                    skip_inputs_validate=False,
                )
                return self._rewrite_backend_tool_names(str(result))
        return f"Error: backend tool {backend_tool_name!r} is unavailable."

    @staticmethod
    def _build_backend_call(
        action: Any, inputs: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        match action:
            case "create":
                return "skill_step_create", {"tasks": inputs["tasks"]}
            case "complete":
                return "skill_step_complete", {
                    "idx": inputs["idx"],
                    "result": inputs.get("result", ""),
                }
            case "complete_batch":
                backend_inputs: dict[str, Any] = {"indices": inputs["indices"]}
                if inputs.get("results") is not None:
                    backend_inputs["results"] = inputs["results"]
                return "skill_step_complete_batch", backend_inputs
            case "insert":
                return "skill_step_insert", {
                    "idx": inputs["idx"],
                    "tasks": inputs["tasks"],
                }
            case "remove":
                return "skill_step_remove", {"idx": inputs["idx"]}
            case "list":
                return "skill_step_list", {}
            case _:
                return "skill_step_unknown", {}

    @staticmethod
    def _rewrite_backend_tool_names(message: str) -> str:
        for old, new in _BACKEND_NAME_REWRITES.items():
            message = message.replace(old, new)
        return message

    def get_tools(self) -> List[Tool]:
        card = ToolCard(
            name=self.TOOL_NAME,
            description=(
                "Track SKILL execution steps. Use action=create, complete, "
                "complete_batch, insert, remove, or list. This is the only "
                "tool for SKILL step planning and progress updates."
            ),
            input_params=SkillStepInput,
        )
        return [LocalFunction(card=card, func=self.skill_step)]


__all__ = ["SkillStepInput", "SkillStepToolkit"]
