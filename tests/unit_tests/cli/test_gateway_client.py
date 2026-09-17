# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenswarm.cli.gateway_client."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from jiuwenswarm.cli.gateway_client import GatewayClient


class TestGatewayClient:
    @pytest.mark.asyncio
    @staticmethod
    async def test_connect_waits_for_ack():
        async def _mock_connect(url, **kwargs):
            class FakeWs:
                async def recv(self):
                    return json.dumps({
                        "type": "event",
                        "event": "connection.ack",
                        "payload": {"protocol_version": "1.0", "transport": "tui"},
                    })

                async def close(self):
                    pass

            return FakeWs()

        with patch("jiuwenswarm.cli.gateway_client._connect_ws", _mock_connect):
            client = GatewayClient("ws://127.0.0.1:19001/tui")
            await client.connect()

    @pytest.mark.asyncio
    @staticmethod
    async def test_connect_rejects_non_ack():
        async def _mock_connect(url, **kwargs):
            class FakeWs:
                async def recv(self):
                    return json.dumps({"type": "req", "id": "x"})

                async def close(self):
                    pass

            return FakeWs()

        with patch("jiuwenswarm.cli.gateway_client._connect_ws", _mock_connect):
            client = GatewayClient("ws://127.0.0.1:19001/tui")
            with pytest.raises(ConnectionError):
                await client.connect()

    @pytest.mark.asyncio
    @staticmethod
    async def test_connect_rejects_invalid_json():
        async def _mock_connect(url, **kwargs):
            class FakeWs:
                async def recv(self):
                    return "not json"

                async def close(self):
                    pass

            return FakeWs()

        with patch("jiuwenswarm.cli.gateway_client._connect_ws", _mock_connect):
            client = GatewayClient("ws://127.0.0.1:19001/tui")
            with pytest.raises(ConnectionError):
                await client.connect()
