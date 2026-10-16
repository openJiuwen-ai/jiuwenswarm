"""JM-14: a colleague reads and writes its own office directory and nothing else."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from jiuwenswarm.server.jiuwenmate import service as service_module
from jiuwenswarm.server.jiuwenmate.execution_models import (
    ExecutionRecord,
    PersistentContractError,
    RequestRecord,
)
from jiuwenswarm.server.jiuwenmate.models import CapabilitySelection, ColleagueSettings, PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import HostWorkspaceAccess, WorkspaceStore
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.service import WORKSPACE_PROMPT, PersistentAgentService
from jiuwenswarm.server.jiuwenmate.spec_provider import build_invocation
from jiuwenswarm.server.jiuwenmate.web_adapter import WEB_ACCESS
from openjiuwen.harness.persistent_agent.workspace.access import validate_read_scope, validate_write_scope
from openjiuwen.harness.persistent_agent.workspace.path_policy import (
    WorkspacePathError,
    resolve_read_path,
    resolve_write_path,
)

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

NS = "workspace-1"
AGENT = "mate_dev"


class FakeModels:
    def default_selection(self):
        return "deepseek#0"

    def resolve(self, selection, *, client_id):
        return TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=client_id,
                client_provider="OpenAI",
                api_key="sk-test",
                api_base="https://example.invalid/v1",
                model_name="deepseek",
            ),
            model_request_config=ModelRequestConfig(model="deepseek"),
        )


class FakePorts:
    async def submit(self, execution, event):
        raise AssertionError("not called")

    async def reserve(self, execution, model_call_id, estimate):
        raise AssertionError("not called")

    async def report(self, execution, model_call_id, usage):
        raise AssertionError("not called")


def make_service(tmp_path) -> PersistentAgentService:
    ticks = count()
    base = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)
    return PersistentAgentService(
        PersistentAgentRepository(tmp_path / "agents.sqlite3"),
        TemplateStore(tmp_path / "templates"),
        models=FakeModels(),
        template_source=lambda _: None,
        clock=lambda: base + timedelta(seconds=next(ticks)),
        windows=WindowBackendStore(tmp_path / "agents.sqlite3"),
    )


def settings(**changes) -> ColleagueSettings:
    fields = {
        "name": "研发同事",
        "description": "负责整理研发进展。",
        "capabilities": CapabilitySelection(model="deepseek#0"),
    }
    fields.update(changes)
    return ColleagueSettings(**fields)


def execution(agent_id: str = AGENT, scope: frozenset[str] = WEB_ACCESS.startup_scope, **changes):
    fields = {
        "namespace_id": NS,
        "agent_id": agent_id,
        "session_id": "session-1",
        "request_id": "request-1",
        "execution_id": "execution-1",
        "source_ref": "source-1",
        "config_version": "v1",
        "config_fingerprint": "0" * 64,
        "principal_ref": "local",
        "startup_scope": scope,
        "source_scope": scope,
        "policy_revision": "local",
        "capability_snapshot": frozenset({"construction", "workspace_read", "workspace_write"}),
    }
    fields.update(changes)
    return core.ExecutionContext(**fields)


async def resolve(access: HostWorkspaceAccess, context, path: str):
    scope = validate_read_scope(await access.resolve_read_scope(context), context)
    return resolve_read_path(scope, path)


async def resolve_write(access: HostWorkspaceAccess, context, path: str):
    scope = validate_write_scope(await access.resolve_write_scope(context), context)
    return resolve_write_path(scope, path)


@pytest.fixture
def office(tmp_path):
    store = WorkspaceStore(tmp_path / "workspaces")
    root = store.ensure(NS, AGENT)
    (root / "资料").mkdir()
    (root / "资料" / "周报.md").write_text("本周完成接入。", encoding="utf-8")
    return store, root


async def test_a_web_request_reads_relative_paths_inside_the_office(office):
    store, root = office
    access = HostWorkspaceAccess(store, NS, AGENT)

    resolved = await resolve(access, execution(), "资料/周报.md")
    listed = await resolve(access, execution(), ".")

    assert resolved.path == (root / "资料" / "周报.md").resolve()
    assert resolved.source_ref == f"workspace://{NS}/{AGENT}/资料/周报.md"
    assert listed.path == root.resolve()


@pytest.mark.parametrize(
    "scope",
    [frozenset({"direct_message"}), frozenset({"group_message", "workspace_read_extra"})],
)
async def test_a_request_without_the_read_scope_is_forbidden(office, scope):
    store, _ = office
    access = HostWorkspaceAccess(store, NS, AGENT)

    granted = await access.resolve_read_scope(execution(scope=scope))
    with pytest.raises(WorkspacePathError) as denied:
        await resolve(access, execution(scope=scope), "资料/周报.md")

    assert (granted.status, granted.allowed_roots) == ("forbidden", ())
    assert denied.value.status == "forbidden"


async def test_the_read_scope_needs_both_startup_and_source_scope(office):
    store, _ = office
    access = HostWorkspaceAccess(store, NS, AGENT)

    granted = await access.resolve_read_scope(execution(source_scope=frozenset({"direct_message"})))

    assert granted.status == "forbidden"


@pytest.mark.parametrize("path", ["../mate_other/secret.md", "资料/../../mate_other", "/etc/hosts", "C:\\Windows"])
async def test_paths_leaving_the_office_are_forbidden(tmp_path, office, path):
    store, root = office
    store.ensure(NS, "mate_other")
    access = HostWorkspaceAccess(store, NS, AGENT)

    with pytest.raises(WorkspacePathError) as denied:
        await resolve(access, execution(), path)

    assert denied.value.status == "forbidden"


async def test_a_symlink_out_of_the_office_is_forbidden(tmp_path, office):
    store, root = office
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    os.symlink(outside, root / "link")
    access = HostWorkspaceAccess(store, NS, AGENT)

    with pytest.raises(WorkspacePathError) as denied:
        await resolve(access, execution(), "link/secret.md")

    assert denied.value.status == "forbidden"


async def test_an_execution_of_another_colleague_is_refused(office):
    store, _ = office
    access = HostWorkspaceAccess(store, NS, AGENT)

    with pytest.raises(PersistentAgentError):
        await access.resolve_read_scope(execution(agent_id="mate_other"))
    with pytest.raises(PersistentAgentError):
        await access.resolve_read_scope(execution(namespace_id="workspace-2"))


@pytest.mark.parametrize(("namespace_id", "agent_id"), [("../x", AGENT), (NS, "a/b"), (NS, ""), (NS, ".")])
def test_office_directory_names_are_plain(tmp_path, namespace_id, agent_id):
    with pytest.raises(PersistentAgentError):
        WorkspaceStore(tmp_path / "workspaces").path(namespace_id, agent_id)


def test_publishing_creates_the_office_and_deleting_removes_it(tmp_path):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings())
    office_dir = service.workspaces.path(NS, view.agent_id)
    spec = service.current_snapshot(NS, view.agent_id).load_spec()

    assert view.workspace_dir == str(office_dir) and office_dir.is_dir()
    assert spec.system_prompt.endswith(WORKSPACE_PROMPT)
    assert set(spec.enabled_features) == {"construction", "workspace_read", "workspace_write"}
    assert spec.enable_sys_operation is True and spec.enable_read_image_multimodal is False

    (office_dir / "笔记.md").write_text("x", encoding="utf-8")
    service.delete_colleague(NS, view.agent_id)

    assert not office_dir.exists()


def test_refresh_republishes_only_colleagues_without_pending_edits(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service_module, "WORKSPACE_PROMPT", "")
    clean = service.create_colleague(NS, settings(name="甲"))
    edited = service.create_colleague(NS, settings(name="乙"))
    service.update_settings(NS, edited.agent_id, settings(name="乙二"))
    monkeypatch.undo()

    service.refresh_published(NS)
    service.refresh_published(NS)

    refreshed = service.current_snapshot(NS, clean.agent_id)
    kept = service.current_snapshot(NS, edited.agent_id)
    assert refreshed.config_version == "v2"
    assert refreshed.load_spec().system_prompt.endswith(WORKSPACE_PROMPT)
    assert kept.config_version == "v1"
    assert service.get_colleague(NS, edited.agent_id).has_unpublished_changes


def request_for(snapshot) -> RequestRecord:
    return RequestRecord(
        namespace_id=NS,
        agent_id=snapshot.agent_id,
        session_id="session-1",
        request_id="request-1",
        source_ref="source-1",
        config_version=snapshot.config_version,
        config_fingerprint=snapshot.fingerprint,
        access=WEB_ACCESS,
        request_input=core.RequestInput(kind="scheduled", text="看一下周报", reply_target="conversation-1"),
        received_at=datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize("port", ["workspace_access", "workspace_write_access"])
@pytest.mark.parametrize("owner", [None, "mate_other"])
def test_an_invocation_without_this_colleagues_scope_is_rejected(tmp_path, owner, port):
    service = make_service(tmp_path)
    view = service.create_colleague(NS, settings())
    snapshot = service.current_snapshot(NS, view.agent_id)
    request = request_for(snapshot)
    windows = WindowBackendStore(tmp_path / "agents.sqlite3").bind(NS, view.agent_id, request.session_id)
    own = HostWorkspaceAccess(service.workspaces, NS, view.agent_id)
    scopes = {"workspace_access": own, "workspace_write_access": own}
    scopes[port] = None if owner is None else HostWorkspaceAccess(service.workspaces, NS, owner)
    ports = core.PersistentPorts(commit=FakePorts(), budget=FakePorts(), window_backend=windows, **scopes)

    with pytest.raises(PersistentContractError, match=port):
        build_invocation(snapshot, request, ExecutionRecord(request_id="request-1", execution_id="e-1"), ports=ports)


async def test_a_web_request_writes_relative_paths_inside_the_office(office):
    store, root = office
    access = HostWorkspaceAccess(store, NS, AGENT)

    resolved = await resolve_write(access, execution(), "报告/总结.md")

    assert resolved.path == (root / "报告" / "总结.md").resolve()
    assert resolved.source_ref == f"workspace://{NS}/{AGENT}/报告/总结.md"


@pytest.mark.parametrize(
    "scope",
    [frozenset({"direct_message", "workspace_read"}), frozenset({"group_message"})],
)
async def test_a_request_without_the_write_scope_cannot_write(office, scope):
    store, _ = office
    access = HostWorkspaceAccess(store, NS, AGENT)

    granted = await access.resolve_write_scope(execution(scope=scope))
    with pytest.raises(WorkspacePathError) as denied:
        await resolve_write(access, execution(scope=scope), "报告/总结.md")

    assert (granted.status, granted.allowed_write_roots, granted.current_session_root) == ("forbidden", (), None)
    assert denied.value.status == "forbidden"


async def test_the_write_scope_needs_both_startup_and_source_scope(office):
    store, _ = office
    access = HostWorkspaceAccess(store, NS, AGENT)

    granted = await access.resolve_write_scope(execution(source_scope=frozenset({"direct_message", "workspace_read"})))

    assert granted.status == "forbidden"


@pytest.mark.parametrize("path", ["../mate_other/secret.md", "资料/../../mate_other/x.md", "/tmp/x.md", "C:\\x.md"])
async def test_writes_leaving_the_office_are_forbidden(office, path):
    store, _ = office
    store.ensure(NS, "mate_other")
    access = HostWorkspaceAccess(store, NS, AGENT)

    with pytest.raises(WorkspacePathError) as denied:
        await resolve_write(access, execution(), path)

    assert denied.value.status == "forbidden"


async def test_a_session_reaches_its_own_drafts_only(office):
    store, root = office
    drafts = root / "sessions"
    (drafts / "session-1").mkdir(parents=True)
    (drafts / "session-2").mkdir()
    (drafts / "session-2" / "草稿.md").write_text("另一个对话的草稿", encoding="utf-8")
    access = HostWorkspaceAccess(store, NS, AGENT)
    mine = execution(session_id="session-1")

    own_read = await resolve(access, mine, "sessions/session-1")
    own_write = await resolve_write(access, mine, "sessions/session-1/草稿.md")

    assert own_read.path == (drafts / "session-1").resolve()
    assert own_write.path == (drafts / "session-1" / "草稿.md").resolve()
    for path in ("sessions", "sessions/session-2", "sessions/session-2/草稿.md"):
        with pytest.raises(WorkspacePathError):
            await resolve(access, mine, path)
    for path in ("sessions/session-2/草稿.md", "sessions/新文件.md"):
        with pytest.raises(WorkspacePathError):
            await resolve_write(access, mine, path)


@pytest.mark.parametrize("session_id", ["../x", "a/b", "."])
async def test_a_session_id_that_is_not_a_plain_name_is_refused(office, session_id):
    store, _ = office
    access = HostWorkspaceAccess(store, NS, AGENT)

    with pytest.raises(PersistentAgentError):
        await access.resolve_write_scope(execution(session_id=session_id))
    with pytest.raises(PersistentAgentError):
        await access.resolve_read_scope(execution(session_id=session_id))


async def test_an_execution_of_another_colleague_cannot_write(office):
    store, _ = office
    access = HostWorkspaceAccess(store, NS, AGENT)

    with pytest.raises(PersistentAgentError):
        await access.resolve_write_scope(execution(agent_id="mate_other"))
