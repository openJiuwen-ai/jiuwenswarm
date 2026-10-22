"""Group proxies use real specs/tools, with no network or model calls."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from jiuwenswarm.agents.swarm import group_chat as gc
from tests.agents.swarm.agent_group_fixtures import _minimal_group


def spec():
    return TeamAgentSpec(
        agents={"leader": DeepAgentSpec(), "teammate": DeepAgentSpec()},
        team_name="public-group",
        leader=LeaderSpec(member_name="host"),
    )


def test_proxy_roster_is_lazy_and_survives_serialization(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime import extension_package_manager as packages

    monkeypatch.setattr(
        packages, "resolve_agent_group_dir", lambda _: _minimal_group(tmp_path)
    )
    outer = spec()
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    import threading

    outer.build_context = SwarmBuildContext(trajectory_span_processor=threading.Lock())
    gc.assemble_group_members(
        outer,
        [{"kind": "expert_team", "definition_id": "group", "member_name": "research"}],
    )
    assert [m.member_name for m in outer.predefined_members] == ["research"]
    restored = TeamAgentSpec.model_validate_json(
        outer.model_dump_json(exclude={"build_context"})
    )
    tool = next(t for t in restored.agents["research"].tools if t.type == gc.RUN_TEAM)
    child = TeamAgentSpec.model_validate(tool.params["child_spec"])
    assert [m.member_name for m in child.predefined_members] == ["member1"]
    assert child.spawn_mode == "inprocess"
    assert not getattr(child, "enable_organization", False)
    assert "run_team" in restored.predefined_members[0].prompt


def test_invalid_roster_does_not_partially_modify_spec(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime import extension_package_manager as packages

    monkeypatch.setattr(
        packages, "resolve_agent_group_dir", lambda _: _minimal_group(tmp_path)
    )
    outer = spec()
    before = outer.model_dump()
    with pytest.raises(ValueError, match="unique"):
        gc.assemble_group_members(
            outer,
            [
                {
                    "kind": "expert_team",
                    "definition_id": "group",
                    "member_name": "research",
                },
                {
                    "kind": "expert_team",
                    "definition_id": "group",
                    "member_name": "research",
                },
            ],
        )
    assert outer.model_dump() == before


def test_existing_expert_capabilities_are_kept(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime import extension_package_manager as packages

    group = _minimal_group(tmp_path)
    monkeypatch.setattr(
        packages, "resolve_agent_template_dir", lambda _: group / "agents" / "leader"
    )
    outer = spec()
    gc.assemble_group_members(
        outer, [{"kind": "expert", "definition_id": "one", "member_name": "advisor"}]
    )
    assert outer.agents["advisor"].agent_template_spec
    assert "Leader rules" in outer.predefined_members[0].prompt
    assert not any(
        getattr(t, "type", None) == gc.RUN_TEAM
        for t in (outer.agents["advisor"].tools or [])
    )


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    from jiuwenswarm.agents.swarm import assembly

    monkeypatch.setattr(assembly, "enrich_team_spec_for_swarm", lambda *a, **kw: None)
    backend = SimpleNamespace(
        conversation_mode="group_chat",
        team_name="public-group",
        member_name="research",
        group_conversation=AsyncMock(
            return_value=SimpleNamespace(history_path=tmp_path / "history.jsonl")
        ),
        append_group_message=AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(message_id="result-message")
            )
        ),
    )
    context = SimpleNamespace(
        session_id="outer-session",
        mode="team",
        project_dir=None,
        trusted_dirs=None,
        user_id="user",
        channel_id="web",
    )
    pause = AsyncMock()
    monkeypatch.setattr(gc.Runner, "pause_agent_team", pause)
    return gc.RunTeam(
        backend, context, spec().model_dump(mode="json", exclude={"build_context"})
    )


@pytest.mark.asyncio
async def test_run_submit_publish_and_retry_without_reexecution(proxy, monkeypatch):
    calls = []

    async def stream(child, inputs, *, session):
        calls.append((child, session))
        submit = next(
            t for t in child.agents["leader"].tools if t.type == gc.SUBMIT_RESULT
        )
        assert (
            await gc.SubmitTeamResult(submit.params["key"]).invoke(
                {"result": "final report"}
            )
        ).success
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    output = await proxy.invoke({"run_id": "job-1", "task": "research"})
    assert output.success and output.data["posted_to_group"]
    assert calls[0][0].team_name != proxy.backend.team_name
    assert calls[0][1] != proxy.context.session_id
    assert proxy.backend.append_group_message.call_args.args == (
        "research",
        "final report",
    )
    repeat = await proxy.invoke({"run_id": "job-1", "task": "research"})
    assert repeat.success and len(calls) == 1
    assert not gc._results
    conflict = await proxy.invoke({"run_id": "job-1", "task": "another task"})
    assert not conflict.success


@pytest.mark.asyncio
async def test_publish_failure_retries_saved_result(proxy, monkeypatch):
    async def stream(child, inputs, *, session):
        key = next(
            t.params["key"]
            for t in child.agents["leader"].tools
            if t.type == gc.SUBMIT_RESULT
        )
        await gc.SubmitTeamResult(key).invoke({"result": "saved result"})
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    proxy.backend.append_group_message.side_effect = [
        OSError("offline"),
        SimpleNamespace(message=SimpleNamespace(message_id="fixed")),
    ]
    assert not (await proxy.invoke({"run_id": "one", "task": "task"})).success
    assert (await proxy.invoke({"run_id": "one", "task": "task"})).success


@pytest.mark.asyncio
async def test_stream_without_submission_fails_and_can_retry(proxy, monkeypatch):
    async def stream(*args, **kwargs):
        yield SimpleNamespace(payload={"event_type": "team.completed"})

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    result = await proxy.invoke({"run_id": "one", "task": "task"})
    assert not result.success and "without submitting" in result.error
    proxy.backend.append_group_message.assert_not_awaited()
    conversation = await proxy.backend.group_conversation()
    state = json.loads(
        next(
            (conversation.history_path.parent / "team-runs").glob("*.json")
        ).read_text()
    )
    assert state["status"] == "interrupted"


@pytest.mark.asyncio
async def test_cancellation_stops_private_worker(proxy, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def stream(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    task = asyncio.create_task(proxy.invoke({"run_id": "one", "task": "task"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set() and not gc._results
    proxy.backend.append_group_message.assert_not_awaited()


def test_roster_restore_rejects_replacement(monkeypatch):
    from jiuwenswarm.server.runtime.session import session_metadata

    members = [{"kind": "expert", "definition_id": "one", "member_name": "advisor"}]
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        lambda *a, **kw: {"group_members": members},
    )
    assert gc.resolve_group_members("session", None, is_first_request=True) == members
    with pytest.raises(ValueError, match="replaced"):
        gc.resolve_group_members("session", [], is_first_request=False)


def test_roster_is_persisted_in_session_metadata(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_metadata as sm

    monkeypatch.setattr(sm, "get_agent_sessions_dir", lambda: tmp_path)
    members = [
        {"kind": "expert_team", "definition_id": "team", "member_name": "research"}
    ]
    sm.update_session_metadata(
        session_id="group-roster-test",
        group_members=members,
        touch_last_message_at=False,
        sync_write=True,
    )
    sm.remove_session_metadata_cache("group-roster-test")
    assert (
        sm.get_session_metadata("group-roster-test", enable_writeback=False)[
            "group_members"
        ]
        == members
    )


def test_proxy_tool_provider_is_registered_with_serializable_binding(proxy):
    from jiuwenswarm.agents.swarm.registry import register_swarm_providers
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from openjiuwen.agent_teams.rails.team_context import inject_team_handles
    from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec

    register_swarm_providers()
    context = SwarmBuildContext(session_id="outer-session")
    inject_team_handles(context.extras, team_backend=proxy.backend)
    tool = BuiltinToolSpec(
        type=gc.RUN_TEAM, params={"child_spec": proxy.child_spec}
    ).build(language="cn", context=context)
    assert isinstance(tool, gc.RunTeam)
    assert tool.backend is proxy.backend


@pytest.mark.asyncio
async def test_result_reaches_real_group_database_and_history(tmp_path, monkeypatch):
    from openjiuwen.agent_teams.context import set_session_id, reset_session_id
    from openjiuwen.agent_teams.paths import (
        set_task_openjiuwen_home,
        reset_task_openjiuwen_home,
    )
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
    from openjiuwen.agent_teams.tools.team import TeamBackend
    from jiuwenswarm.agents.swarm import assembly

    home = set_task_openjiuwen_home(tmp_path / "home")
    token = set_session_id("outer-session")
    db = TeamDatabase(DatabaseConfig(connection_string=":memory:"))
    try:
        await db.initialize()
        await db.team.create_team("public-group", "Public group", "host")
        await db.member.create_member(
            "research", "public-group", "Research", "{}", "ready", role="teammate"
        )
        backend = TeamBackend("public-group", "research", False, db, AsyncMock())
        backend.conversation_mode = "group_chat"
        backend.group_chat_spec = spec()
        backend.bind_group_session("outer-session")
        context = SimpleNamespace(
            session_id="outer-session",
            mode="team",
            project_dir=None,
            trusted_dirs=None,
            user_id="user",
            channel_id="web",
        )
        proxy = gc.RunTeam(
            backend, context, spec().model_dump(mode="json", exclude={"build_context"})
        )
        monkeypatch.setattr(
            assembly, "enrich_team_spec_for_swarm", lambda *a, **kw: None
        )
        monkeypatch.setattr(gc.Runner, "pause_agent_team", AsyncMock())

        async def stream(child, inputs, *, session):
            key = next(
                t.params["key"]
                for t in child.agents["leader"].tools
                if t.type == gc.SUBMIT_RESULT
            )
            await gc.SubmitTeamResult(key).invoke({"result": "expert report"})
            await asyncio.Event().wait()
            yield None

        monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
        for _ in range(2):
            assert (
                await proxy.invoke({"run_id": "stable", "task": "research"})
            ).success
        rows = await db.message.get_team_messages("public-group", broadcast=True)
        assert len(rows) == 1
        assert (
            rows[0].from_member_name == "research"
            and rows[0].content == "expert report"
        )
        history = (await backend.group_conversation()).history_path
        assert [
            json.loads(line)["content"] for line in history.read_text().splitlines()
        ] == ["expert report"]
    finally:
        await db.close()
        reset_session_id(token)
        reset_task_openjiuwen_home(home)
