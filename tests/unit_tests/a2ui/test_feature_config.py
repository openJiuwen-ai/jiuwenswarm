# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations


def test_a2ui_config_defaults_disabled():
    from jiuwenswarm.server.runtime.a2ui.config import get_a2ui_config

    cfg = get_a2ui_config({})

    assert cfg.enabled is False
    assert cfg.protocol_version == "0.8"
    assert cfg.stream_validation_enabled is True
    assert cfg.non_web_fallback_enabled is False
    assert cfg.dev_smoke_tools_enabled is False


def test_a2ui_config_env_can_enable_feature(monkeypatch):
    from jiuwenswarm.server.runtime.a2ui.config import get_a2ui_config

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    cfg = get_a2ui_config({"a2ui": {"enabled": False}})

    assert cfg.enabled is True


def test_legacy_jiuwenclaw_env_alias_no_longer_overrides_config(monkeypatch):
    from jiuwenswarm.server.runtime.a2ui.config import get_a2ui_config

    monkeypatch.delenv("JIUWENSWARM_A2UI_ENABLED", raising=False)
    monkeypatch.setenv("JIUWENCLAW_A2UI_ENABLED", "false")

    cfg = get_a2ui_config({"a2ui": {"enabled": True}})

    assert cfg.enabled is True


def test_a2ui_config_rejects_unknown_protocol_version():
    import pytest

    from jiuwenswarm.server.runtime.a2ui.config import get_a2ui_config

    with pytest.raises(ValueError, match="Unsupported A2UI protocol version"):
        get_a2ui_config({"a2ui": {"protocol_version": "0.9"}})
