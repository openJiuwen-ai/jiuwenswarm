# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Experimental UI switches share the same config contract only."""

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel.config_set_handlers import ConfigChangeSet
from jiuwenswarm.server.control import config_service

SWITCHES = [
    ("comfyui_workflow_enabled", config.update_comfyui_workflow_enabled_in_config),
    ("team_organization_ui_enabled", config.update_team_organization_ui_in_config),
]
SWITCH_IDS = [flag for flag, _ in SWITCHES]


@pytest.mark.parametrize(("flag", "update"), SWITCHES, ids=SWITCH_IDS)
@pytest.mark.parametrize("enabled", [False, True])
def test_switch_round_trip_preserves_other_settings(monkeypatch, tmp_path, flag, update, enabled):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# user configuration\nexperimental:\n  task_asr_enabled: true\n"
        "modes:\n  team:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    update(enabled)
    text = path.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    assert payload["experimental"][flag] is enabled
    assert payload["experimental"]["task_asr_enabled"] is True
    assert payload["modes"]["team"]["enabled"] is True
    assert "# user configuration" in text


@pytest.mark.parametrize(("flag", "update"), SWITCHES, ids=SWITCH_IDS)
@pytest.mark.parametrize(
    ("raw_factory", "expected"),
    [
        (lambda _flag: {}, "false"),
        (lambda _flag: {"experimental": None}, "false"),
        (lambda flag: {"experimental": {flag: True}}, "true"),
    ],
    ids=["empty", "experimental-none", "enabled"],
)
def test_panel_defaults_to_off(monkeypatch, flag, update, raw_factory, expected):
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: raw_factory(flag))
    assert config_service.get_panel()[flag] == expected


@pytest.mark.parametrize("flag", SWITCH_IDS)
def test_switch_reload_is_ui_only(flag):
    changes = ConfigChangeSet(env_updates={}, yaml_updated=[flag])
    assert changes.reload_scopes == {"web_ui"}
