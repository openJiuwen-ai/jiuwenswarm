"""session.create project_store cache_bust reads must leave the event loop."""

from __future__ import annotations

import asyncio
import threading
import types

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.handlers import bootstrap as bootstrap_mod
from jiuwenswarm.server.runtime.session import project_store
from tests.unit_tests.agentserver.test_agentserver_acp import (
    AgentWebSocketServerHarness,
    FakeAgentManager,
    FakeWebSocket,
    fake_encode_agent_response_for_wire,
    patch_session_roots,
    patch_shared_name,
)


def _code_project(tmp_path):
    return types.SimpleNamespace(
        project_id="proj_code",
        project_dir=str(tmp_path / "code_proj"),
        work_mode="code",
        hidden=False,
    )


@pytest.mark.asyncio
async def test_session_create_project_store_reads_go_through_to_thread(
    monkeypatch, tmp_path
):
    """binding + second cache_bust must go through asyncio.to_thread."""
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="sess_offload_proj")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()

    patch_shared_name(
        monkeypatch, "encode_agent_response_for_wire", fake_encode_agent_response_for_wire
    )
    patch_session_roots(monkeypatch, tmp_path / "sessions")

    code_project = _code_project(tmp_path)
    loop_ident = threading.get_ident()
    to_thread_targets: list[object] = []
    naked_on_loop: list[str] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func, /, *args, **kwargs):
        to_thread_targets.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(bootstrap_mod.asyncio, "to_thread", spy_to_thread)

    def wrapped_resolve(_project_id, _project_dir):
        if threading.get_ident() == loop_ident:
            naked_on_loop.append("resolve_session_project_binding")
        return (
            "proj_code",
            code_project.project_dir,
            None,
            None,
        )

    def wrapped_get(project_id, cache_bust=False):
        if threading.get_ident() == loop_ident and cache_bust:
            naked_on_loop.append("get_project_by_id")
        if project_id == "proj_code":
            return code_project
        return None

    monkeypatch.setattr(
        project_store, "resolve_session_project_binding", wrapped_resolve
    )
    monkeypatch.setattr(project_store, "get_project_by_id", wrapped_get)

    request = AgentRequest(
        request_id="req-session-create-offload",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={
            "project_id": "proj_code",
            "work_mode": "work",
            "_work_mode_explicit": False,
            "create_token": "create-offload",
        },
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent and fake_ws.sent[0]["ok"] is True
    assert any(
        getattr(fn, "__name__", "") == "_load_session_create_project_snapshot"
        or getattr(fn, "__qualname__", "").endswith(
            "_load_session_create_project_snapshot"
        )
        for fn in to_thread_targets
    ), "project snapshot must go through asyncio.to_thread"
    assert naked_on_loop == [], f"naked project_store on loop: {naked_on_loop}"


@pytest.mark.asyncio
async def test_session_create_deleted_race_still_not_found(monkeypatch, tmp_path):
    """resolve ok + second get None → NOT_FOUND (bootstrap :186 equivalent)."""
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="sess_deleted_race")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()

    patch_shared_name(
        monkeypatch, "encode_agent_response_for_wire", fake_encode_agent_response_for_wire
    )
    patch_session_roots(monkeypatch, tmp_path / "sessions")

    code_project = _code_project(tmp_path)

    monkeypatch.setattr(
        project_store,
        "resolve_session_project_binding",
        lambda project_id, project_dir: (
            "proj_code",
            code_project.project_dir,
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: None,
    )

    request = AgentRequest(
        request_id="req-session-create-deleted-race",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={
            "project_id": "proj_code",
            "create_token": "create-deleted-race",
        },
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_manager.claim_session_calls == []
    assert len(fake_ws.sent) == 1
    assert fake_ws.sent[0]["ok"] is False
    assert fake_ws.sent[0]["payload"]["code"] == "NOT_FOUND"
    assert "project not found: proj_code" in fake_ws.sent[0]["payload"]["error"]


@pytest.mark.asyncio
async def test_session_create_binding_failure_does_not_pop_work_mode_explicit(
    monkeypatch, tmp_path
):
    """Binding error early-return must not consume _work_mode_explicit marker."""
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="sess_bind_fail")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()

    patch_shared_name(
        monkeypatch, "encode_agent_response_for_wire", fake_encode_agent_response_for_wire
    )
    patch_session_roots(monkeypatch, tmp_path / "sessions")

    monkeypatch.setattr(
        project_store,
        "resolve_session_project_binding",
        lambda project_id, project_dir: (
            "",
            "",
            "project not found",
            "NOT_FOUND",
        ),
    )

    request = AgentRequest(
        request_id="req-session-create-bind-fail",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={
            "project_id": "proj_missing",
            "work_mode": "work",
            "_work_mode_explicit": True,
            "create_token": "create-bind-fail",
        },
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_manager.claim_session_calls == []
    assert fake_ws.sent[0]["ok"] is False
    assert fake_ws.sent[0]["payload"]["code"] == "NOT_FOUND"
    assert request.params.get("_work_mode_explicit") is True


@pytest.mark.asyncio
async def test_session_create_work_mode_mismatch_still_bad_request(
    monkeypatch, tmp_path
):
    """Explicit work_mode vs project work_mode → BAD_REQUEST on the loop."""
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="sess_wm_mismatch")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()

    patch_shared_name(
        monkeypatch, "encode_agent_response_for_wire", fake_encode_agent_response_for_wire
    )
    patch_session_roots(monkeypatch, tmp_path / "sessions")

    code_project = _code_project(tmp_path)

    monkeypatch.setattr(
        project_store,
        "resolve_session_project_binding",
        lambda project_id, project_dir: (
            "proj_code",
            code_project.project_dir,
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: (
            code_project if project_id == "proj_code" else None
        ),
    )

    request = AgentRequest(
        request_id="req-session-create-wm-mismatch",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={
            "project_id": "proj_code",
            "work_mode": "work",
            "_work_mode_explicit": True,
            "create_token": "create-wm-mismatch",
        },
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_manager.claim_session_calls == []
    assert fake_ws.sent[0]["ok"] is False
    assert fake_ws.sent[0]["payload"]["code"] == "BAD_REQUEST"
    assert "work_mode mismatch" in fake_ws.sent[0]["payload"]["error"]
