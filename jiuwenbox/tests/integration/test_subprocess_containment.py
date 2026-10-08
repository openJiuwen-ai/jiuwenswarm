# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native exec lifecycle checks; these do not require a deployed sandbox server."""

import json
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

from jiuwenbox.supervisor import sandbox_daemon, win_exec
from jiuwenbox.supervisor.daemon_ipc import recv_frame


@pytest.fixture
def daemon_roundtrip(monkeypatch):
    from jiuwenbox.server.runtime.process import ProcessRuntime, _DaemonExecCall

    if sys.platform == "win32":
        # Windows can exercise the protocol and real Popen, but has no killpg.
        # Native Linux process-group cleanup is covered by exec_backend below.
        monkeypatch.setattr(sandbox_daemon, "_kill_exec_group", lambda proc: proc.kill())

    def execute(command, stdin=None):
        client, server = socket.socketpair()
        runtime = ProcessRuntime.__new__(ProcessRuntime)
        # Only replace connection establishment. Exercise the real client
        # framing, stdin transfer, daemon dispatch, Popen and response reader.
        monkeypatch.setattr(runtime, "_connect_daemon_socket", lambda _: client)
        worker = threading.Thread(
            target=sandbox_daemon._handle_connection,
            args=(server, sandbox_daemon.DaemonState()), daemon=True,
        )
        worker.start()
        try:
            return runtime._exec_via_daemon_blocking(_DaemonExecCall(
                socket_path=Path("unused.sock"), command=command, env=None,
                workdir=None, stdin_bytes=stdin, timeout=5,
            ))
        finally:
            client.close()
            worker.join(7)
            assert not worker.is_alive(), "daemon failed to finish the request"

    return execute


@pytest.mark.parametrize("stdin", [None, b"hello", b"x" * (256 * 1024)],
                         ids=["no-stdin", "small-stdin", "large-stdin"])
def test_daemon_client_roundtrip_keeps_connection_until_response(daemon_roundtrip, stdin):
    result = daemon_roundtrip([
        sys.executable, "-c",
        "import sys,time; data=sys.stdin.buffer.read(); time.sleep(0.4); "
        "sys.stdout.buffer.write(b'result:' + data); print('diagnostic',file=sys.stderr)",
    ], stdin)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "result:" + (stdin or b"").decode()
    assert result.stderr.strip() == "diagnostic"


@pytest.mark.parametrize("redirect_output", [False, True], ids=["inherited-pipe", "background"])
def test_daemon_normal_exit_preserves_descendants(tmp_path, daemon_roundtrip, redirect_output):
    script = tmp_path / "background.py"
    marker = tmp_path / "finished"
    ready = tmp_path / "ready"
    script.write_text(
        "import pathlib,subprocess,sys,time\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        f"ready=pathlib.Path({str(ready)!r})\n"
        "if len(sys.argv)>1:\n"
        "    ready.write_text('ready')\n"
        "    time.sleep(0.6)\n"
        "    marker.write_text('finished')\n"
        "    print('delayed output', flush=True)\n"
        "else:\n"
        f"    output=subprocess.DEVNULL if {redirect_output!r} else None\n"
        "    subprocess.Popen([sys.executable,__file__,'child'], stdin=subprocess.DEVNULL, "
        "stdout=output, stderr=output)\n"
        "    deadline=time.monotonic()+3\n"
        "    while not ready.exists():\n"
        "        if time.monotonic()>deadline: raise RuntimeError('child did not start')\n"
        "        time.sleep(0.01)\n",
        encoding="utf-8",
    )
    result = daemon_roundtrip([sys.executable, str(script)])
    assert result.exit_code == 0, result.stderr
    if redirect_output:
        assert not marker.exists(), "exec unexpectedly waited for the detached output child"
    else:
        assert result.stdout.strip() == "delayed output"
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.read_text() == "finished"


@pytest.fixture(params=[
    pytest.param("windows", marks=pytest.mark.skipif(
        sys.platform != "win32", reason="requires Windows Job Objects",
    )),
    pytest.param("linux", marks=pytest.mark.skipif(
        sys.platform != "linux", reason="requires Linux process groups",
    )),
])
def exec_backend(request):
    return request.param


