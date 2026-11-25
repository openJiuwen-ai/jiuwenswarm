# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""记忆工具名映射：stock 五件套 → flash 统一入口 ``memory(mode=…)``。

单一来源：``FlashMemoryRail``（记忆段正文改写）与 ``FlashRelayMergeRail``（relay
注入块的工具名改写）共用本表，避免两处各抄一份、改统一工具名时漏改。
"""

from __future__ import annotations

MEMORY_NAME_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("memory_search", "memory(mode=search)"),
    ("memory_get", "memory(mode=read)"),
    ("read_memory", "memory(mode=read)"),
    ("write_memory", "memory(mode=write)"),
    ("edit_memory", "memory(mode=edit)"),
)

__all__ = ["MEMORY_NAME_REPLACEMENTS"]