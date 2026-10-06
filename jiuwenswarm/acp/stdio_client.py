# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""ACP stdio 客户端实现已下沉 ``jiuwenswarm.common.acp``。

此处 re-export 保持既有 import 路径兼容（console script ``jiuwenswarm-acp-chat``
等仍引用本路径）。
"""

from __future__ import annotations

from jiuwenswarm.common.acp.stdio_client import AcpStdioClient

__all__ = ["AcpStdioClient"]