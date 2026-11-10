"""Register Team meeting controls, private playback delivery, and the web UI."""

from __future__ import annotations

from typing import Any

from agent_gateway.extensions.sdk import ApplicationPluginExtension, ApplicationPluginServices, FrontendContribution

from .gateway_binding import MeetingGatewayBinding


class MeetingApplicationPlugin(ApplicationPluginExtension):
    plugin_id = "meeting"

    def __init__(self) -> None:
        self._binding: MeetingGatewayBinding | None = None

    async def initialize(self, config: Any) -> None:
        pass

    async def shutdown(self) -> None:
        if self._binding is not None:
            await self._binding.aclose()

    def bind_web_channel(self, channel: Any, services: ApplicationPluginServices) -> None:
        if self._binding is not None:
            raise RuntimeError("Meeting web channel is already bound")
        self._binding = MeetingGatewayBinding(channel, services.require_agent_client())
        self._binding.bind()

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (FrontendContribution(
            id="meeting", nav_key="app:meeting", title="Team meetings",
            title_i18n_key="meeting.title", render_mode="none", component="meeting", position=76,
        ),)


async def register_extensions(registry: Any) -> list[MeetingApplicationPlugin]:
    extension = MeetingApplicationPlugin()
    registry.register_application_plugin(extension)
    return [extension]
