"""Enterprise Manager Config Receiver A2A projection tests."""

# ruff: noqa: E402 -- install optional openjiuwen-runtime DB stubs before imports

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from a2a.types import Message, Part, Role, StreamResponse
from fastapi.testclient import TestClient


def _install_runtime_db_stubs() -> None:
    """Supply table-definition types when the lightweight test env lacks runtime DB."""
    try:
        __import__("openjiuwen_runtime.foundation.db.table_def")
        return
    except ModuleNotFoundError:
        pass

    class _Definition:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            for key, value in kwargs.items():
                setattr(self, key, value)

    handler = ModuleType("openjiuwen_runtime.foundation.db.handler")
    handler.DBHandler = object  # type: ignore[attr-defined]
    table_def = ModuleType("openjiuwen_runtime.foundation.db.table_def")
    table_def.ColumnDefinition = _Definition  # type: ignore[attr-defined]
    table_def.IndexDefinition = _Definition  # type: ignore[attr-defined]
    table_def.TableDefinition = _Definition  # type: ignore[attr-defined]
    sys.modules[handler.__name__] = handler
    sys.modules[table_def.__name__] = table_def
    db_package = sys.modules["openjiuwen_runtime.foundation.db"]
    for module_name, class_name in (
        ("mysql_handler", "MySQLHandler"),
        ("postgresql_handler", "PostgreSQLHandler"),
        ("sqlite_handler", "SQLiteHandler"),
    ):
        module = ModuleType(f"openjiuwen_runtime.foundation.db.{module_name}")
        setattr(module, class_name, type(class_name, (), {}))
        sys.modules[module.__name__] = module
        setattr(db_package, module_name, module)
    runtime_log = ModuleType("openjiuwen_runtime.foundation.log")
    runtime_log.get_logger = logging.getLogger  # type: ignore[attr-defined]
    sys.modules[runtime_log.__name__] = runtime_log


_install_runtime_db_stubs()

from jiuwenswarm.common.secrets import SecretStore
from jiuwenswarm.gateway.a2a_manager.outbound import (
    A2AOutboundAvailability,
    A2AOutboundDispatch,
    A2AOutboundDispatcher,
    A2AOutboundDispatchMode,
    A2AOutboundDispatchStatus,
    A2AOutboundError,
    A2AOutboundErrorCode,
    A2AOutboundRegistry,
    EnterpriseA2AProjection,
)
from jiuwenswarm.gateway.config.enterprise import (
    clear_enterprise_record_repositories,
    set_enterprise_record_repositories,
)
from jiuwenswarm.gateway.config.enterprise.tables.table_init import (
    ALL_TABLE_DEFINITIONS,
)
from jiuwenswarm.gateway.storage.backends.memory_persistent import (
    InMemoryPersistentBackend,
)
from jiuwenswarm.gateway.storage_assembly.setup import (
    create_a2a_outbound_repository,
    create_enterprise_record_repositories,
)


def import_manager_config_receiver_module(module_suffix: str) -> Any:
    """Load the bundled extension without relying on the removed runtime importer."""
    parent_name = "jiuwenswarm.loaded_extension"
    package_name = f"{parent_name}.manager_config_receiver"
    extension_root = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "jiuwenclaw-ee"
        / "gateway"
        / "extensions"
        / "manager_config_receiver"
    )
    if parent_name not in sys.modules:
        parent = ModuleType(parent_name)
        parent.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent_name] = parent
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(extension_root)]  # type: ignore[attr-defined]
        package.__package__ = package_name
        sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.{module_suffix}")


class _Secrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def set(self, key: str, value: str, *, algorithm: str | None = None) -> None:
        del algorithm
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _CompletedClient:
    async def send_message(self, request: Any, *, context: Any = None):
        del request, context
        yield StreamResponse(
            message=Message(
                message_id="remote-message",
                role=Role.ROLE_AGENT,
                parts=[Part(text="done")],
            )
        )

    async def close(self) -> None:
        return None


class _UnreachableClient(_CompletedClient):
    async def send_message(self, request: Any, *, context: Any = None):
        del request, context
        if False:
            yield None
        raise OSError("connection refused")


