# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team runtime inheritance helpers."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openjiuwen.core.context_engine.image_refs import set_image_summary_provider
from openjiuwen.core.foundation.tool import ToolCard

from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
    MemberInfo,
    RuntimeInfo,
    TeamWorkspaceInfo,
    build_evolution_llm,
    build_member_rails,
    build_skill_evolution_rail,
    filter_inheritable_ability_cards,
    resolve_model_config,
)
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import LocalSectionName
from jiuwenswarm.server.runtime.a2ui.config import A2UIConfig


@pytest.fixture(autouse=True)
def _reset_image_summary_provider():
    """ImageSummaryRail 构造时注册进程级 provider，前后复位避免污染其他测试。"""
    set_image_summary_provider(None)
    yield
    set_image_summary_provider(None)


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
    assert "user_todos" not in inherited_names
    assert "enter_worktree" not in inherited_names
    assert "exit_worktree" not in inherited_names
    assert "task_tool" not in inherited_names
    assert "send_file_to_user" not in inherited_names


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


def test_build_member_rails_appends_image_summary_rail_when_folding_enabled():
    """折叠开（enabled=true）时成员 rails 列表含 ImageSummaryRail。"""
    rails = build_member_rails(
        member_info=MemberInfo(role="teammate"),
        runtime=RuntimeInfo(language="cn"),
        team_workspace=TeamWorkspaceInfo(
            config={
                "react": {
                    "context_engine_config": {
                        "image_window_offloader_config": {"enabled": True},
                    },
                },
            },
        ),
    )

    assert any(type(rail).__name__ == "ImageSummaryRail" for rail in rails)


def test_build_member_rails_appends_image_summary_rail_by_default():
    """缺省（无 image_window_offloader_config 键）等同启用。"""
    rails = build_member_rails(
        member_info=MemberInfo(role="teammate"),
        runtime=RuntimeInfo(language="cn"),
        team_workspace=TeamWorkspaceInfo(config={}),
    )

    assert any(type(rail).__name__ == "ImageSummaryRail" for rail in rails)


def test_build_member_rails_omits_image_summary_rail_when_folding_disabled():
    """折叠关（enabled=false）时不含 ImageSummaryRail。"""
    rails = build_member_rails(
        member_info=MemberInfo(role="teammate"),
        runtime=RuntimeInfo(language="cn"),
        team_workspace=TeamWorkspaceInfo(
            config={
                "react": {
                    "context_engine_config": {
                        "image_window_offloader_config": {"enabled": False},
                    },
                },
            },
        ),
    )

    assert all(type(rail).__name__ != "ImageSummaryRail" for rail in rails)


def test_build_member_rails_omits_image_summary_rail_when_context_engine_disabled():
    """context_engine 整体关闭时不含 ImageSummaryRail。"""
    rails = build_member_rails(
        member_info=MemberInfo(role="teammate"),
        runtime=RuntimeInfo(language="cn"),
        team_workspace=TeamWorkspaceInfo(
            config={"react": {"context_engine_config": {"enabled": False}}},
        ),
    )

    assert all(type(rail).__name__ != "ImageSummaryRail" for rail in rails)


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


def test_build_member_rails_wires_team_trajectory_registry_to_evolution_rails(
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
            self.bound_sink = None
            self.bound_team_id = None
            self.bound_member_role = None
            self._review_runtime = kwargs.get("review_runtime")
            self.experience_manager = SimpleNamespace(
                experience_submission_service=object()
            )

        def set_trajectory_sink(self, sink, *, team_id, member_role):
            self.bound_sink = sink
            self.bound_team_id = team_id
            self.bound_member_role = member_role

    registry = object()
    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
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

    leader_rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            root_dir=str(tmp_path / "team-workspace"),
            skills_dir=str(tmp_path / "skills"),
            team_id="demo-team",
            trajectory_registry=registry,
            config={"evolution": {"auto_scan": True}},
        ),
    )
    member_rails = build_member_rails(
        member_info=MemberInfo(role="teammate"),
        team_workspace=TeamWorkspaceInfo(
            root_dir=str(tmp_path / "team-workspace"),
            skills_dir=str(tmp_path / "skills"),
            team_id="demo-team",
            trajectory_registry=registry,
            config={"evolution": {"auto_scan": True}},
        ),
    )

    leader_rail = next(
        rail for rail in leader_rails if isinstance(rail, _FakeTeamSkillEvolutionRail)
    )
    member_rail = next(
        rail for rail in member_rails if isinstance(rail, _FakeSkillEvolutionRail)
    )

    assert leader_rail.kwargs["trajectory_source"] is registry
    assert leader_rail.kwargs["trajectory_sink"] is registry
    assert leader_rail.kwargs["member_role"] == "leader"
    assert leader_rail.kwargs["signal_trigger"] is False
    assert leader_rail.kwargs["review_trigger"] is True
    assert member_rail.bound_sink is registry
    assert member_rail.bound_team_id == "demo-team"
    assert member_rail.bound_member_role == "teammate"
    assert member_rail.kwargs["signal_trigger"] is True


