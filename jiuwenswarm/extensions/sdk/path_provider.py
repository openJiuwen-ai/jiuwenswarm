"""Extension SDK for a custom path provider."""

from abc import abstractmethod

from jiuwenswarm.common.path_provider import PathProvider
from jiuwenswarm.extensions.sdk.base import BaseExtension
from jiuwenswarm.extensions.types import ExtensionConfig


class PathProviderExtension(BaseExtension):
    @abstractmethod
    def get_path_provider(self) -> PathProvider:
        raise NotImplementedError

    async def initialize(self, config: ExtensionConfig) -> None:
        from jiuwenswarm.extensions.registry import ExtensionRegistry

        ExtensionRegistry.get_instance().register_path_provider(self)

    async def shutdown(self) -> None:
        return None
