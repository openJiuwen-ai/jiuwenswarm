from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
    CLI_FORWARD_NO_LOCAL_HANDLER_METHODS,
    CLI_FORWARD_REQ_METHODS,
    CliHandlersBindParams,
    register_cli_handlers,
)


class _TuiChannel:
    channel_id = "tui"

    def __init__(self) -> None:
        self.local_handlers: dict[str, dict[str, object]] = {}
        self.responses: list[dict] = []

    def register_local_handler(self, path, method, handler) -> None:
        self.local_handlers.setdefault(path, {})[method] = handler

    async def send_response(self, _ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload or {}, "error": error, "code": code}
        )


class _SuccessfulAgentClient:
    def __init__(self, session_id: str = "tui_allocated") -> None:
        self.session_id = session_id
        self.requests: list[object] = []

    async def send_request(self, request):
        self.requests.append(request)
        is_external_create = bool(request.params.get("session_id"))
        return SimpleNamespace(
            ok=True,
            payload={
                "sessionId": self.session_id,
                "session_id": self.session_id,
                "projectId": "default_code",
                "projectDir": "",
                "workMode": "code",
                "prewarm_hit": not is_external_create,
                "prewarm_status": "bypassed" if is_external_create else "ready",
            },
        )


@pytest.mark.asyncio
async def test_session_create_forwards_external_id_without_prewarm() -> None:
    channel = _TuiChannel()
    agent_client = _SuccessfulAgentClient("tui_external_001")
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=agent_client, path="/tui")
    )

    await channel.local_handlers["/tui"]["session.create"](
        object(),
        "register-tui",
        {
            "session_id": "tui_external_001",
            "mode": "code.normal",
        },
        "tui_external_001",
    )

    assert len(agent_client.requests) == 1
    request = agent_client.requests[0]
    assert request.method == "session.create"
    assert request.params["session_id"] == "tui_external_001"
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["prewarm_status"] == "bypassed"


@pytest.mark.asyncio
async def test_tui_session_create_leaves_project_resolution_to_agentserver(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    project_dir = str(tmp_path / "workspace")
    agent_client = _SuccessfulAgentClient("tui_project_bound")
    channel = _TuiChannel()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.find_or_create_code_project_for_tui_params",
        lambda _params: pytest.fail("Gateway must not own non-explicit project binding"),
    )
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=agent_client, path="/tui")
    )
    await channel.local_handlers["/tui"]["session.create"](
        object(),
        "allocate-project",
        {"project_dir": project_dir, "mode": "code.normal"},
        "previous",
    )

    request = agent_client.requests[0]
    assert "project_id" not in request.params
    assert request.params["project_dir"] == project_dir
    assert "session_id" not in request.params
    assert request.params.get("create_token")


@pytest.mark.asyncio
async def test_tui_explicit_session_create_leaves_project_resolution_to_agentserver(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    project_dir = str(tmp_path / "different-workspace")
    channel = _TuiChannel()
    agent_client = _SuccessfulAgentClient("tui_existing")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.find_or_create_code_project_for_tui_params",
        lambda _params: pytest.fail("Gateway must not own explicit-ID project binding"),
    )
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=agent_client, path="/tui")
    )

    await channel.local_handlers["/tui"]["session.create"](
        object(),
        "register-existing",
        {"session_id": "tui_existing", "project_dir": project_dir},
        "tui_existing",
    )

    assert channel.responses[-1]["ok"] is True
    assert agent_client.requests[0].params["session_id"] == "tui_existing"
    assert agent_client.requests[0].params["project_dir"] == project_dir
    assert "project_id" not in agent_client.requests[0].params


def test_session_switch_is_forwarded_without_a_tui_local_handler() -> None:
    channel = _TuiChannel()
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=None, path="/tui")
    )

    assert "session.switch" in CLI_FORWARD_REQ_METHODS
    assert "session.switch" in CLI_FORWARD_NO_LOCAL_HANDLER_METHODS
    assert "session.switch" not in channel.local_handlers.get("/tui", {})


