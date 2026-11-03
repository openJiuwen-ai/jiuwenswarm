# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""audit_emit：企业版审计实现经 external_providers 注入，未注册时静默降级。"""

from __future__ import annotations

import types

import pytest

from gateway.common import external_providers
from gateway.enterprise.capabilities.audit import audit_emit


@pytest.fixture(autouse=True)
def _enterprise_audit_enabled(monkeypatch):
    monkeypatch.setattr("gateway.enterprise.is_enterprise", lambda: True)
    monkeypatch.setattr(
        "gateway.enterprise.capabilities.audit.audit_gate.is_audit_config_enabled",
        lambda: True,
    )
    monkeypatch.setattr(external_providers, "_providers", {}, raising=False)


def test_provider_receives_lifted_identity_fields() -> None:
    calls: list[dict] = []
    external_providers.register(
        external_providers.AUDIT_EMIT_DOMAIN,
        types.SimpleNamespace(audit_claw_log=lambda **kwargs: calls.append(kwargs)),
    )

    audit_emit.emit_audit_ua(SUBMDL="gateway", PROC="proc", UID="u1", session_id="s1")

    assert len(calls) == 1
    assert calls[0]["submdl"] == "gateway"
    assert calls[0]["uid"] == "u1"
    assert calls[0]["session_id"] == "s1"
    assert calls[0]["success"] is True


def test_unregistered_provider_is_skipped_without_raising() -> None:
    assert audit_emit._resolve_audit_claw_log() is None

    audit_emit.emit_audit_evt(SUBMDL="gateway", PROC="proc", MSG="boom")
