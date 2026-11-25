# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 12 small test modules)."""

from __future__ import annotations

import asyncio
import json
import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server
import time
from jiuwenswarm.common import utils
from jiuwenswarm.server.runtime.session import session_history as history
from jiuwenswarm.server.runtime.session import session_metadata as metadata
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
from types import SimpleNamespace
from unittest.mock import AsyncMock
from jiuwenswarm.server.http_invoke import invoke_stream, invoke_unary
from jiuwenswarm.runtime.host_services import send_runtime_push
from jiuwenswarm.server import http_chat
from jiuwenswarm.server.transports.http_sink import SSESink
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session_delete import TeamDeleteResult
from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.attachments import media_attachments
from jiuwenswarm.server.control.repositories.project_repository import (
    handle_project_request,
)
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock
from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session import session_metadata
from jiuwenswarm.agents.harness.common.auto_harness.state import reset_harness_packages_state
from jiuwenswarm.enterprise.tenant.tenant_agent_pool import TenantAgentPool


# --- from test_agent_lifecycle_request.py ---

class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

def _redirect_agent_root(monkeypatch, tmp_path):
    # lifecycle（…/agent/lifecycle/resources）与 project_store（…/agent/projects.json）
    # 的路径最终都解析到 utils.get_user_workspace_dir() 之下，一处补丁整体重定向
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )

def _write_projects(agent_root, records):
    agent_root.mkdir(parents=True, exist_ok=True)
    (agent_root / "projects.json").write_text(
        json.dumps({"projects": records}, ensure_ascii=False), encoding="utf-8"
    )

def _write_project_state(agent_root, project_id, state):
    resources = agent_root / "lifecycle" / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    (resources / f"project_{project_id}.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )

async def _lifecycle_payload(server, project_id):
    request = AgentRequest(
        request_id=f"req-{project_id}",
        channel_id="__cron__",
        req_method=ReqMethod.PROJECT_LIFECYCLE,
        params={"project_id": project_id},
    )
    ws = _FakeWebSocket()
    handled = await server._handle_lifecycle_request(ws, request, asyncio.Lock())
    assert handled is True
    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["ok"] is True
    return frame["payload"]

@pytest.mark.asyncio
async def test_project_lifecycle_single_project_contract(tmp_path, monkeypatch):
    _redirect_agent_root(monkeypatch, tmp_path)
    agent_root = tmp_path / "agent"
    _write_projects(
        agent_root,
        [
            {
                "project_id": "proj_visible",
                "name": "visible",
                "project_dir": "d1",
                "work_mode": "work",
            },
            {
                "project_id": "proj_hidden",
                "name": "hidden",
                "project_dir": "d2",
                "work_mode": "work",
                "hidden": True,
            },
        ],
    )
    _write_project_state(
        agent_root,
        "proj_visible",
        {
            "blocked": True,
            "operation": {
                "operation_id": "op_archive_1",
                "resource_type": "project",
                "resource_id": "proj_visible",
                "kind": "archive",
                "status": "running",
                "phase": "block",
            },
        },
    )

    # __new__ 绕过 __init__；预置 _archive_service 跳过 SessionArchiveService 构建
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._archive_service = object()
    monkeypatch.setattr(
        agent_ws_server,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {
            "response_id": response_id,
            "payload": resp.payload,
            "ok": resp.ok,
        },
    )

    # 可见且带运行中 lifecycle operation 的项目：闸门三项判定 + projection 透传
    payload = await _lifecycle_payload(server, "proj_visible")
    assert payload["exists"] is True
    assert payload["hidden"] is False
    assert payload["execution_blocked"] is True  # state.blocked 经 projection 传导
    assert payload["operation"]["operation_id"] == "op_archive_1"
    assert payload["lifecycle_operation"]["operation_id"] == "op_archive_1"

    # 隐藏项目：scheduler 闸门据此拒绝（exists=True + hidden=True）
    payload = await _lifecycle_payload(server, "proj_hidden")
    assert payload["exists"] is True
    assert payload["hidden"] is True
    assert payload["execution_blocked"] is False

    # 不存在的项目：exists=False；无状态文件 → operation 为 None、不阻塞
    payload = await _lifecycle_payload(server, "proj_missing")
    assert payload["exists"] is False
    assert payload["hidden"] is False
    assert payload["operation"] is None
    assert payload["execution_blocked"] is False

@pytest.mark.asyncio
async def test_single_session_delete_keeps_accept_fields(tmp_path, monkeypatch):
    """The single-id delete response must expose the background-cleanup state.

    The frontend and the Gateway's lifecycle event both consume this payload;
    dropping ``accepted``/``finalizing`` makes a successful accepted delete
    indistinguishable from a synchronous completion.
    """
    _redirect_agent_root(monkeypatch, tmp_path)

    class _Service:
        async def session(self, session_id, action, channel_id):
            return {
                "session_id": session_id,
                "ok": True,
                "accepted": True,
                "finalizing": True,
                "stop_pending": True,
                "project_id": "proj_1",
            }

    server = object.__new__(agent_ws_server.AgentWebSocketServer)
    server._archive_service = _Service()
    monkeypatch.setattr(
        agent_ws_server,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {
            "response_id": response_id,
            "payload": resp.payload,
            "ok": resp.ok,
        },
    )
    request = AgentRequest(
        request_id="req-del",
        channel_id="web",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "s1"},
    )
    ws = _FakeWebSocket()
    handled = await server._handle_lifecycle_request(ws, request, asyncio.Lock())
    assert handled is True
    payload = ws.sent[0]["payload"]
    assert payload["session_id"] == "s1"
    assert payload["project_id"] == "proj_1"
    assert payload["accepted"] is True
    assert payload["finalizing"] is True
    assert payload["stop_pending"] is True

