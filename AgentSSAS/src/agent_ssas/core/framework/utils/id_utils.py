# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS ID 生成工具。

提供 UUID 生成等公共能力,供数据预处理、事件构建等模块使用。
"""

from __future__ import annotations

import uuid


def new_uuid() -> str:
    """生成新的 UUID 字符串。

    返回标准格式的 UUID 字符串(带连字符,小写)。
    用于生成事件唯一标识 event_id 等场景。

    Returns:
        UUID 字符串,如 "a1b2c3d4-e5f6-7890-abcd-ef1234567890"。
    """
    return str(uuid.uuid4())


def new_event_id() -> str:
    """生成新的事件唯一标识。

    事件唯一标识采用 UUID 格式,用于 UnifiedEvent.event_id 字段。

    Returns:
        事件唯一标识 UUID 字符串。
    """
    return new_uuid()


def new_data_id() -> str:
    """生成新的数据节点唯一标识。

    数据节点唯一标识采用 UUID 格式,用于 DataNode.data_id 字段。

    Returns:
        数据节点唯一标识 UUID 字符串。
    """
    return new_uuid()
