"""Host-owned Supervisor logs survive privilege and namespace changes."""
from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.supervisor import daemon_ipc
from jiuwenbox.server.runtime.output import RuntimeOutput


@pytest.fixture(autouse=True)
def runtime_log_root(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENBOX_RUNTIME_LOG_DIR", str(tmp_path))


def test_output_pair_is_separate_append_only_and_ignores_old_ready(tmp_path):
    stdout = tmp_path / "agent.out"
    stderr = tmp_path / "agent.err"
    stdout.write_bytes(b"previous output\n")
    stderr.write_bytes(b"JIUWENBOX_DAEMON_READY\n")
    with RuntimeOutput(str(stdout), str(stderr)) as output:
        for stream in (output.stdout, output.stderr):
            assert fcntl.fcntl(stream.fileno(), fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY
        output.startup_log.seek(0)
        assert output.startup_log.read() == b""
        output.stdout.write(b"new output\n")
        output.stderr.write(b"new error\n")
        output.startup_log.seek(0)
        assert output.startup_log.read() == b"new error\n"
    assert stdout.read_bytes() == b"previous output\nnew output\n"
    assert stderr.read_bytes() == b"JIUWENBOX_DAEMON_READY\nnew error\n"


def test_parent_close_does_not_stop_background_output(tmp_path):
    path = tmp_path / "agent.out"
    output = RuntimeOutput(str(path), None)
    child = subprocess.Popen(
        ["/bin/cat"], stdin=subprocess.PIPE, stdout=output.stdout, stderr=output.stderr,
        **({"user": 65534, "group": 65534, "extra_groups": []} if os.geteuid() == 0 else {}),
    )
    output.close()
    child.communicate(b"after close", timeout=5)
    assert child.returncode == 0
    assert path.read_bytes() == b"after close"


def test_default_streams_share_anonymous_startup_capture():
    with RuntimeOutput(None, None) as output:
        os.write(output.stdout.fileno(), b"out")
        os.write(output.stderr.fileno(), b"err")
        output.startup_log.seek(0)
        assert output.startup_log.read() == b"outerr"


def test_log_permissions_and_path_boundaries(tmp_path):
    log = tmp_path / "agent.out"
    with RuntimeOutput(str(log), None):
        assert log.stat().st_mode & 0o777 == 0o600
    symlink, fifo, hardlink = (tmp_path / name for name in ("link", "fifo", "alias"))
    symlink.symlink_to(log)
    os.mkfifo(fifo)
    original = tmp_path / "original"
    original.write_bytes(b"untouched")
    hardlink.hardlink_to(original)
    for target in (symlink, fifo, hardlink, tmp_path, tmp_path.parent / "outside.out"):
        with pytest.raises(OSError), RuntimeOutput(str(target), None):
            pass
    assert original.read_bytes() == b"untouched"


def test_second_open_failure_releases_stdout(tmp_path):
    stdout = tmp_path / "agent.out"
    with pytest.raises(OSError):
        RuntimeOutput(str(stdout), str(tmp_path / "missing" / "agent.err"))
    assert stdout.exists()
    assert str(stdout) not in [os.path.realpath(fd) for fd in Path("/proc/self/fd").iterdir()]


@pytest.mark.skipif(os.geteuid() != 0, reason="requires privilege drop to check host access denial")
def test_unprivileged_server_cannot_open_private_host_directory(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    target = directory / "agent.out"
    child = subprocess.run(
        [sys.executable, "-c", "import sys; from jiuwenbox.server.runtime.output import RuntimeOutput;"
         "RuntimeOutput(sys.argv[1], None)", str(target)],
        user=65534, group=65534, extra_groups=[], capture_output=True, timeout=10,
        env={**os.environ, "JIUWENBOX_RUNTIME_LOG_DIR": str(directory)},
    )
    assert child.returncode != 0 and b"PermissionError" in child.stderr
    assert not target.exists()


@pytest.mark.asyncio
async def test_redirected_create_bypasses_parked_warm_slot(tmp_path, monkeypatch):
    from jiuwenbox.server.runtime.process import ProcessRuntime
    runtime = ProcessRuntime()
    monkeypatch.setattr(runtime, "_load_policy", lambda _: SecurityPolicy())

    warm = AsyncMock()
    monkeypatch.setattr(runtime, "_try_warm_adopt", warm)
    monkeypatch.setattr(runtime, "_ensure_named_netns", Mock(side_effect=RuntimeError("cold path reached")))
    with pytest.raises(RuntimeError, match="cold path reached"):
        await runtime.create("logs-cold", tmp_path / "policy", stdout=str(tmp_path / "agent.out"))
    warm.assert_not_awaited()


@pytest.mark.parametrize("inherit_stdout,inherit_stderr", [(True, True), (True, False), (False, True), (False, False)])
def test_background_spawn_inherits_selected_streams(tmp_path, monkeypatch, inherit_stdout, inherit_stderr):
    monkeypatch.chdir(tmp_path)

    def request(operation, params=None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect("control.sock")
            daemon_ipc.send_frame(client, daemon_ipc.encode_request(request_type=operation, payload=params))
            return json.loads(daemon_ipc.recv_frame(client, daemon_ipc.MAX_HEADER_BYTES))

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("control.sock")
        listener.listen()
        env = {**os.environ, daemon_ipc.LISTENER_FD_ENV: str(listener.fileno())}
        with subprocess.Popen(
            [sys.executable, "-m", "jiuwenbox.supervisor.sandbox_daemon"], env=env,
            pass_fds=(listener.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as daemon:
            try:
                reply = request("exec_background", {
                    "job_id": "output", "command": ["/bin/sh", "-c", "echo child-out; echo child-err >&2"],
                    "inherit_stdout": inherit_stdout, "inherit_stderr": inherit_stderr,
                })
                assert reply.get("ok"), reply
                assert reply.get("stdout_redirected") is inherit_stdout
                assert reply.get("stderr_redirected") is inherit_stderr
                assert request("shutdown").get("ok")
                stdout, stderr = daemon.communicate(timeout=5)
            finally:
                if daemon.poll() is None:
                    daemon.kill()
    assert daemon.returncode == 0, stderr
    assert (b"child-out" in stdout.splitlines()) == inherit_stdout
    assert (b"child-err" in stderr.splitlines()) == inherit_stderr
