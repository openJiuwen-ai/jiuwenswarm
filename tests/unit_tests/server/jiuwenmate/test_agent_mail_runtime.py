from __future__ import annotations

import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

from jiuwenswarm.server.jiuwenmate.agent_mail_runtime import (
    AgentMailRuntimeService,
    AgentMailRuntimeStore,
    AgentMailScope,
    load_agent_mail_runtime_provider,
)
from jiuwenswarm.server.jiuwenmate.agent_mail_local_provider import LocalAgentMailRuntime
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.agent_mail_tool import prepare_agent_mail_invocation
from openjiuwen.harness.persistent_agent.schema.execution import ExecutionContext


class _Runtime:
    def __init__(self) -> None:
        self.scopes: list[AgentMailScope] = []

    async def status(self, scope: AgentMailScope):
        self.scopes.append(scope)
        return {"status": "not_connected", "cli_version": "1.0.18"}

    async def begin_auth(self, scope: AgentMailScope):
        self.scopes.append(scope)
        return {"status": "authorizing", "operation_id": "op-1", "auth_url": "https://agent.qq.com/auth"}

    async def poll_auth(self, scope: AgentMailScope, operation_id: str):
        self.scopes.append(scope)
        assert operation_id == "op-1"
        return {"status": "connected", "cli_version": "1.0.18", "email": "team@example.com"}

    async def cancel_auth(self, scope: AgentMailScope, operation_id: str):
        self.scopes.append(scope)
        return {"status": "not_connected"}

    async def disconnect(self, scope: AgentMailScope):
        self.scopes.append(scope)
        return {"status": "not_connected"}

    async def execute(self, scope: AgentMailScope, operation: str, payload: dict):
        self.scopes.append(scope)
        return {"status": "ok", "operation": operation}

    async def remove_runtime(self, scope: AgentMailScope):
        self.scopes.append(scope)


def test_provider_loader_uses_only_a_trusted_host_import_and_fails_closed(monkeypatch, tmp_path):
    from jiuwenswarm.server.jiuwenmate import agent_mail_local_provider

    module = ModuleType("test_agent_mail_provider")
    module.create_provider = _Runtime
    monkeypatch.setitem(sys.modules, module.__name__, module)

    monkeypatch.delenv("JIUWENMATE_AGENT_MAIL_RUNTIME_PROVIDER", raising=False)
    monkeypatch.setattr(agent_mail_local_provider, "_find_npx", lambda: None)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert load_agent_mail_runtime_provider() is None
    monkeypatch.setenv("JIUWENMATE_AGENT_MAIL_RUNTIME_PROVIDER", "test_agent_mail_provider:create_provider")
    assert isinstance(load_agent_mail_runtime_provider(), _Runtime)

    monkeypatch.setenv("JIUWENMATE_AGENT_MAIL_RUNTIME_PROVIDER", "missing-provider")
    assert load_agent_mail_runtime_provider() is None


def test_provider_loader_uses_the_packaged_local_runtime_by_default(monkeypatch):
    monkeypatch.delenv("JIUWENMATE_AGENT_MAIL_RUNTIME_PROVIDER", raising=False)

    assert isinstance(load_agent_mail_runtime_provider(), LocalAgentMailRuntime)

@pytest.mark.asyncio
async def test_agent_mail_without_isolation_provider_fails_closed(tmp_path):
    store = AgentMailRuntimeStore(tmp_path / "agent_mail.sqlite3")
    service = AgentMailRuntimeService(store=store)
    scope = AgentMailScope(namespace_id="local", agent_id="employee-1")

    assert (await service.status(scope))["status"] == "unavailable"
    with pytest.raises(PersistentAgentError) as error:
        await service.begin_auth(scope)
    assert error.value.code == "AGENT_MAIL_RUNTIME_UNAVAILABLE"

    with pytest.raises(PersistentAgentError) as error:
        await service.execute(scope, "mail.list", {})
    assert error.value.code == "AGENT_MAIL_RUNTIME_UNAVAILABLE"


@pytest.mark.asyncio
async def test_agent_mail_status_and_auth_are_scoped_per_employee(tmp_path):
    runtime = _Runtime()
    service = AgentMailRuntimeService(
        store=AgentMailRuntimeStore(tmp_path / "agent_mail.sqlite3"), runtime=runtime
    )
    first = AgentMailScope(namespace_id="local", agent_id="employee-1")
    second = AgentMailScope(namespace_id="local", agent_id="employee-2")

    auth = await service.begin_auth(first)
    assert auth == {
        "status": "authorizing",
        "cli_version": None,
        "operation_id": "op-1",
        "auth_url": "https://agent.qq.com/auth",
        "frontend_opens_auth": False,
    }
    assert await service.status(second) == {"status": "not_connected", "cli_version": "1.0.18"}
    assert await service.poll_auth(first, "op-1") == {
        "status": "connected", "cli_version": "1.0.18", "email": "team@example.com"
    }


