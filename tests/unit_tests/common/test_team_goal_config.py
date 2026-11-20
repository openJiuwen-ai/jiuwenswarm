# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scheduled leader goals share one settings transaction and policy schema."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.team_goal_config import normalize_team_goal
from jiuwenswarm.server.control import config_service


@pytest.fixture
def goal_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "# keep user comments\nmodes:\n  team:\n    jiuwen_team:\n"
        "      dispatch_mode: scheduled\n      member_goal: {strategy: transcript}\n"
        "      leader:\n        display_name: Leader\n        goal: null\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: yaml.safe_load(path.read_text()))
    return path


def _team(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["modes"]["team"]["jiuwen_team"]


def test_default_switch_round_trip_preserves_other_fields(goal_config: Path) -> None:
    handlers.apply_config_payload({"team_leader_goal_enabled": "true"})
    team = _team(goal_config)
    assert team["leader"]["goal"] == {"strategy": "hybrid", "max_attempts": 10}
    assert team["leader"]["display_name"] == "Leader"
    assert team["member_goal"] == {"strategy": "transcript"}
    assert "# keep user comments" in goal_config.read_text()
    assert handlers.flatten_team_dispatch_for_config_panel(yaml.safe_load(goal_config.read_text()))[
        "team_leader_goal_enabled"
    ] == "true"
    assert config_service.get_panel()["team_leader_goal_enabled"] == "true"
    handlers.apply_config_payload({"team_leader_goal_enabled": "false"})
    assert _team(goal_config)["leader"]["goal"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_dispatch_and_goal_are_saved_in_one_write(
    goal_config: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool,
) -> None:
    config.update_team_goal_settings_in_config(scheduled=False, enabled=False)
    calls = []
    original = config.dump_yaml_round_trip

    def capture(path: Path, data: dict) -> None:
        calls.append(deepcopy(data))
        original(path, data)

    monkeypatch.setattr(config, "dump_yaml_round_trip", capture)
    items = [("team_scheduled_dispatch_enabled", "true"), ("team_leader_goal_enabled", "true")]
    handlers.apply_config_payload(dict(reversed(items) if reverse else items))
    assert len(calls) == 1
    assert _team(goal_config)["dispatch_mode"] == "scheduled"
    assert _team(goal_config)["leader"]["goal"]["max_attempts"] == 10


def test_invalid_autonomous_enable_does_not_write(goal_config: Path) -> None:
    config.update_team_goal_settings_in_config(scheduled=False, enabled=False)
    before = goal_config.read_bytes()
    with pytest.raises(handlers.ConfigPanelBadRequest, match="scheduled"):
        handlers.apply_config_payload({"team_leader_goal_enabled": "true"})
    assert goal_config.read_bytes() == before


def test_parent_off_clears_goal_without_reenabling_it(goal_config: Path) -> None:
    handlers.apply_config_payload({"team_leader_goal_enabled": True})
    handlers.apply_config_payload({"team_scheduled_dispatch_enabled": False})
    assert _team(goal_config)["leader"]["goal"] is None
    assert config_service.get_panel()["team_leader_goal_enabled"] == "false"
    handlers.apply_config_payload({"team_scheduled_dispatch_enabled": True})
    assert _team(goal_config)["leader"]["goal"] is None


def test_existing_policy_survives_unrelated_and_repeated_save(goal_config: Path) -> None:
    raw = yaml.safe_load(goal_config.read_text())
    team = raw["modes"]["team"]["jiuwen_team"]
    team["leader"]["goal"] = {"strategy": "transcript", "max_attempts": 3}
    goal_config.write_text(yaml.safe_dump(raw))
    handlers.apply_config_payload({"team_leader_goal_enabled": True})
    assert _team(goal_config)["leader"]["goal"] == {"strategy": "transcript", "max_attempts": 3}
    assert handlers.ConfigChangeSet({}, ["team_leader_goal_enabled"]).reload_scopes == {"web_ui"}


@pytest.mark.parametrize("goal", [None, {}, {"strategy": "transcript", "max_attempts": None}])
def test_goal_policy_values(goal: dict | None) -> None:
    result = normalize_team_goal(goal, path="leader.goal")
    assert result is None if goal is None else result["strategy"] == goal.get("strategy", "hybrid")


@pytest.mark.parametrize("goal", [True, 10, [], {"strategy": "unknown"}, {"token_budget": 100},
    {"max_attempts": True}, {"max_attempts": 0}, {"max_attempts": -1}, {"max_attempts": 1.5},
    {"max_attempts": "10"}])
def test_invalid_policy_is_rejected(goal: object) -> None:
    with pytest.raises(ValueError, match="leader.goal"):
        normalize_team_goal(goal, path="leader.goal")


@pytest.mark.parametrize("value", [None, 1, "yes", "invalid"])
def test_switch_rejects_invalid_boolean_before_writing(goal_config: Path, value: object) -> None:
    before = goal_config.read_bytes()
    with pytest.raises(handlers.ConfigPanelBadRequest, match="true or false"):
        handlers.apply_config_payload({"team_leader_goal_enabled": value})
    assert goal_config.read_bytes() == before


def test_rejected_combination_does_not_partially_disable_dispatch(goal_config: Path) -> None:
    handlers.apply_config_payload({"team_leader_goal_enabled": True})
    before = goal_config.read_bytes()
    with pytest.raises(handlers.ConfigPanelBadRequest, match="scheduled"):
        handlers.apply_config_payload({
            "team_scheduled_dispatch_enabled": False, "team_leader_goal_enabled": True,
        })
    assert goal_config.read_bytes() == before