# --- from test_enterprise_history_scope.py ---

def test_background_writers_and_caches_keep_same_session_id_isolated(
    monkeypatch, tmp_path
):
    assert history.flush_pending_writes()
    assert metadata.flush_pending_writes()
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    sid = "shared-session-id"
    for user in ("u", "v"):
        with TenantContext.scoped(f"g::b::{user}"):
            metadata.update_session_metadata(
                session_id=sid, user_id=user, title=user, channel_id="web"
            )
            history.append_history_record(
                session_id=sid,
                request_id=f"r-{user}",
                channel_id="web",
                role="user",
                content=f"only-{user}",
                timestamp=time.time(),
            )
    # Both request scopes have ended before durability/read checks.
    assert history.flush_pending_writes()
    assert metadata.flush_pending_writes()
    for user in ("u", "v"):
        with TenantContext.scoped(f"g::b::{user}"):
            assert metadata.get_session_metadata(sid)["title"] == user
            directory = utils.get_agent_sessions_dir() / sid
            assert (
                json.loads((directory / "metadata.json").read_text())["user_id"] == user
            )
            records = [
                json.loads(line)
                for line in (directory / "history.jsonl").read_text().splitlines()
            ]
            assert [record["content"] for record in records] == [f"only-{user}"]
    assert not list(
        tmp_path.glob(
            "workspace_default*/agent/sessions/shared-session-id/history.jsonl"
        )
    )
    TenantContext.clear()

# --- from test_enterprise_http_tenant_scope.py ---

