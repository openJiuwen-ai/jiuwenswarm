import pytest

from jiuwenswarm.gateway.a2a_manager import A2AIngressConfig, A2AManager
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _register_web_handlers,
)
from jiuwenswarm.gateway.channel_manager.web.web_http_routes import MAPPED_ROUTES


class _WebChannelProbe:
    def __init__(self) -> None:
        self.methods = {}
        self.responses = []

    def register_method(self, name, handler) -> None:
        self.methods[name] = handler

    def on_connect(self, handler) -> None:
        self.on_connect_handler = handler

    async def send_response(
        self, ws, req_id, *, ok, payload=None, error=None, code=None
    ) -> None:
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload, "error": error, "code": code}
        )


class _ChannelManagerProbe:
    def register_channel(self, channel) -> None:
        return None

    def unregister_channel(self, channel_id) -> None:
        return None


class _RepositoryProbe:
    def save(self, config) -> None:
        return None


class _ChannelProbe:
    channel_id = "a2a"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _OutboundRegistryProbe:
    async def discover(self, url, card_path=None):
        return {"discovery_id": "disc-1", "url": url, "card_path": card_path}

    async def register(self, params):
        return {"agent_id": "agent-1", "display_name": params.get("display_name")}

    async def list_agents(self):
        return {"items": [], "total": 0}

    async def get_agent(self, agent_id):
        return {"agent_id": agent_id}

    async def update_agent(self, agent_id, params):
        return {"agent_id": agent_id, **params}

    async def set_user_enabled(self, agent_id, user_enabled):
        return {
            "agent_id": agent_id,
            "user_enabled": user_enabled,
            "manager_enabled": True,
            "effective_enabled": user_enabled,
        }

    async def refresh_agent(self, agent_id):
        return {"agent_id": agent_id, "refreshed": True}

    async def confirm_revision(self, agent_id, *, accept=True):
        return {"agent_id": agent_id, "accepted": accept}

    async def delete_agent(self, agent_id):
        return {"agent_id": agent_id, "deleted": True}

    async def get_dispatch(self, dispatch_id):
        return {"dispatch_id": dispatch_id}

    async def list_dispatches(self, *, limit=200):
        return {"items": [], "total": 0, "limit": limit}


class _OutboundSettingsProbe:
    def __init__(self) -> None:
        self.enabled = False

    def load(self):
        return {"allow_loopback_http": self.enabled}

    def save(self, *, allow_loopback_http):
        self.enabled = allow_loopback_http


@pytest.mark.asyncio
async def test_a2a_ingress_web_handlers_return_snapshots():
    channel = _WebChannelProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))

    await channel.methods["a2a.ingress.get"](object(), "get", {}, "session")
    await channel.methods["a2a.ingress.history"](
        object(), "history", {"limit": 20}, "session"
    )
    await channel.methods["a2a.ingress.update"](
        object(), "update", {"port": 19123}, "session"
    )
    await channel.methods["a2a.ingress.enable"](object(), "enable", {}, "session")

    assert channel.responses[0]["payload"]["state"] == "disabled"
    assert channel.responses[1]["payload"] == {"items": [], "total": 0}
    assert channel.responses[2]["payload"]["desired_port"] == 19123
    assert channel.responses[3]["payload"]["state"] == "running"


@pytest.mark.asyncio
async def test_saved_ingress_credential_is_returned_to_configuration_ui():
    channel = _WebChannelProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))
    credential = "test-viewable-ingress-credential"
    await channel.methods["a2a.ingress.update"](
        object(),
        "save",
        {"config": {"auth_type": "bearer", "credential": credential}, "apply": True},
        "session",
    )
    await channel.methods["a2a.ingress.get"](object(), "refresh", {}, "session")
    await channel.methods["a2a.ingress.get"](object(), "poll", {}, "session")
    for response in channel.responses:
        assert response["ok"] is True
        assert credential not in str(response)
        assert "credential" not in response["payload"]
        assert "desired_credential" not in response["payload"]
        assert "credential_hash" not in response["payload"]
    await channel.methods["a2a.ingress.update"](
        object(), "failed-save", {"config": {"rpc_path": "invalid"}}, "session"
    )
    assert channel.responses[-1]["ok"] is False
    assert credential not in str(channel.responses[-1])
    await channel.methods["a2a.ingress.edit"](object(), "edit", {}, "session")
    assert channel.responses[-1]["payload"]["credential"] == credential
    assert not any(
        route.rpc_method in {"a2a.ingress.edit", "a2a.outbound.edit"}
        for route in MAPPED_ROUTES
    )


@pytest.mark.asyncio
async def test_a2a_ingress_update_apply_disables_the_running_service():
    channel = _WebChannelProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))

    await channel.methods["a2a.ingress.enable"](object(), "enable", {}, "session")
    await channel.methods["a2a.ingress.update"](
        object(), "update", {"config": {"enabled": False}, "apply": True}, "session"
    )

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["state"] == "disabled"
    assert channel.responses[-1]["payload"]["effective_rpc_url"] is None


