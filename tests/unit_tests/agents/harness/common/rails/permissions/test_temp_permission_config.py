# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""assemble_temp_permission_config: sole host merge for check-time config=."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.temp_permission_config import (
    assemble_temp_permission_config,
)


def test_no_overlay_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.has_session_permissions_overlay",
        lambda _sid=None: False,
    )
    assert (
        assemble_temp_permission_config(
            {"enabled": True, "defaults": {"*": "ask"}},
            session_id="s1",
            agent_scope_id="",
        )
        is None
    )


def test_session_overlay_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.has_session_permissions_overlay",
        lambda _sid=None: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.merge_session_permissions_overlay",
        lambda base, session_id=None: {**base, "tools": {"bash": "deny"}},
    )
    out = assemble_temp_permission_config(
        {"enabled": True, "defaults": {"*": "ask"}},
        session_id="s1",
        agent_scope_id="",
    )
    assert out is not None
    assert out["tools"]["bash"] == "deny"


def test_skill_overlay_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.has_session_permissions_overlay",
        lambda _sid=None: False,
    )
    monkeypatch.setattr(
        "openjiuwen.harness.security.skill_authorization.is_skill_authorization_enabled",
        lambda _cfg: True,
    )
    monkeypatch.setattr(
        "openjiuwen.harness.security.skill_authorization.compose_skill_permissions",
        lambda base, overlay: {**base, "tools": {**(base.get("tools") or {}), **overlay.get("tools", {})}},
    )
    scopes = SimpleNamespace(
        has_active_grant=lambda sid, scope: True,
        get_active=lambda sid, scope: SimpleNamespace(
            overlay_snapshot={"tools": {"get_current_user": "allow"}}
        ),
    )
    out = assemble_temp_permission_config(
        {"enabled": True, "defaults": {"*": "ask"}, "skill_authorization": {"enabled": True}},
        session_id="s1",
        agent_scope_id="main",
        published_scopes=scopes,
    )
    assert out is not None
    assert out["tools"]["get_current_user"] == "allow"
