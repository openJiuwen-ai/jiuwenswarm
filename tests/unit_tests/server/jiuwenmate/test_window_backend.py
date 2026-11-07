"""JM-05: the persistent, Session-bound window backend core uses for state records and archives."""

from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone

import pytest
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    ExecutionRecord,
    PersistentContractError,
    RequestRecord,
)
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.spec_provider import build_invocation, publish_spec

from openjiuwen.core.foundation.store import InMemoryKVStore
from openjiuwen.harness import persistent_agent as core

NS = "workspace-1"
AGENT = "mate_dev"
SESSION = "sess_a"
STATE = f"pa:{AGENT}:{SESSION}:state"
INDEX = f"pa:{AGENT}:{SESSION}:archive_index"


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return WindowBackendStore(tmp_path / "data" / "agents.sqlite3", clock=clock)


def archive(n: int, session: str = SESSION) -> str:
    return f"pa:{AGENT}:{session}:archive:{n}"


# JM-05-V-01 persistence and the BaseKVStore contract


async def test_state_and_archives_survive_restart(tmp_path, store):
    backend = store.bind(NS, AGENT, SESSION)
    await backend.set(STATE, '{"window": []}')
    await backend.set(archive(1), b"\x00old window")

    restarted = WindowBackendStore(tmp_path / "data" / "agents.sqlite3").bind(NS, AGENT, SESSION)

    assert await restarted.get(STATE) == '{"window": []}'
    assert await restarted.get(archive(1)) == b"\x00old window"
    assert await restarted.get_by_prefix(f"pa:{AGENT}:{SESSION}:archive:") == {archive(1): b"\x00old window"}


async def test_backend_implements_every_base_kv_store_operation(store):
    backend = store.bind(NS, AGENT, SESSION)
    await backend.set(STATE, "v1")
    await backend.set(STATE, "v2")
    await backend.set(archive(1), "a1")
    await backend.set(archive(2), "a2")

    assert await backend.get(STATE) == "v2"
    assert await backend.exists(STATE)
    assert await backend.mget([STATE, INDEX, archive(2)]) == ["v2", None, "a2"]
    assert await backend.batch_delete([archive(1), INDEX]) == 1
    await backend.delete(STATE)
    assert not await backend.exists(STATE)
    await backend.delete_by_prefix(f"pa:{AGENT}:{SESSION}:")
    assert await backend.get_by_prefix("") == {}


async def test_exclusive_set_respects_live_entries_and_expiry(store, clock):
    backend = store.bind(NS, AGENT, SESSION)

    assert await backend.exclusive_set(INDEX, "first", expiry=10)
    assert not await backend.exclusive_set(INDEX, "second")
    clock.now += 10
    assert await backend.get(INDEX) is None
    assert await backend.exclusive_set(INDEX, "third")
    clock.now += 1_000
    assert await backend.get(INDEX) == "third"


async def test_pipeline_runs_queued_operations_together(store):
    backend = store.bind(NS, AGENT, SESSION)
    pipe = backend.pipeline()
    await pipe.set(STATE, "s")
    await pipe.set(INDEX, b"i")
    await pipe.exists(STATE)
    await pipe.get(INDEX)

    assert await pipe.execute() == [True, True, True, b"i"]
    assert await backend.get(STATE) == "s"


async def test_failed_pipeline_writes_nothing(store):
    backend = store.bind(NS, AGENT, SESSION)
    pipe = backend.pipeline()
    await pipe.set(STATE, "s")
    pipe._operations.append(("set", INDEX, 1))

    with pytest.raises(PersistentContractError):
        await pipe.execute()
    assert await backend.get(STATE) is None


async def test_non_text_values_are_rejected(store):
    backend = store.bind(NS, AGENT, SESSION)

    with pytest.raises(PersistentContractError):
        await backend.set(STATE, 1)
    with pytest.raises(PersistentContractError):
        await backend.pipeline().set(STATE, {"a": 1})


def test_backend_passes_core_port_validation(store):
    class Port:
        async def record(self, execution, event): ...
        async def read_execution_log(self, execution, execution_id): ...
        async def admit(self, execution, model_call_id): ...
        async def report(self, execution, model_call_id, usage): ...

    core.PersistentPorts(commit=Port(), budget=Port(), window_backend=store.bind(NS, AGENT, SESSION)).validate()


# JM-05-V-02 Session isolation and separation from Agent files


