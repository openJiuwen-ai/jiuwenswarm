import asyncio

import pytest

from jiuwenswarm.gateway.channel_manager.tui import (
    harmonyos_dev as harmonyos_dev_module,
)
from jiuwenswarm.gateway.channel_manager.tui import tui_connect as tui_connect_module
from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
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
        self.session_owners = {}

    def register_local_handler(self, path, method, handler):
        if path not in self.local_handlers:
            self.local_handlers[path] = {}
        self.local_handlers[path][method] = handler

    def bind_session_owner(self, channel_id, session_id, ws):
        self.session_owners[(channel_id, session_id)] = ws

    def is_session_bound_to_client(self, channel_id, session_id, ws):
        return self.session_owners.get((channel_id, session_id)) is ws

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


class FakeMessageHandler:
    def __init__(self):
        self.cancelled = []
        self.scheduled = []
        self.reconnected = []
        self.disconnected_websockets = []

    async def unregister_ws_subscriptions(self, channel_id, ws_id):
        self.disconnected_websockets.append((channel_id, ws_id))
        return 1

    async def cancel_agent_sessions_on_disconnect(
        self, session_keys, *, stale_request_keys=None, user_id=None
    ):
        self.cancelled.append((session_keys, stale_request_keys or []))
        return True

    async def schedule_cancel_agent_sessions_on_disconnect(
        self, session_keys, *, stale_request_keys=None, user_id=None
    ):
        self.scheduled.append((session_keys, stale_request_keys or []))

    def cancel_scheduled_disconnect_cancel(self, channel_id, session_id):
        self.reconnected.append((channel_id, session_id))
        return True


class BlockingDisconnectMessageHandler(FakeMessageHandler):
    def __init__(self):
        super().__init__()
        self.cancel_started = asyncio.Event()

    async def cancel_agent_sessions_on_disconnect(
        self, session_keys, *, stale_request_keys=None, user_id=None
    ):
        self.cancel_started.set()
        await asyncio.Future()


class FailedDisconnectMessageHandler(FakeMessageHandler):
    async def cancel_agent_sessions_on_disconnect(
        self, session_keys, *, stale_request_keys=None, user_id=None
    ):
        self.cancelled.append((session_keys, stale_request_keys or []))
        return False


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
    assert "tui.disconnect" in cli_handlers
    assert "harmonyos.dev_init" in cli_handlers
    assert "harmonyos.dev_init_cancel" in cli_handlers
    assert "harmonyos.project_init" in cli_handlers

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


