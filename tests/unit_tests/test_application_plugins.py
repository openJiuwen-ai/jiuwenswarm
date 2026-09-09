from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jiuwenswarm.extensions.application_host import (
    application_plugin_manifest,
    iter_websocket_routes,
    mount_application_plugin_http_routes,
)
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
    FrontendContribution,
    WebSocketRouteContribution,
)
from jiuwenswarm.extensions.types import ExtensionMetadata


class _FakeChannel:
    def __init__(self) -> None:
        self.methods: dict[str, object] = {}
        self.local_only: set[str] = set()

    def register_method(self, method, handler, *, local_only=False) -> None:  # noqa: ANN001
        self.methods[method] = handler
        if local_only:
            self.local_only.add(method)

    async def send_response(self, ws, req_id, **payload) -> None:  # noqa: ANN001
        ws.append({"id": req_id, **payload})


class _TestPlugin(ApplicationPluginExtension):
    plugin_id = "example-plugin"

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    @property
    def metadata(self) -> ExtensionMetadata:
        return ExtensionMetadata(
            id=self.plugin_id,
            name="Example plugin",
            version="1.0.0",
            description="Test application plugin",
            author="tests",
            min_jiuwenswarm_version="0.2.5",
            dependencies={},
            config_schema=None,
            package_type="application",
        )

    async def initialize(self, config) -> None:  # noqa: ANN001
        return None

    async def shutdown(self) -> None:
        return None

    def is_enabled(self) -> bool:
        return self.enabled

    def bind_web_channel(self, channel, services) -> None:  # noqa: ANN001
        async def ping(ws, req_id, params, session_id):  # noqa: ANN001
            await channel.send_response(ws, req_id, ok=True, payload={"pong": True})

        async def settings(ws, req_id, params, session_id):  # noqa: ANN001
            await channel.send_response(ws, req_id, ok=True, payload={"settings": True})

        channel.register_method("example.ping", ping, local_only=True)
        channel.register_method(
            "example.settings",
            settings,
            local_only=True,
            available_when_disabled=True,
        )

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (
            FrontendContribution(
                id="example-page",
                nav_key="app:example-plugin",
                title="Example",
                render_mode="iframe",
                entrypoint="index.html",
            ),
        )


class _AliasedTestPlugin(_TestPlugin):
    plugin_id = "runtime-plugin-id"

    @property
    def metadata(self) -> ExtensionMetadata:
        metadata = super().metadata
        metadata.id = "manifest-plugin-id"
        return metadata

    def websocket_routes(self) -> tuple[WebSocketRouteContribution, ...]:
        async def endpoint(websocket) -> None:  # noqa: ANN001
            del websocket

        return (WebSocketRouteContribution(path="/ws/runtime-plugin", endpoint=endpoint),)


def _registry() -> ExtensionRegistry:
    return ExtensionRegistry(MagicMock(), {}, MagicMock())


def test_application_plugin_core_services_are_explicitly_injected() -> None:
    client = object()
    normalized: list[tuple[dict[str, Any], str | None]] = []
    services = ApplicationPluginServices(
        agent_client=client,
        media_attachment_normalizer=lambda params, session_id: normalized.append(
            (params, session_id)
        ),
    )

    assert services.require_agent_client() is client
    params = {"media_items": []}
    services.normalize_media_attachments(params, "session-1")
    assert normalized == [(params, "session-1")]

    unavailable = ApplicationPluginServices()
    with pytest.raises(RuntimeError, match="Core Agent service"):
        unavailable.require_agent_client()
    with pytest.raises(RuntimeError, match="media attachment service"):
        unavailable.normalize_media_attachments({}, "session-1")


@pytest.mark.asyncio
async def test_registry_binds_local_methods_and_blocks_disabled_plugins() -> None:
    registry = _registry()
    registry.register_application_plugin(_TestPlugin(enabled=False))
    channel = _FakeChannel()
    registry.bind_application_plugins(channel)
    responses: list[dict[str, Any]] = []

    await channel.methods["example.ping"](responses, "req-1", {}, "session-1")
    await channel.methods["example.settings"](responses, "req-2", {}, "session-1")

    assert channel.local_only == {"example.ping", "example.settings"}
    assert channel.application_plugin_registry is registry
    assert responses[0]["code"] == "APPLICATION_PLUGIN_DISABLED"
    assert responses[1]["payload"] == {"settings": True}


def test_websocket_routes_use_the_registered_runtime_plugin_id() -> None:
    registry = _registry()
    registry.register_application_plugin(_AliasedTestPlugin())

    plugin_id, route = next(iter(iter_websocket_routes(registry)))

    assert plugin_id == "runtime-plugin-id"
    assert route.path == "/ws/runtime-plugin"


@pytest.mark.asyncio
async def test_manifest_only_iframe_plugin_requires_no_python_entry(tmp_path: Path) -> None:
    root = tmp_path / "hello-plugin"
    assets = root / "frontend" / "dist"
    assets.mkdir(parents=True)
    (assets / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    (root / "extension.yaml").write_text(
        """
id: hello-plugin
name: Hello plugin
version: 1.0.0
description: Minimal external application
author: Example
min_jiuwenswarm_version: 0.2.5
package_type: application
permissions: [camera]
frontend:
  - id: hello-page
    title: Hello
    entrypoint: index.html
    position: 120
""".strip(),
        encoding="utf-8",
    )
    registry = _registry()

    loaded = await ExtensionLoader(registry).load_extension(root)

    assert loaded
    plugin = registry.get_application_plugin("hello-plugin")
    assert plugin is not None
    manifest = application_plugin_manifest(registry)["plugins"][0]
    assert manifest["render_mode"] == "iframe"
    assert manifest["permissions"] == ["camera"]
    assert manifest["entry_url"].endswith("/hello-plugin/assets/index.html")

    app = FastAPI()
    mount_application_plugin_http_routes(app, registry)
    response = TestClient(app).get(manifest["entry_url"])
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"
