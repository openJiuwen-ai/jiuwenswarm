# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for trajectory store configuration resolution."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from jiuwenswarm.observability.config import (
    DEFAULT_QUEUE_SIZE,
    TrajectoryStoreSettings,
    load_trajectory_store_settings,
    session_database_path,
)

test_logger = logging.getLogger("tests.trajectory_config")


def test_config_defaults_keep_legacy_installations_disabled(tmp_path: Path) -> None:
    settings = load_trajectory_store_settings({}, workspace=tmp_path)

    assert settings == TrajectoryStoreSettings(
        enabled=False,
        database_path=tmp_path / ".trace" / "sessions",
        retention_days=7,
        queue_size=4096,
        batch_size=64,
        flush_interval_ms=200,
        poll_interval_ms=2000,
    )
    test_logger.info("legacy configs require the packaged explicit trajectory opt-in")


def test_config_resolves_relative_path_and_rejects_non_positive_limits(
    tmp_path: Path,
) -> None:
    settings = load_trajectory_store_settings(
        {
            "trajectory_ui": {
                "enabled": "false",
                "db_path": "custom/trajectory.sqlite3",
                "queue_size": 0,
                "batch_size": 12,
            }
        },
        workspace=tmp_path,
    )

    assert settings.enabled is False
    assert settings.database_path == tmp_path / "custom" / "trajectory.sqlite3"
    assert settings.queue_size == DEFAULT_QUEUE_SIZE
    assert settings.batch_size == 12
    test_logger.info("trajectory config normalized paths and unsafe limits")


def test_session_database_path_is_deterministic_and_traversal_safe(
    tmp_path: Path,
) -> None:
    database_root = tmp_path / "trajectory-sessions"

    first = session_database_path(database_root, "../../outside\\session")
    second = session_database_path(database_root, "../../outside\\session")

    assert first == second
    assert first.parent.parent == database_root
    assert re.fullmatch(r"[0-9a-f]{2}", first.parent.name)
    assert re.fullmatch(r"[0-9a-f]{64}\.sqlite3", first.name)
    assert "outside" not in str(first.relative_to(database_root))
    test_logger.info("session database paths contain only SHA-256 components")
