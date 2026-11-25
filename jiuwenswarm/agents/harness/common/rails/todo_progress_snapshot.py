# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""todo 列表快照的读取、校验与异常字段转换（纯函数，供 TodoProgressRail 使用）。"""

from __future__ import annotations

import json
import os
import traceback
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional

from openjiuwen.harness.schema.task import TodoItem, TodoStatus
from openjiuwen.harness.tools import TodoListTool
from pydantic import ValidationError

VALID_STATUSES = frozenset(s.value for s in TodoStatus)
_COMPLETED = TodoStatus.COMPLETED.value
_UNFINISHED = frozenset({TodoStatus.PENDING.value, TodoStatus.IN_PROGRESS.value})


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


def completed_regressions(before: Optional[list[dict]], after: Optional[list[dict]]) -> Optional[list[str]]:
    """写入前已 completed、写入后变回 pending/in_progress 的 id；任一快照不可用时返回 None。"""
    if before is None or after is None:
        return None
    completed = {item["id"] for item in before if item["status"] == _COMPLETED}
    return [item["id"] for item in after if item["id"] in completed and item["status"] in _UNFINISHED]


def completed_missing(before: Optional[list[dict]], after: Optional[list[dict]]) -> Optional[list[str]]:
    """写入前已 completed、写入后 id 不在清单中且清单里没有 content 完全相同条目的 id；任一快照不可用时返回 None。

    同内容换 id 的条目（含省略 id 由工具生成新 id）不算遗漏：按原 id 补回会让同一任务出现两次。
    """
    if before is None or after is None:
        return None
    ids = {item["id"] for item in after}
    contents = {item["content"] for item in after}
    return [item["id"] for item in before
            if item["status"] == _COMPLETED and item["id"] not in ids and item["content"] not in contents]


def _reuses_id(before: Optional[list[dict]], after: Optional[list[dict]]) -> bool:
    if before is None or after is None:
        return False
    return not {item["id"] for item in before}.isdisjoint(item["id"] for item in after)


def _completed_count(snapshot: Optional[list[dict]]) -> Optional[int]:
    return None if snapshot is None else sum(item["status"] == _COMPLETED for item in snapshot)


@dataclass(frozen=True)
class TodoReceiptChange:
    """回执需要展示的清单变化。"""

    regressed_ids: Optional[tuple[str, ...]] = None
    missing_items: Optional[tuple[tuple[str, str], ...]] = None


@dataclass(frozen=True)
class CompletionChange:
    """一次写入前后 completed 项的变化；对应快照不可用时字段为 None。

    missing_ids 只在整体重建（rebuild）时计算：todo_modify 的 delete 是逐项的明确意图，不算遗漏。
    """

    before: Optional[int]
    after: Optional[int]
    regressed_ids: Optional[list[str]]
    rebuild: bool = False
    missing_ids: Optional[list[str]] = None
    reuses_id: bool = False

    @classmethod
    def between(cls, before: Optional[list[dict]], after: Optional[list[dict]], *,
                rebuild: bool = False) -> "CompletionChange":
        return cls(_completed_count(before), _completed_count(after), completed_regressions(before, after),
                   rebuild=rebuild, missing_ids=completed_missing(before, after) if rebuild else None,
                   reuses_id=_reuses_id(before, after))

    @property
    def missing_to_notify(self) -> Optional[list[str]]:
        """回执中要提示的遗漏 id。新清单没有沿用任何旧 id 时视为开始新任务，不提示。"""
        return self.missing_ids if self.reuses_id else None

    def receipt_change(self, before: Optional[list[dict]]) -> TodoReceiptChange:
        missing_items = None
        missing_ids = self.missing_to_notify
        if missing_ids is not None:
            missing_set = set(missing_ids)
            missing_items = tuple(
                (item["id"], item["content"])
                for item in before or ()
                if item["id"] in missing_set
            )
        regressed_ids = None if self.regressed_ids is None else tuple(self.regressed_ids)
        return TodoReceiptChange(regressed_ids=regressed_ids, missing_items=missing_items)

    def log_fields(self) -> dict[str, Any]:
        regressed = None if self.regressed_ids is None else len(self.regressed_ids)
        fields = {"completed_before": self.before, "completed_after": self.after,
                  "completed_to_unfinished_count": regressed}
        if self.rebuild:
            fields["completed_missing_count"] = None if self.missing_ids is None else len(self.missing_ids)
        return fields


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


__all__ = ["CompletionChange", "TodoReceiptChange", "TodoSnapshotError", "VALID_STATUSES", "completed_missing", "completed_regressions",
           "error_fields", "read_todo_snapshot", "snapshot_key", "status_counts"]
