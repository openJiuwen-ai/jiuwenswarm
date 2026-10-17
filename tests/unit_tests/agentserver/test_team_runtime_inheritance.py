# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team runtime inheritance helpers."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openjiuwen.core.foundation.tool import ToolCard

from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
    MemberInfo,
    RuntimeInfo,
    TeamWorkspaceInfo,
    _build_context_processor_rail,
    build_evolution_llm,
    build_member_rails,
    build_skill_evolution_rail,
    filter_inheritable_ability_cards,
    resolve_model_config,
)
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import LocalSectionName
from jiuwenswarm.server.runtime.a2ui.config import A2UIConfig
from jiuwenswarm.agents.harness.common.rails.symphony.retrieval_context_processor import (
    SymphonyRetrievalCompactProcessorConfig,
)


class _FakeAbilityManager:
    def __init__(self, abilities):
        self._abilities = abilities

    def list(self):
        return list(self._abilities)


class _FakePromptBuilder:
    def __init__(self) -> None:
        self.language = "cn"
        self.sections = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


def _make_tool_card(name: str) -> ToolCard:
    return ToolCard(
        id=name,
        name=name,
        description=f"{name} description",
        input_params={"type": "object"},
    )


def test_team_context_processor_rail_includes_symphony_retrieval_compactor() -> None:
    rail = _build_context_processor_rail({})

    assert rail is not None
    assert rail._user_processors[-1][0] == "SymphonyRetrievalCompactProcessor"
    assert isinstance(
        rail._user_processors[-1][1],
        SymphonyRetrievalCompactProcessorConfig,
    )


def test_filter_inheritable_ability_cards_includes_extended_swarm_tools():
    main_agent = SimpleNamespace(
        ability_manager=_FakeAbilityManager(
            [
                _make_tool_card("visual_question_answering"),
                _make_tool_card("audio_question_answering"),
                _make_tool_card("audio_metadata"),
                _make_tool_card("user_todos"),
                _make_tool_card("task_tool"),
                _make_tool_card("acp_chat"),
                _make_tool_card("enter_worktree"),
                _make_tool_card("exit_worktree"),
                _make_tool_card("send_file_to_user"),
            ]
        )
    )

    inherited = filter_inheritable_ability_cards(main_agent)
    inherited_names = {card.name for card in inherited}

    assert "acp_chat" in inherited_names
    assert "visual_question_answering" in inherited_names
    assert "audio_question_answering" in inherited_names
    assert "audio_metadata" in inherited_names
    assert "user_todos" in inherited_names
    assert "enter_worktree" not in inherited_names
    assert "exit_worktree" not in inherited_names
    assert "task_tool" not in inherited_names
    assert "send_file_to_user" not in inherited_names


@pytest.mark.asyncio
async def test_build_member_rails_separates_project_and_team_file_destinations(
    tmp_path: Path,
) -> None:
    """Legacy/hot-reload rail construction preserves the user project root."""
    project_dir = str(tmp_path / "project")
    team_ws_root = str(tmp_path / "team-workspace")
    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            root_dir=team_ws_root,
            project_dir=project_dir,
            team_id="unit-team",
        ),
    )
    rail = next(
        item for item in rails if item.__class__.__name__ == "TeamWorkspaceReportPathRail"
    )
    builder = _FakePromptBuilder()
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    await rail.before_model_call(SimpleNamespace())

    section = builder.sections["team_workspace_report_paths"]
    content = section.render("cn")
    assert f"User project root: `{project_dir}`" in content
    assert f"Team shared workspace (config / internal data): `{team_ws_root}`" in content
    assert "Do not place final project files in the team shared workspace root" in content
    assert "When worktree isolation is active" in content