@pytest.mark.asyncio
async def test_harmonyos_dev_init_cancel_waits_for_background_cleanup(monkeypatch):
    server = FakeGatewayServer()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    received_params = []

    async def fake_run_harmonyos_dev_init(params):
        received_params.append(params)
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        harmonyos_dev_module,
        "run_harmonyos_dev_init",
        fake_run_harmonyos_dev_init,
    )
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    ws = type("FakeWs", (), {})()
    handlers = server.local_handlers["/tui"]

    await handlers["harmonyos.dev_init"](
        ws,
        "req-init",
        {"operationId": "dev-init-op-1", "skipDevecocliUpdate": True},
        "sess-1",
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert received_params == [{"skipDevecocliUpdate": True}]
    assert all(response["id"] != "req-init" for response in server.responses)

    await asyncio.wait_for(
        handlers["harmonyos.dev_init_cancel"](
            ws,
            "req-cancel",
            {"operationId": "dev-init-op-1"},
            "sess-1",
        ),
        timeout=1,
    )

    assert cancelled.is_set()
    assert server.responses[-2] == {
        "id": "req-init",
        "ok": False,
        "payload": {},
        "error": "HarmonyOS Dev Init operation was cancelled",
        "code": "CANCELLED",
    }
    assert server.responses[-1] == {
        "id": "req-cancel",
        "ok": True,
        "payload": {
            "operationId": "dev-init-op-1",
            "cancelRequested": True,
            "cancelled": True,
        },
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_harmonyos_dev_init_is_cancelled_when_websocket_disconnects(monkeypatch):
    server = FakeGatewayServer()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_run_harmonyos_dev_init(_params):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        harmonyos_dev_module,
        "run_harmonyos_dev_init",
        fake_run_harmonyos_dev_init,
    )
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui"))
    binding.install(server)
    ws = type("FakeWs", (), {})()

    await server.local_handlers["/tui"]["harmonyos.dev_init"](
        ws,
        "req-disconnect-init",
        {"operationId": "dev-init-disconnect-op"},
        "sess-disconnect",
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(binding.disconnect_handler(ws, [], []), timeout=1)

    assert cancelled.is_set()
    assert server.responses[-1]["id"] == "req-disconnect-init"
    assert server.responses[-1]["code"] == "CANCELLED"


@pytest.mark.asyncio
async def test_harmonyos_dev_init_rejects_untrackable_websocket(monkeypatch):
    server = FakeGatewayServer()
    called = False

    async def fake_run_harmonyos_dev_init(_params):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(
        harmonyos_dev_module,
        "run_harmonyos_dev_init",
        fake_run_harmonyos_dev_init,
    )
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    class SlottedWs:
        __slots__ = ()

    await server.local_handlers["/tui"]["harmonyos.dev_init"](
        SlottedWs(),
        "req-untrackable",
        {"operationId": "dev-init-untrackable-op"},
        "sess-untrackable",
    )

    assert called is False
    assert server.responses[-1] == {
        "id": "req-untrackable",
        "ok": False,
        "payload": {},
        "error": "websocket does not support HarmonyOS Dev Init task tracking",
        "code": "INTERNAL_ERROR",
    }


@pytest.mark.asyncio
async def test_tui_disconnect_handler_cancels_session_immediately():
    server = FakeGatewayServer()
    handler = FakeMessageHandler()

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=handler,
            on_config_saved=None,
            path="/tui",
        )
    )

    ws = object()
    server.bind_session_owner("tui", "sess-exit", ws)
    await server.local_handlers["/tui"]["tui.disconnect"](
        ws,
        "req-exit",
        {"reason": "user_exit"},
        "sess-exit",
    )

    assert handler.cancelled == [([("tui", "sess-exit")], [])]
    assert server.responses[-1] == {
        "id": "req-exit",
        "ok": True,
        "payload": {"accepted": True, "session_id": "sess-exit"},
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_tui_disconnect_handler_does_not_cancel_session_owned_by_another_ws():
    server = FakeGatewayServer()
    handler = FakeMessageHandler()
    owner_ws = object()
    exiting_ws = object()
    server.bind_session_owner("tui", "sess-shared", owner_ws)

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=handler,
            on_config_saved=None,
            path="/tui",
        )
    )

    await server.local_handlers["/tui"]["tui.disconnect"](
        exiting_ws,
        "req-exit-other",
        {"reason": "user_exit"},
        "sess-shared",
    )

    assert handler.cancelled == []
    assert server.responses[-1] == {
        "id": "req-exit-other",
        "ok": True,
        "payload": {"accepted": True, "session_id": "sess-shared"},
        "error": None,
        "code": None,
    }


def test_build_cli_route_binding_creates_route_and_install_hook():
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui"))
    server = FakeGatewayServer()

    assert binding.path == "/tui"
    assert binding.channel_id == "tui"
    assert "chat.send" in binding.forward_methods
    assert "history.get" in binding.forward_methods
    assert "team.mq.publish" in binding.forward_methods
    assert "team.mq.publish" in binding.forward_no_local_handler_methods
    assert binding.install is not None

    binding.install(server)

    cli_handlers = server.local_handlers["/tui"]
    assert "config.get" in cli_handlers
    assert "config.validate_model" in cli_handlers
    assert "session.list" in cli_handlers
    assert "chat.send" in cli_handlers


@pytest.mark.asyncio
async def test_tui_route_disconnect_schedules_cancel_for_transport_close():
    handler = FakeMessageHandler()
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui", message_handler=handler))

    await binding.disconnect_handler(
        object(),
        [("tui", "sess-drop")],
        [("tui", "req-drop")],
    )

    assert handler.scheduled == [([("tui", "sess-drop")], [("tui", "req-drop")])]
    assert handler.cancelled == []


@pytest.mark.asyncio
async def test_tui_route_disconnect_unregisters_physical_subscriptions():
    handler = FakeMessageHandler()
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui", message_handler=handler))
    ws = type("FakeWs", (), {"_jiuwen_ws_id": "tui-ws-dead"})()

    await binding.disconnect_handler(ws, [("tui", "sess-drop")], [])

    assert handler.disconnected_websockets == [("tui", "tui-ws-dead")]


@pytest.mark.asyncio
async def test_tui_route_disconnect_skips_scheduled_cancel_after_explicit_exit():
    handler = FakeMessageHandler()
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui", message_handler=handler))
    ws = type("FakeWs", (), {})()
    ws._jiuwenswarm_tui_user_exit = True  # pylint: disable=protected-access

    await binding.disconnect_handler(ws, [("tui", "sess-exit")], [])

    assert handler.scheduled == []


