# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Subagent Skill authorization must use the owning Agent's permission template."""

from __future__ import annotations

from typing import Any

import pytest

from openjiuwen.harness.security.skill_authorization import (
    reset_skill_authorization_context,
    setup_skill_authorization_context,
)

from jiuwenswarm.agents.harness.common.rails.permissions import config_loader
from jiuwenswarm.agents.harness.common.tools.subagent_executor.authorization import (
    install_subagent_authorization_wiring,
)


class _ChildAgent:
    def __init__(self) -> None:
        self._initialized = False
        self.rails: list[Any] = []

    def add_rail(self, rail: Any) -> None:
        self.rails.append(rail)

    def find_rails_by_type(self, _rail_types: tuple[type, ...]) -> list[Any]:
        return []


class _ParentAgent:
    def create_subagent(
        self,
        _subagent_type: str,
        _subsession_id: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> _ChildAgent:
        return _ChildAgent()


def test_subagent_wiring_uses_injected_agent_permission_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A management template must enable both child authorization rails."""
    monkeypatch.setattr(
        config_loader,
        "get_effective_permissions_config",
        lambda: {
            "enabled": True,
            "skill_authorization": {"enabled": False},
        },
    )
    parent = _ParentAgent()

    def template_provider() -> dict[str, Any]:
        return {
            "enabled": True,
            "skill_authorization": {"enabled": True},
        }

    token = setup_skill_authorization_context("session-1", "main")
    try:
        assert install_subagent_authorization_wiring(
            parent,
            config_provider=template_provider,
        )
        child = parent.create_subagent("general", "child-1")
    finally:
        reset_skill_authorization_context(token)

    assert len(child.rails) == 2
    assert child.rails[0]._feature_enabled() is True
    assert child.rails[1]._enabled() is True
