from typing import Any, Callable

from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from jiuwenswarm.extensions.callback_compat import unregister_callback_sync
from jiuwenswarm.extensions.extension_tool_entry import ExtensionLocalToolEntry
from jiuwenswarm.gateway import AgentServerClient
from jiuwenswarm.extensions.sdk.agent_server_client import AgentServerClientExtension
from jiuwenswarm.extensions.sdk.crypto_utility import CryptoUtility
from jiuwenswarm.extensions.sdk.telemetry_provider import TelemetryProviderExtension
from jiuwenswarm.extensions.sdk.third_agent import ThirdAgentExtension
from jiuwenswarm.extensions.sdk.skill_source import SkillSourceExtension
from jiuwenswarm.extensions.types import ExtensionConfig
from jiuwenswarm.common.security.base_crypto import CryptoProvider
from jiuwenswarm.gateway.routing.third_agent import ThirdAgent


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
        self._third_agent: ThirdAgentExtension | None = None
        self._telemetry_provider: TelemetryProviderExtension | None = None
        self._skill_source_extensions: dict[str, SkillSourceExtension] = {}
        self._rpc_handlers: dict[str, Callable] = {}
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

    def register_third_agent(self, extension: ThirdAgentExtension) -> None:
        self._third_agent = extension

    def register_telemetry_provider(
        self, extension: TelemetryProviderExtension
    ) -> None:
        self._telemetry_provider = extension

    def register_skill_source_extension(self, extension: SkillSourceExtension) -> None:
        """Register one trusted Skill Source provider factory.

        A configured ``source_id`` is bound to this factory later by AgentServer;
        extensions must not register tenant-specific provider instances here.
        """
        provider_type = str(extension.provider_type or "").strip()
        if not provider_type:
            raise ValueError("skill source provider_type is required")
        if provider_type in self._skill_source_extensions:
            raise ValueError(f"duplicate skill source provider_type: {provider_type}")
        self._skill_source_extensions[provider_type] = extension

    def get_skill_source_extension(self, provider_type: str) -> SkillSourceExtension | None:
        return self._skill_source_extensions.get(str(provider_type or "").strip())

    def list_skill_source_extensions(self) -> list[SkillSourceExtension]:
        return [self._skill_source_extensions[key] for key in sorted(self._skill_source_extensions)]

    def register_rpc_handler(self, method: str, handler: Callable) -> None:
        method_name = str(method or "").strip()
        if not method_name:
            raise ValueError("rpc method is required")
        if not callable(handler):
            raise ValueError(f"rpc handler for {method_name} must be callable")
        self._rpc_handlers[method_name] = handler

    def get_rpc_handler(self, method: str) -> Callable | None:
        return self._rpc_handlers.get(str(method or "").strip())

    def list_rpc_methods(self) -> list[str]:
        return sorted(self._rpc_handlers)

    def get_agent_server_client_extension(self) -> AgentServerClientExtension | None:
        return self._agent_server_client

    def get_agent_server_client(self) -> AgentServerClient | None:
        ext = self._agent_server_client
        return ext.get_client() if ext is not None else None

    def get_crypto_utility_extension(self) -> CryptoUtility | None:
        return self._crypto_tool

    def get_crypto_provider(self) -> CryptoProvider | None:
        ext = self._crypto_tool
        return ext.get_crypto() if ext is not None else None

    def get_third_agent_extension(self) -> ThirdAgentExtension | None:
        return self._third_agent

    def get_telemetry_provider_extension(
        self,
    ) -> TelemetryProviderExtension | None:
        return self._telemetry_provider

    def get_third_agent(self) -> ThirdAgent | None:
        """Return registered ThirdAgent, or None when no extension registered."""
        ext = self._third_agent
        return ext.get_third_agent() if ext is not None else None

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
