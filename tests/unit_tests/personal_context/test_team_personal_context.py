"""Team member assembly reuses the fixed-home PersonalContext Rail and switch."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs
from openjiuwen.harness.prompts import PromptAttachmentManager
from openjiuwen.harness.rails.personal_context import PersonalContextRail
from jiuwenswarm.agents.swarm import register_swarm_providers
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs


@pytest.mark.parametrize(
    "mode",
    [
        "team",
        "code.team",
        "team.plan.normal",
        "team.plan.code",
        "team.work.normal",
        "team.work.plan",
        "team.code.normal",
        "team.code.plan",
    ],
)
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_team_personal_context_spec_survives_member_serialization(
    mode, role, tmp_path, monkeypatch
):
    register_swarm_providers()
    specs, _ = build_member_capability_specs({}, mode, role)
    selected = [spec for spec in specs if spec.type == "swarm.personal_context"]
    assert len(selected) == 1
    assert selected[0].params == {}
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    restored = RailSpec.model_validate(selected[0].model_dump())
    first = restored.build(language="cn", context=SimpleNamespace())
    second = restored.build(language="cn", context=SimpleNamespace())
    assert isinstance(first, PersonalContextRail)
    assert isinstance(second, PersonalContextRail)
    assert first is not second
    assert first._home == tmp_path / ".jiuwenswarm" / ".personal_context"
    assert second._home == first._home


@pytest.mark.asyncio
async def test_team_existing_and_new_members_follow_shared_switch(
    tmp_path, monkeypatch
):
    import openjiuwen.harness.rails.personal_context as rail_module

    register_swarm_providers()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    home = tmp_path / ".jiuwenswarm" / ".personal_context"
    root = home / "workspace" / "context"
    root.mkdir(parents=True)
    (root / "description.md").write_text("# shared personal context", encoding="utf-8")
    config = home / "personal_context.yaml"
    config.write_text("agent_use_enabled: false\ncollection_enabled: false\n")
    members = []

    def add_member(role):
        specs, _ = build_member_capability_specs(
            {}, "team", "leader" if role == "leader" else "teammate"
        )
        spec = next(spec for spec in specs if spec.type == "swarm.personal_context")
        rail = spec.build(language="cn", context=SimpleNamespace())
        manager = PromptAttachmentManager()
        agent = SimpleNamespace(prompt_attachment_manager=manager)
        rail.init(agent)
        ctx = AgentCallbackContext(
            agent=agent,
            inputs=ModelCallInputs(messages=[AssistantMessage(content="hello")]),
            session=SimpleNamespace(session_id=role),
        )
        members.append((rail, manager, ctx, role))

    for role in ("leader", "teammate"):
        add_member(role)
    for enabled in (False, True, False, True):
        config.write_text(
            f"agent_use_enabled: {str(enabled).lower()}\ncollection_enabled: false\n"
        )
        if enabled and len(members) == 2:
            add_member("dynamic-teammate")
        for rail, manager, ctx, role in members:
            await rail.before_model_call(ctx)
            assert bool(await manager.collect_for_session(role)) is enabled
    config.write_text("agent_use_enabled: false\n")
    monkeypatch.setattr(
        rail_module,
        "_read_description",
        lambda path: pytest.fail("disabled must not read Context"),
    )
    for rail, manager, ctx, role in members:
        await rail.before_model_call(ctx)
        assert await manager.collect_for_session(role) == []


@pytest.mark.asyncio
async def test_team_personal_context_keeps_tool_call_result_adjacency(
    tmp_path, monkeypatch
):
    register_swarm_providers()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    home = tmp_path / ".jiuwenswarm" / ".personal_context"
    root = home / "workspace" / "context"
    root.mkdir(parents=True)
    (home / "personal_context.yaml").write_text("agent_use_enabled: true\n")
    (root / "description.md").write_text("context", encoding="utf-8")
    specs, _ = build_member_capability_specs({}, "code.team", "teammate")
    rail = next(spec for spec in specs if spec.type == "swarm.personal_context").build(
        language="cn", context=SimpleNamespace()
    )
    manager = PromptAttachmentManager()
    agent = SimpleNamespace(prompt_attachment_manager=manager)
    rail.init(agent)
    messages = [
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(id="t1", type="function", name="read", arguments="{}")
            ],
        ),
        ToolMessage(content="result", tool_call_id="t1"),
    ]
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=ModelCallInputs(messages=messages),
        session=SimpleNamespace(session_id="member"),
    )
    before = list(messages)
    await rail.before_model_call(ctx)
    assert ctx.inputs.messages == before
    assert len(await manager.collect_for_session("member")) == 1
    await rail.after_model_call(ctx)
    assert await manager.collect_for_session("member") == []


def test_team_personal_context_factory_failure_does_not_break_member(
    monkeypatch, caplog
):
    from jiuwenswarm.agents.swarm.providers import member_rails

    def fail(_home):
        raise RuntimeError("private-failure-detail")

    monkeypatch.setattr(member_rails, "PersonalContextRail", fail)
    assert member_rails._build_personal_context_rail({}, SimpleNamespace()) is None
    assert "private-failure-detail" not in caplog.text
