from __future__ import annotations

from typing import Any

from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
    FrontendContribution,
    WebSocketRouteContribution,
)
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_gateway import (
    QWEN_OMNI_PROXY_PATH,
    serve_qwen_omni_websocket,
)
from jiuwenswarm.extensions.video_duplex.backend.video_live import (
    register_video_live_handler,
    video_duplex_enabled,
)
from jiuwenswarm.extensions.video_duplex.backend import settings


class VideoDuplexApplicationPlugin(ApplicationPluginExtension):
    plugin_id = "video-duplex"

    def is_enabled(self) -> bool:
        return video_duplex_enabled()

    async def initialize(self, config: Any) -> None:
        del config

    async def shutdown(self) -> None:
        return None

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        register_video_live_handler(
            channel,
            agent_client=services.require_agent_client(),
            normalize_media_attachments=services.normalize_media_attachments,
        )

        async def get_settings(ws, req_id, params, session_id):  # noqa: ANN001
            del params, session_id
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=settings.settings_payload(enabled=self.is_enabled()),
            )

        async def update_settings(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            values = params.get("values") if isinstance(params, dict) else None
            if not isinstance(values, dict):
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error="values must be an object",
                    code="BAD_REQUEST",
                )
                return
            try:
                settings.update_settings(values)
            except ValueError as exc:
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error=str(exc),
                    code="BAD_REQUEST",
                )
                return
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=settings.settings_payload(enabled=self.is_enabled()),
            )

        async def set_enabled(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            enabled = params.get("enabled") if isinstance(params, dict) else None
            if not isinstance(enabled, bool):
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error="enabled must be a boolean",
                    code="BAD_REQUEST",
                )
                return
            settings.set_enabled(enabled)
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=settings.settings_payload(enabled=self.is_enabled()),
            )

        channel.register_method(
            "video.duplex.settings.get",
            get_settings,
            local_only=True,
            available_when_disabled=True,
        )
        channel.register_method(
            "video.duplex.settings.update",
            update_settings,
            local_only=True,
            available_when_disabled=True,
        )
        channel.register_method(
            "video.duplex.settings.set_enabled",
            set_enabled,
            local_only=True,
            available_when_disabled=True,
        )

    def websocket_routes(self) -> tuple[WebSocketRouteContribution, ...]:
        return (
            WebSocketRouteContribution(
                path=QWEN_OMNI_PROXY_PATH,
                endpoint=serve_qwen_omni_websocket,
            ),
        )

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (
            FrontendContribution(
                id="video-live",
                nav_key="app:video-duplex",
                title="Full-duplex",
                render_mode="bundled",
                component="video-duplex",
                position=75,
            ),
        )


async def register_extensions(registry: Any) -> list[VideoDuplexApplicationPlugin]:
    extension = VideoDuplexApplicationPlugin()
    registry.register_application_plugin(extension)
    return [extension]
