# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for DesignRail adapter integration in interface_code.py.

Verifies the ``_build_design_rail`` builder gates on
``modes.code.sdd.enabled`` and falls back to ``None`` on config validation
failure (BC-005), never raising.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.code.rails.sdd.design_rail import DesignRail
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)

pytestmark = [pytest.mark.unit]


def _make_adapter(project_dir: Path) -> JiuwenSwarmCodeAdapter:
    """Create an adapter bypassing __init__ (unit-test isolation)."""
    adapter = JiuwenSwarmCodeAdapter.__new__(JiuwenSwarmCodeAdapter)
    adapter._project_dir = str(project_dir)
    return adapter


def _config_with_sdd(enabled: bool | None) -> dict:
    """Build a get_config() return value with the given sdd.enabled state."""
    if enabled is None:
        return {"modes": {"code": {}}}
    return {"modes": {"code": {"sdd": {"enabled": enabled}}}}


def test_build_design_rail_disabled_returns_none(tmp_path: Path) -> None:
    """sdd.enabled=false -> builder returns None (zero regression)."""
    adapter = _make_adapter(tmp_path)
    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        return_value=_config_with_sdd(False),
    ):
        result = adapter._build_design_rail()
    assert result is None


def test_build_design_rail_missing_returns_none(tmp_path: Path) -> None:
    """sdd.enabled missing -> default False -> builder returns None."""
    adapter = _make_adapter(tmp_path)
    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        return_value=_config_with_sdd(None),
    ):
        result = adapter._build_design_rail()
    assert result is None


def test_build_design_rail_enabled_returns_instance(tmp_path: Path) -> None:
    """sdd.enabled=true + valid config -> builder returns a DesignRail."""
    adapter = _make_adapter(tmp_path)
    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        return_value=_config_with_sdd(True),
    ):
        result = adapter._build_design_rail()
    assert result is not None
    assert isinstance(result, DesignRail)


def test_build_design_rail_validation_fail_returns_none(tmp_path: Path) -> None:
    """sdd.enabled=true but config validation fails -> None, no raise (BC-005)."""
    adapter = _make_adapter(tmp_path)
    from jiuwenswarm.agents.harness.code.rails.sdd.design_rail.config_loader import (
        ValidationResult,
    )

    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        return_value=_config_with_sdd(True),
    ), patch(
        "jiuwenswarm.agents.harness.code.rails.sdd.design_rail.config_loader.validate",
        return_value=ValidationResult(ok=False, errors=["missing_core_stages: x"]),
    ):
        result = adapter._build_design_rail()
    assert result is None