@pytest.mark.parametrize(
    ("env_review_trigger", "config", "expected_review_trigger"),
    [
        (None, {"evolution": {"enabled": True, "review_trigger": False}}, False),
        (None, {"evolution": {"enabled": False, "review_trigger": False}}, False),
        (None, {"react": {"evolution": {"enabled": True, "review_trigger": True}}}, True),
        (None, {"evolution": {"auto_scan": True}}, True),
        (None, {"evolution": {"auto_scan": True, "review_trigger": False}}, False),
        ("false", {"evolution": {"enabled": True, "review_trigger": True}}, False),
    ],
)
def test_build_member_rails_creates_leader_team_skill_evolution_rail_with_completion_followup(
    tmp_path,
    monkeypatch,
    env_review_trigger,
    config,
    expected_review_trigger,
):
    class _FakeTeamSkillEvolutionRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.experience_manager = SimpleNamespace(
                experience_submission_service=object()
            )

    if env_review_trigger is None:
        monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
        monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    else:
        monkeypatch.setenv("EVOLUTION_REVIEW_TRIGGER", env_review_trigger)
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
            config=config,
        ),
    )

    team_skill_rails = [rail for rail in rails if isinstance(rail, _FakeTeamSkillEvolutionRail)]
    assert len(team_skill_rails) == 1
    assert team_skill_rails[0].kwargs["signal_trigger"] is False
    assert team_skill_rails[0].kwargs["review_trigger"] is expected_review_trigger


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

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
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
            config={"evolution": {"review_trigger": False, "auto_save": auto_save}},
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


def test_build_member_rails_keeps_member_skill_evolution_when_signal_trigger_disabled(
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

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
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
            config={"react": {"evolution": {"auto_scan": False, "auto_save": False}}},
        ),
    )

    evo_rails = [rail for rail in rails if isinstance(rail, _FakeSkillEvolutionRail)]
    assert len(evo_rails) == 1
    assert evo_rails[0].signal_trigger is False
    assert evo_rails[0].auto_save is True
    interrupt_index = next(
        index for index, rail in enumerate(rails) if isinstance(rail, _FakeEvolutionInterruptRail)
    )
    skill_index = next(index for index, rail in enumerate(rails) if isinstance(rail, _FakeSkillEvolutionRail))
    assert interrupt_index < skill_index
    assert rails[interrupt_index].kwargs["auto_save"] is True


def test_build_member_rails_keeps_team_skill_create_when_signal_trigger_disabled(
    tmp_path, monkeypatch
):
    class _FakeTeamSkillCreateRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.delenv("SKILL_CREATE", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.TeamSkillCreateRail",
        _FakeTeamSkillCreateRail,
    )

    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            skills_dir=str(tmp_path / "skills"),
            config={"evolution": {"signal_trigger": False, "skill_create": True}},
        ),
    )

    assert any(isinstance(rail, _FakeTeamSkillCreateRail) for rail in rails)


def test_build_member_rails_reads_react_evolution_skill_create(
    tmp_path, monkeypatch
):
    class _FakeTeamSkillCreateRail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.delenv("SKILL_CREATE", raising=False)
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

    assert any(isinstance(rail, _FakeTeamSkillCreateRail) for rail in rails)


def test_build_member_rails_env_skill_create_overrides_config(tmp_path, monkeypatch):
    class _FakeTeamSkillCreateRail:
        pass

    monkeypatch.setenv("SKILL_CREATE", "false")
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_runtime_inheritance.TeamSkillCreateRail",
        _FakeTeamSkillCreateRail,
    )

    rails = build_member_rails(
        member_info=MemberInfo(role="leader"),
        team_workspace=TeamWorkspaceInfo(
            skills_dir=str(tmp_path / "skills"),
            config={"evolution": {"skill_create": True}},
        ),
    )

    assert not any(isinstance(rail, _FakeTeamSkillCreateRail) for rail in rails)