def _outbound_payload(
    *, operation: str = "replace", value: str | None = "secret"
) -> dict[str, Any]:
    credential: dict[str, Any] = {"operation": operation}
    if value is not None:
        credential["value"] = value
    return {
        "template_id": "a2a-weather",
        "template_name": "Weather Agent",
        "description": "weather",
        "a2a_tags": ["weather"],
        "source_url": "https://example.test/a2a",
        "card_path": "/.well-known/agent-card.json",
        "agent_card": {"name": "Weather Agent"},
        "card_fingerprint": "sha256:card",
        "card_revision": 1,
        "selected_interface": {
            "url": "https://example.test/a2a",
            "protocol_binding": "JSONRPC",
            "protocol_version": "1.0",
        },
        "connect_timeout_seconds": 10,
        "sync_wait_seconds": 120,
        "enabled": True,
        "credential": credential,
        "data": {},
        "updated_at": "2026-09-05T00:00:00Z",
    }


def _projection_record() -> dict[str, Any]:
    payload = _outbound_payload(operation="clear", value=None)
    payload.pop("credential")
    payload["credential_ref"] = None
    payload["created_at"] = "2026-09-05T00:00:00Z"
    return payload


@pytest.fixture
def a2a_receiver(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, Any], _Secrets]]:
    from jiuwenswarm.gateway.a2a_manager.outbound.discovery import A2AOutboundDiscoveryService

    async def resolve(host, _port):
        return ["8.8.8.8" if host == "gateway.test" else host]

    routers = import_manager_config_receiver_module("routers.template_routers")
    monkeypatch.setattr(
        routers, "A2AOutboundDiscoveryService",
        lambda: A2AOutboundDiscoveryService(address_resolver=resolve),
    )
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    set_enterprise_record_repositories(repos)
    secrets = _Secrets()
    SecretStore.reset_for_tests(secrets)  # type: ignore[arg-type]
    app_module = import_manager_config_receiver_module("http.app")
    try:
        with TestClient(
            app_module.create_app(), base_url="https://gateway.test"
        ) as client:
            yield client, repos, secrets
    finally:
        SecretStore.reset_for_tests()
        clear_enterprise_record_repositories()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_policy", ["yes", {"allow_http": 1}, [], False])
async def test_invalid_network_policy_does_not_break_enterprise_directory(
    invalid_policy, caplog, monkeypatch
) -> None:
    from jiuwenswarm.gateway.a2a_manager.outbound.enterprise import logger

    monkeypatch.setattr(logger, "handlers", [*logger.handlers, caplog.handler])
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    valid = _projection_record()
    invalid = {
        **_projection_record(), "template_id": "invalid-policy-agent",
        "data": {"network_policy": invalid_policy},
    }
    await repos["a2a_outbound_template"].create(valid)
    await repos["a2a_outbound_template"].create(invalid)
    projection = EnterpriseA2AProjection(
        store, templates=repos["a2a_outbound_template"],
        user_states=repos["a2a_outbound_user_state"],
        runtime_states=repos["a2a_outbound_runtime_state"],
    )
    agents = {view.agent.agent_id: view.agent for view in await projection.list_projected_agents()}
    assert set(agents) == {"a2a-weather", "invalid-policy-agent"}
    assert agents["invalid-policy-agent"].network_policy == {}
    assert "Invalid A2A network_policy template_id=invalid-policy-agent" in caplog.text
    direct = await projection.get_projected_agent("invalid-policy-agent")
    assert direct.agent.network_policy == {}


