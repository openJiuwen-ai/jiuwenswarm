# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.server.control import config_service


@pytest.mark.parametrize('saved', [
    {'asr_api_base': 'https://asr.example/v1', 'asr_api_key': 'saved-secret', 'asr_model': 'saved-model'},
    {'asr_api_base': '', 'asr_api_key': '', 'asr_model': ''},
])
def test_asr_saved_config_overrides_stale_process_environment(monkeypatch, tmp_path, saved):
    path = tmp_path / '.env'
    env_keys = {'asr_api_base': 'ASR_API_BASE', 'asr_api_key': 'ASR_API_KEY', 'asr_model': 'ASR_MODEL_NAME'}
    path.write_text('\n'.join(f'{env_keys[key]}="{value}"' for key, value in saved.items()), encoding='utf-8')
    monkeypatch.setattr(config_service, 'get_env_file', lambda: path)
    monkeypatch.setattr(config_service, '_read_raw_config', lambda: {})
    for env_key in env_keys.values():
        monkeypatch.setenv(env_key, 'stale-gateway-value')
    payload = config_service.get_panel()
    assert {key: payload[key] for key in saved} == saved