@pytest.mark.asyncio
async def test_session_create_forwards_previous_plan_root_to_agentserver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    channel = _TuiChannel()
    agent_client = _SuccessfulAgentClient("new-plan-root")
    calls: list[dict] = []

    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda session_id: {"mode": "agent.plan"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.init_session_metadata",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider.is_kv_cache_affinity_enabled",
        lambda: True,
    )
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=agent_client, path="/tui")
    )

    await channel.local_handlers["/tui"]["session.create"](
        object(),
        "create-tui",
        {
            "previous_session_id": "old-plan-root",
            "create_token": "tui-plan-create",
        },
        "old-plan-root",
    )

    assert calls == []
    assert len(agent_client.requests) == 1
    assert agent_client.requests[0].method == "session.create"
    assert agent_client.requests[0].params["previous_session_id"] == "old-plan-root"
    assert "session_id" not in agent_client.requests[0].params
    assert channel.responses[-1]["payload"]["session_id"] == "new-plan-root"
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_session_create_does_not_apply_plan_root_action_to_team_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    channel = _TuiChannel()
    agent_client = _SuccessfulAgentClient("new-team-root")
    calls: list[dict] = []

    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda session_id: {"mode": "team"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.init_session_metadata",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider.is_kv_cache_affinity_enabled",
        lambda: True,
    )
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=agent_client, path="/tui")
    )

    await channel.local_handlers["/tui"]["session.create"](
        object(),
        "create-tui-team-owner",
        {
            "previous_session_id": "old-team-root",
            "mode": "team",
            "create_token": "tui-team-create",
        },
        "old-team-root",
    )

    assert calls == []
    assert agent_client.requests[0].method == "session.create"
    assert agent_client.requests[0].params["mode"] == "team"
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_session_create_skips_kvc_metadata_when_affinity_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    channel = _TuiChannel()
    agent_client = _SuccessfulAgentClient("new-plan-root")

    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.init_session_metadata",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda session_id: pytest.fail("disabled affinity must not read previous metadata"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider.is_kv_cache_affinity_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **kwargs: pytest.fail("disabled affinity must not dispatch offload"),
    )
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=agent_client, path="/tui")
    )

    await channel.local_handlers["/tui"]["session.create"](
        object(),
        "create-tui-disabled",
        {
            "previous_session_id": "old-plan-root",
            "create_token": "tui-affinity-disabled",
        },
        "old-plan-root",
    )

    assert not (sessions_root / "new-plan-root").exists()
    assert agent_client.requests[0].method == "session.create"
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_session_create_contains_kvc_metadata_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    channel = _TuiChannel()
    agent_client = _SuccessfulAgentClient("new-plan-root")

    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.init_session_metadata",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda session_id: (_ for _ in ()).throw(RuntimeError("metadata broken")),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider.is_kv_cache_affinity_enabled",
        lambda: True,
    )
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=agent_client, path="/tui")
    )

    await channel.local_handlers["/tui"]["session.create"](
        object(),
        "create-tui-kvc-failure",
        {
            "previous_session_id": "old-plan-root",
            "create_token": "tui-kvc-failure",
        },
        "old-plan-root",
    )

    assert not (sessions_root / "new-plan-root").exists()
    assert agent_client.requests[0].method == "session.create"
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_session_create_prefers_canonical_switch_owner_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    channel = _TuiChannel()
    agent_client = _SuccessfulAgentClient("new-team-root")

    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.init_session_metadata",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **kwargs: pytest.fail("forwarded lifecycle must not also run local fallback"),
    )
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=agent_client, path="/tui")
    )

    await channel.local_handlers["/tui"]["session.create"](
        object(),
        "create-tui-forwarded",
        {
            "previous_session_id": "old-team-root",
            "mode": "team",
            "previous_mode": "team",
            "create_token": "tui-forwarded-team",
        },
        "old-team-root",
    )

    assert len(agent_client.requests) == 1
    request = agent_client.requests[0]
    assert request.method == "session.create"
    assert "session_id" not in request.params
    assert request.params["previous_session_id"] == "old-team-root"
    assert request.params["mode"] == "team"
    assert request.params["previous_mode"] == "team"
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["session_id"] == "new-team-root"