@pytest.mark.asyncio
@pytest.mark.parametrize("invoke", [invoke_unary, invoke_stream])
@pytest.mark.parametrize("edition", ["enterprise", "personal"])
async def test_http_handler_scope_is_bound_and_restored(monkeypatch, tmp_path, invoke, edition):
    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    request = AgentRequest(
        request_id="r", channel_id="web", user_id="u", session_id="s",
        metadata={"routing": {"group_id": "g", "bot_id": "b", "user_id": "u"}},
    )
    ctx = SimpleNamespace(request=request, sink=AsyncMock())

    async def handler(_ctx):
        # 企业按请求身份绑 tenant_key；个人版固定默认槽，避免多 Manager。
        if edition == "enterprise":
            expected = utils.build_tenant_key("g", "b", "u")
            assert TenantContext.current_tenant_key() == expected
            assert utils.get_agent_sessions_dir().is_relative_to(tmp_path)
        else:
            assert TenantContext.current_tenant_key() == utils.build_tenant_key()

    with TenantContext.scoped("outer"):
        await invoke(ctx, handler)
        assert TenantContext.current_tenant_key() == "outer"

@pytest.mark.asyncio
@pytest.mark.parametrize("invoke", [invoke_unary, invoke_stream])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_http_scope_restores_after_error_or_cancellation(monkeypatch, tmp_path, invoke, error):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    request = AgentRequest(
        request_id="r", channel_id="web", session_id="s", user_id="u",
        metadata={"routing": {"group_id": "g", "bot_id": "b"}},
    )
    sink = AsyncMock()

    async def handler(_ctx):
        assert TenantContext.current_tenant_key() == utils.build_tenant_key("g", "b", "u")
        raise error("interrupted")

    with TenantContext.scoped("outer"):
        ctx = SimpleNamespace(request=request, sink=sink)
        if error is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await invoke(ctx, handler)
            sink.send_response.assert_not_awaited()
        else:
            await invoke(ctx, handler)
            assert sink.send_response.await_args.args[0].ok is False
        assert TenantContext.current_tenant_key() == "outer"
    if invoke is invoke_stream:
        sink.close.assert_awaited_once()

# --- from test_http_file_delivery.py ---

@pytest.mark.asyncio
async def test_file_push_uses_only_its_live_http_request(monkeypatch):
    request = AgentRequest(
        request_id="r", channel_id="web", session_id="s", is_stream=True
    )
    sink = SSESink()
    ctx = SimpleNamespace(request=request, sink=sink)
    message = {
        "request_id": "r",
        "channel_id": "web",
        "session_id": "s",
        "payload": {"event_type": "chat.file", "files": [{"name": "a.txt"}]},
    }

    async def run(ctx):
        assert await send_runtime_push(message)
        assert not await send_runtime_push({**message, "session_id": "other"})
        assert not await send_runtime_push({**message, "request_id": "other"})
        assert not await send_runtime_push(
            {**message, "payload": {"event_type": "chat.final"}}
        )
        await sink.close()
        assert not await send_runtime_push(message)

    monkeypatch.setattr(http_chat, "_handle_http_chat", run)
    await http_chat.handle_http_chat(ctx)
    frames = [frame async for frame in sink.frames()]
    assert len(frames) == 1 and '"chat.file"' in frames[0]

@pytest.mark.asyncio
async def test_unary_does_not_report_invisible_file_delivery(monkeypatch):
    request = AgentRequest(
        request_id="r", channel_id="web", session_id="s", is_stream=False
    )

    async def run(ctx):
        assert not await send_runtime_push(
            {
                "request_id": "r",
                "channel_id": "web",
                "session_id": "s",
                "payload": {"event_type": "chat.file"},
            }
        )

    monkeypatch.setattr(http_chat, "_handle_http_chat", run)
    await http_chat.handle_http_chat(SimpleNamespace(request=request, sink=AsyncMock()))

# --- from test_kv_cache_root_session_product_entries.py ---