@pytest.mark.asyncio
async def test_build_member_rails_syncs_response_prompt_channel_for_a2ui(monkeypatch):
    """Team Web model calls should inherit the runtime channel for A2UI prompts."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )

    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        runtime=RuntimeInfo(channel="web", language="cn"),
    )
    response_rails = [rail for rail in rails if type(rail).__name__ == "ResponsePromptRail"]
    assert len(response_rails) == 1

    prompt_builder = _FakePromptBuilder()
    response_rails[0].init(SimpleNamespace(system_prompt_builder=prompt_builder))
    await response_rails[0].before_model_call(SimpleNamespace(inputs=SimpleNamespace()))

    assert LocalSectionName.A2UI in prompt_builder.sections


def test_build_member_rails_adds_structured_ask_user_rail_for_leader_only():
    leader_rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        runtime=RuntimeInfo(language="cn"),
    )
    teammate_rails = build_member_rails(
        member_info=MemberInfo(role="teammate"),
        runtime=RuntimeInfo(language="cn"),
    )

    assert any(type(rail).__name__ == "StructuredAskUserRail" for rail in leader_rails)
    assert all(type(rail).__name__ != "StructuredAskUserRail" for rail in teammate_rails)


def test_build_member_rails_omits_task_planning_for_leader_only():
    leader_rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        runtime=RuntimeInfo(language="cn"),
    )
    teammate_rails = build_member_rails(
        member_info=MemberInfo(role="teammate"),
        runtime=RuntimeInfo(language="cn"),
    )

    assert all(type(rail).__name__ != "TaskPlanningRail" for rail in leader_rails)
    assert any(type(rail).__name__ == "TaskPlanningRail" for rail in teammate_rails)
    assert all(type(rail).__name__ != "HeartbeatRail" for rail in leader_rails)
    assert all(type(rail).__name__ != "HeartbeatRail" for rail in teammate_rails)


# -- resolve_model_config tests --

def test_resolve_model_config_from_default():
    config = {
        "models": {
            "default": {
                "model_client_config": {"model_name": "test-model", "api_key": "k1"},
                "model_config_obj": {"temperature": 0.5},
            }
        },
        "react": {},
    }
    client_cfg, model_cfg_obj, model_name = resolve_model_config(config)
    assert client_cfg == {"model_name": "test-model", "api_key": "k1"}
    assert model_cfg_obj == {"temperature": 0.5}
    assert model_name == "test-model"


def test_resolve_model_config_fallback_to_react():
    config = {
        "models": {"default": {}},
        "react": {
            "model_client_config": {"model_name": "react-model"},
            "model_config_obj": {"temperature": 0.3},
        },
    }
    client_cfg, model_cfg_obj, model_name = resolve_model_config(config)
    assert client_cfg == {"model_name": "react-model"}
    assert model_cfg_obj == {"temperature": 0.3}
    assert model_name == "react-model"


def test_resolve_model_config_default_name():
    config = {"models": {"default": {}}, "react": {}}
    client_cfg, model_cfg_obj, model_name = resolve_model_config(config)
    assert client_cfg == {}
    assert model_cfg_obj == {}
    assert model_name == "gpt-4"


# -- build_evolution_llm tests --

def test_build_evolution_llm_from_config():
    config = {
        "models": {
            "default": {
                "model_client_config": {"model_name": "test-model"},
                "model_config_obj": {"temperature": 0.5},
            }
        },
        "react": {},
    }
    fake_model = object()
    with patch("openjiuwen.core.foundation.llm.ModelClientConfig", return_value=None), \
            patch("openjiuwen.core.foundation.llm.ModelRequestConfig"), \
            patch("openjiuwen.core.foundation.llm.Model", return_value=fake_model):
        model, model_name = build_evolution_llm(config)

    assert model_name == "test-model"
    assert model is fake_model


def test_build_evolution_llm_fallback_to_react_config():
    config = {
        "models": {"default": {}},
        "react": {
            "model_client_config": {"model_name": "react-model"},
        },
    }
    fake_model = object()
    with patch("openjiuwen.core.foundation.llm.ModelClientConfig", return_value=None), \
            patch("openjiuwen.core.foundation.llm.ModelRequestConfig"), \
            patch("openjiuwen.core.foundation.llm.Model", return_value=fake_model):
        model, model_name = build_evolution_llm(config)

    assert model_name == "react-model"


def test_build_evolution_llm_default_model_name():
    config = {"models": {"default": {}}, "react": {}}
    fake_model = object()
    with patch("openjiuwen.core.foundation.llm.ModelClientConfig", return_value=None), \
            patch("openjiuwen.core.foundation.llm.ModelRequestConfig"), \
            patch("openjiuwen.core.foundation.llm.Model", return_value=fake_model):
        model, model_name = build_evolution_llm(config)

    assert model_name == "gpt-4"


# -- build_skill_evolution_rail tests --

def test_build_skill_evolution_rail_returns_none_on_invalid_config(tmp_path):
    """When config is invalid for Model construction, should return None."""
    result = build_skill_evolution_rail(
        skills_dir=str(tmp_path / "nonexistent"),
        config={
            "models": {"default": {}},
            "react": {},
        },
    )
    # Will fail due to empty model_client_config, returning None
    assert result is None


def test_build_member_rails_wires_team_trajectory_processor_to_evolution_rails(
    tmp_path,
    monkeypatch,
):
    class _FakeTeamSkillEvolutionRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.experience_manager = SimpleNamespace(
                experience_submission_service=object()
            )

    class _FakeSkillEvolutionRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._review_runtime = kwargs.get("review_runtime")
            self.experience_manager = SimpleNamespace(
                experience_submission_service=object()
            )

    processor = object()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.TeamSkillEvolutionRail",
        _FakeTeamSkillEvolutionRail,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.build_evolution_llm",
        lambda config=None: (object(), "model"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.SkillEvolutionRail",
        _FakeSkillEvolutionRail,
    )
    global_skills_dir = tmp_path / "global-skills"
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.get_agent_skills_dir",
        lambda: global_skills_dir,
    )

    leader_rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            root_dir=str(tmp_path / "team-workspace"),
            skills_dir=str(tmp_path / "skills"),
            team_id="demo-team",
            trajectory_span_processor=processor,
            config={"react": {"evolution": {"skill_evolution": True}}},
        ),
    )
    member_rails = build_member_rails(
        member_info=MemberInfo(role="teammate"),
        team_workspace=TeamWorkspaceInfo(
            root_dir=str(tmp_path / "team-workspace"),
            skills_dir=str(tmp_path / "skills"),
            team_id="demo-team",
            trajectory_span_processor=processor,
            config={"react": {"evolution": {"skill_evolution": True}}},
        ),
    )

    leader_rail = next(
        rail for rail in leader_rails if isinstance(rail, _FakeTeamSkillEvolutionRail)
    )
    member_rail = next(
        rail for rail in member_rails if isinstance(rail, _FakeSkillEvolutionRail)
    )

    assert leader_rail.kwargs["trajectory_span_processor"] is processor
    assert leader_rail.kwargs["member_role"] == "leader"
    assert leader_rail.kwargs["signal_trigger"] is False
    assert leader_rail.kwargs["review_trigger"] is True
    # Skills live in exactly one physical library; the team workspace no longer
    # contributes a second root.
    assert leader_rail.kwargs["skills_dir"] == str(global_skills_dir)
    assert member_rail.kwargs["trajectory_span_processor"] is processor
    assert member_rail.kwargs["signal_trigger"] is True
    assert member_rail.kwargs["skills_dir"] == str(global_skills_dir)


def test_build_member_rails_creates_leader_team_skill_evolution_rail_with_canonical_switch(
    tmp_path, monkeypatch,
):
    class _FakeTeamSkillEvolutionRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.experience_manager = SimpleNamespace(
                experience_submission_service=object()
            )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.TeamSkillEvolutionRail",
        _FakeTeamSkillEvolutionRail,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.build_evolution_llm",
        lambda config=None: (object(), "model"),
    )

    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            skills_dir=str(tmp_path / "skills"),
            config={"react": {"evolution": {"skill_evolution": True}}},
        ),
    )

    team_skill_rails = [rail for rail in rails if isinstance(rail, _FakeTeamSkillEvolutionRail)]
    assert len(team_skill_rails) == 1
    assert team_skill_rails[0].kwargs["signal_trigger"] is False
    assert team_skill_rails[0].kwargs["review_trigger"] is True


def test_build_member_rails_ignores_legacy_evolution_fields(tmp_path, monkeypatch):
    class _FakeTeamSkillEvolutionRail:
        pass

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.TeamSkillEvolutionRail",
        _FakeTeamSkillEvolutionRail,
    )
    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            skills_dir=str(tmp_path / "skills"),
            config={"react": {"evolution": {"auto_scan": True, "enabled": True}}},
        ),
    )
    assert not any(isinstance(rail, _FakeTeamSkillEvolutionRail) for rail in rails)


@pytest.mark.parametrize("auto_save", [False, True])
def test_build_member_rails_wires_leader_team_skill_evolution_active_review_rails(
    tmp_path,
    monkeypatch,
    auto_save,
):
    class _FakeEvolutionInterruptRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeTeamSkillEvolutionRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.experience_manager = SimpleNamespace(
                experience_submission_service=object()
            )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.EvolutionInterruptRail",
        _FakeEvolutionInterruptRail,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.TeamSkillEvolutionRail",
        _FakeTeamSkillEvolutionRail,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.build_evolution_llm",
        lambda config=None: (object(), "model"),
    )

    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        runtime=RuntimeInfo(channel="web"),
        team_workspace=TeamWorkspaceInfo(
            skills_dir=str(tmp_path / "skills"),
            config={"react": {"evolution": {"skill_evolution": True, "auto_save": auto_save}}},
        ),
    )

    interrupt_index = next(
        index for index, rail in enumerate(rails) if isinstance(rail, _FakeEvolutionInterruptRail)
    )
    team_index = next(
        index for index, rail in enumerate(rails) if isinstance(rail, _FakeTeamSkillEvolutionRail)
    )
    assert interrupt_index < team_index

    interrupt_rail = rails[interrupt_index]
    team_rail = rails[team_index]
    assert "review_runtime" in team_rail.kwargs
    assert interrupt_rail.kwargs["review_runtime"] is team_rail.kwargs["review_runtime"]
    assert (
        interrupt_rail.kwargs["submission_service"]
        is team_rail.experience_manager.experience_submission_service
    )
    assert interrupt_rail.kwargs["auto_save"] is auto_save
    assert interrupt_rail.kwargs["language"] == "cn"
    assert team_rail.kwargs["auto_save"] is auto_save


def test_build_member_rails_builds_teammate_skill_evolution_with_fixed_triggers(
    tmp_path, monkeypatch
):
    class _FakeEvolutionInterruptRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeSkillEvolutionRail:
        def __init__(self, **kwargs):
            self.signal_trigger = kwargs["signal_trigger"]
            self.auto_save = kwargs["auto_save"]
            self._review_runtime = kwargs.get("review_runtime")
            self.experience_manager = SimpleNamespace(
                experience_submission_service=object()
            )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.EvolutionInterruptRail",
        _FakeEvolutionInterruptRail,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.SkillEvolutionRail",
        _FakeSkillEvolutionRail,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.build_evolution_llm",
        lambda config=None: (object(), "model"),
    )

    rails = build_member_rails(
        member_info=MemberInfo(role="member"),
        runtime=RuntimeInfo(channel="web"),
        team_workspace=TeamWorkspaceInfo(
            skills_dir=str(tmp_path / "skills"),
            config={"react": {"evolution": {"skill_evolution": True, "auto_save": False}}},
        ),
    )

    evo_rails = [rail for rail in rails if isinstance(rail, _FakeSkillEvolutionRail)]
    assert len(evo_rails) == 1
    assert evo_rails[0].signal_trigger is True
    assert evo_rails[0].auto_save is True
    assert evo_rails[0]._review_runtime is not None
    assert not any(isinstance(rail, _FakeEvolutionInterruptRail) for rail in rails)


def test_build_member_rails_builds_team_skill_create_with_canonical_switch(
    tmp_path, monkeypatch
):
    class _FakeTeamSkillCreateRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.TeamSkillCreateRail",
        _FakeTeamSkillCreateRail,
    )

    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            skills_dir=str(tmp_path / "skills"),
            config={"react": {"evolution": {"skill_evolution": True}}},
        ),
    )

    assert any(isinstance(rail, _FakeTeamSkillCreateRail) for rail in rails)


def test_build_member_rails_does_not_mount_create_from_legacy_field(tmp_path, monkeypatch):
    class _FakeTeamSkillCreateRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.TeamSkillCreateRail",
        _FakeTeamSkillCreateRail,
    )

    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            skills_dir=str(tmp_path / "skills"),
            config={"react": {"evolution": {"skill_create": True}}},
        ),
    )

    assert not any(isinstance(rail, _FakeTeamSkillCreateRail) for rail in rails)


def test_build_member_rails_env_skill_create_does_not_override_canonical_config(tmp_path, monkeypatch):
    class _FakeTeamSkillCreateRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("SKILL_CREATE", "false")
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.TeamSkillCreateRail",
        _FakeTeamSkillCreateRail,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.build_evolution_llm",
        lambda config=None: (object(), "model"),
    )

    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            skills_dir=str(tmp_path / "skills"),
            config={"react": {"evolution": {"skill_evolution": True}}},
        ),
    )

    assert any(isinstance(rail, _FakeTeamSkillCreateRail) for rail in rails)
