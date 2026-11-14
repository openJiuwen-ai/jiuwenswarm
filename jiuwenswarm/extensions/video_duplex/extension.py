from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

from agent_gateway.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
    FrontendContribution,
    WebSocketRouteContribution,
)
from jiuwenswarm.extensions.video_duplex.backend import settings

logger = logging.getLogger(__name__)


def _load_backend() -> Any:
    """Check required capabilities before registering any plugin resources."""
    backend = "jiuwenswarm.extensions.video_duplex.backend"
    required = {
        f"{backend}.realtime_context": ("RealtimeContextRestorer", "ContextRestoreExpired"),
        f"{backend}.error_details": ("duplex_failure",),
        f"{backend}.joyai_action_helper": ("select_model",),
        "agent_gateway.extensions.video_duplex.backend.tasks.bridge": ("GatewayTaskEndpoint",),
    }
    for name, attributes in required.items():
        module = importlib.import_module(name)
        for attribute in attributes:
            if not callable(getattr(module, attribute, None)):
                raise ImportError(f"Required full-duplex capability unavailable: {name}.{attribute}")
    protocol = importlib.import_module("e2a.message")
    if getattr(protocol.ReqMethod, "VOICE_TASK_CHECKPOINT_ACK", None) is None:
        raise ImportError("Required protocol capability unavailable: VOICE_TASK_CHECKPOINT_ACK")
    tools = importlib.import_module(f"{backend}.qwen_omni_tools")
    try:
        inspect.signature(tools.qwen_omni_tools).bind(queue_manager=True)
    except (TypeError, ValueError) as exc:
        raise ImportError("qwen_omni_tools does not support queue_manager") from exc
    return importlib.import_module(f"{backend}.video_live")


class VideoDuplexApplicationPlugin(ApplicationPluginExtension):
    plugin_id = "video-duplex"

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def is_enabled(self) -> bool:
        return self._backend.video_duplex_enabled()

    async def initialize(self, config: Any) -> None:
        del config

    async def shutdown(self) -> None:
        manager = getattr(self, "_task_manager", None)
        if manager is not None:
            await manager.close()

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        self._task_manager = self._backend.register_video_live_handler(
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
                clear_secrets = params.get("clear_secrets", False)
                if not isinstance(clear_secrets, bool):
                    raise ValueError("clear_secrets must be a boolean")
                settings.update_settings(values, clear_secrets=clear_secrets)
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

        async def test_connection(ws, req_id, params, session_id):  # noqa: ANN001
            del params, session_id
            try:
                from jiuwenswarm.extensions.video_duplex.backend.connection_check import check_saved_connection

                await check_saved_connection()
            except Exception:  # noqa: BLE001 - do not expose provider URLs or credentials
                await channel.send_response(
                    ws, req_id, ok=False,
                    error="Model connection failed. Check the saved endpoint, API key and model.",
                    code="CONNECTION_FAILED",
                )
                return
            await channel.send_response(ws, req_id, ok=True, payload={"connected": True})

        channel.register_method(
            "video.duplex.settings.test", test_connection,
            local_only=True, available_when_disabled=True,
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
        from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_gateway import (
            QWEN_OMNI_PROXY_PATH,
            serve_qwen_omni_websocket,
        )

        return (
            WebSocketRouteContribution(
                path=QWEN_OMNI_PROXY_PATH,
                endpoint=serve_qwen_omni_websocket,
                available_when_disabled=True,
            ),
        )

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (
            FrontendContribution(
                id="video-live",
                nav_key="app:video-duplex",
                title="Full-duplex",
                # The runtime remains bundled for task-chat integration, but
                # the standalone plugin tab is intentionally hidden. Its
                # configuration now lives under Settings > Experimental.
                render_mode="none",
                component="video-duplex",
                position=75,
            ),
        )


async def register_extensions(registry: Any) -> list[VideoDuplexApplicationPlugin]:
    try:
        backend = _load_backend()
    except (ImportError, AttributeError) as exc:
        logger.error(
            "Full-duplex plugin skipped: incompatible or incomplete dependencies (%s: %s). "
            "Install compatible Swarm, Gateway, Core and protocol versions, then restart Gateway.",
            type(exc).__name__, exc,
        )
        return []
    extension = VideoDuplexApplicationPlugin(backend)
    registry.register_application_plugin(extension)
    return [extension]
