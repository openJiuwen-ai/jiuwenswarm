# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exec containment, disconnect propagation and request isolation."""

import asyncio
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenbox.supervisor import sandbox_daemon, win_exec, win_job


@pytest.mark.parametrize("waits,failed,retries", [
    ([0], False, 0),
    ([0x102, 0], False, 1),
    ([0x102, 0x102], True, 1),
    ([0xFFFFFFFF], True, 0),
    ([0x102, 0xFFFFFFFF], True, 1),
])
def test_confirm_exit_checks_wait_result(monkeypatch, waits, failed, retries):
    kernel = MagicMock()
    kernel.WaitForSingleObject.side_effect = waits
    monkeypatch.setattr(win_exec, "_push_log", MagicMock())
    if failed:
        with pytest.raises(RuntimeError, match="cannot confirm exec process exit"):
            win_exec._confirm_process_exit(kernel, 30)
    else:
        win_exec._confirm_process_exit(kernel, 30)
    assert kernel.TerminateProcess.call_count == retries
    assert kernel.WaitForSingleObject.call_count == len(waits)


@pytest.mark.parametrize("waits,exit_query_ok", [
    ([0, 0x102, 0x102, 0], True),
    ([0, 0xFFFFFFFF, 0], True),
    ([0xFFFFFFFF, 0], True),
    ([0, 0, 0], False),
])
def test_exec_wait_failure_sends_error_not_completion(monkeypatch, waits, exit_query_ok):
    kernel = MagicMock()
    kernel.WaitForSingleObject.side_effect = waits
    kernel.GetExitCodeProcess.return_value = exit_query_ok
    monkeypatch.setattr(win_exec, "get_kernel32", lambda: kernel)
    monkeypatch.setattr(win_exec, "_clear_inherit", lambda *_: None)
    monkeypatch.setattr(win_exec, "_exec_peer_disconnected", lambda *_: False)
    monkeypatch.setattr(win_exec, "_bash_unavailable", lambda: False)
    monkeypatch.setattr(win_exec, "_get_runner_primary_token", lambda: 10)
    monkeypatch.setattr(win_exec, "_push_log", MagicMock())
    monkeypatch.setattr(win_exec, "_create_process_as_user", lambda *a, **k: (30, 31, 32))
    for name in ("assign_process", "resume_process", "close_job"):
        monkeypatch.setattr(win_job, name, MagicMock())
    monkeypatch.setattr(win_job, "create_exec_job", lambda: 20)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda *a: 200))
    monkeypatch.setattr(win_exec._threading, "Thread", MagicMock())
    close_fd = MagicMock()
    monkeypatch.setattr(win_exec.os, "close", close_fd)
    counter = iter(range(100, 104))

    def pipe(read, write, *_):
        read._obj.value, write._obj.value = next(counter), next(counter)
        return True

    kernel.CreatePipe.side_effect = pipe
    response, error = MagicMock(), MagicMock()
    monkeypatch.setattr(win_exec, "_send_response", response)
    monkeypatch.setattr(win_exec, "_send_error_response", error)
    win_exec._handle_exec_request(None, {"command": ["cmd", "/c", "echo hello"]}, None, None, b"")
    error.assert_called_once()
    response.assert_not_called()
    close_fd.assert_called_once_with(200)


