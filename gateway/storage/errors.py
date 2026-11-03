# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 存储层错误类型。"""

from __future__ import annotations


class StorageUnavailableError(RuntimeError):
    """当前注册表不支持该 name，或底层存储不可用。"""


class DuplicateRecordError(ValueError):
    """create 时主键已存在。"""


__all__ = ["DuplicateRecordError", "StorageUnavailableError"]
