# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Restricted /proc/stat must not break Worker identity or PID-reuse checks."""

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from jiuwenswarm.common import process_identity as identity
from jiuwenswarm.server.ipc.protocol import PROTOCOL_VERSION
from jiuwenswarm.server.ipc.startup import startup_acceptance
from jiuwenswarm.server.ipc.stream import StreamConnection
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import resident_lifecycle
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


def _denied():
    raise psutil.AccessDenied(pid=123, msg="/proc/stat permission denied")


@pytest.fixture
def restricted_process(monkeypatch):
    process = SimpleNamespace(
        pid=123, create_time=Mock(side_effect=_denied), username=lambda: "user",
        exe=lambda: sys.executable,
        cmdline=lambda: [sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", "worker"],
        is_running=lambda: True, status=lambda: psutil.STATUS_RUNNING,
        wait=Mock(return_value=0), terminate=Mock(), kill=Mock(),
    )
    monkeypatch.setattr(psutil, "Process", lambda pid=None: process)
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4242)
    return process


@pytest.mark.parametrize("comm", ["python3", "worker (one) with spaces)"])
def test_proc_ticks_parse_field_22_after_process_name(tmp_path, comm):
    path = tmp_path / "123"
    path.mkdir()
    fields = ["S", *("0" for _ in range(18)), "4242", "0"]
    (path / "stat").write_text(f"123 ({comm}) {' '.join(fields)}\n", encoding="utf-8")
    assert identity.proc_start_ticks(123, proc_root=tmp_path) == 4242


@pytest.mark.parametrize("content", [
    b"", b"123 python S", b"123 (worker) S", b"123 (worker) " + b"0 " * 20, b"\xff",
])
def test_unreadable_or_malformed_proc_is_not_identity(tmp_path, content):
    assert identity.proc_start_ticks(123, proc_root=tmp_path) is None
    path = tmp_path / "123"
    path.mkdir()
    (path / "stat").write_bytes(content)
    assert identity.proc_start_ticks(123, proc_root=tmp_path) is None


def test_normal_creation_time_is_preserved(monkeypatch):
    process = SimpleNamespace(create_time=lambda: 123.5)
    monkeypatch.setattr(identity, "proc_start_ticks", Mock(side_effect=AssertionError("unneeded fallback")))
    assert identity.read_process_identity(123, process=process) == 123.5


@pytest.mark.parametrize("protocol", [1, 2, PROTOCOL_VERSION])
def test_hello_birth_fields_are_optional_only_for_protocol_one(restricted_process, protocol):
    hello = {"protocol": protocol, "process_created": None, "process_start_ticks": None}
    if protocol == 1:
        assert identity.validate_process_identity(123, hello) == "proc_start_ticks:4242"
    else:
        with pytest.raises(RuntimeError, match=f"protocol {protocol} HELLO requires a process identity"):
            identity.validate_process_identity(123, hello)


@pytest.mark.parametrize("fields", [{}, {"process_created": None, "process_start_ticks": None}, {"process_started": 12.5}])
def test_current_protocol_requires_explicit_birth_identity(restricted_process, fields):
    """The startup-permission protocol retains the newer identity requirement."""
    with pytest.raises(RuntimeError, match="HELLO requires a process identity"):
        identity.validate_process_identity(123, {"protocol": PROTOCOL_VERSION, **fields})


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["resident", "front_loopback"])
async def test_current_protocol_without_birth_fields_is_refused_by_supervisor(
    monkeypatch, restricted_process, transport,
):
    from jiuwenswarm.server.supervisor.identity import data_scope, deployment_scope

    hello = {"kind": "hello", "protocol": PROTOCOL_VERSION, "generation": 7, "pid": 123,
             "deployment_scope": deployment_scope(), "data_scope": data_scope(),
             "code_identity": "a" * 64, "stub": False, "instance": "worker"}
    if transport == "resident":
        with pytest.raises(RuntimeError, match=f"protocol {PROTOCOL_VERSION} HELLO requires a process identity"):
            resident_lifecycle.worker_process(hello, "worker")
        return
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", transport)
    front = RuntimeSupervisor(Readiness(), stub=False)
    front._launch_generation = 7
    peer = SimpleNamespace(recv=AsyncMock(return_value=hello), close=AsyncMock(), send=AsyncMock())

    async def connect():
        front._conn = peer
        return "a" * 64

    monkeypatch.setattr(front, "_connect_attached", connect)
    with pytest.raises(RuntimeError, match=f"protocol {PROTOCOL_VERSION} HELLO requires a process identity"):
        await front._bring_up()
    assert front.worker_pid is None and front._validated_conn is None
    peer.close.assert_awaited_once()
    peer.send.assert_not_awaited()


