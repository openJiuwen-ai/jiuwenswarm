# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Voice entry switches survive reads through the Control config service."""

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.server.control import config_service


@pytest.mark.parametrize('enabled', [True, False])
def test_voice_switches_round_trip(monkeypatch, tmp_path, enabled):
    path = tmp_path / 'config.yaml'
    path.write_text('experimental:\n  team_organization_ui_enabled: true\n', encoding='utf-8')
    monkeypatch.setattr(config, 'CONFIG_YAML_PATH', path)
    monkeypatch.setattr(config_service, 'get_config_file', lambda: path)
    config.update_task_full_duplex_in_config(enabled)
    config.update_task_asr_in_config(not enabled)
    payload = config_service.get_panel()
    assert payload['task_full_duplex_enabled'] == str(enabled).lower()
    assert payload['task_asr_enabled'] == str(not enabled).lower()
    assert payload['team_organization_ui_enabled'] == 'true'


@pytest.mark.parametrize('raw', [{}, {'experimental': None}])
def test_missing_voice_switches_default_to_disabled(monkeypatch, raw):
    monkeypatch.setattr(config_service, '_read_raw_config', lambda: raw)
    payload = config_service.get_panel()
    assert payload['task_full_duplex_enabled'] == 'false'
    assert payload['task_asr_enabled'] == 'false'


def test_config_read_failure_disables_voice_entries(monkeypatch):
    def fail_read():
        raise OSError('configuration unavailable')
    monkeypatch.setattr(config_service, '_read_raw_config', fail_read)
    payload = config_service.get_panel()
    assert payload['task_full_duplex_enabled'] == 'false'
    assert payload['task_asr_enabled'] == 'false'
