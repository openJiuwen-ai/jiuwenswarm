# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved

"""会话身份口径（remote(PG) 模式）。

pod 侧数据目录 ``workspace_{key}/`` 由请求身份三元组（user_id + routing 的
group_id/bot_id）决定，切换任何一个维度都会落到新目录；web 库 sessions 行
必须携带同样的身份维度并按同样口径过滤，否则切换 group/bot 后侧栏会出现
"列表在、内容不在"的幽灵会话，且同一 user 在不同租户下的会话会互相串号。

过滤语义（``scope_matches``）：
- 行身份列非空 → 必须与查询身份相等（严格隔离，与 pod 目录口径 1:1）；
- 行身份列为空（存量行/身份缺失行）→ 通配，任何查询可见（升级不丢数据）；
- 查询未携带身份 → 退化为仅按 user 过滤（兼容未携带路由身份的内部调用）。

写入语义：身份列首次写入后不再被后续消息更新（first-writer-wins）；
写入方缺身份时留空（NULL），由回填脚本按 pod metadata 补齐。
"""

from __future__ import annotations

from typing import Any

IDENTITY_COLUMNS = ("group_id", "bot_id")


def normalize_identity_value(value: Any) -> str | None:
    """身份值归一化：非空字符串去除空白，空值归 None（= 通配语义）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def scope_matches(
    row: dict[str, Any],
    group_id: str | None,
    bot_id: str | None,
) -> bool:
    """按身份口径判断一行 sessions 是否对当前查询可见（语义见模块 docstring）。"""
    q_group = normalize_identity_value(group_id)
    q_bot = normalize_identity_value(bot_id)
    if not q_group and not q_bot:
        return True
    r_group = normalize_identity_value(row.get("group_id"))
    r_bot = normalize_identity_value(row.get("bot_id"))
    if r_group and q_group and r_group != q_group:
        return False
    if r_bot and q_bot and r_bot != q_bot:
        return False
    return True


__all__ = [
    "IDENTITY_COLUMNS",
    "normalize_identity_value",
    "scope_matches",
]
