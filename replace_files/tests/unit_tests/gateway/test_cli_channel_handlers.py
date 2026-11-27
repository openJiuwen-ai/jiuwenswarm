import pytest

from jiuwenclaw.channel.cli_channel import (
    CliHandlersBindParams,
    CliRouteBindParams,
    build_cli_route_binding,
    register_cli_handlers,
)


class FakeGatewayServer:
    """Fake GatewayServer for testing CLI handler registration."""

    def __init__(self):
        self.local_handlers: dict[str, dict] = {}  # path -> {method: handler}
        self.responses = []

    def register_local_handler(self, path, method, handler):
        if path not in self.local_handlers:
            self.local_handlers[path] = {}
        self.local_handlers[path][method] = handler

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {
                "id": req_id,
                "ok": ok,
                "payload": payload or {},
                "error": error,
                "code": code,
            }
        )


@pytest.mark.asyncio
async def test_register_cli_handlers_registers_local_methods():
    server = FakeGatewayServer()

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    cli_handlers = server.local_handlers["/tui"]
    assert "config.get" in cli_handlers
    assert "config.validate_model" in cli_handlers
    assert "session.list" in cli_handlers
    assert "chat.send" in cli_handlers
    assert "chat.resume" in cli_handlers
    assert "history.get" in cli_handlers

    await cli_handlers["chat.send"](object(), "req-1", {}, "sess-1")

    assert server.responses == [
        {
            "id": "req-1",
            "ok": True,
            "payload": {"accepted": True, "session_id": "sess-1"},
            "error": None,
            "code": None,
        }
    ]


def test_build_cli_route_binding_creates_route_and_install_hook():
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui"))
    server = FakeGatewayServer()

    assert binding.path == "/tui"
    assert binding.channel_id == "tui"
    assert "chat.send" in binding.forward_methods
    assert "history.get" in binding.forward_methods
    assert binding.install is not None

    binding.install(server)

    cli_handlers = server.local_handlers["/tui"]
    assert "config.get" in cli_handlers
    assert "config.validate_model" in cli_handlers
    assert "session.list" in cli_handlers
    assert "chat.send" in cli_handlers


@pytest.mark.asyncio
async def test_config_validate_model_handler_uses_local_probe(monkeypatch):
    server = FakeGatewayServer()

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    cli_handlers = server.local_handlers["/tui"]

    class FakeModel:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def invoke(self, *args, **kwargs):
            return {"content": "hello"}

    monkeypatch.setattr("jiuwenclaw.channel.cli_channel.Model", FakeModel)

    await cli_handlers["config.validate_model"](
        object(),
        "req-validate",
        {
            "model_provider": "openai",
            "model": "gpt-4.1",
            "api_base": "https://api.openai.com/v1",
            "api_key": "secret",
        },
        "sess-1",
    )

    assert server.responses[-1] == {
        "id": "req-validate",
        "ok": True,
        "payload": {
            "provider": "OpenAI",
            "model": "gpt-4.1",
            "response": "hello",
        },
        "error": None,
        "code": None,
    }
