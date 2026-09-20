# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gating tests for the code-profile explore / plan subagents.

Both are default-enabled: an absent config entry keeps them mounted, and only
an explicit ``enabled: false`` drops them. That asymmetry against code_agent /
browser_agent (which need an explicit ``true``) is the point of these tests.
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.swarm.config_specs import build_member_subagent_specs

CODE_TEAM_MODE = "code.team"
TEAM_PLAN_CODE_MODE = "team.plan.code"


def _spec_names(config: dict[str, Any], mode: str = CODE_TEAM_MODE) -> list[str]:
    """Return the sub-agent card names built for *mode* as a team leader."""
    specs = build_member_subagent_specs(config, mode, "leader")
    return [spec.agent_card.name for spec in specs]


def _config(subagents: dict[str, Any] | None) -> dict[str, Any]:
    """Wrap a ``react.subagents`` mapping into a full config mapping."""
    return {"react": {"subagents": subagents}}


def test_explore_and_plan_mounted_when_config_absent():
    names = _spec_names(_config({}))

    assert "explore_agent" in names
    assert "plan_agent" in names


def test_explore_and_plan_mounted_when_subagents_section_missing():
    names = _spec_names({"react": {}})

    assert "explore_agent" in names
    assert "plan_agent" in names


def test_explore_and_plan_mounted_when_react_section_missing():
    names = _spec_names({})

    assert "explore_agent" in names
    assert "plan_agent" in names


def test_explore_and_plan_mounted_when_subagents_section_is_none():
    names = _spec_names(_config(None))

    assert "explore_agent" in names
    assert "plan_agent" in names


def test_explore_and_plan_mounted_when_only_max_iterations_configured():
    names = _spec_names(
        _config(
            {
                "explore_agent": {"max_iterations": 50},
                "plan_agent": {"max_iterations": 50},
            }
        )
    )

    assert "explore_agent" in names
    assert "plan_agent" in names


def test_explicit_enabled_false_drops_explore_and_plan():
    names = _spec_names(
        _config(
            {
                "explore_agent": {"enabled": False},
                "plan_agent": {"enabled": False},
            }
        )
    )

    assert "explore_agent" not in names
    assert "plan_agent" not in names


def test_explore_and_plan_are_gated_independently():
    names = _spec_names(_config({"explore_agent": {"enabled": False}}))

    assert "explore_agent" not in names
    assert "plan_agent" in names


def test_explicit_enabled_true_keeps_explore_and_plan():
    names = _spec_names(
        _config(
            {
                "explore_agent": {"enabled": True},
                "plan_agent": {"enabled": True},
            }
        )
    )

    assert "explore_agent" in names
    assert "plan_agent" in names


def test_gating_applies_to_team_plan_code_mode():
    disabled = _config(
        {
            "explore_agent": {"enabled": False},
            "plan_agent": {"enabled": False},
        }
    )

    assert _spec_names(disabled, TEAM_PLAN_CODE_MODE) == _spec_names(
        disabled, CODE_TEAM_MODE
    )
    assert "explore_agent" not in _spec_names(disabled, TEAM_PLAN_CODE_MODE)


def test_non_code_mode_never_mounts_explore_or_plan():
    names = _spec_names(
        _config(
            {
                "explore_agent": {"enabled": True},
                "plan_agent": {"enabled": True},
            }
        ),
        "team",
    )

    assert "explore_agent" not in names
    assert "plan_agent" not in names


def test_max_iterations_still_honoured_when_enabled():
    specs = build_member_subagent_specs(
        _config({"explore_agent": {"enabled": True, "max_iterations": 50}}),
        CODE_TEAM_MODE,
        "leader",
    )
    explore = next(spec for spec in specs if spec.agent_card.name == "explore_agent")

    assert explore.factory_kwargs["max_iterations"] == 50
