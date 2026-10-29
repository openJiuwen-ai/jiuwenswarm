# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Recovery must preserve resident ownership and observe actual process exit."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from filelock import FileLock
import pytest

from jiuwenswarm import debug_launcher
from jiuwenswarm.server.ipc import client
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor, shutdown, worker_process
from jiuwenswarm.server.worker import service


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [FileNotFoundError, ConnectionRefusedError, TimeoutError, PermissionError])
async def test_probe_only_treats_definitive_absence_as_missing(monkeypatch, tmp_path, error):
    path = tmp_path / "socket"
    path.touch()
    monkeypatch.setattr(client, "connect", AsyncMock(side_effect=error("controlled")))
    if error in (FileNotFoundError, ConnectionRefusedError):
        assert await client.try_connect(str(path)) is None
    else:
        with pytest.raises(error):
            await client.try_connect(str(path))


def test_posix_stale_endpoint_is_not_a_live_owner(monkeypatch, tmp_path):
    path = tmp_path / "stale.sock"
    path.touch()
    monkeypatch.setattr(client, "os", SimpleNamespace(name="posix", path=os.path))
    assert not client.endpoint_is_listening(str(path))
    with FileLock(str(path) + ".owner.lock", timeout=0):
        assert client.endpoint_is_listening(str(path))
    assert not client.endpoint_is_listening(str(path))
    assert path.exists(), "Do not unlink a legacy endpoint without proving its owner"