@pytest.mark.asyncio
async def test_tui_route_disconnect_retries_when_explicit_exit_cleanup_is_cancelled():
    server = FakeGatewayServer()
    handler = BlockingDisconnectMessageHandler()
    binding = build_cli_route_binding(
        CliRouteBindParams(path="/tui", message_handler=handler)
    )
    binding.install(server)
    ws = type("FakeWs", (), {})()
    server.bind_session_owner("tui", "sess-exit-race", ws)

    explicit_exit = asyncio.create_task(
        server.local_handlers["/tui"]["tui.disconnect"](
            ws,
            "req-exit-race",
            {"reason": "user_exit"},
            "sess-exit-race",
        )
    )
    await handler.cancel_started.wait()
    explicit_exit.cancel()
    await asyncio.gather(explicit_exit, return_exceptions=True)

    await binding.disconnect_handler(
        ws,
        [("tui", "sess-exit-race")],
        [],
    )

    assert handler.scheduled == [([("tui", "sess-exit-race")], [])]


@pytest.mark.asyncio
async def test_tui_route_disconnect_retries_when_explicit_exit_cleanup_fails():
    server = FakeGatewayServer()
    handler = FailedDisconnectMessageHandler()
    binding = build_cli_route_binding(
        CliRouteBindParams(path="/tui", message_handler=handler)
    )
    binding.install(server)
    ws = type("FakeWs", (), {})()
    server.bind_session_owner("tui", "sess-exit-failed", ws)

    await server.local_handlers["/tui"]["tui.disconnect"](
        ws,
        "req-exit-failed",
        {"reason": "user_exit"},
        "sess-exit-failed",
    )
    await binding.disconnect_handler(
        ws,
        [("tui", "sess-exit-failed")],
        [],
    )

    assert handler.scheduled == [([("tui", "sess-exit-failed")], [])]


def test_tui_session_bind_handler_cancels_pending_disconnect_cancel():
    handler = FakeMessageHandler()
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui", message_handler=handler))

    binding.session_bind_handler("tui", "sess-reconnect")

    assert handler.reconnected == [("tui", "sess-reconnect")]


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

    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.tui.tui_connect.Model", FakeModel)

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


