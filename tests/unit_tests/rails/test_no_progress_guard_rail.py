# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for no_progress_guard_rail.py
"""
from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.no_progress_guard_rail import (
    NoProgressGuardConfig,
    NoProgressGuardRail,
)


@pytest.fixture
def enabled_config():
    """Fixture for an enabled config."""
    return NoProgressGuardConfig(
        enabled=True,
        max_consecutive_empty_answers=3,
        min_answer_chars=80,
        steering_nudge_threshold=2,
    )


@pytest.fixture
def disabled_config():
    """Fixture for a disabled config."""
    return NoProgressGuardConfig(enabled=False)


def test_config_defaults():
    """Test that NoProgressGuardConfig has the expected defaults."""
    config = NoProgressGuardConfig()
    assert config.enabled is False
    assert config.max_consecutive_empty_answers == 3
    assert config.min_answer_chars == 20
    assert config.steering_nudge_threshold == 2


def test_config_custom_values():
    """Test NoProgressGuardConfig with custom values."""
    config = NoProgressGuardConfig(
        enabled=True,
        max_consecutive_empty_answers=5,
        min_answer_chars=100,
        steering_nudge_threshold=4,
    )
    assert config.enabled is True
    assert config.max_consecutive_empty_answers == 5
    assert config.min_answer_chars == 100
    assert config.steering_nudge_threshold == 4


def test_rail_initialization(enabled_config):
    """Test that NoProgressGuardRail initializes correctly."""
    rail = NoProgressGuardRail(enabled_config, language="en")
    assert rail.config == enabled_config
    assert rail.language == "en"


def test_rail_initialization_with_chinese(enabled_config):
    """Test NoProgressGuardRail with Chinese language."""
    rail = NoProgressGuardRail(enabled_config, language="cn")
    assert rail.language == "cn"


def test_rail_disabled(disabled_config):
    """Test that a disabled rail can be created but does nothing."""
    rail = NoProgressGuardRail(disabled_config, language="en")
    assert rail.config.enabled is False
    assert rail.language == "en"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])