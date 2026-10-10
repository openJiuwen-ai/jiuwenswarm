# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：扩展 SDK 基类已迁 ``gateway_protocol.sdk.base``。"""

from __future__ import annotations

from gateway_protocol.sdk.base import (  # noqa: F401
    MANIFEST_FILENAME,
    BaseExtension,
)

__all__ = ["MANIFEST_FILENAME", "BaseExtension"]
