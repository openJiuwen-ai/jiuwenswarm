# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""persist_to_owner_scope must refresh the rail after disk write (1B)."""

from __future__ import annotations

from unittest.mock import Mock

from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    persist_to_owner_scope,
)


def test_persist_to_owner_scope_calls_rail_update_config(monkeypatch):
    persisted = {
        "enabled": True,
        "owner_scopes": {"web": {"u1": {"tools": {"bash": "allow"}}}},
    }
    mutate_calls: list[dict] = []

    def fake_persist(mutate_fn, **_kwargs):
        body = {"enabled": True, "owner_scopes": {}}
        mutate_fn(body)
        mutate_calls.append(body)
        return persisted

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.persist_permissions_mutate",
        fake_persist,
    )
    rail = Mock()
    config: dict = {"enabled": True, "tools": {}}

    assert persist_to_owner_scope(
        "bash", "allow", "web", "u1", config, rail=rail
    ) is True
    assert mutate_calls
    assert config["owner_scopes"]["web"]["u1"]["tools"]["bash"] == "allow"
    rail.update_config.assert_called_once_with(persisted)


def test_persist_to_owner_scope_without_rail_still_writes(monkeypatch):
    def fake_persist(mutate_fn, **_kwargs):
        body = {"owner_scopes": {}}
        mutate_fn(body)
        return body

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.persist_permissions_mutate",
        fake_persist,
    )
    config: dict = {}
    assert persist_to_owner_scope("bash", "deny", "im", "u2", config) is True
    assert config["owner_scopes"]["im"]["u2"]["tools"]["bash"] == "deny"
