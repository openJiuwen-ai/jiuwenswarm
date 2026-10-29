"""SDK -> HTTP route -> manager -> real daemon IPC -> disk.

The daemon is unprivileged and does not install namespaces in this suite;
policy installation remains a separate system-test obligation.
"""

import asyncio
import hashlib
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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
    from jiuwenswarm.server.runtime.session.session_metadata import (
        init_session_metadata,
    )
    root = tmp_path / "sessions"
    init_session_metadata(session_id="s", sessions_root=root, new_todo_object=True)
    monkeypatch.setattr("jiuwenswarm.agents.harness.flash.rails.flash_todo_rail.get_agent_sessions_dir", lambda: root)
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "test-owned-token")
    with tempfile.TemporaryDirectory(prefix="todo-ipc-") as directory:
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
async def test_flash_research_updates_the_bound_sandbox_plan_and_rejects_old_plan(backend):
    from openjiuwen.harness.tools.todo import TodoCreateTool
    from openjiuwen.harness.tools.todo_store import _BINDING

    from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
        DEEPRESEARCH_STAGES,
    )
    from jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress import (
        capture_deepresearch_todo_target,
        persist_deepresearch_task_update,
    )
    from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import (
        bind_todo_lifecycle,
    )

    provider, _, _, root = backend
    operation = SimpleNamespace(fs=lambda: provider)
    create = TodoCreateTool(operation, str(root / "todos"))
    modify = TodoModifyTool(operation, str(root / "todos"))
    bind_todo_lifecycle(create)
    bind_todo_lifecycle(modify)
    session = SimpleNamespace(get_session_id=lambda: "s")
    tasks = [{"id": f"deepresearch_stage_{i}", "content": title} for i, title in enumerate(DEEPRESEARCH_STAGES, 1)]
    payload = {"event_type": "task.update", "tasks": [
        {"task_id": task["id"], "task_content": task["content"], "status": "completed"} for task in tasks
    ]}
    token = _BINDING.set(None)
    try:
        await create.invoke({"tasks": tasks}, session=session)
        path = Path(modify.get_file_path("s"))
        target = await capture_deepresearch_todo_target(path, tool=modify, session_id="s")
        assert await persist_deepresearch_task_update(payload, todo_path=path, target=target)
        assert all(item.status.value == "completed" for item in await modify.load_todos("s"))

        create.reset_invoke_marker("s")
        newer = await create.invoke({"plan_action": "new", "tasks": [{"id": "new", "content": "new"}]}, session=session)
        with pytest.raises(ValueError, match="older plan"):
            await persist_deepresearch_task_update(payload, todo_path=path, target=target)
        assert [item.id for item in await modify.load_todos("s")] == ["new"]
        assert newer.todo_snapshot["revision"] == 3

        original = path.read_bytes()
        detached = path.parent.with_name("detached")
        path.parent.rename(detached)
        outside = root / "outside"
        outside.mkdir()
        (outside / "todo.json").write_bytes(original)
        path.parent.symlink_to(outside, target_is_directory=True)
        with pytest.raises(RuntimeError, match="snapshot read failed"):
            await persist_deepresearch_task_update(payload, todo_path=path, target=target)
        assert (outside / "todo.json").read_bytes() == original
        assert (detached / "todo.json").read_bytes() == original
        path.parent.unlink()
        detached.rename(path.parent)
        os.link(path, outside / "hardlink.json")
        with pytest.raises(RuntimeError, match="snapshot read failed"):
            await persist_deepresearch_task_update(payload, todo_path=path, target=target)
        assert path.read_bytes() == original
    finally:
        _BINDING.reset(token)


