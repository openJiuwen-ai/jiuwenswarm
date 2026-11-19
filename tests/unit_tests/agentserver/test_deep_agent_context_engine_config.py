# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ContextEngineConfig assembly from ``react.context_engine_config``."""

from pathlib import Path

import pytest
import yaml
from openjiuwen.core.context_engine.schema.config import ContextEngineConfig

from jiuwenswarm.server.runtime.agent_adapter.deep.agent_config_helpers import (
    _deep_agent_context_engine_config,
)

_RESOURCES_DIR = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "resources"
_SHIPPED_CONFIG = _RESOURCES_DIR / "config.yaml"


def test_enable_tiktoken_counter_is_forwarded_when_true():
    """The exact token counter switch must reach ContextEngineConfig."""
    config = _deep_agent_context_engine_config(
        {"context_engine_config": {"enable_tiktoken_counter": True}}
    )

    assert config.enable_tiktoken_counter is True


@pytest.mark.parametrize(
    "react_cfg",
    [{}, {"context_engine_config": {"enable_tiktoken_counter": False}}],
    ids=["absent", "explicit_false"],
)
def test_enable_tiktoken_counter_stays_false(react_cfg):
    """A missing key or an explicit opt-out must both fall back to character estimation."""
    config = _deep_agent_context_engine_config(react_cfg)

    assert config.enable_tiktoken_counter is False


def test_shipped_config_enables_exact_token_counting():
    """Guard the wiring: the shipped personal-edition template must carry the key and forward it."""
    react_cfg = yaml.safe_load(_SHIPPED_CONFIG.read_text(encoding="utf-8"))["react"]

    assert react_cfg["context_engine_config"]["enable_tiktoken_counter"] is True

    config = _deep_agent_context_engine_config(react_cfg)
    assert config.enable_tiktoken_counter is True


def test_exact_token_counter_flag_is_a_public_config_field():
    """跨仓契约：model_copy(update=...) 不校验字段，需显式守住 agent-core 侧存在性。"""
    assert "enable_tiktoken_counter" in ContextEngineConfig.model_fields