def test_a2a_outbound_receiver_crud_and_secret_store(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
) -> None:
    client, repos, secrets = a2a_receiver
    path = "/api/v1/a2a-outbound-templates"
    credential_ref = "a2a/outbound/a2a-weather.api_key"

    response = client.post(path, json=_outbound_payload())
    assert response.status_code == 200
    stored = repos["a2a_outbound_template"]
    projection = client.portal.call(lambda: stored.get(template_id="a2a-weather"))
    assert projection["credential_ref"] == credential_ref
    assert "credential" not in projection
    assert secrets.values[credential_ref] == "secret"

    sanitized = _outbound_payload()
    sanitized["agent_card"]["authorization"] = "Bearer card-secret"
    sanitized["selected_interface"]["api_key"] = "interface-secret"
    sanitized["data"]["access_token"] = "metadata-secret"
    assert client.post(path, json=sanitized).status_code == 200
    projection = client.portal.call(lambda: stored.get(template_id="a2a-weather"))
    assert projection["agent_card"]["authorization"] == "******"
    assert projection["selected_interface"]["api_key"] == "******"
    assert projection["data"]["access_token"] == "******"

    replay = _outbound_payload()
    replay["template_name"] = "Weather Agent v2"
    assert client.post(path, json=replay).status_code == 200
    projection = client.portal.call(lambda: stored.get(template_id="a2a-weather"))
    assert projection["template_name"] == "Weather Agent v2"
    assert secrets.values[credential_ref] == "secret"

    assert (
        client.patch(
            f"{path}/a2a-weather",
            json={"enabled": False, "credential": {"operation": "keep"}},
        ).status_code
        == 200
    )
    projection = client.portal.call(lambda: stored.get(template_id="a2a-weather"))
    assert projection["enabled"] is False
    assert secrets.values[credential_ref] == "secret"

    assert (
        client.patch(
            f"{path}/a2a-weather",
            json={"credential": {"operation": "clear"}},
        ).status_code
        == 200
    )
    projection = client.portal.call(lambda: stored.get(template_id="a2a-weather"))
    assert projection["credential_ref"] is None
    assert credential_ref not in secrets.values

    user_states = repos["a2a_outbound_user_state"]
    runtime_states = repos["a2a_outbound_runtime_state"]
    dispatches = repos["a2a_outbound_dispatch"]
    client.portal.call(
        lambda: user_states.create(
            {
                "template_id": "a2a-weather",
                "user_enabled": False,
                "updated_at": "2026-09-05T00:00:00Z",
            }
        )
    )
    client.portal.call(
        lambda: runtime_states.create(
            {
                "template_id": "a2a-weather",
                "availability": "available",
                "updated_at": "2026-09-05T00:00:00Z",
            }
        )
    )
    client.portal.call(
        lambda: dispatches.create(
            {"dispatch_id": "dispatch-history", "agent_id": "a2a-weather"}
        )
    )

    assert client.request("DELETE", f"{path}/a2a-weather", json={}).status_code == 200
    assert client.portal.call(
        lambda: user_states.get(template_id="a2a-weather")
    ) is None
    assert client.portal.call(
        lambda: runtime_states.get(template_id="a2a-weather")
    ) is None
    assert client.portal.call(
        lambda: dispatches.get(dispatch_id="dispatch-history")
    ) is not None
    assert client.request("DELETE", f"{path}/a2a-weather", json={}).status_code == 200


def test_a2a_access_policy_receiver_is_idempotent(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
) -> None:
    client, repos, _ = a2a_receiver
    path = "/api/v1/a2a-access-policies"
    payload = {
        "policy_id": "policy-1",
        "policy_name": "Production",
        "description": None,
        "mode": "allowlist",
        "member_template_ids": ["a2a-weather", "a2a-weather"],
        "enabled": True,
        "revision": 1,
        "data": {"source": "manager"},
        "updated_at": "2026-09-05T00:00:00Z",
        "sig": "legacy-signature",
        "enc": {"version": "legacy"},
    }
    assert client.post(path, json=payload).status_code == 200
    payload.update({"mode": "denylist", "revision": 2})
    assert client.post(path, json=payload).status_code == 200

    repo = repos["a2a_access_policy_template"]
    row = client.portal.call(lambda: repo.get(policy_id="policy-1"))
    assert row["mode"] == "denylist"
    assert row["member_template_ids"] == ["a2a-weather"]
    assert row["data"] == {"source": "manager"}
    assert "sig" not in row
    assert "enc" not in row

    assert (
        client.patch(
            f"{path}/policy-1", json={"enabled": False, "revision": 3}
        ).status_code
        == 200
    )
    assert client.portal.call(lambda: repo.get(policy_id="policy-1"))["revision"] == 3
    assert client.request("DELETE", f"{path}/policy-1", json={}).status_code == 200
    assert client.request("DELETE", f"{path}/policy-1", json={}).status_code == 200


def test_a2a_receiver_rejects_undecryptable_envelope(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
) -> None:
    client, _, secrets = a2a_receiver
    payload = _outbound_payload(value="ENC:v1:dek:wrapped:ciphertext")
    response = client.post("/api/v1/a2a-outbound-templates", json=payload)
    assert response.status_code == 422
    assert secrets.values == {}

    payload = _outbound_payload(operation="keep", value=None)
    response = client.post("/api/v1/a2a-outbound-templates", json=payload)
    assert response.status_code == 422


def test_a2a_receiver_requires_complete_selected_interface(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
) -> None:
    client, _, _ = a2a_receiver
    path = "/api/v1/a2a-outbound-templates"
    for missing in ("protocol_binding", "protocol_version", "url"):
        payload = _outbound_payload()
        payload["selected_interface"].pop(missing)
        assert client.post(path, json=payload).status_code == 422


