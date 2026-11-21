"""Host-owned Supervisor logs survive privilege and namespace changes."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server.runtime.output import RuntimeOutput


def test_output_pair_is_separate_append_only_and_ignores_old_ready(tmp_path):
    stdout = tmp_path / "agent.out"
    stderr = tmp_path / "agent.err"
    stdout.write_bytes(b"previous output\n")
    stderr.write_bytes(b"JIUWENBOX_DAEMON_READY\n")
    with RuntimeOutput(str(stdout), str(stderr)) as output:
        output.startup_log.seek(0)
        assert output.startup_log.read() == b""
        child = subprocess.Popen(
            [sys.executable, "-c", "import os;os.write(1,b'new output\\n');os.write(2,b'new error\\n')"],
            stdout=output.stdout,
            stderr=output.stderr,
        )
        assert child.wait(timeout=5) == 0
        output.startup_log.seek(0)
        assert output.startup_log.read() == b"new error\n"
    assert stdout.read_bytes() == b"previous output\nnew output\n"
    assert stderr.read_bytes() == b"JIUWENBOX_DAEMON_READY\nnew error\n"


def test_parent_close_does_not_stop_background_output(tmp_path):
    path = tmp_path / "agent.out"
    output = RuntimeOutput(str(path), None)
    child = subprocess.Popen(
        [sys.executable, "-c", """
import os,time
if os.geteuid() == 0:
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
time.sleep(.1)
os.write(1,b'after close')
"""],
        stdout=output.stdout,
        stderr=output.stderr,
    )
    output.close()
    assert child.wait(timeout=5) == 0
    assert path.read_bytes() == b"after close"


def test_default_streams_share_anonymous_startup_capture():
    with RuntimeOutput(None, None) as output:
        child = subprocess.Popen(
            [sys.executable, "-c", "import os;os.write(1,b'out');os.write(2,b'err')"],
            stdout=output.stdout,
            stderr=output.stderr,
        )
        assert child.wait(timeout=5) == 0
        output.startup_log.seek(0)
        assert output.startup_log.read() == b"outerr"


def test_log_file_is_private_and_rejects_symlink_fifo_and_directory(tmp_path):
    path = tmp_path / "agent.out"
    with RuntimeOutput(str(path), None):
        assert path.stat().st_mode & 0o777 == 0o600
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        RuntimeOutput(str(link), None)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError):
        RuntimeOutput(str(fifo), None)
    with pytest.raises(OSError):
        RuntimeOutput(str(tmp_path), None)
    assert path.read_bytes() == b""


def test_second_open_failure_closes_first_descriptor(tmp_path, monkeypatch):
    opened = []
    real_open = os.open

    def record_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", record_open)
    with pytest.raises(OSError):
        RuntimeOutput(str(tmp_path / "agent.out"), str(tmp_path / "missing" / "agent.err"))
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires privilege drop to check host access denial")
def test_unprivileged_server_cannot_open_private_host_directory(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    target = directory / "agent.out"
    script = """
import os,sys
from jiuwenbox.server.runtime.output import RuntimeOutput
os.setgroups([])
os.setgid(65534)
os.setuid(65534)
try:
    RuntimeOutput(sys.argv[1], None)
except PermissionError:
    sys.exit(0)
raise AssertionError('host log permission check was bypassed')
"""
    child = subprocess.run([sys.executable, "-c", script, str(target)], capture_output=True, timeout=10)
    assert child.returncode == 0, child.stderr.decode()
    assert not target.exists()


@pytest.mark.asyncio
async def test_redirected_create_bypasses_parked_warm_slot(tmp_path, monkeypatch):
    from jiuwenbox.server.runtime.process import ProcessRuntime
    runtime = ProcessRuntime()
    monkeypatch.setattr(runtime, "_load_policy", lambda _: SecurityPolicy())
    async def unexpected_adopt(*_args):
        raise AssertionError("redirected creation cannot adopt parked stdio")
    def stop_before_setup(*_args):
        raise RuntimeError("cold path reached")
    monkeypatch.setattr(runtime, "_try_warm_adopt", unexpected_adopt)
    monkeypatch.setattr(runtime, "_ensure_named_netns", stop_before_setup)
    with pytest.raises(RuntimeError, match="cold path reached"):
        await runtime.create("logs-cold", tmp_path / "policy", stdout=str(tmp_path / "agent.out"))


@pytest.mark.parametrize("inherit_stdout,inherit_stderr", [(True, True), (True, False), (False, True), (False, False)])
def test_real_background_spawn_inherits_only_selected_host_streams(tmp_path, inherit_stdout, inherit_stderr):
    stdout = tmp_path / "agent.out"
    stderr = tmp_path / "agent.err"
    script = """
import json,socket,struct,sys
from jiuwenbox.supervisor import sandbox_daemon as daemon
getattr(daemon, '_load_deferred_modules')()
server,client=socket.socketpair()
header={'job_id':'test-job','command':[sys.executable,'-c',"import os;os.write(1,b'background out');os.write(2,b'background err')"],
        'inherit_stdout':INHERIT_STDOUT,'inherit_stderr':INHERIT_STDERR}
getattr(daemon, '_handle_exec_background')(server,header)
size=struct.unpack('!I',client.recv(4))[0]
response=json.loads(client.recv(size))
assert response['ok'],response
assert response['stdout_redirected']==INHERIT_STDOUT
assert response['stderr_redirected']==INHERIT_STDERR
job=getattr(daemon, '_bg_jobs')['test-job']
assert job.proc.wait(timeout=5)==0
server.close();client.close()
""".replace("INHERIT_STDOUT", repr(inherit_stdout)).replace("INHERIT_STDERR", repr(inherit_stderr))
    with RuntimeOutput(str(stdout), str(stderr)) as output:
        result = subprocess.run([sys.executable, "-c", script], stdout=output.stdout, stderr=output.stderr, timeout=10)
    assert result.returncode == 0, stderr.read_text()
    assert stdout.read_bytes() == (b"background out" if inherit_stdout else b"")
    assert stderr.read_bytes() == (b"background err" if inherit_stderr else b"")
