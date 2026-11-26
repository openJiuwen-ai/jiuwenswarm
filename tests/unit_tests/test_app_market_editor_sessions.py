"""Persistent app editor association and routed Core failure boundaries."""
import asyncio
from pathlib import Path

import pytest
from jiuwenswarm.common.schema.message import ReqMethod

from jiuwenswarm.extensions.app_market.editor_sessions import AppEditorSessions, EditorSessionError
from jiuwenswarm.extensions.app_market.manager import AppMarketManager


def page_dir(manager, app_id):
    manifest = manager._service._storage.load_manifest(app_id)
    return Path(manifest.work_dir).resolve()


@pytest.fixture
def editor(tmp_path):
    page = tmp_path / "game.html"
    page.write_text("<title>Game</title>", encoding="utf-8")
    manager = AppMarketManager(str(tmp_path / "market"))
    app = asyncio.run(manager.publish_app(str(page), "source", "alice"))
    return manager, page, app["app_id"]


def core(calls, sessions, projects=None):
    projects = projects if projects is not None else [{"project_id": "2048", "work_mode": "code", "project_dir": "/tmp/source"}]
    next_session = [0]
    created = {}

    async def request(method, params):
        calls.append((method, params))
        if method == ReqMethod.SESSION_GET_METADATA:
            session = sessions.get(params["session_id"])
            return (True, session) if session else (False, {"code": "NOT_FOUND"})
        if method == ReqMethod.PROJECT_LIST:
            return True, {"projects": list(projects)}
        if method == ReqMethod.PROJECT_INFO:
            assert params["include_session_stats"] is False
            project = next((item for item in projects if item["project_id"] == params["project_id"]), None)
            return (True, {"project": project}) if project else (False, {"code": "NOT_FOUND"})
        if method == ReqMethod.PROJECT_GIT_STATUS:
            return True, {"repo": {"is_git": True}}
        if method == ReqMethod.PROJECT_RESTORE:
            for project in projects:
                if project.get("project_id") == params.get("project_id"):
                    project["hidden"] = False
            return True, {"project": next(p for p in projects if p.get("project_id") == params.get("project_id"))}
        if method == ReqMethod.PROJECT_CREATE:
            project = {
                "project_id": f"proj-{len(projects) + 1}",
                "project_dir": params["project_dir"],
                "work_mode": params["work_mode"],
                "name": params["name"],
            }
            projects.append(project)
            return True, {"project": project}
        if method == ReqMethod.SESSION_CREATE:
            token = params["create_token"]
            if token in created:
                return True, {"session_id": created[token]}
            await asyncio.sleep(0)
            next_session[0] += 1
            sid = f"edit-{next_session[0]}"
            created[token] = sid
            sessions[sid] = {"project_id": params["project_id"], "work_mode": params["work_mode"], "title": params.get("title", "")}
            return True, {"session_id": sid}
        raise AssertionError(method)
    return request


def test_every_edit_creates_a_new_editor_session(editor):
    manager, page, app_id = editor
    calls = []
    sessions = {"source": {"project_id": "2048", "work_mode": "code"}}
    request = core(calls, sessions)
    service = AppEditorSessions(manager._service._storage)

    async def open_twice():
        return await asyncio.gather(*(service.open(app_id, "alice", request, {"title": "Edit game"}) for _ in range(2)))

    first, second = asyncio.run(open_twice())
    assert first["created"] is True
    assert second["created"] is True
    assert first["session"]["session_id"] != second["session"]["session_id"]
    assert first["session"]["project_id"] == second["session"]["project_id"] == "2048"
    create = [params for method, params in calls if method == ReqMethod.SESSION_CREATE]
    assert len(create) == 2
    assert create[0]["mode"] == "agent.code.normal"
    assert create[0]["work_mode"] == "code"
    asyncio.run(manager.publish_app(str(page), "source", "alice"))
    restarted = AppMarketManager(str(manager._service._storage.data_dir))
    del sessions["source"]  # Restoring an editor does not depend on the original session.
    third = asyncio.run(AppEditorSessions(restarted._service._storage).open(app_id, "alice", request, {}))
    assert third["created"] is True
    assert third["session"]["session_id"] not in {
        first["session"]["session_id"], second["session"]["session_id"],
    }
    assert third["project_id"] == second["project_id"]
    assert len([m for m, _ in calls if m == ReqMethod.SESSION_CREATE]) == 3
    manifest = manager.storage.load_manifest(app_id)
    assert manifest.editor_session_id == third["session"]["session_id"]
    assert manifest.editor_session_ids == [result["session"]["session_id"] for result in (first, second, third)]
    assert [method for method, _ in calls].count(ReqMethod.PROJECT_LIST) == 1
    older_session_id = first["session"]["session_id"]
    assert restarted.get_editor_app(older_session_id, "alice")["app_id"] == app_id
    assert restarted.get_editor_app(older_session_id, "bob") is None
    assert "editor_session_ids" not in restarted.list_apps("alice")["apps"][0]


