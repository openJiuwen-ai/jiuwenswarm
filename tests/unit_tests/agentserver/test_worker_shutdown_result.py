# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Protocol 1 shutdown results must reach stop and update consumers."""
import ast
import asyncio
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.update_shutdown import create_shutdown_receipt, complete_shutdown_receipt, require_clean_shutdown
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import resident_lifecycle, runtime_supervisor, shutdown
from jiuwenswarm.server.worker import service

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
@pytest.mark.parametrize("close_error", [None, OSError("peer closed"), asyncio.CancelledError()])
async def test_worker_reports_fatal_result_without_losing_the_fatal_exception(monkeypatch, close_error):
    peer = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(return_value={"kind": "shutdown"}),
                           close=AsyncMock(side_effect=close_error))
    server = SimpleNamespace(start=AsyncMock(), accept=AsyncMock(return_value=peer), close=AsyncMock())
    monkeypatch.setattr(service, "IpcServer", lambda _: server)
    monkeypatch.setattr(service, "_load_runtime", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_close_worker", AsyncMock(side_effect=service.WorkerForceExit("dispatch still running")))
    with pytest.raises(service.WorkerForceExit, match="dispatch still running"):
        await service.serve_worker("controlled", host="127.0.0.1", port=0, code_identity_value="pinned")
    hello = peer.send.await_args_list[0].args[0]
    assert hello["protocol"] == 1 and hello["shutdown_result"] is True
    assert peer.send.await_args_list[-1].args[0] == {
        "kind": "shutdown.done", "ok": False, "error": "dispatch still running",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_cancelled_runtime_shutdown_is_incomplete(cancel_task):
    class Runtime:
        async def shutdown(self):
            if cancel_task:
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            raise asyncio.CancelledError()

    with pytest.raises(service.WorkerForceExit, match="runtime shutdown cancelled"):
        await service._shutdown_runtime(Runtime(), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("pump", [False, True])
@pytest.mark.parametrize("outcome", [None, {"kind": "shutdown.done", "ok": False, "error": "shutdown failed"},
                                      {"kind": "shutdown.done", "ok": True}])
async def test_supervisor_shutdown_does_not_hide_failed_or_missing_result(monkeypatch, pump, outcome):
    frames = ([outcome] if outcome is not None else []) + [ConnectionError("Worker exited")]
    peer = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(side_effect=frames), close=AsyncMock())
    process = SimpleNamespace(wait=AsyncMock(return_value=0))
    owner = runtime_supervisor.RuntimeSupervisor(Readiness())
    owner._conn = owner._validated_conn = peer
    owner._expects_shutdown_result = True
    owner._process = process
    task = asyncio.create_task(owner._pump()) if pump else None
    owner._loop_task = task
    try:
        if outcome is None or outcome.get("ok") is not True:
            with pytest.raises(RuntimeError, match="shutdown result could not be verified"):
                await owner._shutdown_owned_worker()
        else:
            await owner._shutdown_owned_worker()
        process.wait.assert_awaited_once()
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("outcome", [None, {"kind": "shutdown.done", "ok": False, "error": "runtime shutdown failed"}])
def test_protocol_one_fatal_never_writes_successful_desktop_receipt(tmp_path, monkeypatch, outcome):
    hello = {"kind": "hello", "protocol": 1, "shutdown_result": True, "pid": 123}
    frames = [hello, outcome if outcome is not None else ConnectionError("fatal exit")]
    peer = SimpleNamespace(recv=AsyncMock(side_effect=frames), send=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(shutdown, "try_connect", AsyncMock(return_value=peer))
    monkeypatch.setattr(shutdown, "default_socket_path", lambda: str(tmp_path / "worker"))
    monkeypatch.setattr(shutdown, "legacy_socket_paths", lambda: [])
    monkeypatch.setattr(resident_lifecycle, "worker_process", lambda *args, **kwargs: (object(), 1.0))
    monkeypatch.setattr(resident_lifecycle, "_wait_exit", AsyncMock())
    tree = ast.parse((ROOT / "jiuwenswarm/channels/desktop/desktop_app.py").read_text(encoding="utf-8"))
    runtime_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DesktopRuntime")
    method = next(node for node in runtime_class.body if isinstance(node, ast.FunctionDef) and node.name == "shutdown")
    namespace = {"time": __import__("time"), "asyncio": asyncio, "logger": logging.getLogger(__name__),
                 "complete_shutdown_receipt": complete_shutdown_receipt,
                 "_terminate_process_tree": Mock(), "_kill_process_tree": Mock()}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "desktop-shutdown", "exec"), namespace)
    receipt = create_shutdown_receipt(tmp_path)
    desktop = SimpleNamespace(_lock=threading.Lock(), _is_shutting_down=False,
        _startup_cancelled=threading.Event(), _abort_all_blob_saves=Mock(),
        processes={"agent": SimpleNamespace(poll=lambda: 0)}, _update_shutdown_receipt=receipt)
    namespace["shutdown"](desktop)
    assert receipt.read_bytes() == b"failed\n"
    with pytest.raises(RuntimeError, match="Update refused"):
        require_clean_shutdown(receipt)


@pytest.mark.parametrize("phase,drop_result", [
    ("dispatch", False), ("load", False), ("shutdown", False),
    ("shutdown_cancelled", False), ("shutdown_task_cancelled", False),
    ("load", True), ("ok", False),
])
def test_real_protocol_one_worker_result_reaches_stop_cli(tmp_path, phase, drop_result):
    hook = tmp_path / "hook"
    hook.mkdir()
    hook.joinpath("sitecustomize.py").write_text(r"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import os
import threading
from jiuwenswarm.server.worker import service

phase = os.environ['CONTROLLED_SHUTDOWN_PHASE']
original_start = service.IpcServer.start
async def start(server):
    await original_start(server)
    print('IPC_BOUND', flush=True)
service.IpcServer.start = start
original_close = service._close_worker
service._close_worker = partial(original_close, dispatch_timeout=.02, load_timeout=.02, runtime_timeout=.02)
pool = ThreadPoolExecutor(max_workers=1)
gate = threading.Event()
started = threading.Event()
def blocked():
    started.set()
    gate.wait()
async def load(host, port, signals, dispatcher, loaded):
    if phase != 'ok':
        pool.submit(blocked)
        started.wait()
    if phase == 'dispatch':
        async def drain(**kwargs):
            return False
        dispatcher.drain = drain
    if phase == 'load':
        await asyncio.Event().wait()
    class Runtime:
        async def shutdown(self):
            if phase == 'shutdown':
                await asyncio.Event().wait()
            if phase == 'shutdown_task_cancelled':
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            if phase == 'shutdown_cancelled':
                raise asyncio.CancelledError()
    loaded['runtime'] = Runtime()
service._load_runtime = load
if os.environ['CONTROLLED_DROP_RESULT'] == '1':
    original_hello = service._send_hello
    async def hello(conn, **kwargs):
        original_send = conn.send
        async def send(frame):
            if frame.get('kind') != 'shutdown.done':
                await original_send(frame)
        conn.send = send
        await original_hello(conn, **kwargs)
    service._send_hello = hello
""", encoding="utf-8")
    endpoint = str(tmp_path / "worker")
    environment = dict(os.environ, JIUWENSWARM_HOME=str(tmp_path),
        JIUWENSWARM_DATA_DIR=str(tmp_path / "data"), JIUWENSWARM_RUNTIME_SOCKET=endpoint,
        PYTHONDONTWRITEBYTECODE="1",
        CONTROLLED_SHUTDOWN_PHASE=phase, CONTROLLED_DROP_RESULT="1" if drop_result else "0")
    environment["PYTHONPATH"] = os.pathsep.join([str(hook), str(ROOT)])
    stderr_path = tmp_path / "worker.stderr"
    with stderr_path.open("wb") as stderr:
        worker = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.worker.main",
            "--socket", endpoint, "--port", "0"], cwd=tmp_path, env=environment,
            stdout=subprocess.PIPE, stderr=stderr)
        output = queue.Queue()
        reader = threading.Thread(target=lambda: output.put(worker.stdout.readline()), daemon=True)
        reader.start()
        try:
            # Read only a bounded marker; the listener is actually bound before CLI probing.
            assert output.get(timeout=15).strip() == b"IPC_BOUND"
            control_env = dict(environment, PYTHONPATH=str(ROOT))
            result = subprocess.run([sys.executable, "-B", "-m", "jiuwenswarm.server.supervisor.shutdown",
                "--shutdown-worker", "--timeout", "10"], cwd=tmp_path, env=control_env,
                capture_output=True, timeout=15)
            tmp_path.joinpath("stop.stdout").write_bytes(result.stdout)
            tmp_path.joinpath("stop.stderr").write_bytes(result.stderr)
            if phase != "ok":
                assert result.returncode != 0, "Fatal exit was reported as a successful stop"
                assert b"incomplete shutdown" in result.stderr or b"without a shutdown acknowledgement" in result.stderr
                if phase in {"shutdown_cancelled", "shutdown_task_cancelled"}:
                    assert b"runtime shutdown cancelled" in result.stderr
            else:
                assert result.returncode == 0, result.stderr.decode(errors="replace")
            worker.wait(timeout=5)
            if phase == "ok":
                assert json.loads(result.stdout)["ok"] is True
                assert worker.returncode == 0
            else:
                assert worker.returncode == 1
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=3)
            reader.join(timeout=1)
            if worker.stdout is not None:
                worker.stdout.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"[]", b"null", b"not-json"])
async def test_malformed_shutdown_frame_still_reaps_owned_process(body):
    import time
    from jiuwenswarm.server.ipc.protocol import HEADER
    from jiuwenswarm.server.ipc.stream import StreamConnection

    reader = asyncio.StreamReader()
    reader.feed_data(HEADER.pack(len(body)) + body)
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    peer = StreamConnection(reader, writer)
    process = SimpleNamespace(wait=AsyncMock(return_value=1))
    owner = runtime_supervisor.RuntimeSupervisor(Readiness())
    owner._conn = owner._validated_conn = peer
    owner._expects_shutdown_result = True
    owner._process = process
    with pytest.raises(RuntimeError, match="shutdown result could not be verified"):
        await owner._shutdown_owned_worker(deadline=time.monotonic() + 1)
    process.wait.assert_awaited_once()
    assert owner._process is None and owner._validated_conn is None
