# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Manager logging level drives openjiuwen core loggers."""

import logging

from jiuwenswarm.common.openjiuwen_logging import apply_openjiuwen_log_level
from jiuwenswarm.common.utils import update_log_levels


def test_update_log_levels_forwards_agent_server_level_for_enterprise(monkeypatch):
    seen: dict[str, int] = {}

    def _capture(level: int) -> None:
        seen["level"] = level

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(
        "jiuwenswarm.common.openjiuwen_logging.apply_openjiuwen_log_level",
        _capture,
    )
    update_log_levels(agent_server="DEBUG", gateway="ERROR")
    assert seen["level"] == logging.DEBUG


def test_update_log_levels_leaves_openjiuwen_alone_for_personal(monkeypatch):
    def _capture(_level: int) -> None:
        raise AssertionError("personal edition must not retarget openjiuwen loggers")

    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.openjiuwen_logging.apply_openjiuwen_log_level",
        _capture,
    )
    update_log_levels(agent_server="DEBUG", gateway="ERROR")


def test_apply_openjiuwen_log_level_updates_live_and_future_loggers(tmp_path):
    from openjiuwen.core.common.logging.log_config import (
        configure_log_config,
        get_log_config_snapshot,
        set_log_path,
    )
    from openjiuwen.core.common.logging.manager import LogManager

    original = get_log_config_snapshot()
    staged = get_log_config_snapshot()
    staged["loggers"] = {"agent": {"level": logging.WARNING}}
    configure_log_config(staged)
    try:
        set_log_path(tmp_path)
        common = LogManager.get_logger("common")
        apply_openjiuwen_log_level(logging.DEBUG)

        assert common.logger().level == logging.DEBUG
        snapshot = get_log_config_snapshot()
        assert snapshot["level"] == logging.DEBUG
        assert "level" not in snapshot.get("loggers", {}).get("agent", {})

        created = LogManager.get_logger("agent")
        assert created.logger().level == logging.DEBUG
    finally:
        configure_log_config(original)
        LogManager.reset()
