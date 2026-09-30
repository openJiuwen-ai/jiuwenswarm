# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一视图合成（SecurityListComposer）。

归一顺序（设计文档 4.2/4.3 求值表）：
物理 user 记录 ∪ cloud 记录（``enabled=True``）→ builtin 投影 →
审批投影（当前会话）。

沙箱运行时副本（``windows-policy.runtime.yaml``）**不进运行时收集**：它是
``sandbox.files.set`` / ``sandbox.network.set`` 与 FileGuard 同步的活配置
（``server/sandbox_policy_render.py`` 直接读写），旧内容由
:func:`store.migrate_sandbox_copy_once` 一次性搬进 ``security_lists.user``。
运行时再投影一次会与沙箱侧双重判定，且"迁移清空副本"会反过来抹掉沙箱配置。

模式隔离不做在 collect 层：审批/记录的 ``mode`` 已映射进 cells 格子，
由 :func:`resolve_cell` 在求值时按当前模式回退解析（模式特化格仅本模式
命中，通用格对所有未特化模式命中）。

**让位**：物理记录（user/cloud）已占用的操作对象，legacy 投影（
``permissions.file_guard.paths`` / ``permissions.net_guard.urls``）不再产出
同名记录——安全中心的写面收敛后，同一对象只能有一个可编辑真源。
``builtin`` 投影**不参与让位**：内置 deny 不可被用户记录的 allow 顶掉
（合成算法步骤 1 的拒绝优先也必须建立在"内置条目确实在候选里"之上）。
"""
from __future__ import annotations

from . import store
from .models import SecurityListRecord
from .normalize import (
    project_approvals,
    project_builtin,
    project_net_guard,
)


class SecurityListComposer:
    """每次求值实时归一（config stamp 缓存保证读路径廉价）。"""

    def defaults(self) -> dict[str, Any]:
        """当前兜底档（``security_lists.defaults``，v3）。

        缺省/空映射 = 无兜底（``evaluate`` 回落到 NO_MATCH 存量语义）。
        段损坏原样上抛（rail fail-closed）。
        """
        return dict(store.get_security_lists().get("defaults") or {})

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
        # 物理记录已接管的操作对象：legacy 投影让位（写面收敛，见 normalize 各投影）
        occupied = frozenset(
            (r.type, r.pattern, r.match) for r in records
        )
        records += project_builtin()
        records += project_approvals(session_id, occupied=occupied)
        records += project_net_guard(occupied=occupied)
        if list_type is not None:
            records = [r for r in records if r.type == list_type]
        return records
