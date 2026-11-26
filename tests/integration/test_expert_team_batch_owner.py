# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify host-created owners are durable before any chat/LLM turn."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.tools.team import TeamBackend

from jiuwenswarm.server.runtime import expert_team_batch as batch
from jiuwenswarm.common.schema.agent import AgentRequest


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("primary_group", [None, "research"])
async def test_confirm_materializes_owner_in_real_sqlite_before_any_chat(monkeypatch, tmp_path, primary_group):
    from openjiuwen.agent_teams import paths
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    monkeypatch.setattr(paths, "get_openjiuwen_home", lambda: tmp_path)
    for name in ("persist_session_team_dispatch_mode", "persist_session_team_bootstrap_mode", "persist_session_swarmflow_config"):
        monkeypatch.setattr(team_helpers, name, Mock())
    monkeypatch.setattr(team_helpers, "_resolve_session_team_dispatch_mode", lambda *a, **k: "autonomous")
    monkeypatch.setattr(team_helpers, "_resolve_session_team_bootstrap_mode", lambda *a, **k: "leader")
    monkeypatch.setattr(team_helpers, "_resolve_session_swarmflow_config", lambda *a, **k: {"enable_swarmflow": False})
    monkeypatch.setattr(batch, "update_session_metadata", Mock())
    monkeypatch.setattr(batch, "_validate_package", AsyncMock())
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.extension_package_manager.resolve_agent_group_display_identity",
        lambda name: {"id": name, "name": name, "display_name": "Research experts"},
    )
    db_path = tmp_path / "owner.db"
    db = TeamDatabase(DatabaseConfig(connection_string=str(db_path)))
    token = set_session_id("batch-session")
    try:
        await db.initialize()
        await db.create_cur_session_tables()
        backend = TeamBackend("batch-owner", "leader", True, db, SimpleNamespace(publish=AsyncMock()))
        agent = SimpleNamespace(team_backend=backend, member_name="leader", stream=Mock())
        spec = SimpleNamespace(team_name="batch-owner", enable_organization=True, agents={},
                               metadata={}, leader=SimpleNamespace(prompt="Research expertise",
                                   model_copy=lambda *, update: SimpleNamespace(**update)),
                               dispatch_mode="autonomous", bootstrap_mode="leader",
                               enable_swarmflow=False, swarmflow_budget=None)
        runtime = SimpleNamespace(pool=SimpleNamespace(get=AsyncMock(return_value=None)),
                                  activate=AsyncMock(return_value=SimpleNamespace(agent=agent)),
                                  pause=AsyncMock(), stop_team=AsyncMock())
        manager = TeamManager()
        monkeypatch.setattr(manager, "get_swarm_enriched_team_spec", AsyncMock(return_value=spec))
        request = SimpleNamespace(session_id="batch-session", request_id="confirm", user_id="", channel_id="web",
                                  params={}, metadata={})
        metadata = {"mode": "team"}
        await batch._prepare_owner(request, manager, runtime, metadata, first_group=primary_group)
        assert (await db.team.get_team("batch-owner")).leader_member_name == "leader"
        if primary_group:
            assert metadata["agent_group_name"] == "research"
            assert (await db.team.get_team("batch-owner")).display_name == "Research experts"
            assert manager.get_swarm_enriched_team_spec.call_args.kwargs["agent_group_name"] == "research"
            from jiuwenswarm.server.runtime.team_organization_identity_store import load_organization_team_identity

            assert load_organization_team_identity("batch-owner")["agent_group_name"] == "research"
        agent.stream.assert_not_called()
        runtime.pause.assert_awaited_once_with(team_name="batch-owner", session_id="batch-session")
        assert not manager.is_session_initialized("batch-session")
        assert not manager.is_runtime_active("batch-session")
        assert not manager.is_runtime_pending("batch-session")
        assert not await team_helpers._team_session_has_runtime(manager, "batch-session")
        # Exercise the actual first-message router after host precreation.
        # interact() must never be used before the leader stream starts.
        monkeypatch.setattr(team_helpers, "get_team_manager", lambda _: manager)
        monkeypatch.setattr(team_helpers, "get_session_metadata", lambda *a, **k: {
            "session_id": "batch-session", "team_name": "batch-owner", "mode": "team",
            **({"agent_group_name": primary_group,
                "team_group_identity": {"id": primary_group, "name": primary_group, "display_name": "Research experts"}}
               if primary_group else {}),
        })
        monkeypatch.setattr(team_helpers, "update_session_metadata", Mock())
        monkeypatch.setattr(team_helpers, "increment_session_round_count", lambda _: 1)
        monkeypatch.setattr(team_helpers, "_persist_team_file_monitor_roots", lambda *args: None)
        monkeypatch.setattr(team_helpers, "_handle_team_slash_command", AsyncMock(return_value=None))
        direct_delivery = AsyncMock(side_effect=AssertionError("Harness has not started"))
        monkeypatch.setattr(manager, "interact", direct_delivery)
        started = []

        async def start_stream(channel_id, session_id, team_spec, query, **kwargs):
            started.append((session_id, query))
            manager.commit_runtime_ready(session_id, team_spec.team_name)
            await manager.broadcast_event(session_id, {"event_type": "chat.final", "content": "ready"})
            await manager.broadcast_event(session_id, {
                "event_type": "chat.processing_status", "is_processing": False, "is_complete": True,
            })
            manager.pop_stream_task(session_id)

        monkeypatch.setattr(team_helpers, "_consume_stream_with_query", start_stream)
        chat = AgentRequest(session_id="batch-session", request_id="first-message", channel_id="web",
                            params={"mode": "team"}, metadata={})
        chunks = [chunk async for chunk in team_helpers.process_team_message_stream(
            chat, {"query": "Give one suggestion"}, object(),
        )]
        direct_delivery.assert_not_awaited()
        assert len(started) == 1
        assert any(chunk.payload and chunk.payload.get("content") == "ready" for chunk in chunks)
        assert not any(chunk.payload and chunk.payload.get("event_type") == "chat.error" for chunk in chunks)
    finally:
        reset_session_id(token)
        await db.close()
    # A new database object is the restart boundary, not the original cache.
    reopened = TeamDatabase(DatabaseConfig(connection_string=str(db_path)))
    try:
        await reopened.initialize()
        assert (await reopened.team.get_team("batch-owner")).leader_member_name == "leader"
    finally:
        await reopened.close()
