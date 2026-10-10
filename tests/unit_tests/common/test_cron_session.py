# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for cron execution session identity."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.cron_session import (
    cron_session_matches_job,
    is_cron_execution_session,
)


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        ("cron_19abc_job1", True),
        ("cron_jobid", True),
        ("sess_19abc", False),
        ("heartbeat_1", False),
        ("", False),
        (None, False),
    ],
)
def test_is_cron_execution_session(session_id, expected):
    assert is_cron_execution_session(session_id) is expected


@pytest.mark.parametrize(
    ("session_id", "cron_id", "expected"),
    [
        # team 执行会话（cron_<ts>_<jobid>）与 proactive 稳定会话（cron_<jobid>）。
        ("cron_1a0d273b71b_9c29c6a6", "9c29c6a6", True),
        ("cron_9c29c6a6", "9c29c6a6", True),
        # 其它任务/非约定命名不命中。
        ("cron_1a0d273b71b_9c29c6a6", "f669b114", False),
        ("__cron___1a0d26c410a_rand", "rand", False),
        ("cron-session", "session", False),
        ("sess_19abc", "9c29c6a6", False),
        ("", "9c29c6a6", False),
        ("cron_1a0d273b71b_9c29c6a6", "", False),
        (None, "9c29c6a6", False),
    ],
)
def test_cron_session_matches_job(session_id, cron_id, expected):
    assert cron_session_matches_job(session_id, cron_id) is expected