@pytest.mark.asyncio
async def test_team_delete_handler_only_maps_runtime_result(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    runtime = SimpleNamespace(
        delete_team=AsyncMock(
            return_value=TeamDeleteResult(
                ok=False,
                team_name="team-a",
                session_ids=("s1",),
                failed_session_ids=("s2",),
                deleted=True,
                recovery_required=True,
                error_code="DELETE_FAILED",
                error_message="partial",
            )
        )
    )
    server._runtime = runtime
    server._execution_runtime = lambda: runtime
    monkeypatch.setattr(
        "jiuwenswarm.server.transports.ws_sink.encode_agent_response_for_wire",
        lambda response, **_kwargs: response,
    )
    send_wire = AsyncMock()
    monkeypatch.setattr(
        "jiuwenswarm.server.transports.ws_sink.send_wire_payload",
        send_wire,
    )
    request = AgentRequest(
        request_id="delete",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "team-a"},
    )
    from jiuwenswarm.server.context import build_ws_request_context
    from jiuwenswarm.server.handlers.team import handle_team_delete

    ws = object()
    await handle_team_delete(build_ws_request_context(server, ws, request, asyncio.Lock()))

    runtime.delete_team.assert_awaited_once_with(team_name="team-a", channel_id="web")
    response = send_wire.await_args.args[1]
    assert response.ok is False
    assert response.payload == {
        "team_name": "team-a",
        "session_ids": ["s1"],
        "failed_session_ids": ["s2"],
        "deleted": True,
        "recovery_required": True,
        "error": "partial",
        "code": "DELETE_FAILED",
    }

@pytest.mark.asyncio
async def test_plain_disconnect_does_not_run_delete_participant():
    participant = SimpleNamespace(
        name="derived-resource",
        before_delete=AsyncMock(),
        release_resources=AsyncMock(),
        delete_failed=AsyncMock(),
        delete_committed=AsyncMock(),
    )
    registry = RuntimeParticipantRegistry()
    registry.replace_session_participants(activity=(), delete=(participant,))
    manager = SimpleNamespace(
        cancel_all_inflight_work=AsyncMock(),
        cleanup=AsyncMock(),
    )
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        participant_registry=registry,
    )

    await runtime.close()

    participant.before_delete.assert_not_awaited()
    participant.release_resources.assert_not_awaited()
    participant.delete_failed.assert_not_awaited()
    participant.delete_committed.assert_not_awaited()

# --- from test_media_attachment_mixed_batch.py ---

def test_images_after_eight_documents_are_still_stored(monkeypatch):
    stored_requests = []

    def fake_store(item, **kwargs):
        stored_requests.append(item)
        return {"type": "image", "path": "/tmp/session/uploads/photo.png"}

    monkeypatch.setattr(media_attachments, "_store_image_item", fake_store)

    image = {"type": "image", "mime_type": "image/png", "filename": "photo.png"}
    params = {"media_items": [{"type": "document"}] * 8 + [image]}

    media_attachments.normalize_chat_media_attachments(params, session_id="sess-mixed")

    assert stored_requests == [image]
    assert params["media_items"] == [
        {"type": "image", "path": "/tmp/session/uploads/photo.png"}
    ]
    assert params["files"]["uploaded_images"][0]["path"] == "/tmp/session/uploads/photo.png"

# --- from test_project_repository.py ---

def _request(method: ReqMethod, params: dict | None = None) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id="web",
        req_method=method,
        params=params or {},
    )

@pytest.mark.asyncio
async def test_project_list_uses_disk_queries_not_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.project_repository.load_project_list",
        lambda params: ({"projects": [{"project_id": "p1"}]}, None, None),
    )
    response = await handle_project_request(_request(ReqMethod.PROJECT_LIST))
    assert response.ok is True
    assert response.payload["projects"][0]["project_id"] == "p1"

@pytest.mark.asyncio
async def test_project_git_status_is_rejected_by_repository() -> None:
    response = await handle_project_request(_request(ReqMethod.PROJECT_GIT_STATUS))
    assert response.ok is False
    assert response.payload["code"] == "BAD_REQUEST"

# --- from test_session_read_only_scan.py ---