def _assert_process_stopped(pid, backend):
    if backend == "windows":
        from ctypes import wintypes

        kernel = win_exec.get_kernel32()
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:
            try:
                assert kernel.WaitForSingleObject(wintypes.HANDLE(handle), 2000) == 0, pid
            finally:
                kernel.CloseHandle(wintypes.HANDLE(handle))
    else:
        from pathlib import Path

        # An orphaned zombie may await the host init's reap; it cannot execute.
        status = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = status.read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                return
            if state == "Z":
                return
            time.sleep(0.02)
        pytest.fail(f"descendant {pid} is still running")


@pytest.mark.parametrize("ending", ["timeout", "disconnect", "blocked_stdin"])
def test_exec_reaps_children_and_grandchildren(tmp_path, monkeypatch, exec_backend, ending):
    script = tmp_path / "tree.py"
    script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "root = pathlib.Path(__file__).parent\n"
        "depth = int(sys.argv[1])\n"
        "(root / f'{depth}.pid').write_text(str(os.getpid()))\n"
        "if depth:\n"
        "    p = subprocess.Popen([sys.executable, __file__, str(depth-1), sys.argv[2]])\n"
        "    if depth == 2 and sys.argv[2] == 'normal':\n"
        "        deadline = time.monotonic() + 4\n"
        "        while not (root / '0.pid').exists():\n"
        "            if time.monotonic() >= deadline: raise RuntimeError('grandchild missing')\n"
        "            time.sleep(0.02)\n"
        "        print('leader done', flush=True)\n"
        "    else: p.wait(timeout=20)\n"
        "else:\n"
        "    time.sleep(20)\n"
        "    (root / 'late.txt').write_text('escaped')\n",
        encoding="utf-8",
    )
    stdin = b"x" * (1024 * 1024) if ending == "blocked_stdin" else b""
    monkeypatch.setattr(win_exec, "_push_log", lambda *a, **k: None)
    # The daemon receives this body before launching the command. Exercise the
    # blocked child stdin, without making IPC body framing part of this test.
    monkeypatch.setattr(sandbox_daemon, "_recv_exact", lambda *a: stdin)
    client, server = socket.socketpair()
    client.settimeout(12)
    errors = []

    def run():
        try:
            header = {"command": [sys.executable, str(script), "2", ending], "timeout": 5}
            if exec_backend == "windows":
                win_exec._handle_exec_request(server, header, None, str(tmp_path), stdin)
            else:
                header.update(workdir=str(tmp_path), stdin_size=len(stdin))
                sandbox_daemon._handle_exec(server, header, None)
        except Exception as exc:
            errors.append(exc)
        finally:
            server.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        if ending in {"disconnect", "blocked_stdin"}:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and worker.is_alive():
                if all((tmp_path / f"{depth}.pid").exists() for depth in range(3)):
                    break
                time.sleep(0.02)
            assert all((tmp_path / f"{depth}.pid").exists() for depth in range(3))
            client.close()
        else:
            response = json.loads(recv_frame(client, 1024 * 1024))
            if ending == "normal":
                assert response["exit_code"] == 0, response
                assert "leader done" in response["stdout"]
            elif exec_backend == "windows":
                assert response["killed"], response
            else:
                assert response["exit_code"] == 124, response
        worker.join(4)
        assert not worker.is_alive(), "exec did not finish after cancellation/timeout"
        assert not errors
        pids = [int((tmp_path / f"{depth}.pid").read_text()) for depth in range(3)]
        assert len(set(pids)) == 3
        for pid in pids:
            _assert_process_stopped(pid, exec_backend)
        assert not (tmp_path / "late.txt").exists()
    finally:
        client.close()
        worker.join(8)


@pytest.mark.skipif(sys.platform != "win32", reason="normal-exit Job cleanup is Windows behavior")
def test_normal_exit_closes_exec_job(tmp_path, monkeypatch):
    test_exec_reaps_children_and_grandchildren(tmp_path, monkeypatch, "windows", "normal")
