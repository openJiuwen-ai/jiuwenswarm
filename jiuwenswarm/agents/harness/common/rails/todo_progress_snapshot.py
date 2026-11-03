# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""todo 列表快照的读取、校验与异常字段转换（纯函数，供 TodoProgressRail 使用）。"""

from __future__ import annotations

import json
import os
import traceback
from collections import Counter
from typing import Any, Optional

from openjiuwen.harness.schema.task import TodoItem, TodoStatus
from openjiuwen.harness.tools import TodoListTool
from pydantic import ValidationError

VALID_STATUSES = frozenset(s.value for s in TodoStatus)


class TodoSnapshotError(Exception):
    """todo 文件读取或校验失败。消息里只放结构信息，不放任何字段值。"""

    def __init__(
        self,
        category: str,
        *,
        index: Optional[int] = None,
        field_name: Optional[str] = None,
        rule: Optional[str] = None,
        line: Optional[int] = None,
        column: Optional[int] = None,
    ) -> None:
        self.category = category
        self.detail = {
            k: v
            for k, v in {
                "index": index,
                "field": field_name,
                "rule": rule,
                "line": line,
                "column": column,
            }.items()
            if v is not None
        }
        super().__init__(category)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


async def read_todo_snapshot(tool: TodoListTool, session_id: str) -> list[TodoItem]:
    """读取会话的 todo 列表；文件不存在返回空列表，其他异常一律抛出。

    只有"文件不存在"算空列表；路径是目录、没有权限、查询属性失败等读不到的情况
    记 ``read_failed``，不能当作空列表，否则会把无法读取的执行算成"没有残留"。

    不经过 ``load_todos``：它把记录直接交给 ``TodoItem.from_dict``，缺 id 的记录
    每次都会补一个新的 uuid，同一文件会被误判为有变化。也不经过 ``tool.fs``：
    sys_operation 会把读到的文件全文打进 INFO 日志，本 rail 每次写入要额外读
    两次，会成倍放大 todo 正文落日志。路径与 todo 工具相同。
    """
    abs_path = os.path.abspath(tool._get_file_path(session_id))  # noqa: SLF001 与 todo 工具同一路径
    try:
        text = _read_text(abs_path)
    except FileNotFoundError:
        return []
    except OSError:
        raise TodoSnapshotError("read_failed") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TodoSnapshotError("invalid_json", line=exc.lineno, column=exc.colno) from None
    if not isinstance(data, list):
        raise TodoSnapshotError("invalid_record", rule="root_not_array")
    seen: set[str] = set()
    for index, record in enumerate(data):
        if not isinstance(record, dict):
            raise TodoSnapshotError("invalid_record", index=index, rule="not_object")
        todo_id = record.get("id")
        if not isinstance(todo_id, str) or not todo_id:
            raise TodoSnapshotError("invalid_record", index=index, field_name="id", rule="missing_id")
        if todo_id in seen:
            raise TodoSnapshotError("invalid_record", index=index, field_name="id", rule="duplicate_id")
        seen.add(todo_id)
        if record.get("status") not in VALID_STATUSES:
            raise TodoSnapshotError("invalid_record", index=index, field_name="status", rule="bad_status")
    return [TodoItem.from_dict(record) for record in data]


def snapshot_key(todos: list[TodoItem]) -> list[dict[str, Any]]:
    return [t.to_dict() for t in todos]


def status_counts(todos: list[TodoItem]) -> dict[str, int]:
    counts = Counter(t.status.value for t in todos)
    return {status: counts.get(status, 0) for status in sorted(VALID_STATUSES)}


def error_fields(exc: Exception) -> dict[str, Any]:
    """把异常转成白名单字段；不使用 str(exc)/repr(exc)/异常链，避免带出 todo 正文。"""
    if isinstance(exc, TodoSnapshotError):
        return {"category": exc.category, "exc_type": type(exc).__name__, "detail": exc.detail}
    if isinstance(exc, ValidationError):
        errors = [
            {"loc": list(e.get("loc", ())), "type": e.get("type")}
            for e in exc.errors(include_url=False, include_input=False)
        ]
        return {"category": "invalid_record", "exc_type": type(exc).__name__, "detail": {"errors": errors}}
    frames = [
        f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}"
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    return {"category": "unexpected", "exc_type": type(exc).__name__, "frames": frames}


__all__ = ["TodoSnapshotError", "VALID_STATUSES", "error_fields", "read_todo_snapshot", "snapshot_key", "status_counts"]