@pytest.mark.asyncio
async def test_rebinding_a_mailbox_releases_the_previous_employee(tmp_path):
    class SharedMailboxRuntime(_Runtime):
        def __init__(self) -> None:
            super().__init__()
            self.connected: set[str] = set()

        async def status(self, scope):
            self.scopes.append(scope)
            if scope.agent_id in self.connected:
                return {"status": "connected", "cli_version": "1.0.18", "email": "team@example.com"}
            return {"status": "not_connected", "cli_version": "1.0.18"}

        async def poll_auth(self, scope, operation_id):
            self.scopes.append(scope)
            self.connected.add(scope.agent_id)
            return {"status": "connected", "cli_version": "1.0.18", "email": " Team@Example.com "}

        async def disconnect(self, scope):
            self.scopes.append(scope)
            self.connected.discard(scope.agent_id)
            return {"status": "not_connected", "cli_version": "1.0.18"}

    runtime = SharedMailboxRuntime()
    service = AgentMailRuntimeService(store=AgentMailRuntimeStore(tmp_path / "agent_mail.sqlite3"), runtime=runtime)
    first = AgentMailScope(namespace_id="local", agent_id="employee-1")
    second = AgentMailScope(namespace_id="local", agent_id="employee-2")

    await service.poll_auth(first, "op-1")
    assert await service.poll_auth(second, "op-1") == {
        "status": "connected", "cli_version": "1.0.18", "email": "Team@Example.com"
    }

    assert runtime.scopes[:3] == [first, second, first]
    assert await service.status(first) == {"status": "not_connected", "cli_version": "1.0.18"}
    assert await service.status(second) == {
        "status": "connected", "cli_version": "1.0.18", "email": "team@example.com"
    }


@pytest.mark.asyncio
async def test_rebinding_moves_the_mailbox_when_previous_logout_fails(tmp_path):
    class FailingDisconnectRuntime(_Runtime):
        async def disconnect(self, scope):
            self.scopes.append(scope)
            raise RuntimeError("logout failed")

    runtime = FailingDisconnectRuntime()
    service = AgentMailRuntimeService(store=AgentMailRuntimeStore(tmp_path / "agent_mail.sqlite3"), runtime=runtime)
    first = AgentMailScope(namespace_id="local", agent_id="employee-1")
    second = AgentMailScope(namespace_id="local", agent_id="employee-2")

    await service.poll_auth(first, "op-1")
    assert (await service.poll_auth(second, "op-1"))["status"] == "connected"

    with sqlite3.connect(tmp_path / "agent_mail.sqlite3") as conn:
        owners = conn.execute("SELECT agent_id FROM agent_mail_bindings ORDER BY agent_id").fetchall()
        previous = conn.execute(
            "SELECT status FROM agent_mail_runtime WHERE agent_id = ?",
            ("employee-1",),
        ).fetchone()
    assert owners == [("employee-2",)]
    assert previous == ("not_connected",)


@pytest.mark.asyncio
async def test_begin_auth_is_idempotent_when_employee_is_already_connected(tmp_path):
    class AlreadyConnectedRuntime(_Runtime):
        async def begin_auth(self, scope):
            self.scopes.append(scope)
            return {"status": "connected", "cli_version": "1.0.18", "email": "employee@example.com"}

    runtime = AlreadyConnectedRuntime()
    service = AgentMailRuntimeService(
        store=AgentMailRuntimeStore(tmp_path / "agent_mail.sqlite3"), runtime=runtime
    )
    scope = AgentMailScope(namespace_id="local", agent_id="employee-1")

    assert await service.begin_auth(scope) == {
        "status": "connected", "cli_version": "1.0.18", "email": "employee@example.com"
    }