@pytest.mark.asyncio
async def test_command_model_switch_sends_scoped_agent_reload(monkeypatch):
    server = FakeGatewayServer()
    sent_envs = []
    defaults = [
        {
            "alias": "glm",
            "model_client_config": {
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "model_name": "GLM-5",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        },
        {
            "alias": "other",
            "model_client_config": {
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "model_name": "other-model",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        },
    ]

    async def fake_send_tui_agent_request(_client, env, *, label):
        sent_envs.append((env, label))

    def fake_update_config(mutator, **kwargs):
        data = {"models": {"defaults": [dict(d) for d in defaults]}}
        return mutator(data)

    monkeypatch.setattr(tui_connect_module, "_send_tui_agent_request", fake_send_tui_agent_request)
    monkeypatch.setattr(tui_connect_module, "update_config", fake_update_config)
    monkeypatch.setattr(
        tui_connect_module,
        "get_config_raw",
        lambda: {"models": {"defaults": defaults}},
    )
    monkeypatch.setattr(tui_connect_module, "get_config", lambda: {"models": {"defaults": defaults}})

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=object(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    await server.local_handlers["/tui"]["command.model"](
        object(),
        "req-switch",
        {"model": "glm"},
        "tui_session_1",
    )
    await asyncio.sleep(0)

    assert server.responses[-1] == {
        "id": "req-switch",
        "ok": True,
        "payload": {
            "current": "GLM-5",
            "requested": "glm",
            "type": "switched",
            "applied": True,
        },
        "error": None,
        "code": None,
    }
    assert len(sent_envs) == 1
    env, label = sent_envs[0]
    assert label == "command.model.switch"
    assert env.params["target_channel_id"] == "tui"
    assert env.params["target_session_id"] == "tui_session_1"
    assert env.params["reason"] == "model_switch"


@pytest.mark.asyncio
async def test_session_list_returns_agent_timeout_before_tui_request_timeout(monkeypatch):
    server = FakeGatewayServer()

    class HangingAgentClient:
        async def send_request(self, env):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.agent_request_timeout._TUI_DEFAULT_UNARY_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=HangingAgentClient(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    await asyncio.wait_for(
        server.local_handlers["/tui"]["session.list"](
            object(),
            "req-session-list",
            {"limit": 10},
            "sess-1",
        ),
        timeout=0.2,
    )

    assert server.responses[-1] == {
        "id": "req-session-list",
        "ok": False,
        "payload": {},
        "error": "AgentServer request timed out",
        "code": "AGENT_SERVER_TIMEOUT",
    }


def test_get_model_names_skips_empty_name_entries(tmp_path, monkeypatch):
    """Bug #2665: get_model_names() should skip entries with unresolved env vars
    (empty resolved name), so available_models indices don't match _raw_defaults indices.
    The frontend should use models[].index instead of available_models array index.
    """
    import yaml

    from jiuwenswarm.common.config import get_model_names

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "defaults": [
                        {
                            "model_client_config": {
                                "api_key": "${API_KEY}",
                                "api_base": "${API_BASE}",
                                "model_name": "${MODEL_NAME}",
                                "client_provider": "${MODEL_PROVIDER}",
                            },
                            "model_config_obj": {"temperature": 0.95},
                            "is_default": True,
                        },
                        {
                            "model_client_config": {
                                "api_key": "sk-test-key",
                                "api_base": "https://dashscope.aliyuncs.com/v1",
                                "model_name": "glm-5",
                                "client_provider": "OpenAI",
                            },
                            "model_config_obj": {"temperature": 0.95},
                        },
                    ]
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.CONFIG_YAML_PATH", cfg, raising=False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config._CONFIG_YAML_PATH", cfg, raising=False,
    )

    import jiuwenswarm.common.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "CONFIG_YAML_PATH", cfg)
    monkeypatch.setattr(cfg_mod, "_CONFIG_YAML_PATH", cfg)

    for var in ("API_KEY", "API_BASE", "MODEL_NAME", "MODEL_PROVIDER"):
        monkeypatch.delenv(var, raising=False)

    names = get_model_names()
    assert names == ["glm-5"], (
        f"get_model_names() should skip the placeholder entry with unresolved env vars, "
        f"got {names}"
    )


def test_model_meta_index_field_matches_raw_defaults_position():
    """Bug #2665: _model_meta must include the _raw_defaults index
    so the frontend can send the correct index for model switching.
    """
    from jiuwenswarm.common.config import resolve_env_vars

    defaults = [
        {
            "model_client_config": {
                "api_key": "${API_KEY}",
                "api_base": "${API_BASE}",
                "model_name": "${MODEL_NAME}",
                "client_provider": "${MODEL_PROVIDER}",
            },
            "model_config_obj": {"temperature": 0.95},
            "is_default": True,
        },
        {
            "model_client_config": {
                "api_key": "sk-test-key",
                "api_base": "https://dashscope.aliyuncs.com/v1",
                "model_name": "glm-5",
                "client_provider": "OpenAI",
            },
            "model_config_obj": {"temperature": 0.95},
        },
    ]

    def _model_meta(i, e):
        mcc = e.get("model_client_config") or {}
        mco = e.get("model_config_obj") or {}
        _alias = e.get("alias", "")
        _resolved_alias = resolve_env_vars(str(_alias)) if _alias else ""
        _model_name = resolve_env_vars(str(mcc.get("model_name", "")))
        _api_key = resolve_env_vars(str(mcc.get("api_key", "")))
        return {
            "index": i,
            "name": _resolved_alias or _model_name,
            "alias": _resolved_alias,
            "model_name": _model_name,
            "model_provider": resolve_env_vars(str(mcc.get("client_provider", ""))),
            "api_base": resolve_env_vars(str(mcc.get("api_base", ""))),
            "reasoning_level": resolve_env_vars(str(mco.get("reasoning_level", ""))),
            "api_key_suffix": _api_key[-4:] if _api_key else "",
            "is_current": i == 0,
        }

    import os
    for var in ("API_KEY", "API_BASE", "MODEL_NAME", "MODEL_PROVIDER"):
        os.environ.pop(var, None)

    models = [_model_meta(i, e) for i, e in enumerate(defaults) if isinstance(e, dict)]

    assert models[0]["index"] == 0
    assert models[0]["name"] == ""
    assert models[1]["index"] == 1
    assert models[1]["name"] == "glm-5"

    selectable = [m for m in models if m["name"] and not m["name"].lower() in ("video", "audio", "vision")]
    assert len(selectable) == 1
    assert selectable[0]["index"] == 1, (
        "Frontend should use selectable[0]['index']=1 as origIdx, "
        "not the available_models array index (0)"
    )