def test_read_only_scan_projects_legacy_metadata_without_scheduling_writes(
    tmp_path, monkeypatch
):
    root = tmp_path / "sessions"
    folder = root / "process_cli_legacy"
    folder.mkdir(parents=True)
    file = folder / "metadata.json"
    file.write_text(
        json.dumps(
            {
                "session_id": folder.name,
                "channel_id": "process_cli",
                "mode": "agent",
                "created_at": 1,
                "last_message_at": 2,
            }
        ),
        encoding="utf-8",
    )
    original = file.read_bytes()
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(session_metadata, "_build_project_lookup", lambda: ({}, {}))
    enqueue = Mock(side_effect=AssertionError("read-only scan scheduled a migration"))
    monkeypatch.setattr(session_metadata, "_enqueue_inference_write", enqueue)
    result = session_metadata.collect_all_sessions_metadata(enable_writeback=False)
    assert result[0]["mode"] == "agent.work.normal"
    assert file.read_bytes() == original
    enqueue.assert_not_called()

def test_overlapping_read_only_and_default_scans_use_separate_results(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path)
    entered, readonly_entered, release = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    policies = []

    def scan(_user_id, *, enable_writeback=True):
        policies.append(enable_writeback)
        (entered if enable_writeback else readonly_entered).set()
        assert release.wait(3)
        return [{"writeback": enable_writeback}]

    monkeypatch.setattr(session_metadata, "_collect_all_sessions_metadata", scan)
    with ThreadPoolExecutor(max_workers=2) as executor:
        default = executor.submit(session_metadata.collect_all_sessions_metadata)
        assert entered.wait(3)
        readonly = executor.submit(
            session_metadata.collect_all_sessions_metadata, enable_writeback=False
        )
        try:
            assert readonly_entered.wait(3)
        finally:
            release.set()
        assert default.result(3) == [{"writeback": True}]
        assert readonly.result(3) == [{"writeback": False}]
    assert sorted(policies) == [False, True]

# --- from test_skill_plaza_lifecycle_guard.py ---

@pytest.mark.parametrize(
    "method",
    [
        ReqMethod.SKILLS_SWARMSKILLS_HUB_RECOMMEND,
        ReqMethod.SKILLS_SWARMSKILLS_HUB_DETAIL,
        ReqMethod.SKILLS_ONLINE_SEARCH,
        ReqMethod.SKILLS_LIST,
        ReqMethod.PLUGINS_LIST,
    ],
)
def test_skill_plaza_reads_skip_session_lifecycle_guard(method: ReqMethod) -> None:
    """Plaza / catalog reads bypass lifecycle.guard when the chat session is gone."""
    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        req_method=method,
        session_id="web_deleted_session",
        params={"session_id": "web_deleted_session"},
    )
    assert AgentWebSocketServer._skips_session_lifecycle_guard(request) is True

@pytest.mark.parametrize(
    "method",
    [
        ReqMethod.SKILLS_TOGGLE,
        ReqMethod.SKILLS_REBUILD,
        ReqMethod.CHAT_SEND,
    ],
)
def test_skill_writes_and_chat_still_require_lifecycle_guard(method: ReqMethod) -> None:
    """Write / chat RPCs must not inherit the plaza read bypass."""
    request = AgentRequest(
        request_id="r2",
        channel_id="web",
        req_method=method,
        session_id="web_deleted_session",
        params={"session_id": "web_deleted_session"},
    )
    assert AgentWebSocketServer._skips_session_lifecycle_guard(request) is False
    # Broader "stateless" (skip adapter) still covers skills.* writes; that is
    # intentional and distinct from the session-lifecycle bypass.
    if method.value.startswith("skills."):
        assert AgentWebSocketServer._is_stateless_method_request(request) is True

# --- from test_startup_harness_state.py ---