@pytest.mark.parametrize("error", [psutil.AccessDenied(123), PermissionError("/proc/stat")])
def test_creation_time_failure_falls_back_to_tagged_ticks(monkeypatch, error):
    process = SimpleNamespace(create_time=Mock(side_effect=error))
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4242)
    assert identity.read_process_identity(123, process=process) == "proc_start_ticks:4242"


def test_missing_process_does_not_use_stale_proc_identity(monkeypatch):
    process = SimpleNamespace(create_time=Mock(side_effect=psutil.NoSuchProcess(123)))
    fallback = Mock(return_value=4242)
    monkeypatch.setattr(identity, "proc_start_ticks", fallback)
    with pytest.raises(psutil.NoSuchProcess):
        identity.read_process_identity(123, process=process)
    fallback.assert_not_called()


def test_both_identity_sources_denied_fail_closed(monkeypatch, restricted_process):
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: None)
    with pytest.raises(psutil.AccessDenied, match=r"identity unavailable.*proc/stat.*proc/123/stat"):
        identity.read_process_identity(123)


def test_stored_ticks_remain_comparable_after_permissions_change(restricted_process):
    restricted_process.create_time.side_effect = None
    restricted_process.create_time.return_value = 1700000000.0
    assert identity.process_identity_matches(123, "proc_start_ticks:4242")
    restricted_process.create_time.assert_not_called()
    assert not identity.process_identity_matches(123, "proc_start_ticks:4243")


@pytest.mark.parametrize("worker_denied", [False, True])
@pytest.mark.parametrize("front_denied", [False, True])
def test_hello_uses_a_common_identity_under_asymmetric_permissions(
    restricted_process, worker_denied, front_denied,
):
    restricted_process.create_time.side_effect = _denied if worker_denied else None
    restricted_process.create_time.return_value = 1700000000.0
    fields = identity.process_identity_fields(123)
    assert fields["process_created"] == (None if worker_denied else 1700000000.0)
    assert fields["process_start_ticks"] == 4242
    restricted_process.create_time.side_effect = _denied if front_denied else None
    validated = identity.validate_process_identity(123, fields)
    assert validated == "proc_start_ticks:4242"


def test_common_ticks_take_priority_over_wall_clock_changes(restricted_process):
    restricted_process.create_time.side_effect = None
    restricted_process.create_time.return_value = 1700000001.0
    assert identity.validate_process_identity(123, {
        "process_created": 1700000000.0, "process_start_ticks": 4242,
    }) == "proc_start_ticks:4242"


def test_unreadable_optional_ticks_keep_comparable_timestamps(monkeypatch, restricted_process):
    restricted_process.create_time.side_effect = None
    restricted_process.create_time.return_value = 1700000000.0
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: None)
    assert identity.validate_process_identity(123, {
        "process_created": 1700000000.0, "process_start_ticks": 4242,
    }) == 1700000000.0