@pytest.mark.asyncio
async def test_a2a_ingress_handler_returns_operation_error_snapshot():
    channel = _WebChannelProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))

    await channel.methods["a2a.ingress.update"](
        object(),
        "update",
        {"config": {"rpc_path": "not-absolute"}, "apply": True},
        "session",
    )

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "A2A_CONFIG_INVALID"
    assert channel.responses[-1]["payload"]["desired_rpc_path"] == "/a2a"


@pytest.mark.asyncio
async def test_a2a_ingress_history_rejects_non_integer_limit():
    channel = _WebChannelProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))

    await channel.methods["a2a.ingress.history"](
        object(), "history", {"limit": "invalid"}, "session"
    )

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "A2A_CONFIG_INVALID"
    assert channel.responses[-1]["error"] == "limit must be an integer"


@pytest.mark.asyncio
async def test_a2a_ingress_lifecycle_handlers_tolerate_missing_manager():
    channel = _WebChannelProbe()
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=None))

    await channel.methods["a2a.ingress.enable"](object(), "enable", {}, "session")
    await channel.methods["a2a.ingress.disable"](object(), "disable", {}, "session")
    await channel.methods["a2a.ingress.reload"](object(), "reload", {}, "session")

    assert [item["ok"] for item in channel.responses] == [False, False, False]
    assert {item["code"] for item in channel.responses} == {"A2A_BIND_FAILED"}


def test_a2a_ingress_http_routes_map_to_rpc_methods():
    routes = {
        (route.http_method, route.path): route.rpc_method for route in MAPPED_ROUTES
    }

    assert routes[("GET", "/a2a/ingress")] == "a2a.ingress.get"
    assert routes[("GET", "/a2a/ingress/history")] == "a2a.ingress.history"
    assert routes[("PATCH", "/a2a/ingress")] == "a2a.ingress.update"
    assert routes[("POST", "/a2a/ingress:enable")] == "a2a.ingress.enable"
    assert routes[("POST", "/a2a/ingress:disable")] == "a2a.ingress.disable"
    assert routes[("POST", "/a2a/ingress:reload")] == "a2a.ingress.reload"


@pytest.mark.asyncio
async def test_a2a_outbound_web_handlers_expose_management_facade():
    channel = _WebChannelProbe()
    settings = _OutboundSettingsProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
        outbound_registry=_OutboundRegistryProbe(),
        outbound_settings_repository=settings,
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))

    await channel.methods["a2a.outbound.discover"](
        object(), "discover", {"url": "https://agent.example.com"}, "session"
    )
    await channel.methods["a2a.outbound.register"](
        object(),
        "register",
        {"discovery_id": "disc-1", "display_name": "Agent"},
        "session",
    )
    await channel.methods["a2a.outbound.list"](object(), "list", {}, "session")
    await channel.methods["a2a.outbound.get"](
        object(), "get", {"agent_id": "agent-1"}, "session"
    )
    await channel.methods["a2a.outbound.update"](
        object(), "update", {"agent_id": "agent-1", "enabled": False}, "session"
    )
    await channel.methods["a2a.outbound.refresh"](
        object(), "refresh", {"agent_id": "agent-1"}, "session"
    )
    await channel.methods["a2a.outbound.confirm_revision"](
        object(), "confirm", {"agent_id": "agent-1", "accept": False}, "session"
    )
    await channel.methods["a2a.outbound.delete"](
        object(), "delete", {"agent_id": "agent-1"}, "session"
    )
    await channel.methods["a2a.outbound.dispatch.get"](
        object(), "dispatch_get", {"dispatch_id": "dispatch-1"}, "session"
    )

    await channel.methods["a2a.outbound.settings.get"](
        object(), "settings-get", {}, "session"
    )
    await channel.methods["a2a.outbound.settings.update"](
        object(), "settings-update", {"allow_loopback_http": True}, "session"
    )
    await channel.methods["a2a.outbound.dispatch.list"](
        object(), "dispatch-list", {"limit": 20}, "session"
    )
    await channel.methods["a2a.outbound.enabled.update"](
        object(),
        "enabled-update",
        {"agent_id": "agent-1", "user_enabled": False},
        "session",
    )

    assert channel.responses[0]["payload"]["discovery_id"] == "disc-1"
    assert channel.responses[1]["payload"]["agent_id"] == "agent-1"
    assert channel.responses[2]["payload"]["total"] == 0
    assert channel.responses[3]["payload"]["agent_id"] == "agent-1"
    assert channel.responses[4]["payload"]["enabled"] is False
    assert channel.responses[5]["payload"]["refreshed"] is True
    assert channel.responses[6]["payload"]["accepted"] is False
    assert channel.responses[7]["payload"]["deleted"] is True
    assert channel.responses[8]["payload"]["dispatch_id"] == "dispatch-1"
    assert channel.responses[9]["payload"] == {"allow_loopback_http": False}
    assert channel.responses[10]["payload"] == {"allow_loopback_http": True}
    assert channel.responses[11]["payload"] == {"items": [], "total": 0, "limit": 20}
    assert channel.responses[12]["payload"]["user_enabled"] is False
    assert settings.enabled is True
    assert all(item["ok"] for item in channel.responses)


