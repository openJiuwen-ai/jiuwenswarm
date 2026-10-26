# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""身份信息数据结构定义。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IdentityInfo:
    """身份信息（user_id / domain_id / app_id），供日志系统自动携带。"""

    user_id: str | None = None
    domain_id: str | None = None
    app_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典，仅含非 None 字段。"""
        result: dict[str, Any] = {}
        if self.user_id is not None:
            result["user_id"] = self.user_id
        if self.domain_id is not None:
            result["domain_id"] = self.domain_id
        if self.app_id is not None:
            result["app_id"] = self.app_id
        if self.extra:
            result["extra"] = self.extra
        return result
