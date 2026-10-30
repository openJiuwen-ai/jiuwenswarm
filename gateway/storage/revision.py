# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""轮询表的乐观锁：写入必须带上读到的 revision。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


class ExpectedRevision:
    """更新条件，不是要写入的新版本。

    文件存储会丢掉它。数据库把它放进 ``WHERE revision = 读到的值``，
    并把该行 ``revision`` 加一。对不上则本次不写。
    """

    def __init__(self, value: int) -> None:
        self.value = int(value)

    @property
    def expected_revision(self) -> int:
        return self.value


class RevisionConflictError(RuntimeError):
    """读到的 revision 已经变了，这次更新没有落库。"""


def revision_of(row: dict[str, Any] | None) -> int | None:
    """行上的 revision；没有或无法转成整数时返回 None。"""
    if not row:
        return None
    raw = row.get("revision")
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, ExpectedRevision):
        return raw.expected_revision
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_expected_revision(value: Any) -> bool:
    return isinstance(value, ExpectedRevision)


async def retry_on_revision_conflict(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
) -> T:
    """冲突后重做 ``operation``。调用方每次都要重新读行再叠加本次改动。"""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    error: RevisionConflictError | None = None
    for _ in range(attempts):
        try:
            return await operation()
        except RevisionConflictError as exc:
            error = exc
    if error is None:
        raise RuntimeError("revision retry exhausted without a conflict error")
    raise error


__all__ = [
    "ExpectedRevision",
    "RevisionConflictError",
    "is_expected_revision",
    "retry_on_revision_conflict",
    "revision_of",
]
