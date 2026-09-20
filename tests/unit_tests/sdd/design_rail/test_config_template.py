# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for resources/config.yaml — modes.code.sdd template addition."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

_CONFIG_PATH = (
    Path(__file__).resolve().parents[4]
    / "jiuwenswarm"
    / "resources"
    / "config.yaml"
)


@pytest.fixture(scope="module")
def config_yaml() -> dict:
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    return yaml.safe_load(text)


def test_sdd_enabled_default_false(config_yaml: dict) -> None:
    """modes.code.sdd.enabled must exist and default to False (NFR-001)."""
    code_cfg = config_yaml["modes"]["code"]
    assert "sdd" in code_cfg
    assert code_cfg["sdd"]["enabled"] is False


def test_existing_memory_key_unchanged(config_yaml: dict) -> None:
    """The pre-existing memory.enabled key is untouched."""
    code_cfg = config_yaml["modes"]["code"]
    assert "memory" in code_cfg
    assert code_cfg["memory"]["enabled"] is True


def test_existing_code_keys_unchanged(config_yaml: dict) -> None:
    """rails / tools / embedding_config remain present (scope fence)."""
    code_cfg = config_yaml["modes"]["code"]
    for key in ("rails", "tools", "embedding_config"):
        assert key in code_cfg, f"{key} must remain in modes.code"
