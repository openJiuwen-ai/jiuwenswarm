# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""resolve_permission_key: one path for personal and enterprise."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.permission_key import (
    DEFAULT_KEY,
    resolve_permission_key,
)


def test_personal_edition_ignores_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_key.is_enterprise",
        lambda: False,
    )
    assert resolve_permission_key() == DEFAULT_KEY
    assert (
        resolve_permission_key(group_id="g", bot_id="b", user_id="u") == DEFAULT_KEY
    )


def test_enterprise_edition_complete_triad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_key.is_enterprise",
        lambda: True,
    )
    assert (
        resolve_permission_key(group_id="g1", bot_id="b1", user_id="u1")
        == "group:g1/bot:b1/user:u1"
    )


def test_enterprise_incomplete_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_key.is_enterprise",
        lambda: True,
    )
    assert resolve_permission_key(group_id="g1", bot_id="b1") == DEFAULT_KEY
    assert resolve_permission_key() == DEFAULT_KEY
    assert resolve_permission_key(bot_id="only-bot") == DEFAULT_KEY
