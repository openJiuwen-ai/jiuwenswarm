# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：扩展通用数据类型已迁 ``gateway_protocol.types``。

ExtensionConfig / ExtensionMetadata 为两仓扩展框架共用的纯 dataclass 契约；
本文件属扩展框架通用模块（随 gateway 仓迁移整体退役），当前保留旧 import
路径供消费方过渡。
"""

from __future__ import annotations

from gateway_protocol.types import (  # noqa: F401
    ExtensionConfig,
    ExtensionMetadata,
)

__all__ = ["ExtensionConfig", "ExtensionMetadata"]
