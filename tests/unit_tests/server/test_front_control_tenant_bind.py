"""Front control plane must bind TenantContext so history/session match chat writes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.common import utils
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.admission import ExecutionAdmission
from jiuwenswarm.server.front.router import MethodRouter, _load_control_services
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext


def _enterprise_history_request(session_id: str = "web_sid") -> AgentRequest:
    return AgentRequest(
        request_id="r1",
        channel_id="web",
        session_id=session_id,
        user_id="user1",
        req_method=ReqMethod.HISTORY_GET,
        is_stream=True,
        params={"session_id": session_id, "cursor": None, "limit": 50},
        metadata={
            "routing": {"group_id": "g1", "bot_id": "b1"},
            "user_id": "user1",
            "query": {
                "user_id": ["user1"],
                "group_id": ["g1"],
                "bot_id": ["b1"],
            },
        },
    )


@pytest.mark.asyncio
async def test_front_history_stream_reads_enterprise_tenant_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)

    expected = utils.build_tenant_key("g1", "b1", "user1")
    wrong = utils.build_tenant_key("default", "default", "user1")
    seen: dict[str, object] = {}

    def _load_query(params):
        seen["tenant_key"] = TenantContext.current_tenant_key()
        seen["sessions_dir"] = str(utils.get_agent_sessions_dir())
        seen["session_id"] = params.get("session_id")
        return {
            "messages": [{"role": "user", "content": "hi"}],
            "next_cursor": None,
            "has_more": False,
            "snapshot_id": "snap",
            "snapshot_end": 12,
        }

    sent: list[dict] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", _send)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router.encode_chunk",
        lambda chunk, *, response_id, sequence: {
            "response_id": response_id,
            "sequence": sequence,
            "payload": chunk.payload,
            "is_complete": chunk.is_complete,
        },
    )
    _load_control_services()
    monkeypatch.setattr("jiuwenswarm.server.front.router._LOAD_HISTORY_QUERY", _load_query)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router._LOAD_HISTORY_TODO_SNAPSHOT",
        lambda _sid: [],
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router._STREAM_HISTORY_RECORDS",
        lambda messages, *, use_split: messages,
    )

    router = MethodRouter(Readiness(), ExecutionAdmission(Readiness()))
    await router.dispatch(object(), _enterprise_history_request(), asyncio.Lock())

    assert seen["tenant_key"] == expected
    assert expected in str(seen["sessions_dir"])
    assert wrong not in str(seen["sessions_dir"])
    assert any(
        p.get("payload", {}).get("status") == "done"
        and p.get("payload", {}).get("snapshot_end") == 12
        for p in sent
    )
    TenantContext.clear()


@pytest.mark.asyncio
async def test_front_session_list_binds_enterprise_tenant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)

    expected = utils.build_tenant_key("g1", "b1", "user1")
    seen: dict[str, object] = {}

    async def _handle_session(request):
        _ = request
        seen["tenant_key"] = TenantContext.current_tenant_key()
        seen["sessions_dir"] = str(utils.get_agent_sessions_dir())
        from types import SimpleNamespace

        return SimpleNamespace(
            request_id="r1",
            channel_id="web",
            ok=True,
            payload={"sessions": []},
        )

    sent: list[dict] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", _send)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router.encode_response",
        lambda response, *, response_id: {
            "response_id": response_id,
            "ok": response.ok,
            "payload": response.payload,
        },
    )
    _load_control_services()
    monkeypatch.setattr("jiuwenswarm.server.front.router._HANDLE_SESSION", _handle_session)

    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        user_id="user1",
        req_method=ReqMethod.SESSION_LIST,
        params={},
        metadata={"routing": {"group_id": "g1", "bot_id": "b1"}, "user_id": "user1"},
    )
    router = MethodRouter(Readiness(), ExecutionAdmission(Readiness()))
    await router.dispatch(object(), request, asyncio.Lock())

    assert seen["tenant_key"] == expected
    assert expected in str(seen["sessions_dir"])
    TenantContext.clear()


@pytest.mark.asyncio
async def test_front_history_stream_personal_keeps_default_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)

    seen: dict[str, object] = {}

    def _load_query(_params):
        seen["tenant_key"] = TenantContext.current_tenant_key()
        seen["agent_root_cv"] = TenantContext.current_agent_root()
        seen["sessions_dir"] = str(utils.get_agent_sessions_dir())
        return {
            "messages": [],
            "next_cursor": None,
            "has_more": False,
            "snapshot_id": None,
            "snapshot_end": 0,
        }

    async def _send(ws, payload) -> bool:
        _ = ws, payload
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", _send)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router.encode_chunk",
        lambda chunk, *, response_id, sequence: {
            "payload": chunk.payload,
            "is_complete": chunk.is_complete,
        },
    )
    _load_control_services()
    monkeypatch.setattr("jiuwenswarm.server.front.router._LOAD_HISTORY_QUERY", _load_query)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router._LOAD_HISTORY_TODO_SNAPSHOT",
        lambda _sid: [],
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router._STREAM_HISTORY_RECORDS",
        lambda messages, *, use_split: messages,
    )

    router = MethodRouter(Readiness(), ExecutionAdmission(Readiness()))
    await router.dispatch(object(), _enterprise_history_request(), asyncio.Lock())

    assert seen["tenant_key"] == utils.build_tenant_key()
    assert seen["agent_root_cv"] is None
    sessions_dir = str(seen["sessions_dir"])
    assert "workspace_" not in sessions_dir
    TenantContext.clear()
