# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for the JiuwenSwarm system-prompt priority registry."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.prompt.priority_registry import (
    PromptPriorityRegistry,
)


def test_runtime_section_can_be_registered_and_unregistered() -> None:
    registry = PromptPriorityRegistry({"static": 10})

    registry.register_section("runtime_only", 120)
    assert registry.priority_for("runtime_only", 999) == 120
    assert "runtime_only" in registry.priorities()

    registry.unregister_section("runtime_only")
    assert registry.priority_for("runtime_only", 999) == 999
    assert "runtime_only" not in registry.priorities()


def test_runtime_section_cannot_change_priority_for_same_name() -> None:
    registry = PromptPriorityRegistry({})
    registry.register_section("stable_name", 120)

    with pytest.raises(ValueError):
        registry.register_section("stable_name", 121)


def test_unregister_does_not_remove_static_policy() -> None:
    registry = PromptPriorityRegistry({"static": 10})

    registry.unregister_section("static")

    assert registry.priority_for("static", 999) == 10


def test_static_section_cannot_be_registered_with_a_different_priority() -> None:
    registry = PromptPriorityRegistry({"static": 10})

    with pytest.raises(ValueError):
        registry.register_section("static", 11)


def test_duplicate_numeric_priority_is_allowed_and_visible() -> None:
    registry = PromptPriorityRegistry({"static": 10})
    registry.register_section("runtime_same_group", 10)

    assert registry.names_for_priority(10) == ("runtime_same_group", "static")