@pytest.mark.asyncio
async def test_cancelled_native_connect_closes_a_late_connection(monkeypatch):
    import multiprocessing.connection

    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    raw = SimpleNamespace(close=closed.set)

    def connect(*args):
        entered.set()
        release.wait(2)
        return raw

    monkeypatch.setattr(multiprocessing.connection, "Client", connect)
    task = asyncio.create_task(client._connect_pipe("controlled"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(closed.wait, 1)
    finally:
        release.set()


def test_late_native_connection_is_closed_after_event_loop_exit(monkeypatch):
    import multiprocessing.connection

    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def connect(*args):
        entered.set()
        release.wait(2)
        return SimpleNamespace(close=closed.set)

    async def cancel_connect():
        task = asyncio.create_task(client._connect_pipe("controlled"))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(multiprocessing.connection, "Client", connect)
    try:
        asyncio.run(cancel_connect())
        release.set()
        assert closed.wait(1), "Late native handle leaked after its asyncio loop exited"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_native_connect_retry_has_a_real_deadline(monkeypatch):
    async def blocked(path):
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "connect", blocked)
    with pytest.raises(TimeoutError, match="ipc not ready"):
        await asyncio.wait_for(client.connect_with_retry("controlled", timeout=.01), .5)


@pytest.mark.asyncio
async def test_stale_legacy_endpoint_does_not_block_migration(monkeypatch, tmp_path):
    current = str(tmp_path / "current")
    old = str(tmp_path / "legacy")
    Path(old).touch()
    monkeypatch.setattr(runtime_supervisor, "default_socket_path", lambda: current)
    monkeypatch.setattr(worker_process, "legacy_socket_paths", lambda: [old])
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(runtime_supervisor, "try_connect", lookup)
    monkeypatch.setattr(client, "os", SimpleNamespace(name="posix", path=os.path))
    monkeypatch.setattr(runtime_supervisor, "endpoint_is_listening", client.endpoint_is_listening)
    owner = runtime_supervisor.RuntimeSupervisor(Readiness(), socket_path=current)
    await owner._migrate_legacy_workers()
    assert not owner._fatal
    assert lookup.await_count == 1
    assert 0 < lookup.await_args.kwargs["timeout"] <= .5
    assert Path(old).exists()


@pytest.mark.asyncio
async def test_migration_reports_specific_failure_and_keeps_peer_alive(monkeypatch, tmp_path):
    from jiuwenswarm.server.supervisor import resident_lifecycle

    current = str(tmp_path / "current")
    old = str(tmp_path / "legacy")
    peer = SimpleNamespace(recv=AsyncMock(return_value={"kind": "hello", "stub": False}), close=AsyncMock())
    monkeypatch.setattr(runtime_supervisor, "default_socket_path", lambda: current)
    monkeypatch.setattr(worker_process, "legacy_socket_paths", lambda: [old])
    monkeypatch.setattr(runtime_supervisor, "try_connect", AsyncMock(return_value=peer))
    monkeypatch.setattr(resident_lifecycle, "worker_process", Mock(return_value=(object(), 1)))
    retire = AsyncMock(side_effect=RuntimeError("deployment ownership not proven"))
    monkeypatch.setattr(resident_lifecycle, "retire_worker", retire)
    readiness = Readiness()
    owner = runtime_supervisor.RuntimeSupervisor(readiness, socket_path=current)
    with pytest.raises(RuntimeError, match="ownership not proven"):
        await owner._migrate_legacy_workers()
    assert readiness.snapshot()["failed"]
    assert "ownership not proven" in owner._fatal_reason
    assert old in owner._fatal_reason
    peer.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_uses_500ms_probe_and_skips_dead_endpoint(monkeypatch, tmp_path):
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(shutdown, "try_connect", lookup)
    monkeypatch.setattr(shutdown, "endpoint_is_listening", lambda path: False)
    result = await shutdown.shutdown_resident_worker(socket_path=str(tmp_path / "stale"), timeout=2)
    assert result == {"ok": True, "absent": True}
    assert lookup.await_args.kwargs["timeout"] == .5


@pytest.mark.parametrize("front_alive", [False, True])
def test_failed_worker_stop_keeps_state_for_retry(monkeypatch, front_alive):
    recorded = SimpleNamespace(pid=123, log_file=None)
    state = {"value": recorded}
    monkeypatch.setattr(debug_launcher, "read_debug_state", lambda: state["value"])
    monkeypatch.setattr(debug_launcher, "is_debug_service_alive", lambda _: front_alive)
    monkeypatch.setattr(debug_launcher, "is_process_alive", lambda _: False)
    monkeypatch.setattr(debug_launcher, "_stop_process_tree", lambda *args, **kwargs: True)
    monkeypatch.setattr(debug_launcher, "clear_debug_state", lambda: state.update(value=None))
    stopper = Mock(side_effect=[False, False, True])
    monkeypatch.setattr(debug_launcher, "_stop_resident_worker", stopper)
    assert debug_launcher.stop_debug_service() == 1
    assert debug_launcher.stop_debug_service() == 1
    assert state["value"] is recorded
    assert debug_launcher.stop_debug_service() == 0
    assert state["value"] is None
    assert stopper.call_count == 3


@pytest.mark.asyncio
async def test_failed_hello_does_not_unload_resident_service(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(service.logger, "warning", warning)
    class Connection:
        def __init__(self, index):
            self.index = index
            self.closed = False

        async def send(self, frame):
            if self.index == 0:
                raise BrokenPipeError("peer left during HELLO")

        async def recv(self):
            return {"kind": "shutdown"}

        async def close(self):
            self.closed = True

    peers = [Connection(0), Connection(1)]

    class Server:
        def __init__(self):
            self.accepted = 0

        async def start(self):
            pass

        async def accept(self):
            peer = peers[self.accepted]
            self.accepted += 1
            return peer

        async def close(self):
            pass

    server = Server()
    monkeypatch.setattr(service, "IpcServer", lambda _: server)
    loader = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "_load_runtime", loader)
    monkeypatch.setattr(service, "_close_worker", AsyncMock())
    await service.serve_worker("controlled", host="127.0.0.1", port=0, code_identity_value="pinned")
    assert server.accepted == 2
    assert peers[0].closed
    loader.assert_awaited_once()
    warning.assert_called_once()
    assert "HELLO/readiness" in warning.call_args.args[0]
    assert warning.call_args.args[1] == "BrokenPipeError"


@pytest.mark.parametrize("entry_mode", ["module", "frozen-dispatch"])
@pytest.mark.parametrize("phase", ["dispatch", "load", "shutdown"])
def test_fatal_worker_entry_exits_with_blocked_executor(tmp_path, phase, entry_mode):
    code = r"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
from jiuwenswarm.server.worker import main, service

phase, endpoint, entry_mode, source_root = sys.argv[1:]
pool = ThreadPoolExecutor(max_workers=1)
gate = threading.Event()
started = threading.Event()

def blocked():
    started.set()
    gate.wait()

async def controlled(*args, **kwargs):
    pool.submit(blocked)
    started.wait()
    print("BLOCKED_EXECUTOR_READY", flush=True)
    class Dispatcher:
        async def drain(self, **kwargs):
            return phase != "dispatch"
    class Runtime:
        async def shutdown(self):
            await asyncio.Event().wait()
    class Server:
        async def close(self):
            print("CLOSED", flush=True)
    async def load():
        await asyncio.Event().wait()
    task = asyncio.create_task(load()) if phase == "load" else None
    await service._close_worker(
        Dispatcher(), task, {"runtime": Runtime() if phase == "shutdown" else None}, Server(),
        dispatch_timeout=.01, load_timeout=.01, runtime_timeout=.01,
    )

service.serve_worker = controlled
main._activate_managed_external_cli_paths = lambda: None
sys.argv = ["worker", "--socket", endpoint]
if entry_mode == "module":
    getattr(main, "_run", main.main)()
else:
    import runpy
    from pathlib import Path
    sys.frozen = True
    sys.argv = [sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", endpoint]
    entry = runpy.run_path(str(Path(source_root) / "scripts" / "jiuwenswarm_exe_entry.py"))
    entry["_dispatch"]()
"""
    environment = dict(os.environ, JIUWENSWARM_HOME=str(tmp_path), JIUWENSWARM_DATA_DIR=str(tmp_path / "data"))
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", code, phase, str(tmp_path / "worker"), entry_mode,
         str(Path(__file__).resolve().parents[3])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
    )
    try:
        stdout,stderr=child.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        stdout,stderr=child.communicate(timeout=3)
        pytest.fail(f"fatal {phase} left process alive: {stderr.decode(errors='replace')[-1000:]}")
    assert child.returncode==1
    assert b"BLOCKED_EXECUTOR_READY" in stdout
    assert b"fatal exit" in stderr


@pytest.mark.asyncio
async def test_readiness_replay_failure_keeps_the_loaded_runtime(monkeypatch):
    loaded_event = asyncio.Event()

    class Connection:
        def __init__(self, index):
            self.index = index
            self.frames = []
            self.closed = False

        async def send(self, frame):
            if self.index == 1 and frame["kind"] == "readiness":
                raise BrokenPipeError("Front left during readiness replay")
            self.frames.append(frame)

        async def recv(self):
            await loaded_event.wait()
            if self.index == 0:
                raise OSError("Front restarted after Runtime became ready")
            return {"kind": "shutdown"}

        async def close(self):
            self.closed = True

    peers = [Connection(index) for index in range(3)]

    class Server:
        def __init__(self):
            self.accepted = 0

        async def start(self):
            pass

        async def accept(self):
            peer = peers[self.accepted]
            self.accepted += 1
            return peer

        async def close(self):
            pass

    server = Server()

    async def load_runtime(host, port, signals, dispatcher, loaded):
        loaded["runtime"] = object()
        signals.agent_ready()
        loaded_event.set()

    loader = AsyncMock(side_effect=load_runtime)
    monkeypatch.setattr(service, "IpcServer", lambda _: server)
    monkeypatch.setattr(service, "_load_runtime", loader)
    monkeypatch.setattr(service, "_close_worker", AsyncMock())
    await service.serve_worker("controlled", host="127.0.0.1", port=0, code_identity_value="pinned")
    assert server.accepted == 3
    assert peers[0].closed and peers[1].closed
    assert any(frame.get("state") == "AGENT_READY" for frame in peers[2].frames)
    loader.assert_awaited_once()


@pytest.mark.parametrize("stopped", [False, True])
def test_stop_with_missing_state_reports_the_detached_worker_result(monkeypatch, stopped):
    monkeypatch.setattr(debug_launcher, "read_debug_state", lambda: None)
    stopper = Mock(return_value=stopped)
    monkeypatch.setattr(debug_launcher, "_stop_resident_worker", stopper)
    assert debug_launcher.stop_debug_service(timeout=.5) == (0 if stopped else 1)
    stopper.assert_called_once_with(timeout=.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_timeout", [30, .1])
async def test_bring_up_uses_500ms_probe_before_hello_validation(monkeypatch, tmp_path, connect_timeout):
    path = str(tmp_path / "current")
    peer = SimpleNamespace(recv=AsyncMock(return_value={"kind": "heartbeat"}), close=AsyncMock())
    lookup = AsyncMock(return_value=peer)
    monkeypatch.setattr(runtime_supervisor, "try_connect", lookup)
    monkeypatch.setattr(runtime_supervisor, "time", SimpleNamespace(monotonic=lambda: 0.0))
    owner = runtime_supervisor.RuntimeSupervisor(Readiness(), socket_path=path, connect_timeout=connect_timeout)
    with pytest.raises(RuntimeError, match="expected hello"):
        await owner._bring_up()
    lookup.assert_awaited_once_with(path, timeout=min(.5, connect_timeout))
    peer.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,outcome,success", [
    (True, {"kind": "shutdown.done", "ok": False, "error": "dispatch still running"}, False),
    (True, None, False),
    (True, {"kind": "shutdown.done", "ok": True}, True),
    (False, None, True),
])
async def test_protocol_one_retirement_requires_advertised_shutdown_result(
    monkeypatch, tmp_path, capability, outcome, success,
):
    from jiuwenswarm.server.supervisor import resident_lifecycle

    hello = {"kind": "hello", "protocol": 1, "pid": 123}
    if capability:
        hello["shutdown_result"] = True
    frames = [hello, outcome if outcome is not None else ConnectionError("Worker exited")]
    peer = SimpleNamespace(recv=AsyncMock(side_effect=frames), send=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(shutdown, "try_connect", AsyncMock(return_value=peer))
    monkeypatch.setattr(resident_lifecycle, "worker_process", lambda *args, **kwargs: (object(), 1.0))
    monkeypatch.setattr(resident_lifecycle, "_wait_exit", AsyncMock())
    if success:
        result = await shutdown.shutdown_resident_worker(socket_path=str(tmp_path / "worker"), timeout=2)
        assert result["ok"] is True
    else:
        with pytest.raises(RuntimeError, match="incomplete shutdown|without a shutdown acknowledgement"):
            await shutdown.shutdown_resident_worker(socket_path=str(tmp_path / "worker"), timeout=2)
    peer.close.assert_awaited_once()
