"""Persistent app editor association and routed Core failure boundaries."""
import asyncio

import pytest
from openjiuwen_protocol.message import ReqMethod

from jiuwenswarm.extensions.app_market.editor_sessions import AppEditorSessions, EditorSessionError
from jiuwenswarm.extensions.app_market.manager import AppMarketManager


@pytest.fixture
def editor(tmp_path):
    page = tmp_path / "game.html"
    page.write_text("<title>Game</title>", encoding="utf-8")
    manager = AppMarketManager(str(tmp_path / "market"))
    app = asyncio.run(manager.publish_app(str(page), "source", "alice"))
    return manager, page, app["app_id"]


def core(calls, sessions):
    async def request(method, params):
        calls.append((method, params))
        if method == ReqMethod.SESSION_GET_METADATA:
            session = sessions.get(params["session_id"])
            return (True, session) if session else (False, {"code": "NOT_FOUND"})
        if method == ReqMethod.PROJECT_LIST:
            return True, {"projects": [{"project_id": "2048", "work_mode": "code"}]}
        if method == ReqMethod.SESSION_CREATE:
            await asyncio.sleep(0)
            sid = f"edit-{len(sessions)}"
            sessions[sid] = {"project_id": params["project_id"], "work_mode": params["work_mode"], "title": params["title"]}
            return True, {"session_id": sid}
        raise AssertionError(method)
    return request


def test_editor_is_shared_across_concurrent_clicks_restart_and_republish(editor):
    manager, page, app_id = editor
    calls = []
    sessions = {"source": {"project_id": "2048", "work_mode": "code"}}
    request = core(calls, sessions)
    service = AppEditorSessions(manager._service._storage)

    async def open_twice():
        return await asyncio.gather(*(service.open(app_id, "alice", request, {"title": "Edit game"}) for _ in range(2)))

    first, second = asyncio.run(open_twice())
    assert first["created"] is True
    assert second["created"] is False
    assert first["session"] == second["session"]
    assert first["session"]["project_id"] == "2048"
    create = [params for method, params in calls if method == ReqMethod.SESSION_CREATE]
    assert len(create) == 1
    assert create[0]["mode"] == "agent.code.normal"
    assert create[0]["work_mode"] == "code"
    asyncio.run(manager.publish_app(str(page), "source", "alice"))
    restarted = AppMarketManager(str(manager._service._storage.data_dir))
    del sessions["source"]  # Restoring an editor does not depend on the original session.
    third = asyncio.run(AppEditorSessions(restarted._service._storage).open(app_id, "alice", request, {}))
    assert third == second
    assert len([m for m, _ in calls if m == ReqMethod.SESSION_CREATE]) == 1


def test_deleted_binding_is_replaced_but_transient_failure_is_not(editor):
    manager, _, app_id = editor
    calls = []
    sessions = {"source": {"project_id": "2048", "work_mode": "code"}}
    request = core(calls, sessions)
    service = AppEditorSessions(manager._service._storage)
    first = asyncio.run(service.open(app_id, "alice", request, {"title": "Edit"}))
    first_token = manager._service._storage.load_manifest(app_id).editor_create_token

    async def unavailable(method, params):
        return False, {"error": "offline", "code": "SERVICE_UNAVAILABLE"}

    with pytest.raises(EditorSessionError, match="offline"):
        asyncio.run(service.open(app_id, "alice", unavailable, {}))
    assert manager._service._storage.load_manifest(app_id).editor_session_id == first["session"]["session_id"]
    del sessions[first["session"]["session_id"]]
    second = asyncio.run(service.open(app_id, "alice", request, {"title": "Edit"}))
    assert second["created"] is True
    assert manager._service._storage.load_manifest(app_id).editor_create_token != first_token
    assert len([m for m, _ in calls if m == ReqMethod.SESSION_CREATE]) == 2


@pytest.mark.parametrize("reason", ["wrong-owner", "missing-source", "hidden-project", "missing-project"])
def test_invalid_source_or_owner_never_creates_a_session(editor, reason):
    manager, _, app_id = editor
    calls = []
    sessions = {} if reason == "missing-source" else {"source": {"project_id": "2048", "work_mode": "code"}}
    base_request = core(calls, sessions)

    async def request(method, params):
        if method == ReqMethod.PROJECT_LIST and reason in {"hidden-project", "missing-project"}:
            return True, {"projects": [{"project_id": "2048", "hidden": True}] if reason == "hidden-project" else []}
        return await base_request(method, params)

    with pytest.raises(EditorSessionError):
        asyncio.run(AppEditorSessions(manager._service._storage).open(app_id, "bob" if reason == "wrong-owner" else "alice", request, {}))
    assert not any(m == ReqMethod.SESSION_CREATE for m, _ in calls)


def test_delete_during_creation_cannot_resurrect_app(editor):
    manager, _, app_id = editor
    sessions = {"source": {"project_id": "2048", "work_mode": "code"}}
    base_request = core([], sessions)

    async def request(method, params):
        result = await base_request(method, params)
        if method == ReqMethod.SESSION_CREATE:
            manager._service._storage.delete_app(app_id)
        return result

    with pytest.raises(EditorSessionError) as exc:
        asyncio.run(AppEditorSessions(manager._service._storage).open(app_id, "alice", request, {"title": "Edit"}))
    assert exc.value.code == "CONFLICT"
    assert manager._service._storage.load_manifest(app_id) is None