@pytest.mark.parametrize("with_token", [False, True])
def test_editor_retry_recovers_created_session_after_lost_reply(editor, with_token):
    manager, _, app_id = editor
    sessions = {"source": {"project_id": "2048", "work_mode": "code"}}
    request = core([], sessions)
    options = {"edit_token": "ca2be687-0fad-4898-a42a-61573335ca92"} if with_token else {}

    async def lose_create_reply(method, params):
        ok, payload = await request(method, params)
        if method == ReqMethod.SESSION_CREATE:
            return False, {"code": "TIMEOUT", "error": "response lost"}
        return ok, payload

    with pytest.raises(EditorSessionError, match="response lost"):
        asyncio.run(manager.edit_session(app_id, "alice", lose_create_reply, options))

    restarted = AppEditorSessions(manager.storage)
    result = asyncio.run(restarted.open(app_id, "alice", request, options))
    assert result["session"]["session_id"] == "edit-1"
    if with_token:
        replay = asyncio.run(restarted.open(app_id, "alice", request, options))
        assert replay["session"] == result["session"]
    assert set(sessions) == {"source", "edit-1"}
    assert manager.storage.load_manifest(app_id).editor_session_ids == ["edit-1"]


def test_transient_failure_keeps_binding_and_next_click_creates(editor):
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
    second = asyncio.run(service.open(app_id, "alice", request, {"title": "Edit"}))
    assert second["created"] is True
    assert second["session"]["session_id"] != first["session"]["session_id"]
    assert manager._service._storage.load_manifest(app_id).editor_create_token != first_token
    assert len([m for m, _ in calls if m == ReqMethod.SESSION_CREATE]) == 2


@pytest.mark.parametrize("reason", ["wrong-owner", "missing-source", "hidden-project", "missing-project"])
def test_source_project_fallbacks_and_owner_boundary(editor, reason):
    manager, _, app_id = editor
    calls = []
    sessions = {} if reason == "missing-source" else {"source": {"project_id": "2048", "work_mode": "code"}}
    projects = [{"project_id": "2048", "work_mode": "code", "project_dir": "/tmp/source"}]
    if reason == "hidden-project":
        projects[0]["hidden"] = True
    elif reason == "missing-project":
        projects = []
    base_request = core(calls, sessions, projects)

    if reason == "wrong-owner":
        with pytest.raises(EditorSessionError):
            asyncio.run(
                AppEditorSessions(manager._service._storage).open(
                    app_id, "bob", base_request, {}
                )
            )
        assert not any(m == ReqMethod.SESSION_CREATE for m, _ in calls)
        return

    result = asyncio.run(
        AppEditorSessions(manager._service._storage).open(app_id, "alice", base_request, {})
    )
    assert result["created"] is True
    assert result["project_id"]
    manifest = manager._service._storage.load_manifest(app_id)
    assert manifest.project_id == result["project_id"]
    assert manifest.project_dir == result["project_dir"]
    # Hidden source projects are restored and reused; projects that are truly
    # missing fall back to a project rooted at the app directory.
    if reason == "hidden-project":
        assert manifest.project_dir == "/tmp/source"
        assert any(m == ReqMethod.PROJECT_RESTORE for m, _ in calls)
    else:
        assert manifest.project_dir == str(page_dir(manager, app_id))


def test_deleted_bound_project_is_replaced(editor):
    manager, _, app_id = editor
    manifest = manager.storage.load_manifest(app_id)
    manifest.project_id = "deleted"
    manifest.project_dir = manifest.work_dir
    manager.storage.save_manifest(app_id, manifest)
    calls = []
    request = core(calls, {}, projects=[])

    result = asyncio.run(manager.edit_session(app_id, "alice", request, {}))

    assert [method for method, _ in calls][:3] == [
        ReqMethod.PROJECT_INFO, ReqMethod.PROJECT_LIST, ReqMethod.PROJECT_CREATE,
    ]
    assert result["project_id"] == manager.storage.load_manifest(app_id).project_id == "proj-1"


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


def _git_init_request(calls, sessions, project_dir):
    projects = [{"project_id": "2048", "work_mode": "code", "project_dir": str(project_dir)}]
    sessions["source"] = {"project_id": "2048", "work_mode": "code"}

    async def request(method, params):
        calls.append((method, params))
        if method == ReqMethod.SESSION_GET_METADATA:
            return True, sessions["source"]
        if method == ReqMethod.PROJECT_LIST:
            return True, {"projects": projects}
        if method == ReqMethod.PROJECT_GIT_STATUS:
            return False, {"error": "not a git repository", "code": "NOT_GIT_REPOSITORY"}
        if method == ReqMethod.PROJECT_GIT_INIT:
            return True, {"repo": {"is_git": True}}
        if method == ReqMethod.SESSION_CREATE:
            sessions["edit-1"] = {
                "project_id": params["project_id"],
                "work_mode": params["work_mode"],
            }
            return True, {"session_id": "edit-1"}
        raise AssertionError(method)

    return request


def test_editor_initializes_git_when_project_has_no_repository(editor, tmp_path):
    manager, _page, app_id = editor
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    calls, sessions = [], {}
    request = _git_init_request(calls, sessions, source_dir)

    result = asyncio.run(
        AppEditorSessions(manager._service._storage).open(app_id, "alice", request, {})
    )

    assert result["git_available"] is True
    assert result["git_initialized"] is True
    assert [method for method, _ in calls].count(ReqMethod.PROJECT_GIT_INIT) == 1
    # The app reuses the user's source project here; never write into it.
    assert not (source_dir / ".gitignore").exists()


def test_editor_writes_gitignore_only_for_app_owned_project(editor, tmp_path):
    manager, _page, app_id = editor
    calls, sessions = [], {}
    request = _git_init_request(calls, sessions, tmp_path)

    result = asyncio.run(
        AppEditorSessions(manager._service._storage).open(app_id, "alice", request, {})
    )

    assert result["git_initialized"] is True
    assert (tmp_path / ".gitignore").is_file()
