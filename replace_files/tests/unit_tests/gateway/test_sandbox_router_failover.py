"""Cross-gateway sandbox routing: DCS adopt, NX race, terminate clears mapping."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.log import interface_info

# Avoid loading gateway/__init__.py (pulls agentserver → broken optional deps in some envs).
_repo_root = Path(__file__).resolve().parents[3]
_gw_pkg_path = _repo_root / "jiuwenclaw" / "gateway"
if "jiuwenclaw.gateway" not in sys.modules:
    _gw_pkg = types.ModuleType("jiuwenclaw.gateway")
    _gw_pkg.__path__ = [str(_gw_pkg_path)]
    sys.modules["jiuwenclaw.gateway"] = _gw_pkg

_sandbox_router_mod = importlib.import_module("jiuwenclaw.gateway.sandbox_router")
SandboxRouterAgentClient = _sandbox_router_mod.SandboxRouterAgentClient
SandboxRuntime = _sandbox_router_mod.SandboxRuntime
SandboxStatus = _sandbox_router_mod.SandboxStatus
from jiuwenclaw.sandbox.open_ability import OpenAbilityConfig, OpenAbilityEndpoint  # noqa: E402
from jiuwenclaw.sandbox.sandbox_client import ExecutionResult  # noqa: E402
from jiuwenclaw.sandbox.sandbox_routing_dcs_store import SandboxRoutingRecord  # noqa: E402
from jiuwenclaw.schema.message import ReqMethod  # noqa: E402


class FakeAgentClient:
    def __init__(self) -> None:
        self.disconnected = False
        self.request_ids: list[str] = []

    async def send_request(self, envelope: E2AEnvelope) -> Any:
        self.request_ids.append(str(envelope.request_id))
        return MagicMock(ok=True, request_id=envelope.request_id, channel_id=envelope.channel or "")

    async def send_request_stream(self, envelope: E2AEnvelope):
        yield MagicMock(request_id=envelope.request_id, channel_id=envelope.channel or "", is_complete=True)

    def set_or_update_server_config(self, *, config: dict, env: dict | None = None) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True


class BackupAgentClient(FakeAgentClient):
    def __init__(
        self,
        results: list[dict[str, Any]],
        *,
        log_result: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.envelopes: list[E2AEnvelope] = []
        self._results = results
        self._log_result = log_result

    async def send_request(self, envelope: E2AEnvelope) -> Any:
        self.envelopes.append(envelope)
        payload: dict[str, Any] = {"results": self._results}
        if self._log_result is not None:
            payload["log_result"] = self._log_result
        return MagicMock(
            ok=True,
            request_id=envelope.request_id,
            channel_id=envelope.channel or "",
            payload=payload,
        )


class FakeWorkspaceDcsStore:
    def __init__(
        self,
        *,
        fail_session_ids: set[str] | None = None,
        initial_records: dict[str, dict[str, str]] | None = None,
        events: list[tuple[str, str]] | None = None,
    ) -> None:
        self.fail_session_ids = fail_session_ids or set()
        self.records: dict[str, dict[str, str]] = dict(initial_records or {})
        self.events = events if events is not None else []

    async def get_workspace(self, session_id: str) -> Any:
        self.events.append(("get", session_id))
        record = self.records.get(session_id)
        if not record:
            return None
        return types.SimpleNamespace(
            url=record.get("url", ""),
            name=record.get("name", ""),
            routing_key=record.get("routing_key", ""),
            sandbox_id=record.get("sandbox_id", ""),
        )

    async def put_workspace(
        self,
        session_id: str,
        *,
        url: str,
        name: str = "",
        routing_key: str = "",
        sandbox_id: str = "",
    ) -> None:
        if session_id in self.fail_session_ids:
            raise RuntimeError(f"DCS write failed for {session_id}")
        self.events.append(("put", session_id))
        self.records[session_id] = {
            "url": url,
            "name": name,
            "routing_key": routing_key,
            "sandbox_id": sandbox_id,
        }

    async def delete_workspace(self, session_id: str) -> None:
        self.events.append(("delete_workspace", session_id))
        self.records.pop(session_id, None)


class FakeSandboxLogDcsStore:
    def __init__(
        self,
        *,
        initial_records: dict[str, dict[str, str]] | None = None,
        events: list[tuple[str, str]] | None = None,
    ) -> None:
        self.records: dict[str, dict[str, str]] = dict(initial_records or {})
        self.events = events if events is not None else []

    async def get_sandbox_log(self, sandbox_id: str) -> Any:
        self.events.append(("log_get", sandbox_id))
        record = self.records.get(sandbox_id)
        if not record:
            return None
        return types.SimpleNamespace(
            url=record.get("url", ""),
            name=record.get("name", ""),
        )

    async def put_sandbox_log(
        self,
        sandbox_id: str,
        *,
        url: str,
        name: str = "",
    ) -> None:
        self.events.append(("log_put", sandbox_id))
        self.records[sandbox_id] = {"url": url, "name": name}


class FakeQueryUrlObs:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.deleted_urls: list[str] = []

    async def get_latest_obs_url(self, file_url: str) -> str:
        self.events.append(("latest", file_url))
        return file_url

    async def invoking_osms_delete(self, file_url: str) -> bool:
        self.events.append(("delete", file_url))
        self.deleted_urls.append(file_url)
        return True


class FakeOpenAbilityClient(FakeAgentClient):
    instances: list["FakeOpenAbilityClient"] = []

    def __init__(self, sandbox_id: str, *, request_timeout_seconds: float = 600.0) -> None:
        super().__init__()
        self.sandbox_id = sandbox_id
        self.request_timeout_seconds = request_timeout_seconds
        self.connected_uri: str | None = None
        self._connection_lost_handler = None
        self._server_push_handler = None
        FakeOpenAbilityClient.instances.append(self)

    async def connect(self, uri: str) -> None:
        self.connected_uri = uri

    def set_server_push_handler(self, handler) -> None:
        self._server_push_handler = handler

    def set_connection_lost_handler(self, handler) -> None:
        self._connection_lost_handler = handler

    async def emit_connection_lost(self, payload: dict[str, Any]) -> None:
        assert self._connection_lost_handler is not None
        await self._connection_lost_handler(payload)


@pytest.fixture(autouse=True)
def disable_periodic_backup_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_BACKUP_ENABLE", "false")


@pytest.fixture
def router() -> SandboxRouterAgentClient:
    router = SandboxRouterAgentClient(
        max_sandboxes=5,
        adopt_existing=True,
        gateway_instance_id="gw-test",
    )
    router._open_ability_config = OpenAbilityConfig(
        ws_path="/ws",
        reconnect_timeout_seconds=0.01,
        readiness_poll_interval_seconds=0.001,
    )
    return router


@pytest.fixture
def mock_sandbox_client(router: SandboxRouterAgentClient):
    client = MagicMock()
    client.create_sandbox = AsyncMock(
        return_value=ExecutionResult(success=True, output="sb-new", error=None)
    )
    client.delete_sandbox = AsyncMock(return_value=ExecutionResult(success=True, output="", error=None))
    client.close = AsyncMock()
    router._sandbox_client = client
    return client


@pytest.fixture
def mock_dcs_store(router: SandboxRouterAgentClient):
    store = MagicMock()
    store.get_openability_endpoint = AsyncMock(
        return_value=OpenAbilityEndpoint(host="127.0.0.1", port=9001)
    )
    store.save_sandbox = AsyncMock()
    store.delete_sandbox = AsyncMock()
    store.close = AsyncMock()
    router._dcs_store = store
    return store


@pytest.fixture
def mock_routing_store(router: SandboxRouterAgentClient):
    store = MagicMock()
    store.get_routing = AsyncMock(return_value=None)
    store.set_routing_nx = AsyncMock(return_value=True)
    store.delete_routing = AsyncMock()
    store.close = AsyncMock()
    router._routing_dcs_store = store
    return store


def _envelope(*, user_id: str = "user-a", session_id: str = "sess-a") -> E2AEnvelope:
    return E2AEnvelope(
        request_id="req-1",
        channel="vibeskill",
        session_id=session_id,
        user_id=user_id,
        method="skilldev.chat",
        params={"task_id": session_id},
        is_stream=False,
    )


def _backup_runtime(agent_client: FakeAgentClient) -> Any:
    return SandboxRuntime(
        routing_key="vibeskill:user:user-a",
        sandbox_id="sb-backup",
        agent_client=agent_client,
        status=SandboxStatus.IDLE,
        task_count=0,
        metadata={
            "routing_key": "vibeskill:user:user-a",
            "user_id": "user-a",
            "session_ids": {"sess-a", "sess-b"},
        },
    )


def test_system_session_is_not_tracked_for_workspace_backup() -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = True
    runtime = _backup_runtime(BackupAgentClient([]))
    runtime.metadata["session_ids"] = set()
    envelope = _envelope(user_id="", session_id="heartbeat_abc123")

    router._track_session_for_runtime(runtime, envelope)

    assert runtime.metadata["session_ids"] == set()
    assert runtime.metadata.get("backup_period_active_sessions") is None
    assert runtime.metadata.get("backup_period_session_versions") is None


def test_interrupt_request_is_not_tracked_for_workspace_backup() -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = True
    runtime = _backup_runtime(BackupAgentClient([]))
    runtime.metadata["session_ids"] = set()
    envelope = _envelope(session_id="deleted-session")
    envelope.method = ReqMethod.SKILLDEV_CANCEL.value

    router._track_session_for_runtime(runtime, envelope)

    assert runtime.metadata["session_ids"] == set()
    assert runtime.metadata.get("backup_period_active_sessions") is None
    assert runtime.metadata.get("backup_period_session_versions") is None


@pytest.mark.asyncio
async def test_interrupt_request_is_dropped_during_openability_reconnect() -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    runtime = _backup_runtime(FakeAgentClient())
    runtime.metadata["openability_reconnect_required"] = True
    router._runtimes[runtime.routing_key] = runtime
    envelope = _envelope(user_id="user-a", session_id="sess-a")
    envelope.method = ReqMethod.SKILLDEV_CANCEL.value

    with pytest.raises(RuntimeError, match="dropping interrupt request"):
        await router._wait_for_openability_reconnect_buffer(envelope)

    assert runtime.routing_key not in router._reconnect_waiters


@pytest.mark.asyncio
async def test_adopt_existing_skips_create_sandbox(
    router: SandboxRouterAgentClient,
    mock_sandbox_client,
    mock_dcs_store,
    mock_routing_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_routing_store.get_routing.return_value = SandboxRoutingRecord(
        routing_key="vibeskill:user:user-a",
        sandbox_id="sb-existing",
        gateway_id="gw-1",
        updated_at=1.0,
    )
    fake_client = FakeAgentClient()
    monkeypatch.setattr(
        router,
        "_connect_open_ability_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(router, "_register_sandbox_record", AsyncMock())

    runtime = await router._acquire_runtime(_envelope())

    assert runtime.sandbox_id == "sb-existing"
    mock_sandbox_client.create_sandbox.assert_not_called()
    router._register_sandbox_record.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stale_routing_without_endpoint_creates_new(
    router: SandboxRouterAgentClient,
    mock_sandbox_client,
    mock_dcs_store,
    mock_routing_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_routing_store.get_routing.return_value = SandboxRoutingRecord(
        routing_key="vibeskill:user:user-a",
        sandbox_id="sb-dead",
        gateway_id="gw-1",
        updated_at=1.0,
    )
    mock_dcs_store.get_openability_endpoint.return_value = None
    fake_client = FakeAgentClient()
    monkeypatch.setattr(
        router,
        "_connect_open_ability_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(
        router,
        "_register_sandbox_record",
        AsyncMock(return_value={"sandbox_id": "sb-new", "api_key": "k", "api_key_sha256": "h"}),
    )

    runtime = await router._acquire_runtime(_envelope())

    assert runtime.sandbox_id == "sb-new"
    mock_routing_store.delete_routing.assert_called_with("vibeskill:user:user-a")
    mock_sandbox_client.create_sandbox.assert_called_once()


@pytest.mark.asyncio
async def test_nx_race_loser_adopts_winner(
    router: SandboxRouterAgentClient,
    mock_sandbox_client,
    mock_dcs_store,
    mock_routing_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_routing_store.set_routing_nx.return_value = False

    call_count = 0

    async def get_routing_side_effect(routing_key: str):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            return SandboxRoutingRecord(
                routing_key=routing_key,
                sandbox_id="sb-peer",
                gateway_id="gw-peer",
                updated_at=2.0,
            )
        return None

    mock_routing_store.get_routing.side_effect = get_routing_side_effect

    fake_client = FakeAgentClient()
    monkeypatch.setattr(
        router,
        "_connect_open_ability_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(router, "_register_sandbox_record", AsyncMock())

    runtime = await router._acquire_runtime(_envelope())

    assert runtime.sandbox_id == "sb-peer"
    mock_sandbox_client.create_sandbox.assert_called_once()
    mock_sandbox_client.delete_sandbox.assert_called_with("sb-new")
    router._register_sandbox_record.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_terminate_deletes_routing_mapping(
    router: SandboxRouterAgentClient,
    mock_sandbox_client,
    mock_dcs_store,
    mock_routing_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeAgentClient()
    monkeypatch.setattr(
        router,
        "_connect_open_ability_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(
        router,
        "_register_sandbox_record",
        AsyncMock(return_value={"sandbox_id": "sb-new", "api_key": "k", "api_key_sha256": "h"}),
    )

    runtime = await router._acquire_runtime(_envelope())
    await router._terminate_runtime(runtime.routing_key)

    mock_routing_store.delete_routing.assert_called_with("vibeskill:user:user-a")
    assert "vibeskill:user:user-a" not in router._runtimes


@pytest.mark.asyncio
async def test_terminate_skips_dcs_when_sandbox_delete_fails(
    router: SandboxRouterAgentClient,
    mock_sandbox_client,
    mock_dcs_store,
    mock_routing_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_sandbox_client.delete_sandbox = AsyncMock(
        return_value=ExecutionResult(success=False, output="", error="network error")
    )
    fake_client = FakeAgentClient()
    monkeypatch.setattr(
        router,
        "_connect_open_ability_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(
        router,
        "_register_sandbox_record",
        AsyncMock(return_value={"sandbox_id": "sb-new", "api_key": "k", "api_key_sha256": "h"}),
    )

    runtime = await router._acquire_runtime(_envelope())
    await router._terminate_runtime(runtime.routing_key)

    mock_dcs_store.delete_sandbox.assert_not_called()
    mock_routing_store.delete_routing.assert_not_called()
    assert "vibeskill:user:user-a" not in router._runtimes


@pytest.mark.asyncio
async def test_adopt_disabled_always_creates(
    mock_sandbox_client,
    mock_dcs_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = SandboxRouterAgentClient(
        max_sandboxes=5,
        adopt_existing=False,
        gateway_instance_id="gw-test",
    )
    routing_store = MagicMock()
    routing_store.get_routing = AsyncMock(
        return_value=SandboxRoutingRecord(
            routing_key="vibeskill:user:user-a",
            sandbox_id="sb-existing",
            gateway_id="gw-1",
            updated_at=1.0,
        )
    )
    router._routing_dcs_store = routing_store
    router._dcs_store = mock_dcs_store
    router._sandbox_client = mock_sandbox_client

    fake_client = FakeAgentClient()
    monkeypatch.setattr(
        router,
        "_connect_open_ability_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(
        router,
        "_register_sandbox_record",
        AsyncMock(return_value={"sandbox_id": "sb-new", "api_key": "k", "api_key_sha256": "h"}),
    )

    runtime = await router._acquire_runtime(_envelope())
    assert runtime.sandbox_id == "sb-new"
    routing_store.get_routing.assert_not_called()


@pytest.mark.asyncio
async def test_oa_physical_disconnect_refreshes_runtime_client(
    router: SandboxRouterAgentClient,
    mock_sandbox_client,
    mock_dcs_store,
    mock_routing_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOpenAbilityClient.instances.clear()
    monkeypatch.setattr(_sandbox_router_mod, "OpenAbilityWebSocketClient", FakeOpenAbilityClient)
    monkeypatch.setattr(
        router,
        "_register_sandbox_record",
        AsyncMock(return_value={"sandbox_id": "sb-new", "api_key": "k", "api_key_sha256": "h"}),
    )

    runtime = await router._acquire_runtime(_envelope())
    callback_events: list[tuple[str, dict[str, Any]]] = []

    async def on_connection_lost(payload: dict[str, Any]) -> None:
        callback_events.append(("lost", payload))

    async def on_reconnected(payload: dict[str, Any]) -> None:
        callback_events.append(("reconnected", payload))

    router.set_openability_connection_lost_handler(on_connection_lost)
    router.set_openability_reconnected_handler(on_reconnected)

    assert len(FakeOpenAbilityClient.instances) == 1
    first_client = FakeOpenAbilityClient.instances[0]
    assert runtime.agent_client is first_client

    mock_dcs_store.get_openability_endpoint.side_effect = [
        OpenAbilityEndpoint(host="127.0.0.1", port=9002),
    ]

    await first_client.emit_connection_lost({"event": "openability.connection_lost"})
    await asyncio.sleep(0)

    assert len(FakeOpenAbilityClient.instances) == 2
    second_client = FakeOpenAbilityClient.instances[1]
    assert runtime.agent_client is second_client
    assert second_client.connected_uri == "ws://127.0.0.1:9002/ws"
    assert first_client.disconnected is True
    assert runtime.metadata.get("openability_reconnect_required") is False
    assert [event for event, _payload in callback_events] == ["lost", "reconnected"]
    assert callback_events[0][1]["session_ids"] == ["sess-a"]
    assert callback_events[0][1]["user_id"] == "user-a"
    assert callback_events[1][1]["session_ids"] == ["sess-a"]
    assert callback_events[1][1]["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_openability_connect_logs_interface_event(
    router: SandboxRouterAgentClient,
    mock_dcs_store,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    interface_log_path = tmp_path / "interface.log"
    monkeypatch.setenv("INTERFACE_LOG_PATH", str(interface_log_path))
    interface_info.configure_interface_log_path()
    FakeOpenAbilityClient.instances.clear()
    monkeypatch.setattr(_sandbox_router_mod, "OpenAbilityWebSocketClient", FakeOpenAbilityClient)
    monkeypatch.setattr(
        router,
        "_probe_link_return_path_for_client",
        AsyncMock(return_value=True),
    )
    try:
        client = await router._connect_open_ability_client(
            "sb-oa",
            "rk-oa",
            {"session_id": "sess-oa"},
        )
    finally:
        monkeypatch.delenv("INTERFACE_LOG_PATH", raising=False)
        interface_info.configure_interface_log_path()

    assert client is FakeOpenAbilityClient.instances[0]
    for handler in interface_info.interface_logger.handlers:
        handler.flush()
    rows = [
        line.split("|")
        for line in interface_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert len(rows[0]) == 24
    assert rows[0][1:11] == [
        "INFO", "sess-oa", "SkillCreator", "OpenAbility",
        "WebSocket", "", "", "", "connect", "success",
    ]
    assert rows[0][11:24] == [""] * 13


@pytest.mark.asyncio
async def test_oa_physical_disconnect_drops_runtime_when_refresh_fails(
    router: SandboxRouterAgentClient,
    mock_sandbox_client,
    mock_dcs_store,
    mock_routing_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOpenAbilityClient.instances.clear()
    monkeypatch.setattr(_sandbox_router_mod, "OpenAbilityWebSocketClient", FakeOpenAbilityClient)
    monkeypatch.setattr(
        router,
        "_register_sandbox_record",
        AsyncMock(return_value={"sandbox_id": "sb-new", "api_key": "k", "api_key_sha256": "h"}),
    )

    runtime = await router._acquire_runtime(_envelope())
    first_client = FakeOpenAbilityClient.instances[0]
    mock_dcs_store.get_openability_endpoint.side_effect = RuntimeError("no fresh endpoint")
    callback_events: list[dict[str, Any]] = []

    async def on_reconnect_failed(payload: dict[str, Any]) -> None:
        callback_events.append(payload)

    router.set_openability_reconnect_failed_handler(on_reconnect_failed)

    await first_client.emit_connection_lost({"event": "openability.connection_lost"})
    await asyncio.sleep(0)

    assert runtime.routing_key not in router._runtimes
    assert first_client.disconnected is True
    assert callback_events
    assert callback_events[-1]["event"] == "openability.reconnect_failed"
    assert callback_events[-1]["session_ids"] == ["sess-a"]


@pytest.mark.asyncio
async def test_requests_are_buffered_during_oa_reconnect(
    router: SandboxRouterAgentClient,
    mock_sandbox_client,
    mock_dcs_store,
    mock_routing_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOpenAbilityClient.instances.clear()
    monkeypatch.setattr(_sandbox_router_mod, "OpenAbilityWebSocketClient", FakeOpenAbilityClient)
    monkeypatch.setattr(
        router,
        "_register_sandbox_record",
        AsyncMock(return_value={"sandbox_id": "sb-new", "api_key": "k", "api_key_sha256": "h"}),
    )

    runtime = await router._acquire_runtime(_envelope())
    first_client = FakeOpenAbilityClient.instances[0]
    reconnect_gate = asyncio.Event()

    async def delayed_connect_open_ability_client(
        sandbox_id: str,
        routing_key: str,
        metadata: dict[str, Any],
        *,
        connect_reason: str = "initial",
    ):
        if metadata.get("openability_reconnect_required"):
            await reconnect_gate.wait()
        return await SandboxRouterAgentClient._connect_open_ability_client(
            router,
            sandbox_id,
            routing_key,
            metadata,
            connect_reason=connect_reason,
        )

    monkeypatch.setattr(router, "_connect_open_ability_client", delayed_connect_open_ability_client)
    mock_dcs_store.get_openability_endpoint.side_effect = [
        OpenAbilityEndpoint(host="127.0.0.1", port=9001),
        OpenAbilityEndpoint(host="127.0.0.1", port=9002),
    ]

    disconnect_task = asyncio.create_task(
        first_client.emit_connection_lost({"event": "openability.connection_lost"})
    )
    await asyncio.sleep(0.01)

    buffered_request = _envelope(user_id="user-a", session_id="")
    buffered_request.request_id = "req-buffered"
    send_task = asyncio.create_task(router.send_request(buffered_request))

    await asyncio.sleep(0.01)
    assert len(router._reconnect_waiters.get(runtime.routing_key, ())) == 1
    assert "req-buffered" not in first_client.request_ids

    reconnect_gate.set()
    await disconnect_task
    response = await send_task

    second_client = FakeOpenAbilityClient.instances[1]
    assert response.request_id == "req-buffered"
    assert runtime.agent_client is second_client
    assert "req-buffered" in second_client.request_ids
    assert "req-buffered" not in first_client.request_ids
    assert runtime.routing_key not in router._reconnect_waiters


@pytest.mark.asyncio
async def test_adopted_runtime_restores_workspace_from_different_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    agent_client = BackupAgentClient(
        [
            {
                "sessionID": "sess-a",
                "status": "success",
            },
        ]
    )
    events: list[tuple[str, str]] = []
    fake_query = FakeQueryUrlObs(events)
    monkeypatch.setattr(_sandbox_router_mod, "_create_query_url_obs", lambda: fake_query)
    router._workspace_dcs_store = FakeWorkspaceDcsStore(
        initial_records={
            "sess-a": {
                "url": "https://obs/old-sess-a.zip",
                "name": "old-a.zip",
                "routing_key": "vibeskill:user:user-a",
                "sandbox_id": "sb-old",
            }
        },
        events=events,
    )  # type: ignore[assignment]
    runtime = SandboxRuntime(
        routing_key="vibeskill:user:user-a",
        sandbox_id="sb-live",
        agent_client=agent_client,
        status=SandboxStatus.IDLE,
        task_count=0,
        metadata={
            "routing_key": "vibeskill:user:user-a",
            "user_id": "user-a",
            "adopted": True,
        },
    )

    await router._ensure_workspace_restored(runtime, _envelope())

    assert events == [
        ("get", "sess-a"),
        ("latest", "https://obs/old-sess-a.zip"),
    ]
    assert len(agent_client.envelopes) == 1
    restore_envelope = agent_client.envelopes[0]
    assert restore_envelope.method == ReqMethod.SKILLDEV_BATCH_DOWNLOAD.value
    assert restore_envelope.params == {
        "items": [
            {
                "sessionID": "sess-a",
                "url": "https://obs/old-sess-a.zip",
                "name": "old-a.zip",
            }
        ]
    }
    assert runtime.metadata["restored_session_ids"] == {"sess-a"}


@pytest.mark.asyncio
async def test_workspace_restore_queries_dcs_once_when_snapshot_missing() -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    events: list[tuple[str, str]] = []
    router._workspace_dcs_store = FakeWorkspaceDcsStore(events=events)  # type: ignore[assignment]
    runtime = SandboxRuntime(
        routing_key="vibeskill:user:user-a",
        sandbox_id="sb-live",
        agent_client=BackupAgentClient([]),
        status=SandboxStatus.IDLE,
        task_count=0,
        metadata={
            "routing_key": "vibeskill:user:user-a",
            "user_id": "user-a",
        },
    )

    await router._ensure_workspace_restored(runtime, _envelope(session_id="sess-new"))
    await router._ensure_workspace_restored(runtime, _envelope(session_id="sess-new"))

    assert events == [("get", "sess-new")]
    assert runtime.metadata["restored_session_ids"] == {"sess-new"}


@pytest.mark.asyncio
async def test_workspace_restore_skips_when_snapshot_is_from_same_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    agent_client = BackupAgentClient([])
    events: list[tuple[str, str]] = []
    fake_query = FakeQueryUrlObs(events)
    monkeypatch.setattr(_sandbox_router_mod, "_create_query_url_obs", lambda: fake_query)
    router._workspace_dcs_store = FakeWorkspaceDcsStore(
        initial_records={
            "sess-a": {
                "url": "https://obs/live-sess-a.zip",
                "name": "live-a.zip",
                "routing_key": "vibeskill:user:user-a",
                "sandbox_id": "sb-live",
            }
        },
        events=events,
    )  # type: ignore[assignment]
    runtime = SandboxRuntime(
        routing_key="vibeskill:user:user-a",
        sandbox_id="sb-live",
        agent_client=agent_client,
        status=SandboxStatus.IDLE,
        task_count=0,
        metadata={
            "routing_key": "vibeskill:user:user-a",
            "user_id": "user-a",
            "adopted": True,
        },
    )

    await router._ensure_workspace_restored(runtime, _envelope())

    assert events == [("get", "sess-a")]
    assert agent_client.envelopes == []
    assert runtime.metadata["restored_session_ids"] == {"sess-a"}


@pytest.mark.asyncio
async def test_release_session_deletes_workspace_obs_before_dcs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    events: list[tuple[str, str]] = []
    fake_query = FakeQueryUrlObs(events)
    monkeypatch.setattr(_sandbox_router_mod, "_create_query_url_obs", lambda: fake_query)
    router._workspace_dcs_store = FakeWorkspaceDcsStore(
        initial_records={
            "sess-a": {
                "url": "https://obs/sess-a.zip",
                "name": "sess-a.zip",
                "routing_key": "vibeskill:user:user-a",
                "sandbox_id": "sb-old",
            }
        },
        events=events,
    )  # type: ignore[assignment]
    runtime = SandboxRuntime(
        routing_key="vibeskill:user:user-a",
        sandbox_id="sb-live",
        agent_client=FakeAgentClient(),
        status=SandboxStatus.IDLE,
        task_count=0,
        metadata={
            "routing_key": "vibeskill:user:user-a",
            "user_id": "user-a",
            "session_ids": {"sess-a"},
            "restored_session_ids": {"sess-a"},
            "backup_period_active_sessions": {"sess-a"},
            "backup_period_session_versions": {"sess-a": 1},
        },
    )
    router._runtimes[runtime.routing_key] = runtime

    result = await router.release_session("sess-a", user_id="user-a")

    assert result["ok"] is True
    assert result["workspace_purged"] is True
    assert result["workspace_obs_url"] == "https://obs/sess-a.zip"
    assert result["workspace_obs_delete_attempted"] is True
    assert result["workspace_obs_deleted"] is True
    assert result["untracked"] is True
    assert result["remaining_session_ids"] == []
    assert events == [
        ("get", "sess-a"),
        ("delete", "https://obs/sess-a.zip"),
        ("delete_workspace", "sess-a"),
    ]
    assert fake_query.deleted_urls == ["https://obs/sess-a.zip"]
    assert runtime.metadata["session_ids"] == set()
    assert runtime.metadata["restored_session_ids"] == set()
    assert runtime.metadata["backup_period_active_sessions"] == set()
    assert runtime.metadata["backup_period_session_versions"] == {}


@pytest.mark.asyncio
async def test_backup_disabled_terminate_uses_all_tracked_session_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_BACKUP_ENABLE", "false")
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = False
    agent_client = BackupAgentClient(
        [
            {"sessionID": "sess-a", "url": "https://obs/sess-a.zip", "name": "a.zip", "status": "success"},
            {"sessionID": "sess-b", "url": "https://obs/sess-b.zip", "name": "b.zip", "status": "success"},
        ]
    )
    router._workspace_dcs_store = FakeWorkspaceDcsStore()  # type: ignore[assignment]
    runtime = _backup_runtime(agent_client)

    await router._backup_workspaces_before_terminate(runtime)

    assert len(agent_client.envelopes) == 1
    envelope = agent_client.envelopes[0]
    assert envelope.method == ReqMethod.SKILLDEV_BATCH_UPLOAD.value
    assert envelope.params == {"session_ids": ["sess-a", "sess-b"]}


@pytest.mark.asyncio
async def test_backup_enabled_terminate_uses_unflushed_active_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_BACKUP_ENABLE", "true")
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = True
    agent_client = BackupAgentClient(
        [
            {"sessionID": "sess-b", "url": "https://obs/sess-b.zip", "name": "b.zip", "status": "success"},
        ]
    )
    router._workspace_dcs_store = FakeWorkspaceDcsStore()  # type: ignore[assignment]
    runtime = _backup_runtime(agent_client)
    runtime.metadata["backup_period_active_sessions"] = {"sess-b"}
    runtime.metadata["backup_period_session_versions"] = {"sess-b": 1}

    await router._backup_workspaces_before_terminate(runtime)

    assert len(agent_client.envelopes) == 1
    assert agent_client.envelopes[0].params == {"session_ids": ["sess-b"]}
    assert runtime.metadata["backup_period_active_sessions"] == set()


@pytest.mark.asyncio
async def test_system_session_skips_workspace_restore() -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    events: list[tuple[str, str]] = []
    router._workspace_dcs_store = FakeWorkspaceDcsStore(
        initial_records={
            "heartbeat_abc123": {
                "url": "https://obs/heartbeat.zip",
                "name": "heartbeat.zip",
            }
        },
        events=events,
    )  # type: ignore[assignment]
    agent_client = BackupAgentClient([])
    runtime = _backup_runtime(agent_client)

    await router._ensure_workspace_restored(
        runtime,
        _envelope(user_id="", session_id="heartbeat_abc123"),
    )

    assert events == []
    assert agent_client.envelopes == []


@pytest.mark.asyncio
async def test_system_sessions_are_filtered_before_terminate_backup() -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = False
    agent_client = BackupAgentClient([])
    runtime = _backup_runtime(agent_client)
    runtime.metadata["session_ids"] = {
        "__heartbeat__",
        "heartbeat_abc123",
        "cron-abc123",
    }

    await router._backup_workspaces_before_terminate(runtime)

    assert agent_client.envelopes == []


@pytest.mark.asyncio
async def test_periodic_backup_removes_only_upload_and_dcs_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_BACKUP_ENABLE", "true")
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = True
    agent_client = BackupAgentClient(
        [
            {"sessionID": "sess-a", "url": "https://obs/sess-a.zip", "name": "a.zip", "status": "success"},
            {"sessionID": "sess-b", "url": "https://obs/sess-b.zip", "name": "b.zip", "status": "success"},
        ]
    )
    router._workspace_dcs_store = FakeWorkspaceDcsStore(
        fail_session_ids={"sess-b"}
    )  # type: ignore[assignment]
    runtime = _backup_runtime(agent_client)
    runtime.metadata["backup_period_active_sessions"] = {"sess-a", "sess-b"}
    runtime.metadata["backup_period_session_versions"] = {"sess-a": 1, "sess-b": 1}
    router._runtimes[runtime.routing_key] = runtime

    await router._backup_active_sessions_for_period()

    assert len(agent_client.envelopes) == 1
    assert agent_client.envelopes[0].params == {"session_ids": ["sess-a", "sess-b"]}
    assert runtime.metadata["backup_period_active_sessions"] == {"sess-b"}


@pytest.mark.asyncio
async def test_backup_deletes_old_workspace_url_before_dcs_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = False
    agent_client = BackupAgentClient(
        [
            {
                "sessionID": "sess-a",
                "url": "https://obs/new-sess-a.zip",
                "name": "new-a.zip",
                "status": "success",
            },
        ]
    )
    events: list[tuple[str, str]] = []
    fake_query = FakeQueryUrlObs(events)
    monkeypatch.setattr(_sandbox_router_mod, "_create_query_url_obs", lambda: fake_query)
    log_messages: list[tuple[str, tuple[Any, ...]]] = []

    class FakeLogger:
        def info(self, message: str, *args: Any) -> None:
            log_messages.append((message, args))

        def warning(self, message: str, *args: Any) -> None:
            log_messages.append((message, args))

        def exception(self, message: str, *args: Any) -> None:
            log_messages.append((message, args))

    monkeypatch.setattr(_sandbox_router_mod, "logger", FakeLogger())
    router._workspace_dcs_store = FakeWorkspaceDcsStore(
        initial_records={
            "sess-a": {
                "url": "https://obs/old-sess-a.zip",
                "name": "old-a.zip",
            }
        },
        events=events,
    )  # type: ignore[assignment]
    runtime = _backup_runtime(agent_client)
    runtime.metadata["session_ids"] = {"sess-a"}

    await router._backup_workspaces_before_terminate(runtime)

    assert events == [
        ("get", "sess-a"),
        ("delete", "https://obs/old-sess-a.zip"),
        ("put", "sess-a"),
    ]
    assert fake_query.deleted_urls == ["https://obs/old-sess-a.zip"]
    rendered_logs = "\n".join(
        message % args if args else message
        for message, args in log_messages
    )
    assert "Deleted old workspace OBS object before DCS overwrite" in rendered_logs
    assert "https://obs/old-sess-a.zip" not in rendered_logs


@pytest.mark.asyncio
async def test_backup_does_not_delete_when_old_url_matches_new_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = False
    agent_client = BackupAgentClient(
        [
            {
                "sessionID": "sess-a",
                "url": "https://obs/same-sess-a.zip",
                "name": "same-a.zip",
                "status": "success",
            },
        ]
    )
    events: list[tuple[str, str]] = []
    fake_query = FakeQueryUrlObs(events)
    monkeypatch.setattr(_sandbox_router_mod, "_create_query_url_obs", lambda: fake_query)
    router._workspace_dcs_store = FakeWorkspaceDcsStore(
        initial_records={
            "sess-a": {
                "url": "https://obs/same-sess-a.zip",
                "name": "same-a.zip",
            }
        },
        events=events,
    )  # type: ignore[assignment]
    runtime = _backup_runtime(agent_client)
    runtime.metadata["session_ids"] = {"sess-a"}

    await router._backup_workspaces_before_terminate(runtime)

    assert events == [
        ("get", "sess-a"),
        ("put", "sess-a"),
    ]
    assert fake_query.deleted_urls == []


@pytest.mark.asyncio
async def test_backup_persists_sandbox_log_and_deletes_old_obs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = False
    agent_client = BackupAgentClient(
        [
            {
                "sessionID": "sess-a",
                "url": "https://obs/new-sess-a.zip",
                "name": "new-a.zip",
                "status": "success",
            },
        ],
        log_result={
            "url": "https://obs/new-run-logs.zip",
            "name": "run_logs.zip",
            "status": "success",
        },
    )
    events: list[tuple[str, str]] = []
    fake_query = FakeQueryUrlObs(events)
    monkeypatch.setattr(_sandbox_router_mod, "_create_query_url_obs", lambda: fake_query)
    router._workspace_dcs_store = FakeWorkspaceDcsStore(events=events)  # type: ignore[assignment]
    router._sandbox_log_dcs_store = FakeSandboxLogDcsStore(
        initial_records={
            "sandbox-1": {
                "url": "https://obs/old-run-logs.zip",
                "name": "run_logs.zip",
            }
        },
        events=events,
    )  # type: ignore[assignment]
    runtime = _backup_runtime(agent_client)
    runtime.sandbox_id = "sandbox-1"
    runtime.metadata["session_ids"] = {"sess-a"}

    await router._backup_workspaces_before_terminate(runtime)

    assert ("log_get", "sandbox-1") in events
    assert ("log_put", "sandbox-1") in events
    assert "https://obs/old-run-logs.zip" in fake_query.deleted_urls
    assert router._sandbox_log_dcs_store.records["sandbox-1"]["url"] == (  # type: ignore[union-attr]
        "https://obs/new-run-logs.zip"
    )


@pytest.mark.asyncio
async def test_backup_skips_sandbox_log_dcs_when_log_result_failed() -> None:
    router = SandboxRouterAgentClient(adopt_existing=False)
    router._backup_enabled = False
    agent_client = BackupAgentClient(
        [
            {
                "sessionID": "sess-a",
                "url": "https://obs/new-sess-a.zip",
                "name": "new-a.zip",
                "status": "success",
            },
        ],
        log_result={
            "url": "",
            "name": "run_logs.zip",
            "status": "error",
            "error": "upload failed",
        },
    )
    events: list[tuple[str, str]] = []
    router._workspace_dcs_store = FakeWorkspaceDcsStore(events=events)  # type: ignore[assignment]
    router._sandbox_log_dcs_store = FakeSandboxLogDcsStore(
        initial_records={
            "sandbox-1": {
                "url": "https://obs/old-run-logs.zip",
                "name": "run_logs.zip",
            }
        },
        events=events,
    )  # type: ignore[assignment]
    runtime = _backup_runtime(agent_client)
    runtime.sandbox_id = "sandbox-1"
    runtime.metadata["session_ids"] = {"sess-a"}

    await router._backup_workspaces_before_terminate(runtime)

    assert events == [("get", "sess-a"), ("put", "sess-a")]
    assert router._sandbox_log_dcs_store.records["sandbox-1"]["url"] == (  # type: ignore[union-attr]
        "https://obs/old-run-logs.zip"
    )