async def test_session_cannot_reach_another_sessions_keys(store):
    mine = store.bind(NS, AGENT, SESSION)
    other = store.bind(NS, AGENT, "sess_b")
    await other.set(f"pa:{AGENT}:sess_b:state", "secret")

    for call in (
        lambda: mine.get(f"pa:{AGENT}:sess_b:state"),
        lambda: mine.set(f"pa:{AGENT}:sess_b:state", "overwrite"),
        lambda: mine.delete(f"pa:{AGENT}:sess_b:state"),
        lambda: mine.mget([f"pa:{AGENT}:sess_b:state"]),
        lambda: mine.exclusive_set("state", "x"),
    ):
        with pytest.raises(PersistentContractError, match="outside this Session"):
            await call()
    assert await mine.get_by_prefix("pa:") == {}
    await mine.delete_by_prefix("pa:")
    assert await other.get(f"pa:{AGENT}:sess_b:state") == "secret"


async def test_same_keys_in_another_namespace_are_separate(store):
    first = store.bind(NS, AGENT, SESSION)
    second = store.bind("workspace-2", AGENT, SESSION)
    await first.set(STATE, "first")

    assert await second.get(STATE) is None
    await second.set(STATE, "second")
    assert await first.get(STATE) == "first"


def test_invalid_scope_is_refused(store):
    with pytest.raises(Exception, match="invalid session_id"):
        store.bind(NS, AGENT, " ")


async def test_window_data_lives_only_in_the_private_database(tmp_path, store):
    await store.bind(NS, AGENT, SESSION).set(STATE, "window-marker")

    data_dir = tmp_path / "data"
    assert [p.name for p in data_dir.iterdir() if not p.name.startswith("agents.sqlite3-")] == ["agents.sqlite3"]
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((data_dir / "agents.sqlite3").stat().st_mode) == 0o600


# JM-05-V-03 retention, deletion cleanup and refusal of other backends


async def test_retention_drops_old_archives_and_expired_entries_only(store, clock):
    backend = store.bind(NS, AGENT, SESSION)
    await backend.set(archive(1), "old")
    await backend.set(STATE, "state")
    await backend.set(INDEX, "index")
    await backend.exclusive_set(f"pa:{AGENT}:{SESSION}:lock", "x", expiry=60)
    clock.now += timedelta(days=31).total_seconds()
    await backend.set(archive(2), "recent")

    assert store.purge_expired(archive_retention=timedelta(days=30)) == 2
    assert await backend.get_by_prefix(f"pa:{AGENT}:{SESSION}:") == {
        archive(2): "recent",
        INDEX: "index",
        STATE: "state",
    }


async def test_purging_a_session_or_agent_leaves_the_rest(store):
    a = store.bind(NS, AGENT, SESSION)
    b = store.bind(NS, AGENT, "sess_b")
    other_agent = store.bind(NS, "mate_ops", SESSION)
    await a.set(STATE, "a")
    await b.set(f"pa:{AGENT}:sess_b:state", "b")
    await other_agent.set(f"pa:mate_ops:{SESSION}:state", "ops")

    assert store.purge_session(NS, AGENT, SESSION) == 1
    assert await a.get(STATE) is None
    assert await b.get(f"pa:{AGENT}:sess_b:state") == "b"
    assert store.purge_agent(NS, AGENT) == 1
    assert await b.get(f"pa:{AGENT}:sess_b:state") is None
    assert await other_agent.get(f"pa:mate_ops:{SESSION}:state") == "ops"


def _invocation():
    snapshot = publish_spec(AGENT, "v1", {"card": {"id": AGENT, "name": "研发同事"}, "enable_sys_operation": False})
    request = RequestRecord(
        namespace_id=NS,
        agent_id=AGENT,
        session_id=SESSION,
        request_id="request-1",
        source_ref="source:msg-1",
        config_version=snapshot.config_version,
        config_fingerprint=snapshot.fingerprint,
        access=AccessSnapshot(
            principal_ref="user-1",
            startup_scope=frozenset({"chat"}),
            source_scope=frozenset({"chat"}),
            policy_revision="policy-1",
        ),
        request_input=core.RequestInput(kind="scheduled", text="早报", reply_target="conversation-1"),
        received_at=datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc),
    )
    return snapshot, request, ExecutionRecord(request_id="request-1", execution_id="e-1")


@pytest.mark.parametrize(
    "backend",
    [
        lambda store: InMemoryKVStore(),
        lambda store: store.bind(NS, AGENT, "sess_b"),
        lambda store: store.bind("workspace-2", AGENT, SESSION),
        lambda store: None,
    ],
)
def test_invocation_refuses_a_backend_not_bound_to_its_session(store, backend):
    snapshot, request, execution = _invocation()

    with pytest.raises(PersistentContractError, match="window_backend"):
        build_invocation(snapshot, request, execution, ports=core.PersistentPorts(window_backend=backend(store)))
