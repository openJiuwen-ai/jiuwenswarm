# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved

"""app_gateway 企业版/外置 Runtime 配置下发跳过逻辑。"""

from __future__ import annotations

from jiuwenswarm.gateway.app_gateway import _uses_external_agent_config


def test_uses_external_agent_config_false_for_personal(monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.delenv("GATEWAY_RUNTIME_MANAGER_URL", raising=False)
    assert _uses_external_agent_config() is False


def test_uses_external_agent_config_true_for_enterprise(monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.delenv("GATEWAY_RUNTIME_MANAGER_URL", raising=False)
    assert _uses_external_agent_config() is True


def test_uses_external_agent_config_true_when_runtime_manager_url_set(
    monkeypatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setenv("GATEWAY_RUNTIME_MANAGER_URL", "http://runtime-manager:8091")
    assert _uses_external_agent_config() is True


def test_uses_external_agent_config_ignores_blank_runtime_manager_url(
    monkeypatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setenv("GATEWAY_RUNTIME_MANAGER_URL", "   ")
    assert _uses_external_agent_config() is False
