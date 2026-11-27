# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from typing import TYPE_CHECKING, Any, Callable, Type

from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from jiuwenclaw.extensions.callback_compat import unregister_callback_sync
from jiuwenclaw.extensions.extension_tool_entry import ExtensionLocalToolEntry
from jiuwenclaw.extensions.sdk.agent_server_client import AgentServerClientExtension
from jiuwenclaw.extensions.sdk.crypto_utility import CryptoUtility
from jiuwenclaw.extensions.sdk.llm_base_model_client import LlmBaseModelClient, create_delegating_client
from jiuwenclaw.extensions.sdk.telemetry_provider import TelemetryProviderExtension
from jiuwenclaw.extensions.types import ExtensionConfig

if TYPE_CHECKING:
    from jiuwenclaw.gateway.agent_client import AgentServerClient
    from jiuwenclaw.security.base_crypto import CryptoProvider


class ExtensionRegistry:
    _instance: "ExtensionRegistry | None" = None

    def __init__(
        self,
        callback_framework: AsyncCallbackFramework,
        config: dict[str, Any],
        logger: Any,
    ):
        self._agent_server_client: AgentServerClientExtension | None = None
        self._crypto_tool: CryptoUtility | None = None
        self._telemetry_provider: TelemetryProviderExtension | None = None
        self.callback_framework = callback_framework
        self._config = ExtensionConfig(config=config, logger=logger)
        self._extension_local_tool_entries: list[ExtensionLocalToolEntry] = []

    @classmethod
    def get_instance(cls) -> "ExtensionRegistry":
        if cls._instance is None:
            raise RuntimeError("ExtensionRegistry 尚未初始化，请先调用 create_instance()")
        return cls._instance

    def update_config(self, full_config) -> None:
        self._config.config = full_config

    def get_config(self):
        return self._config.config

    @classmethod
    def create_instance(
        cls,
        callback_framework: AsyncCallbackFramework,
        config: dict[str, Any],
        logger: Any,
    ) -> "ExtensionRegistry":
        if cls._instance is not None:
            logger.warning("ExtensionRegistry 已初始化，将返回已存在实例")
            return cls._instance
        cls._instance = cls(
            callback_framework=callback_framework,
            config=config,
            logger=logger,
        )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def register_agent_server_client(self, extension: AgentServerClientExtension) -> None:
        self._agent_server_client = extension

    def register_crypto_utility(self, extension: CryptoUtility) -> None:
        self._crypto_tool = extension

    def register_telemetry_provider(self, extension: TelemetryProviderExtension) -> None:
        self._telemetry_provider = extension

    def get_agent_server_client_extension(self) -> AgentServerClientExtension | None:
        return self._agent_server_client

    def get_agent_server_client(self) -> "AgentServerClient | None":
        ext = self._agent_server_client
        if ext is None:
            return None
        # 延迟导入以避免循环导入
        from jiuwenclaw.gateway.agent_client import AgentServerClient
        client = ext.get_client()
        return client if isinstance(client, AgentServerClient) else None

    def get_crypto_utility_extension(self) -> CryptoUtility | None:
        return self._crypto_tool

    def get_telemetry_provider_extension(self) -> TelemetryProviderExtension | None:
        return self._telemetry_provider

    def get_crypto_provider(self) -> "CryptoProvider | None":
        ext = self._crypto_tool
        return ext.get_crypto() if ext is not None else None

    @property
    def extension_local_tool_entries(self) -> list[ExtensionLocalToolEntry]:
        return self._extension_local_tool_entries

    def register_tool(
        self,
        name: str,
        description: str,
        input_params: dict[str, Any],
        func: Callable[..., Any],
        *,
        source_id: str = "extension",
    ) -> None:
        """登记扩展本地工具

        Args:
            name: 工具名（与内置工具冲突时将在 create_instance 合并阶段跳过并打日志）。
            description: 工具说明。
            input_params: 与 ToolCard 一致的入参 schema 字典。
            func: 同步调用实现；返回值需可被框架序列化为工具结果。
            source_id: 扩展标识，用于生成稳定 ToolCard.id 与日志。
        """
        n = (name or "").strip()
        if not n:
            raise ValueError("register_tool: name must be non-empty")
        sid = (source_id or "").strip() or "extension"
        if not isinstance(input_params, dict):
            raise TypeError("register_tool: input_params must be a dict")
        if not callable(func):
            raise TypeError("register_tool: func must be callable")
        self._extension_local_tool_entries.append(
            ExtensionLocalToolEntry(
                name=n,
                description=description or "",
                input_params=input_params,
                func=func,
                source_id=sid,
            )
        )

    def register_model(self, name: str, model_cls: Type[LlmBaseModelClient]) -> None:
        if not name:
            self._config.logger.warning("register model failed: name must be non-empty")
            return
        create_delegating_client(
            client_name=name,
            llm_client_class=model_cls,
        )
        self._config.logger.info(
            "register model success: %s -> %s", name, model_cls
        )

    def register(
        self,
        event: str,
        handler: Callable,
        priority: int = 100,
        **kwargs,
    ) -> None:
        self.callback_framework.register_sync(event, handler, priority=priority, **kwargs)

    def unregister(self, event: str, handler: Callable | None = None) -> None:
        unregister_callback_sync(self.callback_framework, event, handler)

    async def trigger(self, event: str, context: Any | None = None, **kwargs: Any) -> None:
        """触发事件。约定由调用方传入的 context 承载回调副作用"""
        if context is None and not kwargs:
            await self.callback_framework.trigger(event)
        elif context is not None:
            await self.callback_framework.trigger(event, context, **kwargs)
        else:
            await self.callback_framework.trigger(event, **kwargs)

    @property
    def config(self) -> ExtensionConfig:
        return self._config