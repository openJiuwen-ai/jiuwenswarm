"""SDK -> HTTP route -> manager -> real daemon IPC -> disk.

The daemon is unprivileged and does not install namespaces in this suite;
policy installation remains a separate system-test obligation.
"""

import asyncio
import hashlib
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from jiuwenbox.models.sandbox import SandboxPhase, SandboxRef
from jiuwenbox.server.auth import BearerTokenAuthMiddleware
from jiuwenbox.server.routes import sandbox as routes
from jiuwenbox.server.runtime.process import ProcessRuntime
from jiuwenbox.server.sandbox_manager import (
    CodePolicyDenialError,
    SandboxManager,
    SandboxNotFoundError,
    SandboxStateError,
)
from jiuwenbox.supervisor.daemon_ipc import (
    LISTENER_FD_ENV,
    REQUEST_TYPE_SHUTDOWN,
    SANDBOX_CONTROL_SOCKET_NAME,
    encode_request,
    recv_frame,
    send_frame,
)
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxEndpoint
from openjiuwen.extensions.sys_operation.sandbox.providers.jiuwenbox import (
    JiuwenBoxFSProvider,
    _JiuwenBoxClient,
)
from openjiuwen.harness.tools.todo import TodoModifyTool

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Real POSIX daemon uses an inherited Unix socket"
)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJIUWEN_TODO_V2", "1")
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "test-owned-token")
    with tempfile.TemporaryDirectory(prefix="req01-ipc-") as directory:
        control = Path(directory)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(control / SANDBOX_CONTROL_SOCKET_NAME))
        listener.listen()
        child = subprocess.Popen(
            [sys.executable, "-B", "-m", "jiuwenbox.supervisor.sandbox_daemon"],
            env={
                **os.environ,
                LISTENER_FD_ENV: str(listener.fileno()),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            pass_fds=(listener.fileno(),),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        runtime = ProcessRuntime()
        runtime._control_dirs["probe"] = control
        runtime._daemon_socket_ready["probe"] = True
        runtime._processes["probe"] = child
        manager = SandboxManager(
            runtime=runtime,
            state_dir=tmp_path / "state",
            audit_logger=SimpleNamespace(log=lambda *args, **kwargs: None),
        )
        manager._sandboxes["probe"] = SandboxRef(
            id="probe", phase=SandboxPhase.READY, pid=child.pid
        )
        monkeypatch.setattr(routes, "_mgr", lambda: manager)
        app = FastAPI()
        app.add_middleware(BearerTokenAuthMiddleware)
        app.include_router(routes.router, prefix="/api/v1")
        app.add_exception_handler(
            SandboxStateError,
            lambda req, exc: JSONResponse(status_code=409, content={"error": str(exc)}),
        )
        app.add_exception_handler(
            SandboxNotFoundError,
            lambda req, exc: JSONResponse(status_code=404, content={"error": str(exc)}),
        )
        app.add_exception_handler(
            CodePolicyDenialError,
            lambda req, exc: JSONResponse(status_code=403, content={"error": str(exc)}),
        )
        try:
            with TestClient(
                app, headers={"Authorization": "Bearer test-owned-token"}
            ) as http:
                original_post = http.post

                def sdk_post(url, **kwargs):
                    # In-process ASGI has no socket timeout; the SDK still sends
                    # its real deadline on the production HTTP client.
                    kwargs.pop("timeout", None)
                    return original_post(url, **kwargs)

                monkeypatch.setattr(http, "post", sdk_post)
                client = _JiuwenBoxClient.__new__(_JiuwenBoxClient)
                client._client = http
                provider = JiuwenBoxFSProvider(
                    SandboxEndpoint(base_url="http://testserver", sandbox_id="probe")
                )
                provider._client = client
                monkeypatch.setattr(provider, "_get_sandbox_id", lambda: "probe")
                yield provider, http, manager, tmp_path
        finally:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(3)
                    connection.connect(str(control / SANDBOX_CONTROL_SOCKET_NAME))
                    send_frame(
                        connection,
                        encode_request(request_type=REQUEST_TYPE_SHUTDOWN, payload={}),
                    )
                    recv_frame(connection, 1024)
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(5)
                listener.close()


@pytest.mark.asyncio
async def test_real_rpc_todo_transactions_and_conflicting_writes(backend):
    provider, _http, _manager, root = backend
    assert await provider.capabilities() == {"atomic_compare_replace": True}
    operation = SimpleNamespace(fs=lambda: provider)
    first, second = (TodoModifyTool(operation, str(root / "todos")) for _ in range(2))
    session = SimpleNamespace(get_session_id=lambda: "s")
    await first.invoke(
        {
            "action": "append",
            "todos": [
                {"id": "A", "content": "A", "status": "pending"},
                {"id": "B", "content": "B", "status": "pending"},
            ],
        },
        session=session,
    )
    results = await asyncio.gather(
        *(
            tool.invoke(
                {"action": "update", "todos": [{"id": item, "status": "completed"}]},
                session=session,
            )
            for tool, item in [(first, "A"), (second, "B")]
        )
    )
    assert len({r.todo_receipt.snapshot.metadata["revision"] for r in results}) == 2
    assert all(row.status.value == "completed" for row in await first.load_todos("s"))
    path = str(root / "compete.json")
    await provider.write_file(
        path,
        "old",
        prepend_newline=False,
        options={"atomic_replace": True, "expected_exists": False},
    )
    options = {
        "atomic_replace": True,
        "expected_exists": True,
        "expected_content_sha256": hashlib.sha256(b"old").hexdigest(),
    }
    writes = await asyncio.gather(
        *(
            provider.write_file(path, value, prepend_newline=False, options=options)
            for value in ["one", "two"]
        )
    )
    assert sum(result.code == 0 for result in writes) == 1
    assert [r.failure_kind for r in writes if r.code != 0] == ["conflict"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux access ACL and unprivileged mode checks")
@pytest.mark.parametrize("readonly", [False, True], ids=["acl", "readonly"])
@pytest.mark.asyncio
async def test_rpc_todo_preserves_file_protection(backend, readonly):
    if readonly and os.geteuid() == 0:
        pytest.skip("Requires an unprivileged writer")
    provider, _http, _manager, root = backend
    tool = TodoModifyTool(SimpleNamespace(fs=lambda: provider), str(root / "protected"))
    session = SimpleNamespace(get_session_id=lambda: "s")
    await tool.invoke({"action": "append", "todos": [{"id": "A", "content": "A", "status": "pending"}]},
                      session=session)
    path = Path(tool.todo_store("s").path)
    before = path.read_bytes()
    update = {"action": "update", "todos": [{"id": "A", "status": "completed"}]}
    if readonly:
        path.chmod(0o400)
        try:
            with pytest.raises(BaseError):
                await tool.invoke(update, session=session)
            assert path.read_bytes() == before
        finally:
            path.chmod(0o600)
    else:
        entries = [(1, 6, 0xFFFFFFFF), (2, 4, 65534), (4, 0, 0xFFFFFFFF),
                   (16, 4, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)]
        acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
        os.setxattr(path, "system.posix_acl_access", acl)
    result = await tool.invoke(update, session=session)
    assert "Successfully updated 1" in result["message"]
    assert (await tool.load_todos("s"))[0].status.value == "completed"
    if not readonly:
        assert os.getxattr(path, "system.posix_acl_access") == acl
    assert not list(path.parent.glob(".todo.json-*.tmp"))


@pytest.mark.asyncio
async def test_response_loss_never_replays_conditional_write(backend, monkeypatch):
    provider, http, _manager, root = backend
    original = http.post
    calls = []

    def lose_response(url, **kwargs):
        response = original(url, **kwargs)
        calls.append(response.status_code)
        raise httpx.ReadTimeout("injected lost acknowledgement")

    monkeypatch.setattr(http, "post", lose_response)
    path = root / "committed.json"
    result = await provider.write_file(
        str(path),
        "committed",
        prepend_newline=False,
        options={"atomic_replace": True, "expected_exists": False},
    )
    assert result.code != 0 and result.failure_kind == "outcome_unknown"
    assert calls == [200] and path.read_text() == "committed"


@pytest.mark.asyncio
async def test_cancelled_remote_write_is_drained_before_return(backend, monkeypatch):
    provider, http, _manager, root = backend
    original = http.post
    entered, release = threading.Event(), threading.Event()

    def hold(url, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(url, **kwargs)

    monkeypatch.setattr(http, "post", hold)
    path = root / "cancelled.json"
    task = asyncio.create_task(
        provider.write_file(
            str(path),
            "complete",
            prepend_newline=False,
            options={"atomic_replace": True, "expected_exists": False},
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not path.exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert path.read_text() == "complete"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_http_auth_and_policy_are_not_bypassed(backend, monkeypatch):
    _provider, http, manager, root = backend
    payload = {
        "operation": "compare_replace",
        "path": str(root / "forbidden.json"),
        "content_base64": "YQ==",
    }
    assert (
        http.post(
            "/api/v1/sandboxes/probe/file-snapshot",
            json=payload,
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )

    async def deny(*args, **kwargs):
        raise CodePolicyDenialError("test denied path")

    monkeypatch.setattr(manager, "_enforce_path_policy", deny)
    assert (
        http.post("/api/v1/sandboxes/probe/file-snapshot", json=payload).status_code
        == 403
    )
    assert not (root / "forbidden.json").exists()


@pytest.mark.asyncio
async def test_old_server_and_old_daemon_are_not_treated_as_capable(
    backend, monkeypatch
):
    provider, http, manager, _root = backend
    original = http.post
    monkeypatch.setattr(
        http,
        "post",
        lambda *a, **k: httpx.Response(
            404, request=httpx.Request("POST", "http://testserver")
        ),
    )
    assert await provider.capabilities() == {"atomic_compare_replace": False}
    monkeypatch.setattr(http, "post", original)

    async def old_daemon(*args, **kwargs):
        return {"ok": False, "error": "unknown_request_type"}, b""

    monkeypatch.setattr(manager.runtime, "file_snapshot", old_daemon)
    assert await provider.capabilities() == {"atomic_compare_replace": False}


@pytest.mark.asyncio
async def test_binary_hash_and_ordinary_file_routes_remain_compatible(backend):
    provider, _http, _manager, root = backend
    raw = b"\xff\x00\r\noriginal"
    path = str(root / "binary.dat")
    assert (
        await provider.write_file(
            path,
            raw,
            mode="bytes",
            options={"atomic_replace": True, "expected_exists": False},
        )
    ).code == 0
    read = await provider.read_file(
        path, mode="bytes", options={"include_content_sha256": True}
    )
    assert (
        read.data.content == raw
        and read.data.content_sha256 == hashlib.sha256(raw).hexdigest()
    )
    ordinary = str(root / "ordinary.txt")
    written = await provider.write_file(ordinary, "ordinary", prepend_newline=False)
    assert written.code == 0 and written.data.atomic_replace is None
    read = await provider.read_file(ordinary)
    assert (
        read.code == 0
        and read.data.content == "ordinary"
        and read.data.content_sha256 is None
    )


@pytest.mark.asyncio
async def test_failed_sandbox_is_distinguished_from_an_unsupported_protocol(backend):
    from openjiuwen.core.common.exception.errors import BaseError

    provider, _http, manager, root = backend
    manager._sandboxes["probe"].phase = SandboxPhase.ERROR
    tool = TodoModifyTool(
        SimpleNamespace(fs=lambda: provider), str(root / "unavailable")
    )
    with pytest.raises(BaseError, match="Cannot verify Todo backend capability"):
        await tool.invoke(
            {
                "action": "append",
                "todos": [{"id": "A", "content": "A", "status": "pending"}],
            },
            session=SimpleNamespace(get_session_id=lambda: "s"),
        )
    assert not (root / "unavailable").exists()


@pytest.mark.asyncio
async def test_capability_probe_preserves_readonly_recovery_after_sandbox_loss(
    backend, monkeypatch
):
    provider, _http, manager, _root = backend
    lost = manager._sandboxes.pop("probe")
    recreated = []

    async def recreate(*, stale_sandbox_id):
        recreated.append(stale_sandbox_id)
        manager._sandboxes["probe"] = lost
        return "probe"

    monkeypatch.setattr(provider, "_recreate_sandbox_after_loss", recreate)
    assert await provider.capabilities() == {"atomic_compare_replace": True}
    assert recreated == ["probe"]
