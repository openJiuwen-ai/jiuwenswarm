from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
    CliHandlersBindParams,
    register_cli_handlers,
)
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _register_web_handlers,
)
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module


class _WireWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class _WebChannel:
    def __init__(self) -> None:
        self.methods: dict[str, object] = {}
        self.responses: list[dict] = []

    def register_method(self, name, handler) -> None:
        self.methods[name] = handler

    def on_connect(self, _handler) -> None:
        return None

    async def send_response(self, _ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload, "error": error, "code": code}
        )


class _TuiChannel:
    def __init__(self) -> None:
        self.local_handlers: dict[str, dict[str, object]] = {}
        self.responses: list[dict] = []

    def register_local_handler(self, path, method, handler) -> None:
        self.local_handlers.setdefault(path, {})[method] = handler

    async def send_response(self, _ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload or {}, "error": error, "code": code}
        )


class _FailingAgentClient:
    server_ready = True

    async def send_request(self, _request):
        raise RuntimeError("agent server unavailable")


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
        agent_ws_server_module, "get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(agent_ws_server_module, "encode_agent_response_for_wire", _wire_response)
    monkeypatch.setattr(
        agent_ws_server_module, "remove_session_metadata_cache",
        lambda _sid: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: {"mode": mode, "channel_id": "web"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_agentos_delete_only_drains_for_enabled_kvc(monkeypatch, tmp_path, enabled):
    sessions_root = tmp_path / "sessions"
    (sessions_root / "session-a").mkdir(parents=True)
    server = _AgentServer()
    ws = _WireWebSocket()
    events = []
    _enable_affinity(monkeypatch, enabled)
    _patch_server_delete_dependencies(monkeypatch, sessions_root, mode="code.normal")

    async def drain(**_kwargs):
        events.append("drain")
        return True

    async def release(_session_id):
        events.append("runner-release")

    server._agent_manager.cleanup_session_runtime = drain
    monkeypatch.setattr("openjiuwen.core.runner.Runner.release", release)
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: _KVCSession(events),
    )
    request = AgentRequest(
        request_id="delete", channel_id="web", req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "session-a"},
    )
    await server._handle_session_delete(ws, request, asyncio.Lock())
    assert events == (["drain", "release-kvc", "runner-release"] if enabled else ["runner-release"])
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_agentos_shutdown_closes_kvc_after_connection_drain(monkeypatch):
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_application_runtime, kv_cache_product_hooks

    server = _AgentServer()
    events = []

    class Listener:
        def close(self):
            events.append("stop-admission")

        async def wait_closed(self):
            events.append("connections-drained")

    async def close_runtime():
        events.append("close-kvc")

    server._server = Listener()
    server._jiuwenbox_runner = SimpleNamespace(stop=AsyncMock())
    monkeypatch.setattr(kv_cache_product_hooks, "cancel_pending_tasks", AsyncMock())
    monkeypatch.setattr(kv_cache_application_runtime, "close_kv_cache_runtime", close_runtime)
    await server.stop()
    assert events == ["stop-admission", "connections-drained", "close-kvc"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["cancel_pending_tasks", "close_kv_cache_runtime"])
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_agentos_kvc_shutdown_failure_still_stops_business_runner(monkeypatch, stage, error_type):
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_application_runtime, kv_cache_product_hooks

    server = _AgentServer()
    server._server = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
    server._jiuwenbox_runner = SimpleNamespace(stop=AsyncMock())
    cancel_tasks = AsyncMock()
    close_runtime = AsyncMock()
    failing = cancel_tasks if stage == "cancel_pending_tasks" else close_runtime
    failing.side_effect = error_type("cleanup interrupted")
    monkeypatch.setattr(kv_cache_product_hooks, "cancel_pending_tasks", cancel_tasks)
    monkeypatch.setattr(kv_cache_application_runtime, "close_kv_cache_runtime", close_runtime)

    if error_type is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await server.stop()
    else:
        await server.stop()
        close_runtime.assert_awaited_once()
    server._jiuwenbox_runner.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_fails", [False, True])
async def test_local_session_delete_preserves_off_and_kvc_failure(monkeypatch, tmp_path, release_fails):
    from jiuwenswarm.server.runtime.gateway_adapter.session_adapter import SessionAdapter

    (tmp_path / "local-session").mkdir()
    events = []
    # OFF must not construct a KVC Session; ON must tolerate a failed release.
    _enable_affinity(monkeypatch, release_fails)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata", lambda _sid: {}
    )

    def factory(**_kwargs):
        assert release_fails, "OFF must not construct a KVC Session"
        return _KVCSession(events, fail=True)

    monkeypatch.setattr("openjiuwen.core.session.agent.create_agent_session", factory)
    request = AgentRequest(
        request_id="local-delete", channel_id="web", req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "local-session"},
    )
    response = await SessionAdapter().handle(request)
    assert response.ok is True
    assert not (tmp_path / "local-session").exists()
    assert events == (["release-kvc"] if release_fails else [])


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
@pytest.mark.parametrize("release_fails", [False, True])
async def test_team_session_delete_orders_drain_kvc_and_runner_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    release_fails: bool,
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
        lambda **_kwargs: _KVCSession(events, fail=release_fails),
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
@pytest.mark.parametrize("release_fails", [False, True])
async def test_team_delete_releases_each_root_before_shared_runner_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    release_fails: bool,
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
        lambda **kwargs: _KVCSession(events, fail=release_fails),
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

@pytest.mark.asyncio
@pytest.mark.parametrize("channel_kind", ["web", "tui"])
async def test_session_delete_unavailable_returns_service_unavailable_without_local_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    channel_kind: str,
) -> None:
    """Phase 2(决策 D8): session.delete 只走 E2A,AgentServer 不可达返回可重试错误,
    不再本地 fallback(不以部署侧目录代替用户目录执行删除)。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "unavail-root").mkdir(parents=True)

    if channel_kind == "web":
        channel = _WebChannel()
        _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=None))
        handler = channel.methods["session.delete"]
    else:
        channel = _TuiChannel()
        register_cli_handlers(CliHandlersBindParams(channel=channel, agent_client=None, path="/tui"))
        handler = channel.local_handlers["/tui"]["session.delete"]

    await handler(object(), "delete-unavail", {"session_id": "unavail-root"}, "unavail-root")

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "SERVICE_UNAVAILABLE"
    assert (sessions_root / "unavail-root").exists()
