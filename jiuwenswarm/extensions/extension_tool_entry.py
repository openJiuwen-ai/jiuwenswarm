# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""扩展本地工具登记项（供 ExtensionRegistry 存储）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ExtensionLocalToolEntry:
    """扩展注册的本地函数工具描述，在 Agent create_instance 时转为 openjiuwen LocalFunction。"""

    name: str
    description: str
    input_params: dict[str, Any]
    func: Callable[..., Any]
    source_id: str
