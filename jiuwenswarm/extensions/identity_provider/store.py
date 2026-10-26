# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""身份信息存储（基于 contextvars，并发安全）。

enterprise_dev 原为进程级单例；dev-stable 的 agent server 同进程并发多 WS 连接，
单例会让 user_id 跨连接串台。改为 contextvar：每连接在自己 context 中 set，
IdentityFieldFilter 在日志时 get。asyncio.create_task 自动 copy_context，
故 _connection_handler 内 set 的身份会传播到其派生的 _handle_message 任务。
provider 为进程级（业务层启动时注册一次，无并发态）。
"""
from __future__ import annotations
import contextvars
import logging
from typing import Optional

from jiuwenswarm.extensions.identity_provider.types import IdentityInfo
from jiuwenswarm.extensions.identity_provider.base import IdentityProviderBase

logger = logging.getLogger(__name__)

_identity_var: "contextvars.ContextVar[Optional[IdentityInfo]]" = contextvars.ContextVar(
    "jiuwenswarm_identity", default=None
)
_fetched_var: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "jiuwenswarm_identity_fetched", default=False
)
_provider: "Optional[IdentityProviderBase]" = None


class IdentityStore:
    """身份信息存储（静态方法，基于 contextvars，无单例实例）。

    用法：
        IdentityStore.register_provider(MyProvider())   # 启动时
        token = await IdentityStore.fetch_and_store()    # 连接建立时（返回 token 或 None）
        identity = IdentityStore.get_identity()          # 日志 filter 读取
        IdentityStore.clear(token)                       # 连接断开时
    """

    @classmethod
    def register_provider(cls, provider: IdentityProviderBase) -> None:
        global _provider
        _provider = provider
        logger.info("[IdentityStore] 已注册身份提供者: %s", type(provider).__name__)

    @classmethod
    def unregister_provider(cls) -> None:
        global _provider
        _provider = None
        _identity_var.set(None)
        _fetched_var.set(False)

    @staticmethod
    def get_identity() -> Optional[IdentityInfo]:
        return _identity_var.get()

    @staticmethod
    def get_provider() -> Optional[IdentityProviderBase]:
        return _provider

    @staticmethod
    def is_fetched() -> bool:
        return _fetched_var.get()

    @staticmethod
    def set_identity(info: Optional[IdentityInfo]) -> contextvars.Token:
        """显式设置身份，返回 token 供 clear 用。"""
        return _identity_var.set(info)

    @staticmethod
    def clear(token: contextvars.Token) -> None:
        """在 set 的同一 context 中重置身份。token 不匹配时静默忽略。"""
        try:
            _identity_var.reset(token)
        except (ValueError, LookupError, TypeError):
            pass

    @staticmethod
    def set_test_state(identity: Optional[IdentityInfo] = None, fetched: bool = True) -> None:
        """测试用：直接设置当前 context 的身份/标记。"""
        _identity_var.set(identity)
        _fetched_var.set(fetched)

    @classmethod
    async def fetch_and_store(cls) -> Optional[contextvars.Token]:
        """调 provider 获取身份并写入当前 context。

        无 provider 或失败时返回 None（身份保持 null，连接继续）。
        成功时返回 contextvars.Token，供连接断开 clear 用。
        """
        global _provider
        if _provider is None:
            logger.debug("[IdentityStore] 未注册身份提供者，跳过获取")
            _fetched_var.set(True)
            return None
        try:
            identity = await _provider.fetch_identity()
            token = _identity_var.set(identity)
            _fetched_var.set(True)
            logger.info(
                "[IdentityStore] 身份信息已获取: user_id=%s domain_id=%s app_id=%s",
                identity.user_id, identity.domain_id, identity.app_id,
            )
            return token
        except Exception as e:
            logger.warning("[IdentityStore] 身份获取失败: %s", e)
            _fetched_var.set(True)
            fallback = await _provider.on_fetch_failed(e)
            fallback = None
            try:
                fallback = await _provider.on_fetch_failed(e)
            except Exception as fe:
                logger.warning("[IdentityStore] fallback 获取失败: %s", fe)
            if fallback is not None:
                token = _identity_var.set(fallback)
                logger.info(
                    "[IdentityStore] 使用 fallback 身份: user_id=%s domain_id=%s app_id=%s",
                    fallback.user_id, fallback.domain_id, fallback.app_id,
                )
                return token
            return None