@pytest.mark.parametrize(
    "card_path",
    ["../agent-card.json", "//evil.test/card", "/card?source=manager"],
)
def test_a2a_receiver_rejects_invalid_card_path(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
    card_path: str,
) -> None:
    client, _, _ = a2a_receiver
    payload = _outbound_payload()
    payload["card_path"] = card_path
    assert client.post("/api/v1/a2a-outbound-templates", json=payload).status_code == 422


def test_a2a_credential_replace_requires_https(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
) -> None:
    _, _, secrets = a2a_receiver
    app_module = import_manager_config_receiver_module("http.app")
    with TestClient(app_module.create_app(), base_url="http://gateway.test") as client:
        response = client.post(
            "/api/v1/a2a-outbound-templates",
            json=_outbound_payload(),
        )
    assert response.status_code == 400
    assert secrets.values == {}


def test_a2a_credential_replace_accepts_trusted_https_proxy(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, secrets = a2a_receiver
    monkeypatch.setenv("GATEWAY_CONFIG_FORWARDED_ALLOW_IPS", "testclient")
    app_module = import_manager_config_receiver_module("http.app")
    with TestClient(app_module.create_app(), base_url="http://gateway.test") as client:
        response = client.post(
            "/api/v1/a2a-outbound-templates",
            json=_outbound_payload(),
            headers={"X-Forwarded-Proto": "https"},
        )
    assert response.status_code == 200
    assert secrets.values == {"a2a/outbound/a2a-weather.api_key": "secret"}


def test_a2a_credential_replace_rejects_untrusted_https_proxy_header(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, secrets = a2a_receiver
    monkeypatch.setenv("GATEWAY_CONFIG_FORWARDED_ALLOW_IPS", "192.0.2.1")
    app_module = import_manager_config_receiver_module("http.app")
    with TestClient(app_module.create_app(), base_url="http://gateway.test") as client:
        response = client.post(
            "/api/v1/a2a-outbound-templates",
            json=_outbound_payload(),
            headers={"X-Forwarded-Proto": "https"},
        )
    assert response.status_code == 400
    assert secrets.values == {}


def test_a2a_receiver_rejects_wildcard_trusted_proxy_config() -> None:
    config_module = import_manager_config_receiver_module("infrastructure.config")
    with pytest.raises(ValueError, match="must not trust all hosts"):
        config_module.Settings(GATEWAY_CONFIG_FORWARDED_ALLOW_IPS="*")


def test_a2a_credential_is_restored_when_projection_update_fails(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repos, secrets = a2a_receiver
    path = "/api/v1/a2a-outbound-templates"
    credential_ref = "a2a/outbound/a2a-weather.api_key"
    assert client.post(path, json=_outbound_payload()).status_code == 200

    repo = repos["a2a_outbound_template"]

    async def _fail_update(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("write failed")

    monkeypatch.setattr(repo, "update", _fail_update)
    with pytest.raises(RuntimeError, match="write failed"):
        client.patch(
            f"{path}/a2a-weather",
            json={"credential": {"operation": "replace", "value": "new-secret"}},
        )
    assert secrets.values[credential_ref] == "secret"


def test_a2a_credential_is_removed_when_projection_create_fails(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repos, secrets = a2a_receiver
    repo = repos["a2a_outbound_template"]

    async def _fail_create(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("write failed")

    monkeypatch.setattr(repo, "create", _fail_create)
    with pytest.raises(RuntimeError, match="write failed"):
        client.post(
            "/api/v1/a2a-outbound-templates",
            json=_outbound_payload(),
        )
    assert secrets.values == {}


def test_a2a_outbound_purge_removes_projection_and_secret(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
) -> None:
    client, repos, secrets = a2a_receiver
    path = "/api/v1/a2a-outbound-templates"
    assert client.post(path, json=_outbound_payload()).status_code == 200

    lifecycle = import_manager_config_receiver_module(
        "core.instance.instance_data_lifecycle"
    )
    count = client.portal.call(
        lambda: lifecycle._purge_enterprise_table("a2a_outbound_template")
    )
    assert count == 1
    assert (
        client.portal.call(
            lambda: repos["a2a_outbound_template"].get(
                template_id="a2a-weather"
            )
        )
        is None
    )
    assert secrets.values == {}
    assert "a2a_outbound_template" in lifecycle.INSTANCE_PURGE_TABLES
    assert "a2a_access_policy_template" in lifecycle.INSTANCE_PURGE_TABLES
    assert "a2a_outbound_user_state" in lifecycle.INSTANCE_PURGE_TABLES
    assert "a2a_outbound_runtime_state" in lifecycle.INSTANCE_PURGE_TABLES
    assert "a2a_outbound_dispatch" in lifecycle.INSTANCE_PURGE_TABLES


def test_a2a_tables_are_in_enterprise_init_and_catalog() -> None:
    table_names = {definition.table_name for definition in ALL_TABLE_DEFINITIONS}
    assert "a2a_outbound_template" in table_names
    assert "a2a_access_policy_template" in table_names
    assert "a2a_outbound_user_state" in table_names
    assert "a2a_outbound_runtime_state" in table_names
    assert "a2a_outbound_dispatch" in table_names

    repos = create_enterprise_record_repositories(InMemoryPersistentBackend())
    assert "a2a_outbound_template" in repos
    assert "a2a_access_policy_template" in repos
    assert "a2a_outbound_user_state" in repos
    assert "a2a_outbound_runtime_state" in repos
    assert "a2a_outbound_dispatch" in repos


def test_manager_config_public_endpoint_can_advertise_https() -> None:
    config = import_manager_config_receiver_module("infrastructure.config")
    public_endpoint = import_manager_config_receiver_module(
        "infrastructure.public_endpoint"
    )
    settings = config.Settings(
        GATEWAY_CONFIG_PUBLIC_SCHEME="https",
        GATEWAY_CONFIG_PUBLIC_HOST="gateway.test",
        GATEWAY_CONFIG_HTTP_PORT=443,
    )
    assert public_endpoint.resolve_public_endpoint(settings) == (
        "https://gateway.test:443"
    )


@pytest.mark.asyncio
async def test_enterprise_projection_merges_manager_user_and_runtime_state() -> None:
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    await repos["a2a_outbound_template"].create(_projection_record())
    projection = EnterpriseA2AProjection(
        store,
        templates=repos["a2a_outbound_template"],
        user_states=repos["a2a_outbound_user_state"],
        runtime_states=repos["a2a_outbound_runtime_state"],
    )
    registry = A2AOutboundRegistry(projection)

    initial = await projection.get_projected_agent("a2a-weather")
    assert initial is not None
    assert initial.manager_enabled is True
    assert initial.user_enabled is True
    assert initial.effective_enabled is True
    assert initial.agent.enabled is True
    assert initial.agent.network_policy == {}
    network_policy = {"allow_http": True, "allow_private_network": True}
    await repos["a2a_outbound_template"].update(
        {"template_id": "a2a-weather"}, {"data": {"network_policy": network_policy}}
    )
    updated_policy = await projection.get_projected_agent("a2a-weather")
    assert updated_policy.agent.network_policy == network_policy
    await repos["a2a_outbound_template"].update(
        {"template_id": "a2a-weather"}, {"data": {"network_policy": {}}}
    )
    revoked_policy = await projection.get_projected_agent("a2a-weather")
    assert revoked_policy.agent.network_policy == {}

    disabled = await projection.set_user_enabled("a2a-weather", False)
    assert disabled.manager_enabled is True
    assert disabled.user_enabled is False
    assert disabled.effective_enabled is False
    user_row = await store.get(
        "a2a_outbound_user_state", {"template_id": "a2a-weather"}
    )
    assert isinstance(user_row["updated_at"], datetime)
    with pytest.raises(A2AOutboundError) as exc_info:
        await projection.set_user_enabled("missing-agent", True)
    assert exc_info.value.code is A2AOutboundErrorCode.AGENT_NOT_REGISTERED
    await repos["a2a_outbound_template"].update(
        {"template_id": "a2a-weather"},
        {"template_name": "Weather Agent v2", "card_revision": 2},
    )
    persisted = await projection.get_projected_agent("a2a-weather")
    assert persisted is not None and persisted.user_enabled is False

    await projection.set_user_enabled("a2a-weather", True)
    await repos["a2a_outbound_template"].update(
        {"template_id": "a2a-weather"}, {"enabled": False}
    )
    manager_disabled = await projection.get_projected_agent("a2a-weather")
    assert manager_disabled is not None
    assert manager_disabled.manager_enabled is False
    assert manager_disabled.user_enabled is True
    assert manager_disabled.effective_enabled is False
    assert manager_disabled.agent.enabled is False

    await projection.update_runtime_state(
        "a2a-weather",
        A2AOutboundAvailability.UNREACHABLE,
        error_code=A2AOutboundErrorCode.CARD_FETCH_FAILED.value,
    )
    runtime_failed = await projection.get_projected_agent("a2a-weather")
    assert runtime_failed is not None
    assert runtime_failed.agent.availability is A2AOutboundAvailability.UNREACHABLE
    assert runtime_failed.agent.last_error_summary == "无法获取第三方 Agent Card。"
    runtime_row = await store.get(
        "a2a_outbound_runtime_state", {"template_id": "a2a-weather"}
    )
    assert isinstance(runtime_row["updated_at"], datetime)
    assert isinstance(runtime_row["last_checked_at"], datetime)

    listed = await registry.list_agents()
    assert listed["items"][0]["manager_enabled"] is False
    assert listed["items"][0]["user_enabled"] is True
    assert listed["items"][0]["effective_enabled"] is False
    with pytest.raises(A2AOutboundError) as exc_info:
        await registry.edit_agent("a2a-weather")
    assert exc_info.value.code is A2AOutboundErrorCode.STORE_INVALID


@pytest.mark.asyncio
async def test_enterprise_projection_reuses_dispatch_repository() -> None:
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    await repos["a2a_outbound_template"].create(_projection_record())
    projection = EnterpriseA2AProjection(
        store,
        templates=repos["a2a_outbound_template"],
        user_states=repos["a2a_outbound_user_state"],
        runtime_states=repos["a2a_outbound_runtime_state"],
    )
    dispatch = A2AOutboundDispatch(
        dispatch_id="dispatch-1",
        agent_id="a2a-weather",
        agent_name="Weather Agent",
        agent_revision=1,
        mode=A2AOutboundDispatchMode.ASYNC,
        status=A2AOutboundDispatchStatus.CREATED,
        request_message_id="message-1",
        source_session_id="session-1",
        source_resource_id="resource-1",
        created_at="2026-09-05T00:00:00Z",
        updated_at="2026-09-05T00:00:00Z",
    )

    await projection.create_dispatch(dispatch)
    await projection.transition_dispatch(
        "dispatch-1",
        A2AOutboundDispatchStatus.COMPLETED,
        accepted_at="2026-09-05T00:00:01Z",
    )
    restored = await projection.get_dispatch("dispatch-1")
    assert restored is not None
    assert restored.status is A2AOutboundDispatchStatus.COMPLETED
    assert restored.agent_name == "Weather Agent"
    assert restored.source_resource_id == "resource-1"
    dispatch_row = await store.get(
        "a2a_outbound_dispatch", {"dispatch_id": "dispatch-1"}
    )
    assert isinstance(dispatch_row["created_at"], datetime)
    assert isinstance(dispatch_row["updated_at"], datetime)
    assert isinstance(dispatch_row["accepted_at"], datetime)
    assert isinstance(dispatch_row["finished_at"], datetime)

    await projection.set_user_enabled("a2a-weather", False)
    restarted = EnterpriseA2AProjection(
        store,
        templates=repos["a2a_outbound_template"],
        user_states=repos["a2a_outbound_user_state"],
        runtime_states=repos["a2a_outbound_runtime_state"],
    )
    restored_after_restart = await restarted.get_projected_agent("a2a-weather")
    assert restored_after_restart is not None
    assert restored_after_restart.user_enabled is False
    assert (await restarted.get_dispatch("dispatch-1")) is not None


@pytest.mark.asyncio
async def test_enterprise_dispatch_updates_shared_runtime_state() -> None:
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    await repos["a2a_outbound_template"].create(_projection_record())
    projection = EnterpriseA2AProjection(
        store,
        templates=repos["a2a_outbound_template"],
        user_states=repos["a2a_outbound_user_state"],
        runtime_states=repos["a2a_outbound_runtime_state"],
    )

    async def build_client(agent: Any, credential: str) -> _CompletedClient:
        del agent, credential
        return _CompletedClient()

    dispatcher = A2AOutboundDispatcher(projection, client_builder=build_client)
    result = await dispatcher.dispatch(
        agent_id="a2a-weather",
        task="weather",
        mode="sync",
        source_session_id="session-1",
        source_user_id="user-1",
    )

    assert result["status"] == A2AOutboundDispatchStatus.COMPLETED.value
    runtime_row = await store.get(
        "a2a_outbound_runtime_state", {"template_id": "a2a-weather"}
    )
    assert runtime_row["availability"] == A2AOutboundAvailability.AVAILABLE.value
    assert isinstance(runtime_row["last_success_at"], datetime)


@pytest.mark.asyncio
async def test_enterprise_dispatch_records_unreachable_runtime_state() -> None:
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    await repos["a2a_outbound_template"].create(_projection_record())
    projection = EnterpriseA2AProjection(
        store,
        templates=repos["a2a_outbound_template"],
        user_states=repos["a2a_outbound_user_state"],
        runtime_states=repos["a2a_outbound_runtime_state"],
    )

    async def build_client(agent: Any, credential: str) -> _UnreachableClient:
        del agent, credential
        return _UnreachableClient()

    dispatcher = A2AOutboundDispatcher(projection, client_builder=build_client)
    result = await dispatcher.dispatch(
        agent_id="a2a-weather",
        task="weather",
        mode="sync",
        source_session_id="session-1",
        source_user_id="user-1",
    )

    assert result["status"] == A2AOutboundDispatchStatus.DISPATCH_FAILED.value
    runtime_row = await store.get(
        "a2a_outbound_runtime_state", {"template_id": "a2a-weather"}
    )
    assert runtime_row["availability"] == A2AOutboundAvailability.UNREACHABLE.value
    assert (
        runtime_row["last_error_code"] == A2AOutboundErrorCode.AGENT_UNAVAILABLE.value
    )


def test_a2a_delete_restores_projection_and_local_state_on_cleanup_failure(
    a2a_receiver: tuple[TestClient, dict[str, Any], _Secrets],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repos, secrets = a2a_receiver
    path = "/api/v1/a2a-outbound-templates"
    credential_ref = "a2a/outbound/a2a-weather.api_key"
    assert client.post(path, json=_outbound_payload()).status_code == 200
    user_states = repos["a2a_outbound_user_state"]
    runtime_states = repos["a2a_outbound_runtime_state"]
    client.portal.call(
        lambda: user_states.create(
            {
                "template_id": "a2a-weather",
                "user_enabled": False,
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )
    client.portal.call(
        lambda: runtime_states.create(
            {
                "template_id": "a2a-weather",
                "availability": A2AOutboundAvailability.UNREACHABLE.value,
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )
    original_delete = runtime_states.delete

    async def fail_delete(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        raise RuntimeError("runtime state delete failed")

    monkeypatch.setattr(runtime_states, "delete", fail_delete)
    template_module = import_manager_config_receiver_module(
        "core.template.a2a_outbound_template"
    )
    service = template_module.A2AOutboundTemplateService()
    with pytest.raises(RuntimeError, match="runtime state delete failed"):
        client.portal.call(lambda: service.delete("a2a-weather"))

    assert (
        client.portal.call(
            lambda: repos["a2a_outbound_template"].get(template_id="a2a-weather")
        )
        is not None
    )
    assert (
        client.portal.call(lambda: user_states.get(template_id="a2a-weather"))[
            "user_enabled"
        ]
        is False
    )
    assert client.portal.call(
        lambda: runtime_states.get(template_id="a2a-weather")
    )["availability"] == A2AOutboundAvailability.UNREACHABLE.value
    assert secrets.values[credential_ref] == "secret"

    monkeypatch.setattr(runtime_states, "delete", original_delete)
    client.portal.call(lambda: service.delete("a2a-weather"))
    assert (
        client.portal.call(lambda: user_states.get(template_id="a2a-weather")) is None
    )


@pytest.mark.asyncio
async def test_repository_factory_creates_enterprise_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    await repos["a2a_outbound_template"].create(_projection_record())
    repository = create_a2a_outbound_repository(store)
    assert isinstance(repository, EnterpriseA2AProjection)
    projected = await repository.get_agent("a2a-weather")
    assert projected is not None and projected.display_name == "Weather Agent"


@pytest.mark.asyncio
async def test_enterprise_projection_resolves_policy_manager_and_user_intersection() -> None:
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    weather = _projection_record()
    calendar = {**_projection_record(), "template_id": "a2a-calendar"}
    await repos["a2a_outbound_template"].create(weather)
    await repos["a2a_outbound_template"].create(calendar)
    await repos["a2a_access_policy_template"].create(
        {
            "policy_id": "policy-1",
            "policy_name": "Allowed",
            "mode": "allowlist",
            "member_template_ids": ["a2a-weather", "a2a-calendar"],
            "enabled": True,
            "revision": 1,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    await repos["agent_template"].create(
        {
            "template_id": "agent-template-1",
            "template_name": "Agent",
            "template_ref": {"a2a_access_policy": ["policy-1"]},
            "enabled": True,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    await repos["instance_agent_resource"].create(
        {
            "resource_id": "resource-1",
            "resource_name": "Agent",
            "ref_template_id": "agent-template-1",
            "enabled": True,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    projection = EnterpriseA2AProjection(
        store,
        templates=repos["a2a_outbound_template"],
        user_states=repos["a2a_outbound_user_state"],
        runtime_states=repos["a2a_outbound_runtime_state"],
        policies=repos["a2a_access_policy_template"],
        agent_templates=repos["agent_template"],
        instance_resources=repos["instance_agent_resource"],
    )

    assert await projection.resolve_effective_a2a_agent_ids("resource-1") == {
        "a2a-weather",
        "a2a-calendar",
    }
    await repos["a2a_access_policy_template"].update(
        {"policy_id": "policy-1"}, {"member_template_ids": []}
    )
    assert await projection.resolve_effective_a2a_agent_ids("resource-1") == frozenset()
    await repos["a2a_access_policy_template"].update(
        {"policy_id": "policy-1"}, {"mode": "denylist"}
    )
    assert await projection.resolve_effective_a2a_agent_ids("resource-1") == {
        "a2a-weather",
        "a2a-calendar",
    }
    await repos["a2a_access_policy_template"].update(
        {"policy_id": "policy-1"},
        {
            "mode": "allowlist",
            "member_template_ids": ["a2a-weather", "a2a-calendar"],
        },
    )
    await repos["instance_agent_resource"].update(
        {"resource_id": "resource-1"}, {"enabled": False}
    )
    assert await projection.resolve_effective_a2a_agent_ids("resource-1") == frozenset()
    await repos["instance_agent_resource"].update(
        {"resource_id": "resource-1"},
        {"enabled": True, "expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc)},
    )
    assert await projection.resolve_effective_a2a_agent_ids("resource-1") == frozenset()
    await repos["instance_agent_resource"].update(
        {"resource_id": "resource-1"}, {"expires_at": None}
    )
    await repos["a2a_access_policy_template"].update(
        {"policy_id": "policy-1"}, {"enabled": False}
    )
    assert await projection.resolve_effective_a2a_agent_ids("resource-1") == frozenset()
    await repos["a2a_access_policy_template"].update(
        {"policy_id": "policy-1"}, {"enabled": True}
    )
    await repos["a2a_access_policy_template"].update(
        {"policy_id": "policy-1"},
        {"mode": "denylist", "member_template_ids": ["a2a-weather"]},
    )
    assert await projection.resolve_effective_a2a_agent_ids("resource-1") == {
        "a2a-calendar"
    }
    await repos["a2a_access_policy_template"].update(
        {"policy_id": "policy-1"},
        {
            "mode": "allowlist",
            "member_template_ids": ["a2a-weather", "a2a-calendar"],
        },
    )
    await projection.set_user_enabled("a2a-calendar", False)
    assert await projection.resolve_effective_a2a_agent_ids("resource-1") == {
        "a2a-weather"
    }
    await repos["a2a_outbound_template"].update(
        {"template_id": "a2a-weather"}, {"enabled": False}
    )
    assert await projection.resolve_effective_a2a_agent_ids("resource-1") == frozenset()
    assert await projection.resolve_authorized_a2a_agent_ids("resource-1") == {
        "a2a-weather",
        "a2a-calendar",
    }


@pytest.mark.asyncio
async def test_enterprise_projection_fails_closed_for_invalid_resource_or_policy() -> None:
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    projection = EnterpriseA2AProjection(
        store,
        templates=repos["a2a_outbound_template"],
        user_states=repos["a2a_outbound_user_state"],
        runtime_states=repos["a2a_outbound_runtime_state"],
        policies=repos["a2a_access_policy_template"],
        agent_templates=repos["agent_template"],
        instance_resources=repos["instance_agent_resource"],
    )

    assert await projection.resolve_effective_a2a_agent_ids("missing") == frozenset()


@pytest.mark.parametrize("host", ["10.0.0.8", "8.8.8.8", "127.0.0.1", "169.254.169.254", "100.64.0.1"])
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("flags", [
    (False, False, False), (True, True, False), (True, False, True),
    (False, True, True), (True, True, True),
])
def test_credential_receiver_network_policy(a2a_receiver, host, scheme, flags):
    _, _, secrets = a2a_receiver
    app_module = import_manager_config_receiver_module("http.app")
    policy = dict(zip(("allow_http", "allow_private_network", "allow_public_http"), flags))
    payload = _outbound_payload()
    payload["data"] = {"network_policy": policy}
    allowed = (
        (host == "8.8.8.8" or (host == "10.0.0.8" and flags[1]))
        and (scheme == "https" or (flags[0] and (host != "8.8.8.8" or flags[2])))
    )
    with TestClient(app_module.create_app(), base_url=f"{scheme}://{host}") as client:
        response = client.post("/api/v1/a2a-outbound-templates", json=payload)
    assert response.status_code == (200 if allowed else 400)
    assert bool(secrets.values) is allowed