@pytest.mark.parametrize("fields", [
    {"process_start_ticks": 4243},
    {"process_created": 123.0},
    {"process_start_ticks": True},
    {"process_start_ticks": "4242"},
    {"process_created": True},
    {"process_created": "4242"},
    {"process_created": float("nan")},
    {"process_created": float("inf")},
])
def test_hello_rejects_reused_or_malformed_process_identity(restricted_process, fields):
    restricted_process.create_time.side_effect = None
    restricted_process.create_time.return_value = 1700000000.0
    with pytest.raises(RuntimeError):
        identity.validate_process_identity(123, fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("stub", [False, True])
async def test_worker_and_stub_send_hello_when_creation_time_denied(monkeypatch, restricted_process, stub):
    from jiuwenswarm.server.worker import service, stub as stub_module

    peer = SimpleNamespace(send=AsyncMock())
    accepted = False

    async def receive():
        nonlocal accepted
        if not accepted:
            accepted = True
            return startup_acceptance(peer.send.call_args_list[0].args[0])
        return {"kind": "shutdown"}

    peer.recv = AsyncMock(side_effect=receive)
    if stub:
        monkeypatch.setattr(stub_module, "arm_stall_dump", lambda: None)
        assert await stub_module._serve_one(peer, generation=7, ready_delay=0, exit_on_request=False,
            runtime_loaded=False, instance="worker", code_identity_value="a" * 64)
    else:
        await service._send_hello(peer, runtime_loaded=False, generation=7,
            instance="worker", code_identity_value="a" * 64)
    hello = peer.send.call_args_list[0].args[0]
    assert hello["kind"] == "hello" and hello["pid"] == os.getpid()
    assert hello["process_created"] is None and hello["process_start_ticks"] == 4242


@pytest.mark.asyncio
async def test_worker_hello_failure_identifies_pid_and_stage(monkeypatch, restricted_process):
    from jiuwenswarm.server.worker import service

    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: None)
    peer = SimpleNamespace(send=AsyncMock())
    with pytest.raises(RuntimeError, match=rf"Worker HELLO pid={os.getpid()}.*identity.*proc/stat"):
        await service._send_hello(peer, runtime_loaded=False, generation=7,
            instance="worker", code_identity_value="a" * 64)
    peer.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ticks", [4242, 4243, None])
async def test_attached_supervisor_validates_ticks_or_cleans_failed_hello(
    monkeypatch, restricted_process, ticks,
):
    from jiuwenswarm.server.supervisor.identity import data_scope, deployment_scope

    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "front_loopback")
    front = RuntimeSupervisor(Readiness(), stub=False)
    front._launch_generation = 7
    hello = {"kind": "hello", "protocol": PROTOCOL_VERSION, "generation": 7, "pid": 123,
        "data_scope": data_scope(), "deployment_scope": deployment_scope(), "code_identity": "a" * 64,
        "stub": False, "instance": "worker", "process_created": None, "process_start_ticks": 4242}
    peer = SimpleNamespace(recv=AsyncMock(return_value=hello), close=AsyncMock(), send=AsyncMock())

    async def connect():
        front._conn = peer
        return "a" * 64

    monkeypatch.setattr(front, "_connect_attached", connect)
    monkeypatch.setattr(front, "_start_side_tasks", Mock())
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: ticks)
    if ticks == 4242:
        await front._bring_up()
        assert front.worker_pid == 123 and front._worker_create_time == "proc_start_ticks:4242"
        assert front._validated_conn is peer
        peer.send.assert_awaited_once_with(startup_acceptance(hello))
    else:
        with pytest.raises(RuntimeError, match="PID identity changed" if ticks else "HELLO pid=123.*identity"):
            await front._bring_up()
        assert front.worker_pid is None and front._validated_conn is None
        peer.close.assert_awaited_once()
        peer.send.assert_not_awaited()


def test_resident_validation_and_reconnect_use_ticks(monkeypatch, restricted_process):
    monkeypatch.setattr(resident_lifecycle, "deployment_scope", lambda: "deployment")
    monkeypatch.setattr(resident_lifecycle, "data_scope", lambda: "data")
    hello = {"kind": "hello", "protocol": 2, "pid": 123,
        "deployment_scope": "deployment", "data_scope": "data", "process_start_ticks": 4242}
    process, created = resident_lifecycle.worker_process(hello, "worker")
    assert process is restricted_process and created == "proc_start_ticks:4242"
    assert resident_lifecycle._alive(process, created)
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4243)
    with pytest.raises(RuntimeError, match="PID identity changed"):
        resident_lifecycle._alive(process, created)


@pytest.mark.parametrize("reused", [False, True])
def test_shutdown_rechecks_ticks_before_signalling(monkeypatch, restricted_process, reused):
    front = RuntimeSupervisor(Readiness())
    front._worker_pid, front._worker_create_time = 123, "proc_start_ticks:4242"
    if reused:
        monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4243)
        with pytest.raises(RuntimeError, match="PID identity changed"):
            front._reap_validated_worker(deadline=time.monotonic() + 1, terminate=True)
        restricted_process.terminate.assert_not_called()
    else:
        assert not front._confirmed_worker_exit()
        assert not front._reap_validated_worker(deadline=time.monotonic() + 1, terminate=True)
        restricted_process.terminate.assert_called_once()
    restricted_process.kill.assert_not_called()


