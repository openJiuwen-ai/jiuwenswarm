from __future__ import annotations

from typing import Any

from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
)


class ReaSetupApplicationPlugin(ApplicationPluginExtension):
    """Expose REA setup RPCs through the Web channel."""

    plugin_id = "rea-setup"

    async def initialize(self, config: Any) -> None:
        del config

    async def shutdown(self) -> None:
        return None

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        del services
        from jiuwenswarm.gateway.channel_manager.web.rea_setup_handlers import (
            register_rea_setup_handlers,
        )

        register_rea_setup_handlers(channel)


async def register_extensions(registry: Any) -> list[ReaSetupApplicationPlugin]:
    extension = ReaSetupApplicationPlugin()
    registry.register_application_plugin(extension)
    return [extension]
