from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from jiuwenswarm.common.security import base_crypto
from jiuwenswarm.common.security.base_crypto import CryptoProvider
from jiuwenswarm.extensions.callback_compat import unregister_callback_sync
from jiuwenswarm.extensions.sdk.crypto_utility import CryptoUtility
from jiuwenswarm.extensions.types import ExtensionConfig

if TYPE_CHECKING:
    from jiuwenswarm.extensions.sdk.agent_server_client import (
        AgentServerClientExtension,
    )
    from jiuwenswarm.extensions.sdk.application_plugin import (
        ApplicationPluginExtension,
    )
    from jiuwenswarm.extensions.sdk.third_agent import ThirdAgentExtension
    from jiuwenswarm.common.client.agent_client import AgentServerClient
    from jiuwenswarm.common.client.third_agent import ThirdAgent
else:
    # Keep runtime type-hint introspection valid without importing Gateway and
    # transport adapters into a Runtime-direct process.
    AgentServerClientExtension = Any
    ApplicationPluginExtension = Any
    ThirdAgentExtension = Any
    AgentServerClient = Any
    ThirdAgent = Any


class _RegistryCryptoBridge:
    """把 ExtensionRegistry 的 crypto 扩展桥接到 ``common.security.base_crypto`` 全局钩子。

    common（如 config 的 api_key 解密）不能 import 扩展框架（gateway 拆分约束），
    改经 base_crypto 钩子获取 provider。按调用时点解析 registry 实例与 crypto
    扩展（未就绪时原样返回/由调用方回退原文），与原 ``sys.modules`` 软查找的
    惰性语义一致。
    """
    @staticmethod
    def encrypt(plaintext: str, **kwargs) -> str:
        ext = ExtensionRegistry.get_instance().get_crypto_utility_extension()
        provider = ext.get_crypto() if ext is not None else None
        return provider.encrypt(plaintext, **kwargs) if provider is not None else plaintext

    @staticmethod
    def decrypt(ciphertext: str, **kwargs) -> str:
        ext = ExtensionRegistry.get_instance().get_crypto_utility_extension()
        provider = ext.get_crypto() if ext is not None else None
        return provider.decrypt(ciphertext, **kwargs) if provider is not None else ciphertext


class _ApplicationPluginChannel:
    def __init__(self, channel: Any, plugin: ApplicationPluginExtension):
        self._channel = channel
        self._plugin = plugin

    def __getattr__(self, name: str) -> Any:
        return getattr(self._channel, name)

    def register_method(
        self,
        method: str,
        handler: Callable,
        *,
        local_only: bool = False,
        available_when_disabled: bool = False,
    ) -> None:
        async def enabled_handler(ws, req_id, params, session_id):  # noqa: ANN001
            if not available_when_disabled and not self._plugin.is_enabled():
                await self._channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error=f"application plugin {self._plugin.plugin_id} is disabled",
                    code="APPLICATION_PLUGIN_DISABLED",
                )
                return
            await handler(ws, req_id, params, session_id)

        self._channel.register_method(method, enabled_handler, local_only=local_only)


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
        self._application_plugins: dict[str, ApplicationPluginExtension] = {}
        self.callback_framework = callback_framework
        self._config = ExtensionConfig(config=config, logger=logger)

    @classmethod
    def get_instance(cls) -> "ExtensionRegistry":
        if cls._instance is None:
            raise RuntimeError("ExtensionRegistry 尚未初始化，请先调用 create_instance()")
        return cls._instance

    @classmethod
    def create_instance(
        cls,
        callback_framework: AsyncCallbackFramework,
        config: dict[str, Any],
        logger: Any,
    ) -> "ExtensionRegistry":
        if cls._instance is not None:
            raise RuntimeError("ExtensionRegistry 已初始化，请勿重复调用 create_instance()")
        cls._instance = cls(
            callback_framework=callback_framework,
            config=config,
            logger=logger,
        )
        # 桥接 crypto 扩展到 common 全局钩子（common/config 的 api_key 解密入口）。
        base_crypto.set_crypto_provider(_RegistryCryptoBridge())
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None
        # registry 实例重置后，common 侧不得再持有旧实例的 crypto 桥接
        base_crypto.set_crypto_provider(None)

    def register_agent_server_client(self, extension: AgentServerClientExtension) -> None:
        self._agent_server_client = extension

    def register_crypto_utility(self, extension: CryptoUtility) -> None:
        self._crypto_tool = extension

    def register_third_agent(self, extension: ThirdAgentExtension) -> None:
        self._third_agent = extension

    def register_application_plugin(
        self,
        extension: ApplicationPluginExtension,
    ) -> None:
        plugin_id = str(extension.plugin_id or "").strip()
        if not plugin_id:
            plugin_id = str(extension.metadata.id or "").strip()
        if not plugin_id:
            raise ValueError("application plugin id must not be empty")
        if plugin_id in self._application_plugins:
            raise ValueError(f"application plugin already registered: {plugin_id}")
        self._application_plugins[plugin_id] = extension

    def get_application_plugins(self) -> tuple[ApplicationPluginExtension, ...]:
        return tuple(self._application_plugins.values())

    def get_application_plugin(
        self,
        plugin_id: str,
    ) -> ApplicationPluginExtension | None:
        return self._application_plugins.get(plugin_id)

    def bind_application_plugins(
        self,
        channel: Any,
        *,
        agent_client: Any = None,
        media_attachment_normalizer: Callable[[dict[str, Any], str | None], None]
        | None = None,
    ) -> None:
        from jiuwenswarm.extensions.sdk.application_plugin import (
            ApplicationPluginServices,
        )

        services = ApplicationPluginServices(
            agent_client=agent_client,
            media_attachment_normalizer=media_attachment_normalizer,
        )
        for plugin in self.get_application_plugins():
            plugin.bind_web_channel(_ApplicationPluginChannel(channel, plugin), services)
        channel.application_plugin_registry = self

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

    def get_third_agent(self) -> ThirdAgent | None:
        """Return registered ThirdAgent, or None when no extension registered."""
        ext = self._third_agent
        return ext.get_third_agent() if ext is not None else None

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
