# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for cron execution session identity."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.cron_session import (
    cron_session_matches_job,
    is_cron_execution_context,
    is_cron_execution_session,
)


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        ("cron_19abc_job1", True),
        ("cron_jobid", True),
        # Unified pre-allocation ids (create_session on the __cron__ channel).
        ("__cron___19abc_rand12", True),
        ("__cron___1a0d26c410a_rand", True),
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


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # 调度器在 cron 请求上打的标记（cron 运行当轮的请求信号）。
        ({"request_metadata": {"cron": {"job_id": "j1"}}}, True),
        # 跨进程转发的会话级 cron 归属（本轮不是调度器触发）。
        ({"request_metadata": {"cron_session": {"job_id": "j1"}}}, True),
        # 单 Agent 链路的内部执行渠道。
        ({"channel_id": "__cron__"}, True),
        # 隔离执行会话的 ID 约定（cron_* / __cron___*）。
        ({"session_id": "cron_19abc_job1"}, True),
        ({"session_id": "__cron___19abc_rand12"}, True),
        # 普通会话不命中。
        ({"session_id": "sess_19abc"}, False),
        ({"session_id": ""}, False),
        ({"session_id": None}, False),
    ],
)
def test_is_cron_execution_context_request_signals(kwargs, expected):
    """请求级信号与 ID 约定都不需要读会话元数据。"""
    kwargs.setdefault("request_metadata", {})
    kwargs.setdefault("session_id", None)
    assert is_cron_execution_context(**kwargs) is expected


def test_is_cron_execution_context_uses_caller_supplied_cron_id() -> None:
    """Web 定时任务执行会话是普通 ``web_*`` ID，只能靠持久化的 cron_id 识别。

    读取会话元数据属于上层职责（``common`` 不得 import ``server``，R1 分层
    规则），这里只验证映射传入后的判定。
    """
    assert (
        is_cron_execution_context(
            "web_19abc_abcdef123456",
            channel_id="web",
            request_metadata={"mode": "team"},
            session_metadata={"cron_id": "job-42"},
        )
        is True
    )


def test_is_cron_execution_context_ignores_normal_web_session() -> None:
    assert (
        is_cron_execution_context(
            "web_19abc_abcdef123456",
            channel_id="web",
            request_metadata={"mode": "team"},
            session_metadata={"cron_id": "", "mode": "team"},
        )
        is False
    )


def test_is_cron_execution_context_does_not_read_session_metadata(monkeypatch) -> None:
    """``common`` 层不得读会话元数据；未传入时按普通会话处理。"""
    from jiuwenswarm.server.runtime.session import session_metadata

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("common must not read session metadata")

    monkeypatch.setattr(session_metadata, "get_session_metadata", _unexpected)

    assert (
        is_cron_execution_context(
            "web_19abc_abcdef123456",
            channel_id="web",
            request_metadata={"mode": "team"},
        )
        is False
    )
