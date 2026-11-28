# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The first IPC frame authorizes one deployment, never a different connection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.ipc.protocol import PROTOCOL_VERSION
from jiuwenswarm.server.ipc.startup import await_startup_acceptance, startup_acceptance
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


@pytest.fixture
def hello():
    return {"kind": "hello", "protocol": PROTOCOL_VERSION, "generation": 1,
            "instance": "worker-one", "code_identity": "code-one", "data_scope": "data-one"}


@pytest.mark.asyncio
async def test_exact_acceptance_releases_startup(hello):
    await await_startup_acceptance(SimpleNamespace(recv=AsyncMock(return_value=startup_acceptance(hello))), hello)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["kind", "protocol", "generation", "instance", "code_identity", "data_scope"])
async def test_other_deployment_or_old_protocol_never_releases_startup(hello, field):
    frame = startup_acceptance(hello)
    frame[field] = "different"
    with pytest.raises(RuntimeError, match="not accepted"):
        await await_startup_acceptance(SimpleNamespace(recv=AsyncMock(return_value=frame)), hello)


@pytest.mark.asyncio
async def test_missing_permission_is_bounded_and_does_not_construct_runtime(hello):
    with pytest.raises(TimeoutError):
        await await_startup_acceptance(SimpleNamespace(recv=asyncio.Event().wait), hello, timeout=.01)


@pytest.mark.asyncio
async def test_gateway_and_snapshot_cannot_send_before_validation():
    supervisor = RuntimeSupervisor(Readiness(), socket_path="unopened-test-endpoint")
    conn = SimpleNamespace(send=AsyncMock())
    supervisor._conn = conn
    await supervisor._notify_gateway(attached=True)
    snapshot = await supervisor.get_runtime_snapshot(Mock())
    assert snapshot["recovery_pending"] is True
    conn.send.assert_not_awaited()
    supervisor._validated_conn = conn
    await supervisor._notify_gateway(attached=True)
    conn.send.assert_awaited_once_with({"kind": "gateway.attach"})


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False])
async def test_failed_front_stops_only_its_own_launch(owned):
    supervisor = RuntimeSupervisor(Readiness(), socket_path="unopened-test-endpoint")
    process = SimpleNamespace(stop=AsyncMock())
    supervisor._process = process
    supervisor._owns_process = owned
    supervisor._loop_task = asyncio.create_task(asyncio.Event().wait())
    await supervisor.abort_startup()
    assert process.stop.await_count == int(owned)


def test_scandir_preserves_source_membership_and_marker_order(tmp_path, monkeypatch):
    from jiuwenswarm.server.supervisor import identity_cache

    (tmp_path / "z.py").write_text("z=1")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "a.py").write_text("a=1")
    excluded = tmp_path / "channels/web/frontend/node_modules"
    excluded.mkdir(parents=True)
    (excluded / "ignore.py").write_text("ignored=1")
    paths = identity_cache._source_paths(tmp_path)
    assert paths == sorted([tmp_path / "z.py", nested / "a.py"])
    monkeypatch.setattr(identity_cache, "_ntfs_marker", lambda path: path.name)
    assert identity_cache._source_markers(paths) == [path.name for path in paths]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ready", "degraded", "warming"])
async def test_late_front_bind_preserves_early_worker_signal(state):
    from jiuwenswarm.server.front.server import AgentServerFront

    readiness = Readiness()
    readiness.mark_runtime_warming()
    if state != "warming":
        readiness.mark_agent_ready()
    if state == "degraded":
        readiness.mark_degraded()
    readiness.note_runtime_progress(runtime_phase="EXECUTION_MIN_READY", capability_readiness={"image": "pending"})
    expected = readiness.snapshot()
    front = AgentServerFront(port=0, readiness=readiness)
    try:
        await front.start()
        assert readiness.snapshot() == expected
    finally:
        await front.stop()


@pytest.mark.asyncio
async def test_resident_runtime_cannot_publish_on_pending_hello():
    import json
    from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge

    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    pending = SimpleNamespace(send=AsyncMock())
    generation = holder.begin_session()
    await bridge.send(json.dumps({"request_id": "prior-execution", "body": {"event_type": "chat.delta", "delta": "retained"}}))
    pending.send.assert_not_awaited()
    assert holder.conn is None
    holder.activate_session(pending, generation)
    assert holder.conn is pending
    holder.close_session()
    new_generation = holder.begin_session()
    assert new_generation != generation
    with pytest.raises(RuntimeError, match="stale"):
        holder.activate_session(pending, generation)
