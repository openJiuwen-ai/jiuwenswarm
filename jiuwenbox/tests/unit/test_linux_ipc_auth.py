# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Portable checks for daemon authentication; no sandbox server is contacted."""

import io
import json
from pathlib import Path
import socket
import struct
import threading
from unittest.mock import Mock

import pytest

from jiuwenbox.server.runtime.process import ProcessRuntime, _DaemonExecCall
from jiuwenbox.server.runtime import process as process_module
from jiuwenbox.supervisor import sandbox_daemon as daemon
from jiuwenbox.supervisor.daemon_ipc import CONTROL_TOKEN_BYTES, recv_frame, send_frame


TOKEN = b"a" * CONTROL_TOKEN_BYTES
REQUESTS = ["exec", "shutdown", "write_file", "read_file", "list_dir",
            "exec_background", "bg_status", "bg_kill", "security_status"]


def roundtrip(header, token, monkeypatch):
    client, server = socket.socketpair()
    client.settimeout(3)
    state = daemon.DaemonState(TOKEN)
    reached = []

    def handler(conn, request, *args):
        reached.append(request["type"])
        daemon._send_response(conn, {"ok": True})

    for name in REQUESTS:
        if name != "shutdown":
            monkeypatch.setattr(daemon, "_handle_" + name, handler)
    worker = threading.Thread(target=daemon._handle_connection, args=(server, state), daemon=True)
    worker.start()
    try:
        # None simulates an old unauthenticated client sending its JSON header first.
        if token is not None:
            send_frame(client, token)
        try:
            send_frame(client, json.dumps(header).encode())
        except OSError:
            pass  # Rejection can close the socket before the untrusted header is sent.
        try:
            response = json.loads(recv_frame(client, 4096))
        except ConnectionError:
            # An untrusted peer can race the close by sending a request body.
            # TCP-backed socketpair on Windows then reports reset, not JSON.
            response = None
    finally:
        client.close()
        worker.join(5)
    assert not worker.is_alive()
    assert state.in_flight == 0
    return response, reached, state.shutdown_event.is_set()


@pytest.mark.parametrize("kind", REQUESTS)
@pytest.mark.parametrize("token", [None, b"", b"b" * CONTROL_TOKEN_BYTES, TOKEN + b"x"])
def test_untrusted_connection_cannot_dispatch_any_request(monkeypatch, kind, token):
    response, reached, shutdown = roundtrip({"type": kind}, token, monkeypatch)
    if response is not None:
        assert response["ok"] is False
        assert response["error"] == "unauthorized"
    assert reached == []
    assert not shutdown


@pytest.mark.parametrize("kind", REQUESTS)
def test_authorized_connection_can_dispatch(monkeypatch, kind):
    response, reached, shutdown = roundtrip({"type": kind}, TOKEN, monkeypatch)
    assert response["ok"] is True
    assert reached == ([] if kind == "shutdown" else [kind])
    assert shutdown == (kind == "shutdown")


@pytest.mark.parametrize("data", [b"", b"\x00", struct.pack(">I", 0),
                                     struct.pack(">I", 1024), struct.pack(">I", 32) + b"short"])
def test_missing_or_invalid_bootstrap_is_rejected_and_closed(data):
    stream = io.BytesIO(data)
    with pytest.raises(RuntimeError):
        daemon._read_control_token(stream)
    assert stream.closed


def test_bootstrap_reads_partial_pipe_data_and_closes_it():
    class PartialPipe(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 3))

    stream = PartialPipe(struct.pack(">I", len(TOKEN)) + TOKEN)
    assert daemon._read_control_token(stream) == TOKEN
    assert stream.closed


def test_daemon_never_adopts_listener_without_bootstrap(monkeypatch):
    monkeypatch.setattr(daemon, "_read_control_token", Mock(side_effect=RuntimeError("missing")))
    listener = Mock(side_effect=AssertionError("Must not accept connections"))
    monkeypatch.setattr(daemon, "_adopted_listener", listener)
    assert daemon.main() == 1
    listener.assert_not_called()


