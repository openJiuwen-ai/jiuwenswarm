# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""身份信息提供者抽象基类。"""
from __future__ import annotations
from abc import ABC, abstractmethod
from jiuwenswarm.extensions.identity_provider.types import IdentityInfo


class IdentityProviderBase(ABC):
    """业务层继承并实现 fetch_identity()，启动前用 IdentityStore.register_provider() 注册。"""

    @abstractmethod
    async def fetch_identity(self) -> IdentityInfo:
        """获取身份信息（调 API / 读配置 / 查库）。字段可为 None。"""
        ...

    async def on_fetch_failed(self, error: Exception) -> IdentityInfo | None:
        """获取失败回调（可选覆盖）。返回 None 表示允许连接继续。"""
        return None
