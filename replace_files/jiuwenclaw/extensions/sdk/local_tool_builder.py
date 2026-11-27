# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""在 jiuwenclaw 内部将扩展登记项构造为 openjiuwen Tool；扩展作者不应 import 本模块。"""

from __future__ import annotations

import re
from collections.abc import Sequence

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenclaw.extensions.extension_tool_entry import ExtensionLocalToolEntry


def _sanitize_id_part(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw.strip())[:64]
    return s or "ext"


def extension_tool_card_id(source_id: str, name: str) -> str:
    """生成稳定、可去重的 ToolCard.id。"""
    return f"jiuwenclaw.ext.{_sanitize_id_part(source_id)}.{_sanitize_id_part(name)}"


def make_tool(entry: ExtensionLocalToolEntry) -> Tool:
    """将单条扩展登记项构造为 LocalFunction。"""
    card = ToolCard(
        id=extension_tool_card_id(entry.source_id, entry.name),
        name=entry.name.strip(),
        description=entry.description,
        input_params=entry.input_params,
    )
    return LocalFunction(card=card, func=entry.func)


def make_extension_tools(entries: Sequence[ExtensionLocalToolEntry]) -> list[Tool]:
    """将已登记的扩展工具描述全部实例化为 openjiuwen Tool。"""
    return [make_tool(e) for e in entries]
