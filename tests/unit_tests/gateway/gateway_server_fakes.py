"""Minimal gateway server fakes shared by tests outside the gateway suite."""

import json

from agent_gateway.app_gateway import GatewayServer, GatewayServerConfig


class DummyBus:
    @staticmethod
    async def publish_user_messages(msg):
        return None


class _FakeRequestHeaders:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = {k.lower(): v for k, v in mapping.items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._mapping.get(key.lower(), default)


class FakeWebSocket:
    def __init__(self, *, user_id: str | None = None) -> None:
        self.sent_frames = []
        self.closed = False
        if user_id is not None:
            self._gateway_user_id = user_id
            self.request = type(
                "Request",
                (),
                {"headers": _FakeRequestHeaders({"X-User-Id": user_id})},
            )()

    async def send(self, data):
        self.sent_frames.append(json.loads(data))

    async def close(self, code=None, reason=None):
        self.closed = True
        return code, reason


class GatewayServerProbe(GatewayServer):
    async def handle_raw_message_public(self, ws, raw: str, *, path: str = "/acp") -> None:
        await self._handle_raw_message(ws, raw, path, self.config.routes[path])


def build_server() -> GatewayServerProbe:
    return GatewayServerProbe(GatewayServerConfig(), DummyBus())
