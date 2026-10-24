# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Todo 协议兼容与能力检查；不反向依赖宿主装配模块。"""

from __future__ import annotations

import copy
import os
from typing import Any


def todo_v2_enabled() -> bool:
    return os.getenv("OPENJIUWEN_TODO_V2", "").lower() in {"1", "true", "yes"}


def decode_todo_document(raw: Any) -> tuple[list[dict], dict | None]:
    if isinstance(raw, list):
        return raw, None
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 2
        or not isinstance(raw.get("todos"), list)
    ):
        raise ValueError("Invalid Todo snapshot format")
    revision = raw.get("revision")
    if (
        not isinstance(revision, str)
        or not revision.isascii()
        or not revision.isdecimal()
        or str(int(revision)) != revision
    ):
        raise ValueError("Invalid Todo snapshot revision")
    if any(
        not isinstance(raw.get(key), str) or not raw[key]
        for key in ("store_id", "plan_id", "writer_epoch")
    ):
        raise ValueError("Invalid Todo snapshot identity")
    if not all(isinstance(item, dict) for item in raw["todos"]):
        raise ValueError("Invalid Todo entries")
    ids = set()
    for item in raw["todos"]:
        task_id = item.get("id")
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or task_id.strip() in ids
            or not isinstance(item.get("content"), str)
            or item.get("status")
            not in {"pending", "in_progress", "completed", "cancelled"}
            or (
                item.get("meta_data") is not None
                and not isinstance(item["meta_data"], dict)
            )
        ):
            raise ValueError("Invalid or duplicate Todo entry")
        ids.add(task_id.strip())
    return raw["todos"], {
        key: copy.deepcopy(value) for key, value in raw.items() if key != "todos"
    }


def todo_event_metadata(metadata: dict | None, view: str = "display") -> dict | None:
    if metadata is None:
        return None
    return {
        "schema_version": 2,
        "store_id": metadata["store_id"],
        "plan_id": metadata["plan_id"],
        "revision": metadata["revision"],
        "commit_id": metadata.get("last_commit_id", ""),
        "view": view,
        "writer_epoch": metadata["writer_epoch"],
        "legacy_plan": metadata.get("legacy_plan", False),
    }


async def prepare_todo_context(
    tool: Any, session: Any, *, new_execution: bool = False
) -> dict | None:
    if not todo_v2_enabled() or tool is None or session is None:
        return
    prepare = getattr(tool, "prepare_context", None)
    if not callable(prepare):
        raise TypeError(
            "OPENJIUWEN_TODO_V2 requires an SDK with Todo transaction support"
        )
    return await prepare(session, new_execution=new_execution)


def bind_todo_tool_context(tool: Any, ctx: Any) -> None:
    binding = ctx.extra.get("todo_call_context_v2")
    if (
        not todo_v2_enabled()
        or not isinstance(binding, dict)
        or tool is None
        or ctx.session is None
    ):
        return
    from openjiuwen.harness.tools.todo_store import bind_todo_context

    bind_todo_context(tool.todo_store(ctx.session.get_session_id()).key, binding)


def todo_plan_event_fields(tool: Any, session: Any) -> dict:
    if not todo_v2_enabled() or tool is None or session is None:
        return {}
    from openjiuwen.harness.tools.todo_store import session_todo_binding

    binding = session_todo_binding(
        session, tool.todo_store(session.get_session_id()).key
    )
    if not binding or not all(
        binding.get(key) for key in ("store_id", "plan_id", "revision")
    ):
        return {}
    return {
        "todo_scope": {
            key: binding[key]
            for key in (
                "store_id",
                "plan_id",
                "revision",
                "writer_epoch",
                "legacy_plan",
            )
            if key in binding
        }
    }
