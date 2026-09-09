from __future__ import annotations

from typing import Any, Iterable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk.application_plugin import (
    ApplicationPluginExtension,
    WebSocketRouteContribution,
)


APPLICATION_PLUGIN_API_PREFIX = "/api/application-plugins"


def _plugins(registry: ExtensionRegistry | None) -> tuple[ApplicationPluginExtension, ...]:
    if registry is None:
        return ()
    return registry.get_application_plugins()


def application_plugin_manifest(registry: ExtensionRegistry | None) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for plugin in _plugins(registry):
        metadata = plugin.metadata
        plugin_id = plugin.plugin_id or metadata.id
        contributions = plugin.frontend_contributions()
        if not contributions:
            entries.append({
                "plugin_id": plugin_id,
                "plugin_version": metadata.version,
                "description": metadata.description,
                "permissions": list(metadata.permissions),
                "enabled": plugin.is_enabled(),
                "id": f"{plugin_id}:management",
                "nav_key": f"app:{plugin_id}",
                "title": metadata.name or plugin_id,
                "title_i18n_key": "",
                "render_mode": "none",
                "component": "",
                "position": 1000,
            })
        for contribution in contributions:
            item = {
                "plugin_id": plugin_id,
                "plugin_version": metadata.version,
                "description": metadata.description,
                "permissions": list(metadata.permissions),
                "enabled": plugin.is_enabled(),
                "id": contribution.id,
                "nav_key": contribution.nav_key,
                "title": contribution.title,
                "title_i18n_key": contribution.title_i18n_key,
                "render_mode": contribution.render_mode,
                "component": contribution.component,
                "position": contribution.position,
            }
            if contribution.entrypoint:
                item["entry_url"] = (
                    f"{APPLICATION_PLUGIN_API_PREFIX}/{plugin_id}/assets/"
                    f"{contribution.entrypoint.lstrip('/')}"
                )
            entries.append(item)
    entries.sort(key=lambda item: (int(item["position"]), str(item["nav_key"])))
    return {"api_version": 1, "plugins": entries}


def _plugin_or_404(
    registry: ExtensionRegistry | None,
    plugin_id: str,
) -> ApplicationPluginExtension:
    plugin = registry.get_application_plugin(plugin_id) if registry is not None else None
    if plugin is None:
        raise HTTPException(status_code=404, detail="application plugin not found")
    return plugin


def iter_websocket_routes(
    registry: ExtensionRegistry | None,
) -> Iterable[tuple[str, WebSocketRouteContribution]]:
    for plugin in _plugins(registry):
        for route in plugin.websocket_routes():
            yield plugin.plugin_id or plugin.metadata.id, route


def mount_application_plugin_http_routes(
    app: FastAPI,
    registry: ExtensionRegistry | None,
) -> None:
    @app.get(APPLICATION_PLUGIN_API_PREFIX)
    async def list_application_plugins() -> dict[str, Any]:
        return application_plugin_manifest(registry)

    @app.get(f"{APPLICATION_PLUGIN_API_PREFIX}/{{plugin_id}}/assets/{{asset_path:path}}")
    async def application_plugin_asset(plugin_id: str, asset_path: str) -> FileResponse:
        plugin = _plugin_or_404(registry, plugin_id)
        root = plugin.frontend_asset_root()
        if root is None:
            raise HTTPException(status_code=404, detail="plugin has no frontend assets")
        root = root.resolve()
        target = (root / asset_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="asset not found") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(target, headers={"Cache-Control": "no-cache"})
