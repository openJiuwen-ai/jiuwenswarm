from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module


class _WireWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class _AgentServer(agent_ws_server_module.AgentWebSocketServer):
    def __init__(self) -> None:
        super().__init__()
        self.team_session_ids: list[str] = []
        self._agent_manager = SimpleNamespace(
            get_agent_nowait=lambda *args, **kwargs: None,
            release_subagent_runtime_for_session=AsyncMock(return_value=False),
            cleanup_session_runtime=AsyncMock(return_value=False),
        )

    async def _ensure_persistent_checkpointer_response(self, _request):
        return None

    async def _find_team_session_ids(self, _team_name: str) -> list[str]:
        return list(self.team_session_ids)


class _KVCSession:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self._events = events
        self._fail = fail

    async def release_kvc(self) -> bool:
        self._events.append("release-kvc")
        if self._fail:
            raise RuntimeError("provider unavailable")
        return True


def _wire_response(response, *, response_id):
    return {"response_id": response_id, "payload": response.payload, "ok": response.ok}


def _enable_affinity(monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider."
        "is_kv_cache_affinity_enabled",
        lambda: enabled,
    )


def _patch_server_delete_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    sessions_root,
    *,
    mode: str,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(agent_ws_server_module, "encode_agent_response_for_wire", _wire_response)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.remove_session_metadata_cache",
        lambda _sid: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: {"mode": mode, "channel_id": "web"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("release_fails", [False, True])
async def test_plan_session_delete_releases_kvc_without_blocking_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    release_fails: bool,
) -> None:
    sessions_root = tmp_path / "sessions"
    (sessions_root / "plan-root").mkdir(parents=True)
    server = _AgentServer()
    ws = _WireWebSocket()
    events: list[str] = []

    _enable_affinity(monkeypatch)
    _patch_server_delete_dependencies(
        monkeypatch,
        sessions_root,
        mode="agent.plan",
    )
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: _KVCSession(events, fail=release_fails),
    )

    async def _release_runner(session_id: str) -> None:
        events.append(f"runner-release:{session_id}")

    monkeypatch.setattr("openjiuwen.core.runner.Runner.release", _release_runner)

    request = AgentRequest(
        request_id="delete-plan",
        channel_id="web",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "plan-root"},
    )
    await server._handle_session_delete(ws, request, asyncio.Lock())

    assert events == ["release-kvc", "runner-release:plan-root"]
    assert not (sessions_root / "plan-root").exists()
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_team_session_delete_orders_drain_kvc_and_runner_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    (sessions_root / "team-root").mkdir(parents=True)
    manager = TeamManager()
    server = _AgentServer()
    ws = _WireWebSocket()
    events: list[str] = []

    _enable_affinity(monkeypatch)
    _patch_server_delete_dependencies(monkeypatch, sessions_root, mode="team")
    monkeypatch.setattr(manager, "_resolve_delete_session_team_name", lambda _sid: "demo-team")

    async def _stop(*_args, **kwargs) -> bool:
        events.append(f"drain:{kwargs.get('stop_runner')}")
        return True

    async def _delete(**_kwargs) -> bool:
        events.append("runner-delete")
        return True

    monkeypatch.setattr(manager, "stop_session_runtime", _stop)
    monkeypatch.setattr("jiuwenswarm.agents.harness.team.get_team_manager", lambda _cid: manager)
    monkeypatch.setattr(
        "openjiuwen.core.session.agent_team.create_agent_team_session",
        lambda **_kwargs: _KVCSession(events),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.delete_agent_team",
        _delete,
    )

    request = AgentRequest(
        request_id="delete-team-session",
        channel_id="web",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "team-root"},
    )
    await server._handle_session_delete(ws, request, asyncio.Lock())

    assert events == ["drain:False", "release-kvc", "runner-delete"]
    assert not (sessions_root / "team-root").exists()
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_team_delete_releases_each_root_before_shared_runner_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    for session_id in ("team-root-1", "team-root-2"):
        (sessions_root / session_id).mkdir(parents=True)
    server = _AgentServer()
    server.team_session_ids = ["team-root-1", "team-root-2"]
    ws = _WireWebSocket()
    events: list[str] = []

    _enable_affinity(monkeypatch)
    monkeypatch.setattr(agent_ws_server_module, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(agent_ws_server_module, "encode_agent_response_for_wire", _wire_response)
    monkeypatch.setattr(agent_ws_server_module, "remove_session_metadata_cache", lambda _sid: None)

    async def _stop(session_id: str, **kwargs) -> bool:
        events.append(f"drain:{session_id}:{kwargs.get('stop_runner')}")
        return True

    async def _delete(**_kwargs) -> bool:
        events.append("runner-delete")
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.stop_team_session_runtime_across_managers",
        _stop,
    )
    monkeypatch.setattr(
        "openjiuwen.core.session.agent_team.create_agent_team_session",
        lambda **kwargs: _KVCSession(events),
    )
    monkeypatch.setattr("openjiuwen.core.runner.Runner.delete_agent_team", _delete)

    request = AgentRequest(
        request_id="delete-team",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "demo-team"},
    )
    await server._handle_team_delete(ws, request, asyncio.Lock())

    assert events == [
        "drain:team-root-1:False",
        "release-kvc",
        "drain:team-root-2:False",
        "release-kvc",
        "runner-delete",
    ]
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_disabled_team_delete_keeps_original_stop_behavior(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    (sessions_root / "team-root").mkdir(parents=True)
    server = _AgentServer()
    server.team_session_ids = ["team-root"]
    ws = _WireWebSocket()
    stop_kwargs: list[dict] = []

    _enable_affinity(monkeypatch, False)
    monkeypatch.setattr(agent_ws_server_module, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(agent_ws_server_module, "encode_agent_response_for_wire", _wire_response)
    monkeypatch.setattr(agent_ws_server_module, "remove_session_metadata_cache", lambda _sid: None)

    async def _stop(_session_id: str, **kwargs) -> bool:
        stop_kwargs.append(kwargs)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.stop_team_session_runtime_across_managers",
        _stop,
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.delete_agent_team",
        AsyncMock(return_value=True),
    )

    request = AgentRequest(
        request_id="delete-team",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "demo-team"},
    )
    await server._handle_team_delete(ws, request, asyncio.Lock())

    assert stop_kwargs == [{"reason": "team.delete: "}]
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_plain_disconnect_does_not_release_session_kvc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = agent_ws_server_module.AgentWebSocketServer()
    server._agent_manager = SimpleNamespace(
        cancel_all_inflight_work=AsyncMock(return_value=None),
    )
    server._stop_scheduler = AsyncMock(return_value=None)

    class EmptyWebSocket:
        remote_address = ("127.0.0.1", 10000)

        async def send(self, _payload: str) -> None:
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: pytest.fail("disconnect must not release Session KVC"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.cancel_all_team_stream_tasks_across_managers",
        AsyncMock(return_value=None),
    )

    await server._connection_handler(EmptyWebSocket())