@pytest.mark.asyncio
async def test_agent_mail_store_never_persists_authorization_url_or_mail_content(tmp_path):
    database = tmp_path / "agent_mail.sqlite3"
    store = AgentMailRuntimeStore(database)
    service = AgentMailRuntimeService(store=store, runtime=_Runtime())
    scope = AgentMailScope(namespace_id="local", agent_id="employee-1")

    await service.begin_auth(scope)
    await service.poll_auth(scope, "op-1")

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_mail_runtime)")}
        row = connection.execute("SELECT * FROM agent_mail_runtime").fetchone()

    assert columns == {"namespace_id", "agent_id", "status", "cli_version", "updated_at", "error_code"}
    assert "https://agent.qq.com/auth" not in repr(row)
    assert "mail content" not in repr(row)


@pytest.mark.asyncio
async def test_mail_tool_is_only_added_to_the_connected_employee_invocation(tmp_path):
    class ConnectedRuntime(_Runtime):
        async def status(self, scope):
            self.scopes.append(scope)
            return {"status": "connected", "cli_version": "1.0.18", "email": "employee@example.com"}

    class Spec:
        tools = []

        def model_copy(self, *, update):
            return SimpleNamespace(**update, fingerprint=lambda: "b" * 64)

    scope = AgentMailScope(namespace_id="local", agent_id="employee-1")
    execution = ExecutionContext(
        namespace_id="local",
        agent_id=scope.agent_id,
        session_id="session-1",
        request_id="request-1",
        execution_id="execution-1",
        source_ref="source-1",
        config_version="version-1",
        config_fingerprint="a" * 64,
        principal_ref="principal-1",
        startup_scope=frozenset({"conversation"}),
        source_scope=frozenset({"direct_message"}),
        policy_revision="policy-1",
        capability_snapshot=frozenset({"construction"}),
    )
    context = SimpleNamespace(execution=execution, extras={})
    service = AgentMailRuntimeService(
        store=AgentMailRuntimeStore(tmp_path / "agent_mail.sqlite3"), runtime=ConnectedRuntime()
    )
    await service.status(scope)
    prepared = await prepare_agent_mail_invocation(
        Spec(),
        context,
        service,
    )

    assert [item.type for item in prepared.tools] == ["jiuwenmate.agent_mail"]
    assert context.extras["jiuwenmate.agent_mail"][1] == scope
    assert context.execution.config_fingerprint == "b" * 64


@pytest.mark.asyncio
async def test_mail_runtime_outage_does_not_fail_an_unrelated_employee_execution(tmp_path):
    class OfflineRuntime(_Runtime):
        async def status(self, scope):
            raise RuntimeError("provider unavailable")

    spec = SimpleNamespace(tools=[])
    context = SimpleNamespace(
        execution=SimpleNamespace(namespace_id="local", agent_id="employee-1"), extras={}
    )
    service = AgentMailRuntimeService(
        store=AgentMailRuntimeStore(tmp_path / "agent_mail.sqlite3"), runtime=OfflineRuntime()
    )

    assert await prepare_agent_mail_invocation(spec, context, service) is spec
    assert context.extras == {}


@pytest.mark.asyncio
async def test_agent_mail_execution_allows_write_operations_for_connected_employee(tmp_path):
    class ConnectedRuntime(_Runtime):
        async def status(self, scope):
            self.scopes.append(scope)
            return {"status": "connected", "cli_version": "1.0.18", "email": "employee@example.com"}

    runtime = ConnectedRuntime()
    service = AgentMailRuntimeService(
        store=AgentMailRuntimeStore(tmp_path / "agent_mail.sqlite3"), runtime=runtime
    )
    scope = AgentMailScope(namespace_id="local", agent_id="employee-1")

    result = await service.execute(
        scope, "mail.send", {"to": "someone@example.com", "subject": "Hello", "body": "World"}
    )

    assert result == {"status": "ok", "operation": "mail.send"}
    assert runtime.scopes == [scope, scope]


@pytest.mark.asyncio
async def test_agent_mail_execution_still_rejects_unknown_operations(tmp_path):
    runtime = _Runtime()
    service = AgentMailRuntimeService(
        store=AgentMailRuntimeStore(tmp_path / "agent_mail.sqlite3"), runtime=runtime
    )
    scope = AgentMailScope(namespace_id="local", agent_id="employee-1")

    with pytest.raises(PersistentAgentError) as error:
        await service.execute(scope, "mail.send_magic", {})

    assert error.value.code == "AGENT_MAIL_OPERATION_UNSUPPORTED"
    assert runtime.scopes == []


def test_agent_mail_scope_rejects_path_components():
    with pytest.raises(ValueError):
        AgentMailScope(namespace_id="local/other", agent_id="employee-1")

    with pytest.raises(ValueError):
        AgentMailScope(namespace_id="local", agent_id="../employee-1")
