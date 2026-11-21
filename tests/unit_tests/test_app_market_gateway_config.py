"""Gateway configuration supplied by the app-market plugin at binding time."""
import asyncio
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.app_market.extension import AppMarketPlugin
from jiuwenswarm.extensions.sdk import ApplicationPluginServices


class GatewayConfigTest(unittest.TestCase):
    def test_binding_passes_gateway_defaults_without_explicit_host_or_port(self):
        self._check_binding({}, "127.0.0.1", 19001)

    def test_binding_uses_gateway_port_when_host_is_implicit(self):
        self._check_binding({"GATEWAY_PORT": "29001"}, "127.0.0.1", 29001)

    def test_binding_preserves_explicit_gateway_address(self):
        self._check_binding({"GATEWAY_HOST": "localhost", "GATEWAY_PORT": "29001"}, "localhost", 29001)

    def test_registered_market_list_uses_web_channel_identity(self):
        import asyncio
        from unittest.mock import AsyncMock

        methods = {}
        channel = SimpleNamespace(
            config=SimpleNamespace(host="127.0.0.1", port=29000),
            register_method=methods.__setitem__,
            send_response=AsyncMock(),
        )
        connection = SimpleNamespace(_web_connection_user_id="market-user")
        with tempfile.TemporaryDirectory() as directory, patch(
            "jiuwenswarm.extensions.app_market.extension.get_user_workspace_dir",
            return_value=Path(directory),
        ):
            plugin = AppMarketPlugin()
            plugin.bind_web_channel(channel, ApplicationPluginServices(agent_client=object()))
            with patch.object(plugin._manager, "list_apps", return_value={"apps": []}) as list_apps:
                asyncio.run(methods["app_market.list"](connection, "list-request", {}, None))
                list_apps.assert_called_once_with("market-user")
            channel.send_response.assert_awaited_once_with(
                connection, "list-request", ok=True, payload={"apps": []},
            )

    def _check_binding(self, gateway_env, host, port):
        channel = SimpleNamespace(
            config=SimpleNamespace(host="127.0.0.2", port=29000),
            register_method=lambda *args: None,
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, gateway_env, clear=True), patch(
                "jiuwenswarm.extensions.app_market.extension.get_user_workspace_dir",
                return_value=Path(directory),
            ):
                plugin = AppMarketPlugin()
                plugin.bind_web_channel(channel, ApplicationPluginServices(agent_client=object()))
                env = plugin._manager._service._gateway_env()
        self.assertEqual(env.get("WORKSWARM_GATEWAY_HOST"), host)
        self.assertEqual(env.get("WORKSWARM_GATEWAY_PORT"), str(port))
        self.assertEqual(env.get("WORKSWARM_GATEWAY_API_URL"), f"ws://{host}:29000/ws")


if __name__ == "__main__":
    unittest.main()


def test_market_has_no_ai_data_plane():
    from jiuwenswarm.extensions.app_market.extension import AppMarketPlugin
    from jiuwenswarm.extensions.app_market.app_market.market_service import MarketService
    assert AppMarketPlugin().websocket_routes() == ()
    service = MarketService('/tmp/app-market-boundary-test')
    assert not hasattr(service, '_runtime_sessions')
    assert not hasattr(service, '_dispatch_app')


@contextmanager
def _bound_publish_handler():
    methods = {}
    channel = SimpleNamespace(
        config=SimpleNamespace(host="127.0.0.1", port=29000),
        register_method=methods.__setitem__,
        send_response=AsyncMock(),
    )
    connection = SimpleNamespace(_web_connection_user_id="market-user")
    with tempfile.TemporaryDirectory() as directory, patch(
        "jiuwenswarm.extensions.app_market.extension.get_user_workspace_dir",
        return_value=Path(directory),
    ):
        plugin = AppMarketPlugin()
        plugin.bind_web_channel(channel, ApplicationPluginServices(agent_client=object()))
        yield channel, connection, Path(directory), methods


def test_publish_handler_forwards_version_and_returns_it():
    with _bound_publish_handler() as (channel, connection, directory, methods):
        page = directory / "app.html"
        page.write_text("<html>app</html>", encoding="utf-8")
        asyncio.run(methods["app_market.publish"](
            connection,
            "publish-request",
            {"entry_path": str(page), "source_session": "session", "version": "2.0.0"},
            None,
        ))
        response = channel.send_response.await_args.kwargs
        assert response["ok"] is True
        assert response["payload"]["version"] == "2.0.0"


def test_publish_handler_rejects_non_string_version():
    with _bound_publish_handler() as (channel, connection, directory, methods):
        page = directory / "app.html"
        page.write_text("<html>app</html>", encoding="utf-8")
        asyncio.run(methods["app_market.publish"](
            connection,
            "publish-request",
            {"entry_path": str(page), "source_session": "session", "version": 2},
            None,
        ))
        response = channel.send_response.await_args.kwargs
        assert response["ok"] is False
        assert response["code"] == "BAD_REQUEST"


def test_market_data_dir_follows_workspace_resolver(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.app_market import extension as extension_module

    monkeypatch.setattr(extension_module, "get_user_workspace_dir", lambda: tmp_path)
    plugin = AppMarketPlugin()
    asyncio.run(plugin.initialize(None))

    assert plugin._data_dir == tmp_path / "app_market"
    assert (tmp_path / "app_market").is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["app_market.start", "app_market.edit_session"])
async def test_project_request_reaches_gateway_as_wire_method(tmp_path, monkeypatch, route):
    methods = {}
    channel = SimpleNamespace(channel_id="web", config=SimpleNamespace(host="127.0.0.1", port=29000),
                              register_method=methods.__setitem__, send_response=AsyncMock())
    ws = SimpleNamespace(_web_connection_user_id="market-user")
    client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True, payload={"projects": []})))
    plugin = AppMarketPlugin()
    monkeypatch.setattr(plugin, "_data_dir", tmp_path, raising=False)
    plugin.bind_web_channel(channel, ApplicationPluginServices(agent_client=client))

    async def check(request):
        ok, payload = await request(ReqMethod.PROJECT_LIST, {"filter": "all"})
        assert ok
        return payload

    async def start(app_id, owner_id, consent, project_request):
        return await check(project_request)

    async def edit(app_id, owner_id, request, options):
        return await check(request)

    monkeypatch.setattr(plugin._manager, "start_app", start)
    monkeypatch.setattr(plugin._manager, "edit_session", edit)
    await methods[route](ws, "rpc", {"app_id": "demo"}, None)

    client.send_request.assert_awaited_once()
    envelope = client.send_request.await_args.args[0]
    assert envelope.method == "project.list"
    assert envelope.params == {"filter": "all"}
    assert envelope.channel == "web" and envelope.user_id == "market-user"
    channel.send_response.assert_awaited_once_with(ws, "rpc", ok=True, payload={"projects": []})