def test_booting_worker_observation_uses_ticks(monkeypatch, restricted_process):
    from jiuwenswarm.server.supervisor import shutdown

    restricted_process.info = {"cmdline": restricted_process.cmdline(), "exe": sys.executable, "username": "user"}
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([restricted_process]))
    pending = shutdown._pending_workers(["worker"])
    assert pending["worker"] == [(restricted_process, "proc_start_ticks:4242")]
    assert shutdown._pending_worker_alive(pending["worker"])


@pytest.mark.parametrize("phase", ["exited", "reused", "zombie", "unobservable"])
def test_pending_worker_liveness_preserves_guard_order(monkeypatch, restricted_process, phase):
    from jiuwenswarm.server.supervisor import shutdown

    if phase == "exited":
        restricted_process.is_running = lambda: False
        monkeypatch.setattr(identity, "proc_start_ticks", Mock(side_effect=AssertionError("exited process")))
        restricted_process.status = Mock(side_effect=AssertionError("exited process"))
    elif phase == "reused":
        monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4243)
        restricted_process.status = Mock(side_effect=AssertionError("reused process"))
    elif phase == "zombie":
        restricted_process.status = lambda: psutil.STATUS_ZOMBIE
    else:
        restricted_process.status = Mock(side_effect=psutil.AccessDenied(123))

    assert shutdown._pending_worker_alive([(restricted_process, "proc_start_ticks:4242")]) is (phase == "unobservable")


def test_group_owner_has_pid_reuse_protection_without_creation_time(monkeypatch, restricted_process):
    from jiuwenswarm.agents.swarm import group_route

    fields = group_route.owner_process_fields()
    assert fields == {"owner_process": [123, "proc_start_ticks:4242"]}
    assert group_route._owner_process_alive(fields)
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4243)
    assert not group_route._owner_process_alive(fields)


def test_instance_metadata_retains_numeric_display_time_and_tick_ownership(monkeypatch, restricted_process):
    from jiuwenswarm import start_services
    from jiuwenswarm.instance_manager import status

    restricted_process.cmdline = lambda: [sys.executable, "-m", "jiuwenswarm.app", "--dotenv", "/private/.env"]
    created = start_services._launcher_create_time()
    assert created == "proc_start_ticks:4242"
    metadata = start_services._instance_process_metadata({"app": restricted_process}, created)
    assert metadata["launcher"]["create_time"] is None
    assert metadata["launcher"]["process_identity"] == created
    assert metadata["witnesses"][0]["process_identity"] == created
    assert status._pid_matches_record(123, created)
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4243)
    assert not status._pid_matches_record(123, created)


@pytest.mark.asyncio
async def test_worker_hello_reports_phase_when_all_identity_reads_fail(monkeypatch, restricted_process, caplog):
    from jiuwenswarm.server.worker import service

    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: None)
    peer = SimpleNamespace(send=AsyncMock())
    with pytest.raises(RuntimeError, match=r"Worker HELLO pid=\d+.*identity.*proc/stat"):
        await service._send_hello(peer, runtime_loaded=False, generation=7,
            instance="worker", code_identity_value="a" * 64)
    peer.send.assert_not_awaited()
    assert "[Worker] HELLO" in caplog.text and "Traceback" in caplog.text


@pytest.mark.parametrize("ticks", [4242, 4243, None])
def test_tick_identity_is_required_before_service_tree_signals(monkeypatch, restricted_process, ticks):
    from jiuwenswarm.common import process_supervision

    restricted_process.children = lambda recursive: []
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: ticks)
    if ticks == 4242:
        assert process_supervision.signal_service_tree(123, expected_created="proc_start_ticks:4242")
        restricted_process.terminate.assert_called_once()
    else:
        with pytest.raises(RuntimeError, match="identity"):
            process_supervision.signal_service_tree(123, expected_created="proc_start_ticks:4242")
        restricted_process.terminate.assert_not_called()


def test_unobservable_booting_worker_prevents_false_shutdown_success(monkeypatch, restricted_process):
    from jiuwenswarm.server.supervisor import shutdown

    restricted_process.info = {"cmdline": restricted_process.cmdline(), "exe": sys.executable, "username": "user"}
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([restricted_process]))
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: None)
    pending = shutdown._pending_workers(["worker"])
    assert pending["worker"] == [(restricted_process, None)]
    assert shutdown._pending_worker_alive(pending["worker"])
    restricted_process.terminate.assert_not_called()
    restricted_process.kill.assert_not_called()