@pytest.mark.asyncio
async def test_a2a_outbound_dispatch_list_rejects_non_integer_limit():
    channel = _WebChannelProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
        outbound_registry=_OutboundRegistryProbe(),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))

    await channel.methods["a2a.outbound.dispatch.list"](
        object(), "dispatch-list", {"limit": "invalid"}, "session"
    )

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "A2A_OUTBOUND_STORE_INVALID"
    assert channel.responses[-1]["error"] == "limit must be an integer"


@pytest.mark.asyncio
async def test_a2a_outbound_dispatch_list_clamps_limit_to_200():
    channel = _WebChannelProbe()
    registry = _OutboundRegistryProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
        outbound_registry=registry,
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))

    await channel.methods["a2a.outbound.dispatch.list"](
        object(), "dispatch-list", {"limit": 500}, "session"
    )

    assert channel.responses[-1]["payload"]["limit"] == 200


@pytest.mark.asyncio
async def test_a2a_outbound_user_enabled_requires_boolean():
    channel = _WebChannelProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
        outbound_registry=_OutboundRegistryProbe(),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))

    await channel.methods["a2a.outbound.enabled.update"](
        object(),
        "enabled-update",
        {"agent_id": "agent-1", "user_enabled": "false"},
        "session",
    )

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "A2A_OUTBOUND_STORE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params", [{"agent_id": "agent-1"}, {"agent_id": "agent-1", "accept": "false"}]
)
async def test_a2a_outbound_confirm_revision_requires_explicit_boolean(params):
    channel = _WebChannelProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_RepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(),
        outbound_registry=_OutboundRegistryProbe(),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=manager))

    await channel.methods["a2a.outbound.confirm_revision"](
        object(), "confirm", params, "session"
    )

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "A2A_OUTBOUND_STORE_INVALID"


@pytest.mark.asyncio
async def test_a2a_outbound_handlers_tolerate_missing_manager():
    channel = _WebChannelProbe()
    _register_web_handlers(WebHandlersBindParams(channel=channel, a2a_manager=None))

    await channel.methods["a2a.outbound.discover"](
        object(), "discover", {"url": "https://agent.example.com"}, "session"
    )
    await channel.methods["a2a.outbound.list"](object(), "list", {}, "session")
    await channel.methods["a2a.outbound.get"](
        object(), "get", {"agent_id": "agent-1"}, "session"
    )
    await channel.methods["a2a.outbound.update"](
        object(), "update", {"agent_id": "agent-1"}, "session"
    )
    await channel.methods["a2a.outbound.enabled.update"](
        object(), "enabled-update", {"agent_id": "agent-1", "user_enabled": True}, "session"
    )
    await channel.methods["a2a.outbound.refresh"](
        object(), "refresh", {"agent_id": "agent-1"}, "session"
    )
    await channel.methods["a2a.outbound.confirm_revision"](
        object(), "confirm", {"agent_id": "agent-1", "accept": True}, "session"
    )
    await channel.methods["a2a.outbound.delete"](
        object(), "delete", {"agent_id": "agent-1"}, "session"
    )
    await channel.methods["a2a.outbound.dispatch.get"](
        object(), "dispatch_get", {"dispatch_id": "dispatch-1"}, "session"
    )

    assert [item["ok"] for item in channel.responses] == [False] * 9
    assert {item["code"] for item in channel.responses} == {
        "A2A_OUTBOUND_STORE_INVALID"
    }


def test_a2a_outbound_http_routes_map_to_rpc_methods():
    routes = {
        (route.http_method, route.path): route.rpc_method for route in MAPPED_ROUTES
    }

    assert routes[("GET", "/a2a/outbound/settings")] == "a2a.outbound.settings.get"
    assert routes[("PATCH", "/a2a/outbound/settings")] == "a2a.outbound.settings.update"
    assert routes[("POST", "/a2a/outbound/discover")] == "a2a.outbound.discover"
    assert routes[("POST", "/a2a/outbound/agents")] == "a2a.outbound.register"
    assert routes[("GET", "/a2a/outbound/agents")] == "a2a.outbound.list"
    assert routes[("GET", "/a2a/outbound/dispatches")] == "a2a.outbound.dispatch.list"
    assert (
        routes[("PATCH", "/a2a/outbound/agents/{agent_id}/enabled")]
        == "a2a.outbound.enabled.update"
    )
    assert routes[("PATCH", "/a2a/outbound/agents/{agent_id}")] == "a2a.outbound.update"
    assert (
        routes[("POST", "/a2a/outbound/agents/{agent_id}:refresh")]
        == "a2a.outbound.refresh"
    )
    assert (
        routes[("POST", "/a2a/outbound/agents/{agent_id}:confirm-revision")]
        == "a2a.outbound.confirm_revision"
    )
    assert (
        routes[("DELETE", "/a2a/outbound/agents/{agent_id}")] == "a2a.outbound.delete"
    )