@pytest.mark.parametrize("token", [b"", b"short", None])
def test_daemon_state_never_allows_an_empty_credential(token):
    with pytest.raises(ValueError):
        daemon.DaemonState(token)


def test_host_bootstrap_writes_only_to_private_pipe():
    class RecordingPipe(io.BytesIO):
        def close(self):
            if not self.closed:
                self.sent = self.getvalue()
            super().close()

    pipe = RecordingPipe()
    proc = Mock(stdin=pipe)
    ProcessRuntime._bootstrap_daemon_token(proc, TOKEN)
    assert pipe.closed
    assert pipe.sent == struct.pack(">I", len(TOKEN)) + TOKEN
    proc.kill.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "write", "short_write", "flush", "close"])
def test_bootstrap_failure_kills_and_reaps_daemon(failure):
    pipe = Mock()
    pipe.__enter__ = Mock(return_value=pipe)
    pipe.__exit__ = Mock(return_value=False)
    pipe.write.return_value = len(TOKEN) + 4
    if failure == "write":
        pipe.write.side_effect = BrokenPipeError("closed")
    elif failure == "short_write":
        pipe.write.return_value = 1
    elif failure == "flush":
        pipe.flush.side_effect = OSError("flush failed")
    elif failure == "close":
        pipe.__exit__.side_effect = OSError("close failed")
    proc = Mock(stdin=None if failure == "missing" else pipe)
    with pytest.raises(RuntimeError, match="Failed to deliver"):
        ProcessRuntime._bootstrap_daemon_token(proc, TOKEN)
    proc.kill.assert_called_once()
    proc.wait.assert_called_once_with(timeout=5)


def test_host_refuses_unknown_socket_credential():
    runtime = ProcessRuntime.__new__(ProcessRuntime)
    runtime._daemon_control_tokens = {}
    sock = Mock()
    with pytest.raises(PermissionError):
        runtime._authenticate_daemon_socket(sock, Path("unknown.sock"))
    sock.sendall.assert_not_called()


@pytest.mark.parametrize("credential_exists", [True, False])
def test_common_connector_authenticates_target_socket_or_closes(monkeypatch, credential_exists):
    runtime = ProcessRuntime.__new__(ProcessRuntime)
    path = Path("second-control.sock")
    runtime._daemon_control_tokens = {Path("first-control.sock"): b"b" * CONTROL_TOKEN_BYTES}
    if credential_exists:
        runtime._daemon_control_tokens[path] = TOKEN
    sock = Mock()
    # This connector is mocked; native AF_UNIX support is covered on Linux.
    monkeypatch.setattr(process_module.socket, "AF_UNIX", getattr(socket, "AF_UNIX", 1), raising=False)
    monkeypatch.setattr(process_module.socket, "socket", Mock(return_value=sock))
    if credential_exists:
        assert runtime._connect_daemon_socket(path) is sock
        sent = b"".join(call.args[0] for call in sock.sendall.call_args_list)
        assert sent == struct.pack(">I", len(TOKEN)) + TOKEN
        sock.close.assert_not_called()
    else:
        with pytest.raises(PermissionError):
            runtime._connect_daemon_socket(path)
        sock.sendall.assert_not_called()
        sock.close.assert_called_once()


def test_preallocated_exec_socket_is_authenticated_before_command(monkeypatch):
    runtime = ProcessRuntime.__new__(ProcessRuntime)
    path = Path("control.sock")
    runtime._daemon_control_tokens = {path: TOKEN}
    sock = Mock()
    monkeypatch.setattr(runtime, "_read_response_blob", Mock(return_value={"exit_code": 0}))

    def send_request(*args):
        sent = b"".join(call.args[0] for call in sock.sendall.call_args_list)
        assert sent == struct.pack(">I", len(TOKEN)) + TOKEN

    monkeypatch.setattr(runtime, "_send_request_blob", send_request)
    result = runtime._exec_via_daemon_blocking(_DaemonExecCall(
        socket_path=path, command=["true"], env=None, workdir=None,
        stdin_bytes=None, timeout=1, exec_socket=sock,
    ))
    assert result.exit_code == 0
    sock.close.assert_called_once()
