"""Gateway configuration supplied by the app-market plugin at binding time."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jiuwenswarm.extensions.app_market.extension import AppMarketPlugin
from jiuwenswarm.extensions.sdk import ApplicationPluginServices


class GatewayConfigTest(unittest.TestCase):
    def test_binding_passes_gateway_defaults_without_explicit_host_or_port(self):
        self._check_binding({}, "127.0.0.1", 19001)

    def test_binding_uses_gateway_port_when_host_is_implicit(self):
        self._check_binding({"GATEWAY_PORT": "29001"}, "127.0.0.1", 29001)

    def test_binding_preserves_explicit_gateway_address(self):
        self._check_binding({"GATEWAY_HOST": "localhost", "GATEWAY_PORT": "29001"}, "localhost", 29001)

    def _check_binding(self, gateway_env, host, port):
        channel = SimpleNamespace(
            config=SimpleNamespace(host="127.0.0.2", port=29000),
            register_method=lambda *args: None,
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, gateway_env, clear=True), patch(
                "jiuwenswarm.extensions.app_market.extension._DATA_DIR", Path(directory)
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
