# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unregistered subagent configuration fails closed without empty registration."""

from __future__ import annotations

import pytest
from agent_shield.tool_permission import get_permission_manager
from jiuwenswarm.agents.harness.common.rails.permissions.published_scopes import (
    find_published_scopes,
)


def test_unpublished_subagent_scope_does_not_register_empty_key():
    key = "subagent-empty-key-fixture"
    manager = get_permission_manager()
    if manager.has_config(key):
        manager.remove_config(key)

    with pytest.raises(KeyError):
        find_published_scopes(key, "unregistered-session")
    assert not manager.has_config(key)
