# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The member-subtask switch is a sub-switch of scheduled dispatch (core F_128).

The setting is a switch, the config value a depth: on writes a depth > 0
(keeping one the user already set), off writes 0.
"""

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel import config_set_handlers
from jiuwenswarm.common.config_panel.config_set_handlers import (
    ConfigChangeSet,
    apply_config_payload,
    flatten_team_dispatch_for_config_panel,
)
from jiuwenswarm.server.control import config_service


def _write_config(path, *, dispatch_mode: str, depth: int | None) -> None:
    depth_line = "" if depth is None else f"      max_subtask_depth: {depth}\n"
    path.write_text(
        "# user configuration\n"
        "modes:\n"
        "  team:\n"
        "    jiuwen_team:\n"
        f"      dispatch_mode: {dispatch_mode}\n"
        "      enable_member_messaging: true\n"
        f"{depth_line}",
        encoding="utf-8",
    )


def _team_section(path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["modes"]["team"]["jiuwen_team"]


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(
        config_set_handlers,
        "get_config_raw",
        lambda: yaml.safe_load(path.read_text(encoding="utf-8")),
    )
    return path


@pytest.mark.parametrize(("existing", "expected"), [(None, 2), (0, 2), (3, 3)])
def test_turning_on_keeps_a_configured_depth(config_file, existing, expected):
    _write_config(config_file, dispatch_mode="scheduled", depth=existing)

    config.update_team_member_subtasks_in_config(True)

    section = _team_section(config_file)
    assert section["max_subtask_depth"] == expected
    assert section["enable_member_messaging"] is True
    assert "# user configuration" in config_file.read_text(encoding="utf-8")


def test_turning_off_writes_zero(config_file):
    _write_config(config_file, dispatch_mode="scheduled", depth=3)

    config.update_team_member_subtasks_in_config(False)

    assert _team_section(config_file)["max_subtask_depth"] == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2, 2), ("2", 2), (0, 0), (-1, 0), (None, 0), ("x", 0), (True, 0)],
)
def test_coerce_subtask_depth(value, expected):
    assert config.coerce_subtask_depth(value) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, "false"),
        ({"modes": {"team": {"jiuwen_team": {"max_subtask_depth": 0}}}}, "false"),
        ({"modes": {"team": {"jiuwen_team": {"max_subtask_depth": 2}}}}, "true"),
    ],
)
def test_flatten_reports_the_switch(raw, expected):
    assert flatten_team_dispatch_for_config_panel(raw)["team_member_subtasks_enabled"] == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, "false"),
        ({"modes": {"team": {"jiuwen_team": {"max_subtask_depth": "bad"}}}}, "false"),
        ({"modes": {"team": {"jiuwen_team": {"max_subtask_depth": 2}}}}, "true"),
    ],
)
def test_panel_reads_the_switch(monkeypatch, raw, expected):
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: raw)
    assert config_service.get_panel()["team_member_subtasks_enabled"] == expected


def test_switch_change_reloads_the_team():
    changes = ConfigChangeSet(env_updates={}, yaml_updated=["team_member_subtasks_enabled"])
    assert "team" in changes.reload_scopes


def test_enabling_under_scheduled_dispatch_persists(config_file):
    _write_config(config_file, dispatch_mode="scheduled", depth=0)

    apply_config_payload({"team_member_subtasks_enabled": "true"})

    assert _team_section(config_file)["max_subtask_depth"] == config.DEFAULT_TEAM_MEMBER_SUBTASK_DEPTH


def test_enabling_without_scheduled_dispatch_is_dropped(config_file):
    _write_config(config_file, dispatch_mode="autonomous", depth=0)

    apply_config_payload({"team_member_subtasks_enabled": "true"})

    assert _team_section(config_file)["max_subtask_depth"] == 0


def test_turning_scheduled_dispatch_off_clears_the_sub_switch(config_file):
    _write_config(config_file, dispatch_mode="scheduled", depth=2)

    apply_config_payload({"team_scheduled_dispatch_enabled": "false"})

    section = _team_section(config_file)
    assert section["dispatch_mode"] == "autonomous"
    assert section["max_subtask_depth"] == 0