@pytest.mark.parametrize("failure", ["job", "create", "assign", "resume"])
def test_exec_start_failure_never_runs_uncontained(monkeypatch, failure):
    kernel = MagicMock()
    monkeypatch.setattr(win_exec, "get_kernel32", lambda: kernel)
    monkeypatch.setattr(win_exec, "_clear_inherit", lambda *_: None)
    monkeypatch.setattr(win_exec, "_exec_peer_disconnected", lambda *_: False)
    monkeypatch.setattr(win_exec, "_bash_unavailable", lambda: False)
    monkeypatch.setattr(win_exec, "_get_runner_primary_token", lambda: 10)
    monkeypatch.setattr(win_exec, "_push_log", lambda *a, **k: None)
    error = MagicMock()
    monkeypatch.setattr(win_exec, "_send_error_response", error)
    counter = iter(range(100, 120))

    def pipe(read, write, *_):
        read._obj.value, write._obj.value = next(counter), next(counter)
        return True

    kernel.CreatePipe.side_effect = pipe
    events = []

    def action(name, value=None):
        def call(*a, **k):
            events.append(name)
            if failure == name:
                raise OSError(name)
            return value
        return call

    monkeypatch.setattr(win_job, "create_exec_job", action("job", 20))
    monkeypatch.setattr(win_exec, "_create_process_as_user", action("create", (30, 31, 32)))
    monkeypatch.setattr(win_job, "assign_process", action("assign"))
    monkeypatch.setattr(win_job, "resume_process", action("resume"))
    close_job = MagicMock()
    monkeypatch.setattr(win_job, "close_job", close_job)
    win_exec._handle_exec_request(None, {"command": ["cmd", "/c", "echo hello"]}, None, None, b"")
    assert events == ["job", "create", "assign", "resume"][:["job", "create", "assign", "resume"].index(failure) + 1]
    error.assert_called_once()
    assert kernel.TerminateProcess.call_count == int(failure in {"assign", "resume"})
    assert close_job.call_count == int(failure != "job")
    closed = [call.args[0].value for call in kernel.CloseHandle.call_args_list]
    # HANDLE objects are cleared only on the success path in this test.
    assert set(range(100, 104)).issubset(closed)


@pytest.mark.parametrize("detector", [
    pytest.param(win_exec._exec_peer_disconnected, id="windows"),
    pytest.param(sandbox_daemon._exec_disconnected, id="linux"),
])
def test_peer_disconnect_is_request_local(detector):
    left, right = socket.socketpair()
    other_left, other_right = socket.socketpair()
    try:
        assert not detector(left)
        right.close()
        assert detector(left)
        assert not detector(other_left)
    finally:
        for sock in (left, right, other_left, other_right):
            sock.close()


