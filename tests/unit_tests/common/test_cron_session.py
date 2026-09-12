# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for cron execution session identity."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.cron_session import is_cron_execution_session


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