def test_instance_pid_file_separates_display_time_and_birth_identity(monkeypatch, restricted_process, tmp_path):
    from jiuwenswarm import start_services
    from jiuwenswarm.instance_manager import InstanceConfig, read_pid_file

    config = InstanceConfig("harmony", tmp_path, {"gateway": 20001})
    monkeypatch.setattr(start_services, "_start_process", Mock(return_value=restricted_process))
    monkeypatch.setattr(start_services, "_wait_for_services_ready", Mock())
    monkeypatch.setattr(start_services, "_wait_for_process_exit", Mock(return_value=0))
    monkeypatch.setattr(start_services, "_shutdown_services", Mock(return_value=True))
    assert start_services._run_instance_with_pid([("app", ["stub"], tmp_path)], config) == 0
    record = read_pid_file(config)
    assert isinstance(record["started_at"], float)
    assert record["launcher"]["create_time"] is None
    assert record["launcher"]["process_identity"] == "proc_start_ticks:4242"


def test_dead_launcher_witness_can_use_tick_identity(monkeypatch, restricted_process, tmp_path):
    from jiuwenswarm import start_services
    from jiuwenswarm.instance_manager import status

    restricted_process.cmdline = lambda: [sys.executable, "-m", "jiuwenswarm.app", "--dotenv", str(tmp_path / ".env")]
    record = start_services._instance_process_metadata({"app": restricted_process}, "proc_start_ticks:4242")
    launcher_pid = record["launcher"]["pid"]
    record.update(pgid=launcher_pid, sid=launcher_pid, dedicated_session=True, started_at=time.time())
    monkeypatch.setattr(status.os, "getpgid", lambda pid: launcher_pid, raising=False)
    monkeypatch.setattr(status.os, "getsid", lambda pid: launcher_pid, raising=False)
    assert status._has_live_group_witness(record, launcher_pid)
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4243)
    assert not status._has_live_group_witness(record, launcher_pid)


@pytest.mark.asyncio
@pytest.mark.parametrize(("frozen", "peer_pid", "has_deployment"), [
    (True, 123, True), (True, 456, True), (True, None, True),
    (False, 123, True), (False, 456, True), (False, None, True),
    (False, 123, False),
])
async def test_macos_retires_drifted_worker_only_with_verified_unix_peer(
    monkeypatch, restricted_process, tmp_path, frozen, peer_pid, has_deployment,
):
    monkeypatch.setattr(resident_lifecycle, "sys", SimpleNamespace(
        platform="darwin", frozen=frozen, executable=sys.executable,
    ))
    monkeypatch.setattr(resident_lifecycle, "deployment_scope", lambda: "deployment")
    monkeypatch.setattr(resident_lifecycle, "data_scope", lambda: "data")
    socket_path = str(tmp_path / "worker")
    monkeypatch.setattr(restricted_process, "cmdline", lambda: [
        sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", socket_path,
    ])
    restricted_process.create_time.side_effect = None
    restricted_process.create_time.return_value = 1700000001.0
    hello = {
        "kind": "hello", "protocol": 2, "pid": 123,
        "deployment_scope": "deployment" if has_deployment else None, "data_scope": "data",
        "process_created": 1700000000.0,
    }
    connection = Mock(spec=StreamConnection)
    connection.macos_unix_peer_pid.return_value = peer_pid
    connection.recv.return_value = {"kind": "shutdown.done", "ok": True}
    wait_exit = AsyncMock()
    monkeypatch.setattr(resident_lifecycle, "_wait_exit", wait_exit)

    if peer_pid == hello["pid"] and has_deployment:
        outcome = await resident_lifecycle.retire_worker(
            connection, hello, socket_path, deadline=time.monotonic() + 5, allow_legacy=True,
        )
        assert outcome == {"kind": "shutdown.done", "ok": True}
        connection.send.assert_awaited_once_with({"kind": "shutdown"})
        assert wait_exit.await_args.args[:2] == (restricted_process, 1700000001.0)
    else:
        message = "Worker PID identity changed" if has_deployment else "has no deployment identity"
        with pytest.raises(RuntimeError, match=message):
            await resident_lifecycle.retire_worker(
                connection, hello, socket_path, deadline=time.monotonic() + 5, allow_legacy=True,
            )
        connection.send.assert_not_awaited()
        wait_exit.assert_not_awaited()
