"""Extension SDK for a custom config provider."""

from abc import abstractmethod

from jiuwenswarm.common.config_provider import ConfigProvider
from jiuwenswarm.extensions.sdk.base import BaseExtension
from jiuwenswarm.extensions.types import ExtensionConfig


class ConfigProviderExtension(BaseExtension):
    @abstractmethod
    def get_config_provider(self) -> ConfigProvider:
        raise NotImplementedError

    async def initialize(self, config: ExtensionConfig) -> None:
        from jiuwenswarm.extensions.registry import ExtensionRegistry

        ExtensionRegistry.get_instance().register_config_provider(self)

    async def shutdown(self) -> None:
        return None