@pytest.mark.parametrize("backend", ["windows", "linux"])
@pytest.mark.asyncio
async def test_cancel_runtime_closes_only_current_exec(monkeypatch, backend):
    from jiuwenbox.server.runtime import process
    from jiuwenbox.supervisor.daemon_ipc import REQUEST_TYPE_EXEC

    pairs = [socket.socketpair(), socket.socketpair()]
    clients = iter(client for client, _ in pairs)
    started = [asyncio.Event(), asyncio.Event()]
    finished = [asyncio.Event(), asyncio.Event()]
    loop = asyncio.get_running_loop()
    # Replace only the runtime's socket factory, not asyncio's global socket module.
    monkeypatch.setattr(process, "socket", SimpleNamespace(
        socket=lambda *a: next(clients), AF_INET=socket.AF_INET,
        AF_UNIX=getattr(socket, "AF_UNIX", 1), SOCK_STREAM=socket.SOCK_STREAM,
        SHUT_RDWR=socket.SHUT_RDWR, timeout=socket.timeout,
    ))
    runtime = process.ProcessRuntime.__new__(process.ProcessRuntime)

    def receive(exec_socket):
        index = next(i for i, pair in enumerate(pairs) if pair[0] is exec_socket)
        loop.call_soon_threadsafe(started[index].set)
        try:
            return exec_socket.recv(1).decode()
        except OSError:
            return ""
        finally:
            loop.call_soon_threadsafe(finished[index].set)

    if backend == "windows":
        monkeypatch.setattr(runtime, "_win_roundtrip_blocking",
                            lambda *a, exec_socket, **k: (receive(exec_socket), b""))

        def execute():
            return runtime._win_runner_roundtrip(
                "sb", {"control_port": 1}, REQUEST_TYPE_EXEC, {}, None,
            )
    else:
        monkeypatch.setattr(runtime, "_control_socket_host_path", lambda _: "control.sock")
        monkeypatch.setattr(runtime, "_daemon_ipc_available", lambda _: True)
        monkeypatch.setattr(runtime, "_exec_via_daemon_blocking",
                            lambda call: receive(call.exec_socket))

        def execute():
            return runtime._exec_via_daemon("sb", SimpleNamespace(
                command=["command"], env=None, workdir=None, stdin_data=None, timeout=None,
            ))

    tasks = []
    try:
        for event in started:
            tasks.append(asyncio.create_task(execute()))
            await asyncio.wait_for(event.wait(), 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        await asyncio.wait_for(finished[0].wait(), 2)
        assert pairs[0][0].fileno() == -1
        pairs[0][1].settimeout(2)
        assert pairs[0][1].recv(1) == b""
        assert not tasks[1].done()
        assert pairs[1][0].fileno() != -1
        pairs[1][1].sendall(b"x")
        assert await asyncio.wait_for(tasks[1], 2) == "x"
        assert pairs[1][0].fileno() == -1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for client, peer in pairs:
            peer.close()
            client.close()


@pytest.mark.asyncio
async def test_http_disconnect_cancels_only_current_exec(monkeypatch):
    from jiuwenbox.server.routes import sandbox

    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def execution(*, request, **kwargs):
        try:
            await release.wait()
            return {"stdout": request.command[0], "exit_code": 0}
        finally:
            if request.command[0] == "cancel":
                cancelled.set()

    monkeypatch.setattr(sandbox, "_mgr", lambda: SimpleNamespace(exec_in_sandbox=execution))
    other = asyncio.create_task(sandbox.exec_in_sandbox(
        "sb", sandbox.ExecRequest(command=["keep"]),
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
    ))
    try:
        response = await asyncio.wait_for(sandbox.exec_in_sandbox(
            "sb", sandbox.ExecRequest(command=["cancel"]),
            SimpleNamespace(is_disconnected=AsyncMock(return_value=True)),
        ), 2)
        assert response.status_code == 499
        assert cancelled.is_set()
        assert not other.done()
        release.set()
        assert await asyncio.wait_for(other, 2) == {"stdout": "keep", "exit_code": 0}
    finally:
        other.cancel()
        await asyncio.gather(other, return_exceptions=True)


@pytest.mark.parametrize("ending", ["normal", "timeout", "disconnect", "leader_exited"])
def test_daemon_only_reaps_exec_group_on_abort(monkeypatch, ending):
    daemon = sandbox_daemon
    proc = MagicMock(pid=123, returncode=0)
    timeout = daemon.subprocess.TimeoutExpired("command", 0.2)
    proc.communicate.side_effect = (
        [timeout, (b"output", b"")] if ending in {"timeout", "leader_exited"}
        else [(b"output", b"")]
    )
    proc.poll.return_value = 0 if ending == "leader_exited" else None
    spawn = MagicMock(return_value=proc)
    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    monkeypatch.setattr(daemon, "_exec_disconnected", MagicMock(
        side_effect=[False, True] if ending == "disconnect" else None,
        return_value=False,
    ))
    killpg = MagicMock()
    monkeypatch.setattr(daemon.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(daemon.signal, "SIGKILL", 9, raising=False)
    response = MagicMock()
    monkeypatch.setattr(daemon, "_send_response", response)
    daemon._handle_exec(None, {
        "command": ["command"], "timeout": 0 if ending == "timeout" else 30,
    }, None)
    assert spawn.call_args.kwargs["start_new_session"] is True
    if ending in {"timeout", "disconnect"}:
        killpg.assert_called_with(123, 9)
    else:
        killpg.assert_not_called()
    proc.wait.assert_called_once()
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        pipe.close.assert_called_once()
    if ending == "timeout":
        assert response.call_args.args[1]["exit_code"] == 124
    elif ending in {"normal", "leader_exited"}:
        assert response.call_args.args[1]["stdout"] == "output"
