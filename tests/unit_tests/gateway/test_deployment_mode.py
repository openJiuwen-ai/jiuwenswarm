# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""deployment_mode helper：三种模式的归一化与能力判断。"""

from __future__ import annotations

import pytest

from jiuwenswarm.deployment_mode import (
    MODE_ACTIVE_STANDBY,
    MODE_DISTRIBUTED,
    MODE_STANDALONE,
    VALID_DEPLOYMENT_MODES,
    channel_config_overlay_default,
    default_cron_enabled,
    distributed_channel_whitelist,
    history_storage_backend,
    normalize_deployment_mode,
    session_storage_backend,
    uses_gateway_redis,
    uses_leader_election,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("standalone", MODE_STANDALONE),
        ("active-standby", MODE_ACTIVE_STANDBY),
        ("distributed", MODE_DISTRIBUTED),
        ("  Distributed  ", MODE_DISTRIBUTED),
        ("ACTIVE-STANDBY", MODE_ACTIVE_STANDBY),
        ("", MODE_STANDALONE),
        (None, MODE_STANDALONE),
        ("cluster", MODE_STANDALONE),
        (123, MODE_STANDALONE),
    ],
)
def test_normalize_deployment_mode(raw, expected):
    assert normalize_deployment_mode(raw) == expected


def test_valid_modes_cover_three():
    assert set(VALID_DEPLOYMENT_MODES) == {
        MODE_STANDALONE,
        MODE_ACTIVE_STANDBY,
        MODE_DISTRIBUTED,
    }


def test_uses_gateway_redis():
    assert not uses_gateway_redis(MODE_STANDALONE)
    assert uses_gateway_redis(MODE_ACTIVE_STANDBY)
    assert uses_gateway_redis(MODE_DISTRIBUTED)
    assert not uses_gateway_redis("bogus")


def test_uses_leader_election_only_active_standby():
    assert not uses_leader_election(MODE_STANDALONE)
    assert uses_leader_election(MODE_ACTIVE_STANDBY)
    assert not uses_leader_election(MODE_DISTRIBUTED)


def test_session_storage_backend():
    assert session_storage_backend(MODE_STANDALONE) == "local"
    assert session_storage_backend(MODE_ACTIVE_STANDBY) == "redis"
    assert session_storage_backend(MODE_DISTRIBUTED) == "redis"
    assert session_storage_backend("bogus") == "local"


def test_history_storage_backend():
    assert history_storage_backend(MODE_STANDALONE) == "memory"
    assert history_storage_backend(MODE_ACTIVE_STANDBY) == "mysql"
    assert history_storage_backend(MODE_DISTRIBUTED) == "mysql"
    assert history_storage_backend("bogus") == "memory"


def test_default_cron_enabled():
    assert default_cron_enabled(MODE_STANDALONE)
    assert default_cron_enabled(MODE_ACTIVE_STANDBY)
    assert not default_cron_enabled(MODE_DISTRIBUTED)


def test_channel_config_overlay_default_only_active_standby():
    assert not channel_config_overlay_default(MODE_STANDALONE)
    assert channel_config_overlay_default(MODE_ACTIVE_STANDBY)
    assert not channel_config_overlay_default(MODE_DISTRIBUTED)


def test_distributed_channel_whitelist():
    assert distributed_channel_whitelist() == frozenset({"web", "tui"})