@pytest.mark.asyncio
async def test_actual_sdk_http_daemon_conditional_commit(backend):
    provider, _http, _manager, root = backend
    path = str(root / "todo.json")
    snapshot = await provider.read_file(
        path, options={"snapshot": True, "missing_ok": True}
    )
    assert snapshot.code == 0 and snapshot.data.exists is False
    options = {
        "atomic_replace": True,
        "expected_exists": False,
        "expected_content_sha256": snapshot.data.content_sha256,
    }
    results = await asyncio.gather(
        *[
            provider.write_file(path, value, prepend_newline=False, options=options)
            for value in ("one", "two")
        ]
    )
    assert sum(result.data.atomic_replace for result in results) == 1
    assert sum(result.data.conflict for result in results) == 1
    read = await provider.read_file(path, options={"snapshot": True})
    assert read.data.content in ("one", "two")
    assert (
        read.data.content_sha256
        == hashlib.sha256(read.data.content.encode()).hexdigest()
    )


@pytest.mark.asyncio
async def test_real_sandbox_todo_updates_keep_both_changes(backend):
    provider, _http, _manager, root = backend
    from openjiuwen.harness.tools.todo_store import _BINDING

    token = _BINDING.set(None)
    try:
        operation = SimpleNamespace(fs=lambda: provider)
        first, second = (
            TodoModifyTool(operation, str(root / "todos")) for _ in range(2)
        )
        from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import (
            bind_todo_lifecycle,
        )
        bind_todo_lifecycle(first)
        bind_todo_lifecycle(second)
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
            *[
                tool.invoke(
                    {
                        "action": "update",
                        "todos": [{"id": ident, "status": "completed"}],
                    },
                    session=session,
                )
                for tool, ident in [(first, "A"), (second, "B")]
            ]
        )
        assert len({r.todo_snapshot["revision"] for r in results}) == 2
        assert all(
            row.status.value == "completed" for row in await first.load_todos("s")
        )
    finally:
        _BINDING.reset(token)


@pytest.mark.asyncio
async def test_gateway_wrapper_keeps_transaction_during_lazy_backend_binding(backend, monkeypatch):
    from openjiuwen.core.sys_operation import OperationMode
    from openjiuwen.core.sys_operation.config import (
        PreDeployLauncherConfig,
        SandboxGatewayConfig,
    )
    from openjiuwen.core.sys_operation.sandbox.fs_operation import FsOperation
    from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxGateway
    from openjiuwen.core.sys_operation.sandbox.gateway.gateway_client import (
        SandboxGatewayClient,
    )
    from openjiuwen.core.sys_operation.sandbox.run_config import SandboxRunConfig
    from openjiuwen.harness.tools.todo_store import _BINDING

    provider, _http, _manager, root = backend
    config = SandboxGatewayConfig(launcher_config=PreDeployLauncherConfig(
        base_url='http://testserver', sandbox_type='jiuwenbox',
        extra_params={'lifecycle_hook': lambda *_args: None},
    ))
    def bind_backend():
        config.launcher_config.extra_params['sandbox_id'] = 'probe'
        return 'probe'
    monkeypatch.setattr(provider, '_get_sandbox_id', bind_backend)
    gateway = SandboxGateway()
    async def locate(*_args, **_kwargs):
        return provider
    monkeypatch.setattr(gateway, '_get_or_create_provider', locate)
    fs = FsOperation('fs', OperationMode.SANDBOX, 'test', SandboxRunConfig(config, 'isolated'))
    fs._gateway_client = SandboxGatewayClient(config=config, isolation_key='isolated', gateway=gateway)
    calls = []
    real = provider._client.file_snapshot
    def counted(*args, **kwargs):
        calls.append(args[2])
        return real(*args, **kwargs)
    monkeypatch.setattr(provider._client, 'file_snapshot', counted)
    token = _BINDING.set(None)
    try:
        tool = TodoModifyTool(SimpleNamespace(fs=lambda: fs), str(root / 'gateway-todo'))
        from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import (
            bind_todo_lifecycle,
        )
        bind_todo_lifecycle(tool)
        result = await tool.invoke({'action': 'append', 'todos': [
            {'id': 'A', 'content': 'A', 'status': 'pending'}]},
            session=SimpleNamespace(get_session_id=lambda: 's'))
        assert result.todo_snapshot['revision'] == 1
        assert calls == ['read', 'compare_replace']
        assert config.launcher_config.extra_params['sandbox_id'] == 'probe'
    finally:
        _BINDING.reset(token)
