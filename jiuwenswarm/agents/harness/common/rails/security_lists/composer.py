# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一视图合成（SecurityListComposer）。

归一顺序（设计文档 4.2/4.3 求值表）：
物理 user 记录 ∪ cloud 记录（``enabled=True``）→ builtin 投影 →
审批投影（当前会话）→ 沙箱副本过渡投影。

模式隔离不做在 collect 层：审批/记录的 ``mode`` 已映射进 cells 格子，
由 :func:`resolve_cell` 在求值时按当前模式回退解析（模式特化格仅本模式
命中，通用格对所有未特化模式命中）。
"""
from __future__ import annotations

from . import store
from .models import SecurityListRecord
from .normalize import (
    project_approvals,
    project_builtin,
    project_sandbox_runtime_copy,
)


class SecurityListComposer:
    """每次求值实时归一（config stamp 缓存保证读路径廉价）。"""

    def collect(
        self,
        list_type: str | None = None,
        *,
        session_id: str | None = None,
        mode: str = "*",
    ) -> list[SecurityListRecord]:
        """归一收集候选记录。

        :class:`SecurityListsCorruptedError` 原样上抛（rail fail-closed）。
        ``mode`` 仅透传语义（模式过滤在 resolve_cell），不改变收集范围。
        """
        lists = store.get_security_lists()
        records = [r for r in lists["user"] if r.enabled]
        records += [r for r in lists["cloud"]["records"] if r.enabled]
        records += project_builtin()
        records += project_approvals(session_id)
        records += project_sandbox_runtime_copy()
        if list_type is not None:
            records = [r for r in records if r.type == list_type]
        return records
