# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CC-aligned todo tool descriptions and schemas for code mode."""

from __future__ import annotations

from typing import Any

CODE_TODO_CREATE_DESCRIPTION_EN = """
Create a todo list for the current session. Scale the list to how complex the work actually is.

## When to skip (do the work directly)

- Single focused change: one bug, one function, one config tweak, or a short answer
- A few related edits with no real phase boundaries (rename, small refactor)
- You can finish in one continuous pass without losing track

## How many items

| Complexity | Items | Examples |
|------------|-------|----------|
| Medium | 2–3 | Greenfield app: backend, frontend/UI, verify end-to-end |
| Complex | 4–6 max | Multi-service feature, large refactor, many deliverables, unclear order |
| User asks for a plan | Match their structure | Still prefer outcomes over files |

Do NOT mirror the user's spec headings (Section 1, Section 2…) or project file list as separate todos.
Do NOT create one todo per file unless the user explicitly asks for file-level tracking.

## Granularity

- Each item = one outcome or phase (e.g. "Implement Flask API and SQLite", "Build Canvas game UI", "Verify server and score flow").
- Each task needs id, content, activeForm, and description; keep them brief.
- Include verification in the last milestone — do not add separate todos per curl, test, or check.

## Usage

Call once before substantive work. Prefer todo_create in parallel with the first write/bash when possible — avoid a todo-only first round.

{"tasks": [{"id": "backend", "content": "Implement Flask backend", "activeForm": "Implementing Flask backend", "description": "app.py, SQLite scores API, static routes"}, {"id": "verify", "content": "Verify end-to-end", "activeForm": "Verifying end-to-end", "description": "Run app, exercise APIs, confirm game loads"}]}
""".strip()

CODE_TODO_LIST_DESCRIPTION_EN = """
List all todos for the current session.

Use rarely — only when the plan is unclear, after a long interruption, or the user asks.
Do not call routinely between steps; use todo_modify to update progress instead.
""".strip()

CODE_TODO_GET_DESCRIPTION_EN = """
Get full details for one todo by id. Use only when you need fields not visible in recent todo_modify results.
""".strip()

CODE_TODO_MODIFY_DESCRIPTION_EN = """
Update todo items for the current session.

## Prefer efficiency

- Mark a milestone completed and start the next in the same response as the next write/bash/edit — parallel tool calls when independent.
- Avoid todo-only rounds: do not call todo_modify alone just to flip status unless wrapping up.
- Batch multiple updates in one call (e.g. complete backend + set frontend in_progress).

## Actions

- update: change status or fields — mark completed as soon as a milestone is done
- append: add follow-up milestones when scope genuinely expands (use sparingly; prefer 4–6 total items)
- cancel / delete: remove items that are no longer needed
- insert_after / insert_before: only for mid-plan scope changes, not file-by-file progress

To replace the entire plan, call todo_create instead of many inserts.

Do not create a new todo for each file written or each verification command.
""".strip()

_CODE_TODO_ITEM_PROPERTIES_EN: dict[str, Any] = {
    "id": {
        "type": "string",
        "description": "Short semantic task id (e.g. 'backend', 'frontend', 'verify'). Unique within the session.",
    },
    "content": {
        "type": "string",
        "description": "Brief outcome-based title (phase or deliverable, not a filename).",
    },
    "activeForm": {
        "type": "string",
        "description": "Present-tense spinner label; keep short.",
    },
    "description": {
        "type": "string",
        "description": "What this phase achieves; one or two sentences. May mention multiple files.",
    },
    "status": {
        "type": "string",
        "description": "Task status.",
        "enum": ["pending", "in_progress", "completed", "cancelled"],
    },
}


def get_code_todo_create_input_params() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": (
                    "Outcome-based milestones scaled to complexity: skip todos for trivial work; "
                    "2–3 for medium multi-phase work; 4–6 max for complex work. "
                    "Each task requires id, content, activeForm, and description."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _CODE_TODO_ITEM_PROPERTIES_EN["id"],
                        "content": _CODE_TODO_ITEM_PROPERTIES_EN["content"],
                        "activeForm": _CODE_TODO_ITEM_PROPERTIES_EN["activeForm"],
                        "description": _CODE_TODO_ITEM_PROPERTIES_EN["description"],
                    },
                    "required": ["id", "content", "activeForm", "description"],
                },
            },
        },
        "required": ["tasks"],
    }


def get_code_todo_modify_input_params() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: update, delete, cancel, append, insert_after, insert_before.",
            },
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task ids for delete or cancel.",
            },
            "todos": {
                "type": "array",
                "description": (
                    "Tasks to update or append. Prefer batching several status changes in one call."
                ),
                "items": {
                    "type": "object",
                    "properties": _CODE_TODO_ITEM_PROPERTIES_EN,
                },
            },
            "todo_data": {
                "type": "object",
                "description": "Target and items for insert_after / insert_before.",
            },
        },
        "required": ["action"],
    }


def get_code_todo_get_input_params() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": _CODE_TODO_ITEM_PROPERTIES_EN["id"],
        },
        "required": ["id"],
    }


CODE_TODO_TOOL_PROMPTS: dict[str, tuple[str, dict[str, Any] | None]] = {
    "todo_create": (CODE_TODO_CREATE_DESCRIPTION_EN, get_code_todo_create_input_params()),
    "todo_list": (CODE_TODO_LIST_DESCRIPTION_EN, {"type": "object", "properties": {}, "required": []}),
    "todo_get": (CODE_TODO_GET_DESCRIPTION_EN, get_code_todo_get_input_params()),
    "todo_modify": (CODE_TODO_MODIFY_DESCRIPTION_EN, get_code_todo_modify_input_params()),
}
