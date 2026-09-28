# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.common.auth import account_kit, remote_config


@pytest.fixture
def config_configured(monkeypatch):
    monkeypatch.setattr(account_kit, "config_url", lambda: "https://config.example.com")


def _config(*, effective: bool):
    return remote_config.parse_config({"is_effective": effective})


def test_running_campaign_is_active(monkeypatch, config_configured):
    monkeypatch.setattr(account_kit, "get_remote_config", lambda: _config(effective=True))
    assert account_kit.campaign_state() == "active"


def test_finished_campaign_is_ended(monkeypatch, config_configured):
    monkeypatch.setattr(account_kit, "get_remote_config", lambda: _config(effective=False))
    assert account_kit.campaign_state() == "ended"


def test_unreachable_config_is_its_own_state(monkeypatch, config_configured):
    monkeypatch.setattr(account_kit, "get_remote_config", lambda: None)
    assert account_kit.campaign_state() == "unavailable"


def test_locally_disabled_shows_nothing(monkeypatch):
    monkeypatch.setattr(account_kit, "config_url", lambda: "")
    monkeypatch.setattr(account_kit, "get_remote_config", lambda: pytest.fail("关掉了就不该去读配置"))
    assert account_kit.campaign_state() == "off"
