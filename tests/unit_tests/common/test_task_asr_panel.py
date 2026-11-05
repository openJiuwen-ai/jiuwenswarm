# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.server.control import config_service


@pytest.mark.parametrize("experimental,expected", [
    ({}, "false"),
    ({"task_asr_enabled": False}, "false"),
    ({"task_asr_enabled": True}, "true"),
])
def test_panel_returns_saved_task_asr_switch(monkeypatch, experimental, expected):
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: {"experimental": experimental})
    assert config_service.get_panel()["task_asr_enabled"] == expected
