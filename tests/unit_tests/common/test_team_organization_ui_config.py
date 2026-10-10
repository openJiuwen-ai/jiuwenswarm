# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Organization UI uses the existing experimental config contract only."""

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel.config_set_handlers import ConfigChangeSet
from jiuwenswarm.server.control import config_service


@pytest.mark.parametrize("enabled", [False, True])
def test_switch_round_trip_preserves_other_settings(monkeypatch, tmp_path, enabled):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# user configuration\nexperimental:\n  task_asr_enabled: true\n"
        "modes:\n  team:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    config.update_team_organization_ui_in_config(enabled)
    text = path.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    assert payload["experimental"]["team_organization_ui_enabled"] is enabled
    assert payload["experimental"]["task_asr_enabled"] is True
    assert payload["modes"]["team"]["enabled"] is True
    assert "# user configuration" in text


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, "false"),
        ({"experimental": None}, "false"),
        ({"experimental": {"team_organization_ui_enabled": True}}, "true"),
    ],
)
def test_panel_defaults_to_off(monkeypatch, raw, expected):
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: raw)
    assert config_service.get_panel()["team_organization_ui_enabled"] == expected


def test_switch_reload_is_ui_only():
    changes = ConfigChangeSet(
        env_updates={}, yaml_updated=["team_organization_ui_enabled"]
    )
    assert changes.reload_scopes == {"web_ui"}
