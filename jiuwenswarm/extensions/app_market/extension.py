"""App Market extension — register as ApplicationPluginExtension."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import uuid
from typing import Any

from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
)

from jiuwenswarm.extensions.app_market.manager import AppMarketManager
from jiuwenswarm.extensions.app_market.editor_sessions import AppEditorSessions, EditorSessionError

logger = logging.getLogger(__name__)

_DATA_DIR = Path.home() / ".jiuwenswarm" / "app_market"


class AppMarketPlugin(ApplicationPluginExtension):
    plugin_id = "app_market"

    def is_enabled(self) -> bool:
        return True

    async def initialize(self, config: Any) -> None:
        del config
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._manager: AppMarketManager | None = None

    async def shutdown(self) -> None:
        if self._manager is not None:
            await self._manager.shutdown()

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        self._manager = AppMarketManager(str(_DATA_DIR))
        editors = AppEditorSessions(self._manager._service._storage)
        self._manager._service.configure_gateway(
            gateway_port=os.environ.get("GATEWAY_PORT", "19001"),
            web_port=channel.config.port,
            # Resolve the same configuration defaults as GatewayServerConfig.
            # Applications still receive an explicit address from the market.
            gateway_host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
            app_host=channel.config.host,
        )

        def owner(ws):
            from gateway.channel_manager.web.web_connect import WebChannel
            return WebChannel.connection_user_id(ws) or ""

        async def handle_publish(ws, req_id, params, session_id):  # noqa: ANN001
            if not isinstance(params, dict):
                params = {}
            entry_path = params.get("entry_path")
            source_session = params.get("source_session") or session_id
            if not isinstance(entry_path, str) or not entry_path.strip():
                await channel.send_response(
                    ws, req_id, ok=False, error="entry_path is required", code="BAD_REQUEST"
                )
                return
            if not isinstance(source_session, str) or not source_session.strip():
                await channel.send_response(
                    ws, req_id, ok=False, error="source_session is required", code="BAD_REQUEST"
                )
                return
            try:
                result = await self._manager.publish_app(entry_path, source_session, owner(ws))
            except (ValueError, OSError) as exc:
                await channel.send_response(
                    ws, req_id, ok=False, error=str(exc), code="PUBLISH_FAILED"
                )
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_list(ws, req_id, params, session_id):  # noqa: ANN001
            del params, session_id
            result = self._manager.list_apps(owner(ws))
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_instances(ws, req_id, params, session_id):  # noqa: ANN001
            del params, session_id
            result = self._manager.list_instances(owner(ws))
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_start(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            app_id = params.get("app_id", "") if isinstance(params, dict) else ""
            if not app_id:
                await channel.send_response(
                    ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST"
                )
                return
            result = await self._manager.start_app(app_id, owner(ws))
            if result is None:
                await channel.send_response(
                    ws, req_id, ok=False, error=self._manager.start_error(app_id), code="START_FAILED"
                )
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_stop(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            instance_id = params.get("instance_id", "") if isinstance(params, dict) else ""
            if not instance_id:
                await channel.send_response(
                    ws, req_id, ok=False, error="instance_id is required", code="BAD_REQUEST"
                )
                return
            ok = await self._manager.stop_app(instance_id, owner(ws))
            await channel.send_response(ws, req_id, ok=ok, payload={"stopped": ok})

        async def handle_delete(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            app_id = params.get("app_id", "") if isinstance(params, dict) else ""
            if not app_id:
                await channel.send_response(
                    ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST"
                )
                return
            ok = await self._manager.delete_app(app_id, owner(ws))
            await channel.send_response(ws, req_id, ok=ok, payload={"deleted": ok})

        async def handle_edit_session(ws, req_id, params, session_id):
            from gateway.routing.e2a_proxy import fetch_agent_unary

            del session_id
            params = params if isinstance(params, dict) else {}
            app_id = params.get("app_id")
            if not isinstance(app_id, str) or not app_id or "/" in app_id or "\\" in app_id or app_id in {".", ".."}:
                await channel.send_response(ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST")
                return

            async def request(method, payload):
                return await fetch_agent_unary(
                    agent_client=services.agent_client,
                    req_method=method,
                    params=payload,
                    session_id=None,
                    user_id=owner(ws) or None,
                    channel_id=channel.channel_id,
                    label="app_market.edit_session",
                    timeout_seconds=60,
                )

            options = {key: params[key] for key in ("title", "model_name", "model_selection") if key in params}
            try:
                result = await editors.open(app_id, owner(ws), request, options)
            except EditorSessionError as exc:
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code=exc.code)
                return
            except OSError:
                logger.exception("Failed to persist application editor session")
                await channel.send_response(ws, req_id, ok=False, error="Cannot save editor session", code="EDIT_SESSION_FAILED")
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        channel.register_method("app_market.edit_session", handle_edit_session)
        channel.register_method("app_market.publish", handle_publish)
        channel.register_method("app_market.list", handle_list)
        channel.register_method("app_market.instances", handle_instances)
        channel.register_method("app_market.start", handle_start)
        channel.register_method("app_market.stop", handle_stop)
        channel.register_method("app_market.delete", handle_delete)


async def register_extensions(registry):
    plugin = AppMarketPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
