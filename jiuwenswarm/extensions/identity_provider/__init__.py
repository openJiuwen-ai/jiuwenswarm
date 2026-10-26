# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""身份信息扩展模块。

业务层实现 IdentityProviderBase 并在启动时 IdentityStore.register_provider() 注册。
AgentWebSocketServer._connection_handler() 连接建立时调 fetch_and_store()，
身份写入当前 context，IdentityFieldFilter 日志时读取。
"""
from __future__ import annotations
from jiuwenswarm.extensions.identity_provider.types import IdentityInfo
from jiuwenswarm.extensions.identity_provider.base import IdentityProviderBase
from jiuwenswarm.extensions.identity_provider.store import IdentityStore

__all__ = ["IdentityInfo", "IdentityProviderBase", "IdentityStore"]
