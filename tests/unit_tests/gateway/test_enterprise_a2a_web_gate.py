# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.gateway.channel_manager.web import invoke as web_invoke


def test_enterprise_a2a_web_gate_only_allows_directory_toggle_and_history(
    monkeypatch,
):
    monkeypatch.setattr(web_invoke, "is_enterprise", lambda: True)
    allowed = {
        "a2a.outbound.list",
        "a2a.outbound.enabled.update",
        "a2a.outbound.dispatch.list",
        "a2a.outbound.dispatch.get",
    }
    blocked = {
        "a2a.outbound.settings.get",
        "a2a.outbound.settings.update",
        "a2a.outbound.discover",
        "a2a.outbound.register",
        "a2a.outbound.get",
        "a2a.outbound.edit",
        "a2a.outbound.update",
        "a2a.outbound.refresh",
        "a2a.outbound.confirm_revision",
        "a2a.outbound.delete",
        "a2a.ingress.get",
        "a2a.ingress.edit",
        "a2a.ingress.history",
        "a2a.ingress.update",
        "a2a.ingress.enable",
        "a2a.ingress.disable",
        "a2a.ingress.reload",
    }

    assert all(not web_invoke.is_enterprise_write_forbidden(item) for item in allowed)
    assert all(web_invoke.is_enterprise_write_forbidden(item) for item in blocked)
    monkeypatch.setattr(web_invoke, "is_enterprise", lambda: False)
    assert all(not web_invoke.is_enterprise_write_forbidden(item) for item in blocked)
