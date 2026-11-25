"""App Market extension — register as ApplicationPluginExtension."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import uuid
from typing import Any

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
)
from jiuwenswarm.common.paths import get_app_market_dir

from jiuwenswarm.extensions.app_market.manager import (
    AppMarketError,
    AppMarketManager,
    RunConsentRequired,
)
from jiuwenswarm.extensions.app_market.editor_sessions import CoreRequest, EditorSessionError

logger = logging.getLogger(__name__)


def _invalid_app_id(app_id: Any) -> bool:
    """app_id 必须是非空且不含路径分隔符的单段标识。"""
    if not isinstance(app_id, str) or not app_id:
        return True
    return "/" in app_id or "\\" in app_id or app_id in {".", ".."}


class AppMarketPlugin(ApplicationPluginExtension):
    plugin_id = "app_market"

    def is_enabled(self) -> bool:
        return True

    def _market_data_dir(self) -> Path:
        """Resolve the data root at use time, like the other workspace paths."""
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            data_dir = get_app_market_dir()
        self._data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    async def initialize(self, config: Any) -> None:
        del config
        self._market_data_dir()
        self._manager: AppMarketManager | None = None

    async def shutdown(self) -> None:
        if self._manager is not None:
            await self._manager.shutdown()

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        self._manager = AppMarketManager(str(self._market_data_dir()))
        self._manager.configure_gateway(
            gateway_port=os.environ.get("GATEWAY_PORT", "19001"),
            web_port=channel.config.port,
            # Resolve the same configuration defaults as GatewayServerConfig.
            # Applications still receive an explicit address from the market.
            gateway_host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
            app_host=channel.config.host,
        )

        def owner(ws):
            from agent_gateway.channel_manager.web.web_connect import WebChannel
            return WebChannel.connection_user_id(ws) or ""

        def routed_request(user_id: str, label: str) -> CoreRequest:
            from agent_gateway.common.request_method import parse_request_method
            from agent_gateway.routing.e2a_proxy import fetch_agent_unary

            async def request(method: ReqMethod, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
                gateway_method = parse_request_method(method.value)
                if gateway_method is None:
                    return False, {"code": "METHOD_NOT_FOUND", "error": f"Unknown Gateway method: {method.value}"}
                return await fetch_agent_unary(
                    agent_client=services.agent_client,
                    req_method=gateway_method,
                    params=payload,
                    session_id=None,
                    user_id=user_id or None,
                    channel_id=channel.channel_id,
                    label=label,
                    timeout_seconds=60,
                )

            return request

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
            version = params.get("version")
            if version is not None and not isinstance(version, str):
                await channel.send_response(
                    ws, req_id, ok=False, error="version must be a string", code="BAD_REQUEST"
                )
                return
            try:
                result = await self._manager.publish_app(entry_path, source_session, owner(ws), version)
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

        async def handle_editor_app(ws, req_id, params, session_id):
            del session_id
            editor_session_id = params.get("editor_session_id") if isinstance(params, dict) else None
            if not isinstance(editor_session_id, str) or not editor_session_id.strip():
                await channel.send_response(
                    ws, req_id, ok=False, error="editor_session_id is required", code="BAD_REQUEST",
                )
                return
            app = self._manager.get_editor_app(editor_session_id.strip(), owner(ws))
            await channel.send_response(ws, req_id, ok=True, payload={"app": app})

        async def handle_instances(ws, req_id, params, session_id):  # noqa: ANN001
            del params, session_id
            result = self._manager.list_instances(owner(ws))
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_resources(ws, req_id, params, session_id):
            del params, session_id
            result = await self._manager.resources(owner(ws))
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def send_market_error(ws, req_id, exc: Exception) -> None:  # noqa: ANN001
            payload = (
                {"artifact_sha256": exc.artifact_sha256}
                if isinstance(exc, RunConsentRequired)
                else None
            )
            code = getattr(exc, "code", "APP_MARKET_FAILED")
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code=code, payload=payload
            )

        async def handle_pin(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            params = params if isinstance(params, dict) else {}
            app_id = params.get("app_id")
            if _invalid_app_id(app_id):
                await channel.send_response(ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST")
                return
            pinned = params.get("pinned")
            if not isinstance(pinned, bool):
                await channel.send_response(ws, req_id, ok=False, error="pinned must be boolean", code="BAD_REQUEST")
                return
            try:
                result = self._manager.set_app_pinned(app_id, owner(ws), pinned)
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_ai_roles(ws, req_id, params, session_id):
            del session_id
            app_id = params.get("app_id") if isinstance(params, dict) else None
            if _invalid_app_id(app_id):
                await channel.send_response(ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST")
                return
            try:
                groups = await self._manager.get_ai_role_groups(app_id, owner(ws))
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload={"session_groups": groups})

        async def handle_start(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            params = params if isinstance(params, dict) else {}
            app_id = params.get("app_id", "")
            if _invalid_app_id(app_id):
                await channel.send_response(
                    ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST"
                )
                return
            run_consent = params.get("run_consent")
            owner_id = owner(ws)
            try:
                result = await self._manager.start_app(
                    app_id,
                    owner_id,
                    run_consent if isinstance(run_consent, dict) else None,
                    project_request=routed_request(owner_id, "app_market.start"),
                )
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
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
            params = params if isinstance(params, dict) else {}
            app_id = params.get("app_id", "")
            if _invalid_app_id(app_id):
                await channel.send_response(
                    ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST"
                )
                return
            try:
                result = await self._manager.delete_app(
                    app_id, owner(ws), remove_files=params.get("remove_files") is True
                )
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=bool(result.get("deleted")), payload=result)

        async def handle_market_list(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            params = params if isinstance(params, dict) else {}
            try:
                result = await self._manager.market_list(
                    query=str(params.get("query") or ""),
                    page=int(params.get("page") or 1),
                    page_size=int(params.get("page_size") or 20),
                    owner_id=owner(ws),
                )
            except Exception as exc:  # noqa: BLE001 - upstream errors stay user-facing
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_market_detail(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            params = params if isinstance(params, dict) else {}
            asset_id = params.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id.strip():
                await channel.send_response(
                    ws, req_id, ok=False, error="asset_id is required", code="BAD_REQUEST"
                )
                return
            try:
                result = await self._manager.market_detail(
                    asset_id=asset_id.strip(),
                    version=str(params.get("version") or ""),
                    owner_id=owner(ws),
                )
            except Exception as exc:  # noqa: BLE001 - upstream errors stay user-facing
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_install_dir_default(ws, req_id, params, session_id):  # noqa: ANN001
            del params, session_id
            result = await self._manager.install_dir_default()
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_install(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            params = params if isinstance(params, dict) else {}
            asset_id = params.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id.strip():
                await channel.send_response(
                    ws, req_id, ok=False, error="asset_id is required", code="BAD_REQUEST"
                )
                return
            owner_id = owner(ws)
            try:
                result = await self._manager.start_install(
                    asset_id=asset_id.strip(),
                    version=str(params.get("version") or ""),
                    install_dir=str(params.get("install_dir") or ""),
                    owner_id=owner_id,
                    expected_sha256=str(params.get("expected_sha256") or ""),
                    project_request=routed_request(owner_id, "app_market.install"),
                )
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_install_status(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            params = params if isinstance(params, dict) else {}
            try:
                result = self._manager.install_status(
                    str(params.get("job_id") or ""), owner(ws)
                )
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_install_cancel(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            params = params if isinstance(params, dict) else {}
            try:
                result = self._manager.cancel_install(
                    str(params.get("job_id") or ""), owner(ws)
                )
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_update(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            params = params if isinstance(params, dict) else {}
            app_id = params.get("app_id")
            if not isinstance(app_id, str) or not app_id.strip():
                await channel.send_response(
                    ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST"
                )
                return
            try:
                result = await self._manager.start_update(
                    app_id=app_id.strip(),
                    owner_id=owner(ws),
                    version=str(params.get("version") or ""),
                    expected_sha256=str(params.get("expected_sha256") or ""),
                    editor_is_busy=getattr(channel, "is_session_busy", None),
                )
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_uninstall(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            params = params if isinstance(params, dict) else {}
            app_id = params.get("app_id")
            if not isinstance(app_id, str) or not app_id.strip():
                await channel.send_response(
                    ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST"
                )
                return
            try:
                result = await self._manager.uninstall_app(
                    app_id.strip(),
                    owner(ws),
                    params.get("remove_files") is True,
                )
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_convert_to_local(ws, req_id, params, session_id):
            del session_id
            app_id = params.get("app_id") if isinstance(params, dict) else None
            if _invalid_app_id(app_id):
                await channel.send_response(ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST")
                return

            try:
                result = await self._manager.convert_to_local(
                    app_id,
                    owner(ws),
                    project_request=routed_request(owner(ws), "app_market.convert_to_local"),
                )
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        async def handle_edit_session(ws, req_id, params, session_id):
            del session_id
            params = params if isinstance(params, dict) else {}
            app_id = params.get("app_id")
            if _invalid_app_id(app_id):
                await channel.send_response(ws, req_id, ok=False, error="app_id is required", code="BAD_REQUEST")
                return

            options = {
                key: params[key]
                for key in ("title", "model_name", "model_selection", "edit_token")
                if key in params
            }
            owner_id = owner(ws)
            request = routed_request(owner_id, "app_market.edit_session")
            try:
                result = await self._manager.edit_session(app_id, owner_id, request, options)
            except AppMarketError as exc:
                await send_market_error(ws, req_id, exc)
                return
            except EditorSessionError as exc:
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code=exc.code)
                return
            except OSError:
                logger.exception("Failed to persist application editor session")
                await channel.send_response(
                    ws, req_id, ok=False, error="Cannot save editor session", code="EDIT_SESSION_FAILED"
                )
                return
            await channel.send_response(ws, req_id, ok=True, payload=result)

        channel.register_method("app_market.edit_session", handle_edit_session)
        channel.register_method("app_market.editor_app", handle_editor_app)
        channel.register_method("app_market.convert_to_local", handle_convert_to_local)
        channel.register_method("app_market.publish", handle_publish)
        channel.register_method("app_market.list", handle_list)
        channel.register_method("app_market.ai_roles", handle_ai_roles)
        channel.register_method("app_market.pin", handle_pin)
        channel.register_method("app_market.instances", handle_instances)
        channel.register_method("app_market.resources", handle_resources)
        channel.register_method("app_market.start", handle_start)
        channel.register_method("app_market.stop", handle_stop)
        channel.register_method("app_market.delete", handle_delete)
        channel.register_method("app_market.market_list", handle_market_list)
        channel.register_method("app_market.market_detail", handle_market_detail)
        channel.register_method("app_market.install_dir_default", handle_install_dir_default)
        channel.register_method("app_market.install", handle_install)
        channel.register_method("app_market.install_status", handle_install_status)
        channel.register_method("app_market.install_cancel", handle_install_cancel)
        channel.register_method("app_market.update", handle_update)
        channel.register_method("app_market.uninstall", handle_uninstall)


async def register_extensions(registry):
    plugin = AppMarketPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
