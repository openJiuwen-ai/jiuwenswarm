# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The member-to-member messaging switch is a sub-switch of scheduled dispatch."""

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


def _write_config(path, *, dispatch_mode: str, member_messaging: bool) -> None:
    path.write_text(
        "# user configuration\n"
        "modes:\n"
        "  team:\n"
        "    jiuwen_team:\n"
        f"      dispatch_mode: {dispatch_mode}\n"
        "      enable_task_verification: false\n"
        f"      enable_member_messaging: {str(member_messaging).lower()}\n",
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


@pytest.mark.parametrize("enabled", [False, True])
def test_update_round_trip_preserves_other_settings(config_file, enabled):
    _write_config(config_file, dispatch_mode="scheduled", member_messaging=not enabled)

    config.update_team_member_messaging_in_config(enabled)

    section = _team_section(config_file)
    assert section["enable_member_messaging"] is enabled
    assert section["dispatch_mode"] == "scheduled"
    assert "# user configuration" in config_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, "false"),
        ({"modes": {"team": {"jiuwen_team": {"enable_member_messaging": True}}}}, "true"),
    ],
)
def test_flatten_reports_the_switch(raw, expected):
    assert flatten_team_dispatch_for_config_panel(raw)["team_member_messaging_enabled"] == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, "false"),
        ({"modes": {"team": {"jiuwen_team": {"enable_member_messaging": True}}}}, "true"),
    ],
)
def test_panel_reads_the_switch(monkeypatch, raw, expected):
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: raw)
    assert config_service.get_panel()["team_member_messaging_enabled"] == expected


def test_switch_change_reloads_the_team():
    changes = ConfigChangeSet(env_updates={}, yaml_updated=["team_member_messaging_enabled"])
    assert "team" in changes.reload_scopes


def test_enabling_under_scheduled_dispatch_persists(config_file):
    _write_config(config_file, dispatch_mode="scheduled", member_messaging=False)

    apply_config_payload({"team_member_messaging_enabled": "true"})

    assert _team_section(config_file)["enable_member_messaging"] is True


def test_enabling_without_scheduled_dispatch_is_dropped(config_file):
    _write_config(config_file, dispatch_mode="autonomous", member_messaging=False)

    apply_config_payload({"team_member_messaging_enabled": "true"})

    assert _team_section(config_file)["enable_member_messaging"] is False


def test_turning_scheduled_dispatch_off_clears_the_sub_switch(config_file):
    _write_config(config_file, dispatch_mode="scheduled", member_messaging=True)

    apply_config_payload({"team_scheduled_dispatch_enabled": "false"})

    section = _team_section(config_file)
    assert section["dispatch_mode"] == "autonomous"
    assert section["enable_member_messaging"] is False