def test_reset_only_changes_existing_activation_fields(tmp_path):
    path = tmp_path / "harness-packages.json"
    path.write_text(json.dumps({
        "active_package_ids": ["active"],
        "active_package_id": "active",
        "active_extension_name": "legacy",
        "native_version": {"id": "native", "is_active": False},
        "packages": [{"id": "inactive"}, {"id": "active", "is_active": True}],
        "other": "preserved",
    }), encoding="utf-8")
    reset_harness_packages_state(path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "active_package_ids": [],
        "native_version": {"id": "native", "is_active": True},
        "packages": [{"id": "inactive"}, {"id": "active", "is_active": False}],
        "other": "preserved",
    }
    previous = path.read_bytes()
    reset_harness_packages_state(path)
    assert path.read_bytes() == previous

# --- from test_startup_logging_config.py ---

def test_logging_section_and_cross_section_alias(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(utils, "get_config_file", lambda: path)
    path.write_text('models: {description: "large unrelated configuration"}\nlogging:\n  level: DEBUG\nreact: {}\n', encoding="utf-8")
    assert utils._load_logging_config_from_yaml() == {"level": "DEBUG"}
    path.write_text('base: &logging_defaults {level: WARNING}\nlogging: *logging_defaults\n', encoding="utf-8")
    assert utils._load_logging_config_from_yaml() == {"level": "WARNING"}

def test_logging_flow_mapping_keeps_existing_yaml_semantics(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(utils, "get_config_file", lambda: path)
    path.write_text('{models: {}, logging: {level: DEBUG, console_level: WARNING}}', encoding="utf-8")
    assert utils._load_logging_config_from_yaml() == {"level": "DEBUG", "console_level": "WARNING"}
    path.write_text('[logging, DEBUG]', encoding="utf-8")
    assert utils._load_logging_config_from_yaml() == {}

# --- from test_tenant_extract_from_http_query.py ---

@pytest.mark.parametrize(
    "metadata,user_id,expected_parts",
    [
        (
            {
                "routing": {
                    "group_id": "df0b704c-2c3f-4f6a-a998-4c03d569ec67",
                    "bot_id": "169274f4-2f12-4f6a-8993-e1a67bfb6773",
                },
                "user_id": "user1",
            },
            "user1",
            (
                "df0b704c-2c3f-4f6a-a998-4c03d569ec67",
                "169274f4-2f12-4f6a-8993-e1a67bfb6773",
                "user1",
            ),
        ),
        # routing missing (E2A loss): recover from HTTP query bag (list-valued).
        (
            {
                "query": {
                    "user_id": ["user1"],
                    "group_id": ["df0b704c-2c3f-4f6a-a998-4c03d569ec67"],
                    "bot_id": ["169274f4-2f12-4f6a-8993-e1a67bfb6773"],
                },
                "method": "history.get",
                "user_id": "user1",
            },
            "user1",
            (
                "df0b704c-2c3f-4f6a-a998-4c03d569ec67",
                "169274f4-2f12-4f6a-8993-e1a67bfb6773",
                "user1",
            ),
        ),
    ],
)
def test_extract_tenant_key_enterprise_from_routing_or_query(
    monkeypatch, metadata, user_id, expected_parts
):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    request = AgentRequest(
        request_id="r",
        channel_id="web",
        session_id="web_sid",
        user_id=user_id,
        metadata=metadata,
    )
    assert TenantAgentPool.extract_triple(request) == expected_parts
    assert TenantAgentPool.extract_tenant_key(request) == utils.build_tenant_key(
        *expected_parts
    )
    # Must not collapse to default::default::user1
    assert TenantAgentPool.extract_tenant_key(request) != utils.build_tenant_key(
        "default", "default", "user1"
    )

def test_extract_tenant_key_personal_ignores_http_query(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    request = AgentRequest(
        request_id="r",
        channel_id="web",
        user_id="user1",
        metadata={
            "query": {
                "user_id": ["user1"],
                "group_id": ["g"],
                "bot_id": ["b"],
            },
            "routing": {"group_id": "g", "bot_id": "b"},
        },
    )
    assert TenantAgentPool.extract_tenant_key(request) == utils.build_tenant_key()

