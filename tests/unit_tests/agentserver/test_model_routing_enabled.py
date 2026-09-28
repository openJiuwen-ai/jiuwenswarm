# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the model_routing enable gate (config + relay env override)."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _model_routing_enabled,
    _parse_bool,
)

_ENV = "JIUWENSWARM_MODEL_ROUTING_ENABLED"


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (False, False),
        (None, False),
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
        ("bogus", False),
    ],
)
def test_parse_bool(value, expected):
    assert _parse_bool(value) is expected


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    assert _model_routing_enabled(None) is False
    assert _model_routing_enabled({}) is False
    assert _model_routing_enabled({"model_routing": {}}) is False
    assert _model_routing_enabled({"model_routing": {"enabled": False}}) is False


def test_config_true_enables(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    assert _model_routing_enabled({"model_routing": {"enabled": True}}) is True


def test_config_string_true_enables(monkeypatch):
    """config.yaml 插值后可能是字符串，开关必须容忍。"""
    monkeypatch.delenv(_ENV, raising=False)
    assert _model_routing_enabled({"model_routing": {"enabled": "true"}}) is True


def test_env_overrides_config_false(monkeypatch):
    monkeypatch.setenv(_ENV, "true")
    assert _model_routing_enabled({"model_routing": {"enabled": False}}) is True


def test_env_overrides_config_true(monkeypatch):
    monkeypatch.setenv(_ENV, "false")
    assert _model_routing_enabled({"model_routing": {"enabled": True}}) is False


def test_empty_env_falls_back_to_config(monkeypatch):
    monkeypatch.setenv(_ENV, "  ")
    assert _model_routing_enabled({"model_routing": {"enabled": True}}) is True
    assert _model_routing_enabled({"model_routing": {"enabled": False}}) is False
