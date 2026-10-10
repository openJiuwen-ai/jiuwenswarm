# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""名单合成算法（方案A，评审定稿）。

1. 任一来源 deny 命中 → DENY（全局一票否决）；
2. 固定分层 ``user_approval > user > builtin > cloud``，层内多条取最严
   （``ask > allow``）；
3. 全未命中 → 取**兜底档** ``defaults[mode][type]``（v3；白名单模式即
   ``defaults.*.type = deny``）；无兜底格 → ``None``（NO_MATCH，交现有权限
   引擎管线，**非放行**）。

兜底档**只**在"没有任何记录命中"时取值，因此**不参与**步骤 1 的一票否决——
这正是"若干 allow + 兜底 deny"能表达白名单、而不是退化成全拒的原因。
"""
from __future__ import annotations

from dataclasses import dataclass

from .composer import SecurityListComposer
from .matcher import match_record
from .models import SecurityListRecord, resolve_cell, resolve_default

#: 层内严格度（同层多条取大者）
_SEVERITY = {"allow": 0, "ask": 1, "deny": 2}

#: 固定分层顺序（前者优先）
_SOURCE_ORDER = ("user_approval", "user", "builtin", "cloud")


@dataclass
class Verdict:
    """合成裁决：动作 + 生效记录（审计/提示/记住语义用）+ 来源层。

    ``source="default"`` 时 ``record`` 为 ``None``——兜底档不是记录，没有可
    "记住"的对象（审批记住只对具体记录成立）。
    """

    action: str
    record: SecurityListRecord | None
    source: str


def evaluate(
    list_type: str,
    target: str,
    *,
    op: str,
    mode: str,
    composer: SecurityListComposer,
    session_id: str | None = None,
) -> Verdict | None:
    """对单个目标求值。

    返回 ``None`` 表示名单无表态（NO_MATCH），调用方交权限引擎继续。
    """
    records = composer.collect(list_type, session_id=session_id, mode=mode)
    matched: list[tuple[SecurityListRecord, str]] = []
    for rec in records:
        if not rec.enabled or rec.type != list_type:
            continue
        if not match_record(rec, target):
            continue
        action = resolve_cell(rec.cells, mode, op)
        if action is None:
            continue
        matched.append((rec, action))
    if not matched:
        # v3 兜底档：所有记录都不匹配时的取值（白名单＝defaults deny）。
        # 无兜底格 → None（NO_MATCH，交权限引擎继续求值）。
        default_action = resolve_default(composer.defaults(), mode, list_type)
        if default_action is None:
            return None
        return Verdict(default_action, None, "default")

    # 1. deny 全局一票否决（任一来源，取归一顺序首条）
    for rec, action in matched:
        if action == "deny":
            return Verdict("deny", rec, rec.source)

    # 2. 固定分层：user_approval > user > builtin > cloud；层内取最严
    for source in _SOURCE_ORDER:
        group = [(rec, action) for rec, action in matched if rec.source == source]
        if not group:
            continue
        rec, action = max(group, key=lambda item: _SEVERITY.get(item[1], 0))
        return Verdict(action, rec, source)
    return None
