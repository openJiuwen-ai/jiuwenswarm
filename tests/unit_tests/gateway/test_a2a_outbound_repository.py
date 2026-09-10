"""Stage-1 coverage for the A2A outbound domain and persistence contract."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from jiuwenswarm.gateway.a2a_manager.outbound import (
    A2ACompatibleInterface,
    A2ADiscoveredAgent,
    A2AOutboundAgent,
    A2AOutboundAvailability,
    A2AOutboundCredentialStore,
    A2AOutboundDiscovery,
    A2AOutboundDispatch,
    A2AOutboundDispatchMode,
    A2AOutboundDispatchStatus,
    A2AOutboundError,
    A2AOutboundErrorCode,
    A2AOutboundRepository,
)
from jiuwenswarm.gateway.storage.backends.file_persistent import FilePersistentBackend
from jiuwenswarm.gateway.storage.backends.memory_persistent import (
    InMemoryPersistentBackend,
)
from jiuwenswarm.gateway.storage_assembly import (
    build_gateway_store_registry,
    create_a2a_outbound_repository,
)
from jiuwenswarm.gateway.a2a_manager.outbound.locks import KeyedLockPool


def _stamp(offset_days: int = 0) -> str:
    value = datetime(2026, 8, 26, tzinfo=timezone.utc) + timedelta(days=offset_days)
    return value.isoformat().replace("+00:00", "Z")


def _agent(agent_id: str = "agent-1") -> A2AOutboundAgent:
    return A2AOutboundAgent(
        agent_id=agent_id,
        display_name="Research Agent",
        source_url="https://agent.example.com",
        card_path="/.well-known/agent-card.json",
        card_fingerprint="sha256:card",
        card_revision=1,
        agent_card={
            "name": "Research Agent",
            "authorization": "Bearer must-not-persist",
            "nested": {
                "apiKey": "also-secret",
                "clientSecret": "client-secret",
                "secret_key": "secret-key",
                "passwordField": "password",
                "tokenValue": "token",
                "credential_data": "credential",
                "description": "safe",
            },
        },
        selected_interface=A2ACompatibleInterface(
            protocol_binding="JSONRPC",
            protocol_version="1.0",
            url="https://agent.example.com/a2a",
        ),
        enabled=True,
        availability=A2AOutboundAvailability.AVAILABLE,
        credential_ref=f"a2a/outbound/{agent_id}.api_key",
        connect_timeout_seconds=10,
        sync_wait_seconds=300,
        created_at=_stamp(),
        updated_at=_stamp(),
    )


def _dispatch(
    dispatch_id: str = "dispatch-1",
    *,
    status: A2AOutboundDispatchStatus = A2AOutboundDispatchStatus.CREATED,
    created_at: str | None = None,
) -> A2AOutboundDispatch:
    return A2AOutboundDispatch(
        dispatch_id=dispatch_id,
        agent_id="agent-1",
        agent_revision=1,
        mode=A2AOutboundDispatchMode.ASYNC,
        status=status,
        request_message_id=f"message-{dispatch_id}",
        source_session_id="session-1",
        created_at=created_at or _stamp(),
        updated_at=created_at or _stamp(),
        input_length=12,
        input_content_type="text/plain",
        input_digest="sha256:input",
    )


class _SecretProbe:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def set(self, key: str, value: str, *, algorithm: str | None = None) -> None:
        assert algorithm is None
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.mark.asyncio
async def test_agent_roundtrip_redacts_card_secrets_and_hides_credential_ref() -> None:
    store = InMemoryPersistentBackend()
    repository = A2AOutboundRepository(store)

    created = await repository.create_agent(_agent())
    row = await store.get("a2a_outbound_agent", {"agent_id": "agent-1"})

    assert row is not None
    assert row["credential_ref"] == "a2a/outbound/agent-1.api_key"
    assert row["agent_card"]["authorization"] == "******"
    assert row["agent_card"]["nested"] == {
        "apiKey": "******",
        "clientSecret": "******",
        "secret_key": "******",
        "passwordField": "******",
        "tokenValue": "******",
        "credential_data": "******",
        "description": "safe",
    }
    assert created.public_dict()["has_credential"] is True
    assert "credential_ref" not in created.public_dict()


@pytest.mark.asyncio
async def test_agent_crud_uses_repository_contract() -> None:
    secret_probe = _SecretProbe()
    credentials = A2AOutboundCredentialStore(secret_probe)
    credentials.set_for_agent("agent-1", "secret-value")
    repository = A2AOutboundRepository(
        InMemoryPersistentBackend(), credential_store=credentials
    )
    await repository.create_agent(_agent())

    updated = await repository.update_agent(
        "agent-1",
        lambda current: replace(current, enabled=False, updated_at=_stamp(1)),
    )

    assert updated is not None
    assert updated.enabled is False
    assert [item.agent_id for item in await repository.list_agents()] == ["agent-1"]
    assert await repository.delete_agent("agent-1") is True
    assert await repository.get_agent("agent-1") is None
    assert secret_probe.values == {}


@pytest.mark.asyncio
async def test_agent_updates_are_atomic_under_per_agent_lock() -> None:
    repository = A2AOutboundRepository(InMemoryPersistentBackend())
    await repository.create_agent(_agent())

    first, second = await asyncio.gather(
        repository.update_agent(
            "agent-1",
            lambda current: replace(current, enabled=False),
        ),
        repository.update_agent(
            "agent-1",
            lambda current: replace(current, display_name="Updated Agent"),
        ),
    )

    persisted = await repository.get_agent("agent-1")
    assert first is not None and second is not None and persisted is not None
    assert persisted.enabled is False
    assert persisted.display_name == "Updated Agent"


@pytest.mark.asyncio
async def test_terminal_dispatch_cannot_be_overwritten_by_late_event() -> None:
    repository = A2AOutboundRepository(InMemoryPersistentBackend())
    await repository.create_dispatch(_dispatch())
    completed = await repository.transition_dispatch(
        "dispatch-1",
        A2AOutboundDispatchStatus.COMPLETED,
        result={"text": "done"},
        updated_at=_stamp(1),
    )
    late = await repository.transition_dispatch(
        "dispatch-1",
        A2AOutboundDispatchStatus.CANCELED,
        error_code=A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value,
        updated_at=_stamp(2),
    )

    assert completed is not None and late is not None
    assert late.status is A2AOutboundDispatchStatus.COMPLETED
    assert late.result == {"text": "done"}
    assert late.finished_at == _stamp(1)


@pytest.mark.asyncio
async def test_timed_out_dispatch_can_later_converge_to_remote_terminal_state() -> None:
    repository = A2AOutboundRepository(InMemoryPersistentBackend())
    await repository.create_dispatch(_dispatch())
    timed_out = await repository.transition_dispatch(
        "dispatch-1",
        A2AOutboundDispatchStatus.TIMED_OUT,
        remote_task_id="remote-1",
        error_code=A2AOutboundErrorCode.DISPATCH_TIMEOUT.value,
    )
    completed = await repository.transition_dispatch(
        "dispatch-1",
        A2AOutboundDispatchStatus.COMPLETED,
        result={"text": "eventual result"},
    )

    assert timed_out is not None
    assert timed_out.error_summary == "等待第三方 Agent 回复超时。"
    assert completed is not None
    assert completed.status is A2AOutboundDispatchStatus.COMPLETED
    assert completed.remote_task_id == "remote-1"


@pytest.mark.asyncio
async def test_concurrent_terminal_updates_share_per_dispatch_lock() -> None:
    repository = A2AOutboundRepository(InMemoryPersistentBackend())
    await repository.create_dispatch(_dispatch())

    first, second = await asyncio.gather(
        repository.transition_dispatch(
            "dispatch-1",
            A2AOutboundDispatchStatus.COMPLETED,
            result={"text": "done"},
        ),
        repository.transition_dispatch(
            "dispatch-1",
            A2AOutboundDispatchStatus.FAILED,
            error_code=A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value,
        ),
    )

    assert first is not None and second is not None
    assert first.status == second.status
    assert first.status in {
        A2AOutboundDispatchStatus.COMPLETED,
        A2AOutboundDispatchStatus.FAILED,
    }


class _RacingStore(InMemoryPersistentBackend):
    def __init__(self) -> None:
        super().__init__()
        self.readers = 0
        self.ready = asyncio.Event()

    async def get(self, name, key):
        if name == "a2a_outbound_dispatch" and self.readers < 2:
            self.readers += 1
            if self.readers == 2:
                self.ready.set()
            await self.ready.wait()
        return await super().get(name, key)


@pytest.mark.asyncio
async def test_compare_and_set_protects_terminal_state_across_repositories() -> None:
    store = _RacingStore()
    first_repository = A2AOutboundRepository(store)
    second_repository = A2AOutboundRepository(store)
    await first_repository.create_dispatch(_dispatch())

    first, second = await asyncio.gather(
        first_repository.transition_dispatch(
            "dispatch-1",
            A2AOutboundDispatchStatus.COMPLETED,
            result={"text": "done"},
        ),
        second_repository.transition_dispatch(
            "dispatch-1",
            A2AOutboundDispatchStatus.FAILED,
            error_code=A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value,
        ),
    )

    persisted = await first_repository.get_dispatch("dispatch-1")
    assert first is not None and second is not None and persisted is not None
    assert first.status == second.status == persisted.status
    assert persisted.status in {
        A2AOutboundDispatchStatus.COMPLETED,
        A2AOutboundDispatchStatus.FAILED,
    }


@pytest.mark.asyncio
async def test_retention_uses_age_then_record_limit() -> None:
    repository = A2AOutboundRepository(InMemoryPersistentBackend())
    for index, day in enumerate((-40, -3, -2, -1)):
        await repository.create_dispatch(
            _dispatch(
                f"dispatch-{index}",
                status=A2AOutboundDispatchStatus.COMPLETED,
                created_at=_stamp(day),
            )
        )

    deleted = await repository.cleanup_dispatches(
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        max_age_days=30,
        max_records=2,
    )

    assert deleted == 2
    assert [item.dispatch_id for item in await repository.list_dispatches()] == [
        "dispatch-3",
        "dispatch-2",
    ]


@pytest.mark.asyncio
async def test_retention_never_deletes_non_terminal_dispatches() -> None:
    repository = A2AOutboundRepository(InMemoryPersistentBackend())
    for dispatch in (
        _dispatch(
            "working-old",
            status=A2AOutboundDispatchStatus.WORKING,
            created_at=_stamp(-40),
        ),
        replace(
            _dispatch(
                "timed-out-old",
                status=A2AOutboundDispatchStatus.TIMED_OUT,
                created_at=_stamp(-40),
            ),
            remote_task_id="remote-1",
        ),
        _dispatch(
            "unknown-old",
            status=A2AOutboundDispatchStatus.UNKNOWN,
            created_at=_stamp(-40),
        ),
        _dispatch(
            "completed-old",
            status=A2AOutboundDispatchStatus.COMPLETED,
            created_at=_stamp(-40),
        ),
    ):
        await repository.create_dispatch(dispatch)

    deleted = await repository.cleanup_dispatches(
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        max_age_days=30,
        max_records=1,
    )

    assert deleted == 1
    assert {item.dispatch_id for item in await repository.list_dispatches()} == {
        "working-old",
        "timed-out-old",
        "unknown-old",
    }


@pytest.mark.asyncio
async def test_zero_retention_limits_disable_both_cleanup_dimensions() -> None:
    repository = A2AOutboundRepository(InMemoryPersistentBackend())
    await repository.create_dispatch(
        _dispatch(
            "completed-old",
            status=A2AOutboundDispatchStatus.COMPLETED,
            created_at=_stamp(-100),
        )
    )

    deleted = await repository.cleanup_dispatches(
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        max_age_days=0,
        max_records=0,
    )

    assert deleted == 0
    assert await repository.get_dispatch("completed-old") is not None


@pytest.mark.asyncio
async def test_personal_json_layout_survives_backend_recreation(tmp_path) -> None:
    registry = build_gateway_store_registry(persistent_root=tmp_path)
    first = FilePersistentBackend(registry=registry)
    await A2AOutboundRepository(first).create_agent(_agent())

    second = FilePersistentBackend(registry=registry)
    restored = await A2AOutboundRepository(second).get_agent("agent-1")

    assert restored is not None
    assert restored.display_name == "Research Agent"
    payload = json.loads((tmp_path / "a2a_outbound_agents.json").read_text("utf-8"))
    assert "must-not-persist" not in json.dumps(payload)


class _BarrierFileStore:
    def __init__(self, backend: FilePersistentBackend, barrier: asyncio.Event) -> None:
        self._backend = backend
        self._barrier = barrier
        self._counter: list[int] | None = None

    def bind_counter(self, counter: list[int]) -> None:
        self._counter = counter

    async def get(self, name, key):
        row = await self._backend.get(name, key)
        if name == "a2a_outbound_dispatch" and self._counter is not None:
            self._counter[0] += 1
            if self._counter[0] == 2:
                self._barrier.set()
            if self._counter[0] <= 2:
                await self._barrier.wait()
        return row

    def __getattr__(self, name):
        return getattr(self._backend, name)


@pytest.mark.asyncio
async def test_file_backend_cas_preserves_first_terminal_winner(tmp_path) -> None:
    registry = build_gateway_store_registry(persistent_root=tmp_path)
    seed_backend = FilePersistentBackend(registry=registry)
    await A2AOutboundRepository(seed_backend).create_dispatch(_dispatch())

    barrier = asyncio.Event()
    counter = [0]
    first_store = _BarrierFileStore(FilePersistentBackend(registry=registry), barrier)
    second_store = _BarrierFileStore(FilePersistentBackend(registry=registry), barrier)
    first_store.bind_counter(counter)
    second_store.bind_counter(counter)
    first_repository = A2AOutboundRepository(first_store)
    second_repository = A2AOutboundRepository(second_store)

    first, second = await asyncio.gather(
        first_repository.transition_dispatch(
            "dispatch-1",
            A2AOutboundDispatchStatus.COMPLETED,
            result={"text": "done"},
        ),
        second_repository.transition_dispatch(
            "dispatch-1",
            A2AOutboundDispatchStatus.FAILED,
            error_code=A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN.value,
        ),
    )

    restored = await A2AOutboundRepository(
        FilePersistentBackend(registry=registry)
    ).get_dispatch("dispatch-1")
    assert first is not None and second is not None and restored is not None
    assert first.status == second.status == restored.status
    assert restored.status in {
        A2AOutboundDispatchStatus.COMPLETED,
        A2AOutboundDispatchStatus.FAILED,
    }


def test_storage_registry_uses_file_dispatch_for_personal_and_db_for_enterprise(
    tmp_path,
) -> None:
    personal = build_gateway_store_registry(persistent_root=tmp_path)
    enterprise = build_gateway_store_registry()

    for name, filename, key in (
        ("a2a_outbound_agent", "a2a_outbound_agents.json", "agent_id"),
        ("a2a_outbound_dispatch", "a2a_outbound_dispatches.json", "dispatch_id"),
    ):
        personal_layout = personal.get(name)
        assert personal_layout is not None and personal_layout.file is not None
        assert personal_layout.file.path == str(tmp_path / filename)
        assert personal_layout.file.key_fields == (key,)
        assert personal_layout.db is None
    assert enterprise.get("a2a_outbound_agent") is None
    enterprise_dispatch = enterprise.get("a2a_outbound_dispatch")
    assert enterprise_dispatch is not None
    assert enterprise_dispatch.file is None
    assert enterprise_dispatch.db is not None
    assert enterprise_dispatch.db.table == "a2a_outbound_dispatch"


def test_repository_factory_creates_personal_json_repository() -> None:
    store = InMemoryPersistentBackend()
    personal = create_a2a_outbound_repository(store)

    assert isinstance(personal, A2AOutboundRepository)


def test_credentials_live_in_secret_store_behind_reference() -> None:
    probe = _SecretProbe()
    credentials = A2AOutboundCredentialStore(probe)

    credential_ref = credentials.set_for_agent("agent-1", "secret-value")

    assert credential_ref == "a2a/outbound/agent-1.api_key"
    assert credentials.get(credential_ref) == "secret-value"
    assert "secret-value" not in _agent().to_record().values()
    credentials.delete(credential_ref)
    assert credentials.get(credential_ref) == ""


def test_discovery_dto_is_preview_only_and_redacts_credentials() -> None:
    preview = A2AOutboundDiscovery(
        discovery_id="disc-1",
        expires_at=_stamp(1),
        source_url="https://agent.example.com",
        card_path="/.well-known/agent-card.json",
        card_fingerprint="sha256:card",
        agent=A2ADiscoveredAgent(
            name="Research Agent",
            compatible_interfaces=(
                A2ACompatibleInterface(
                    "JSONRPC", "1.0", "https://agent.example.com/a2a"
                ),
            ),
        ),
        agent_card={"clientSecret": "must-not-leak"},
    ).to_dict()

    assert preview["discovery_id"] == "disc-1"
    assert "agent_id" not in preview
    assert "enabled" not in preview
    assert preview["agent_card"]["clientSecret"] == "******"


def test_invalid_external_credential_reference_is_rejected() -> None:
    credentials = A2AOutboundCredentialStore(_SecretProbe())
    with pytest.raises(ValueError, match="invalid"):
        credentials.get("llm.api_key")


def test_credential_reference_must_belong_to_agent() -> None:
    with pytest.raises(A2AOutboundError, match="A2A 出站数据无效"):
        replace(
            _agent("agent-1"),
            credential_ref=A2AOutboundCredentialStore.reference_for("agent-2"),
        ).validate()


@pytest.mark.asyncio
async def test_keyed_lock_pool_evicts_inactive_keys() -> None:
    pool = KeyedLockPool()
    for index in range(100):
        async with pool.hold(f"key-{index}"):
            assert pool.size == 1
    assert pool.size == 0
