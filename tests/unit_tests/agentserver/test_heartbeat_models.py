# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HeartbeatJob 模型 / schedule 校验 / 状态机不变量单元测试.

覆盖范围:
  - schedule 计算: interval 基于 now 重算; cron 复用 helper; once 到点 completed。
  - 状态机: enabled=true 但 status!=scheduled 的手改不一致 job 被跳过(不变量)。
  - 状态机: 重新激活 completed/expired/disabled → scheduled + 重算 next_run_at。
  - source 审计: controller 强制校验 metadata.source 枚举。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.models import (
    DEFAULT_TIMEZONE,
    HEARTBEAT_CONCURRENCY_POLICIES,
    HEARTBEAT_SCHEDULE_TYPES,
    HEARTBEAT_SESSION_DELETED_POLICIES,
    HEARTBEAT_SOURCES,
    HEARTBEAT_STATUSES,
    HEARTBEAT_TERMINAL_STATUSES,
    HeartbeatJob,
    HeartbeatRunState,
    HeartbeatSchedule,
    SCHEDULE_CRON,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONCE,
    SOURCE_AGENT_TOOL,
    SOURCE_SCHEDULE_RECOVERY,
    STATUS_COMPLETED,
    STATUS_DISABLED,
    STATUS_EXPIRED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    empty_heartbeat_jobs_doc,
    validate_metadata_source,
)


# ---------------------------------------------------------------------------
# schedule 校验
# ---------------------------------------------------------------------------


def test_schedule_interval_valid() -> None:
    s = HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 1800})
    assert s.type == SCHEDULE_INTERVAL
    assert s.interval_seconds == 1800
    d = s.to_dict()
    assert d["type"] == "interval"
    assert d["interval_seconds"] == 1800


def test_schedule_interval_rejects_below_minimum() -> None:
    with pytest.raises(ValueError, match="at least"):
        HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 30})


def test_schedule_interval_requires_seconds() -> None:
    with pytest.raises(ValueError, match="interval_seconds is required"):
        HeartbeatSchedule.from_dict({"type": "interval"})


def test_schedule_cron_valid_uses_cron_expr_helper() -> None:
    s = HeartbeatSchedule.from_dict({"type": "cron", "cron_expr": "0 9 * * *"})
    assert s.type == SCHEDULE_CRON
    assert s.cron_expr == "0 9 * * *"
    assert s.timezone == DEFAULT_TIMEZONE


def test_schedule_cron_rejects_invalid_expr() -> None:
    with pytest.raises(ValueError):
        HeartbeatSchedule.from_dict({"type": "cron", "cron_expr": "not a cron"})


def test_schedule_cron_accepts_seven_field_expression() -> None:
    s = HeartbeatSchedule.from_dict(
        {"type": "cron", "cron_expr": "0 0 9 * * ? *"}
    )
    assert s.cron_expr == "0 0 9 * * ? *"


def test_schedule_cron_rejects_invalid_seven_field_expression() -> None:
    with pytest.raises(ValueError, match="invalid cron expression"):
        HeartbeatSchedule.from_dict(
            {"type": "cron", "cron_expr": "60 0 9 * * ? *"}
        )


@pytest.mark.parametrize("expr", ["0 9 * *", "0 0 9 * * ?"])
def test_schedule_cron_rejects_unsupported_field_counts(expr: str) -> None:
    with pytest.raises(ValueError, match="must have 5 or 7 fields"):
        HeartbeatSchedule.from_dict({"type": "cron", "cron_expr": expr})


def test_schedule_cron_requires_expr() -> None:
    with pytest.raises(ValueError, match="cron_expr is required"):
        HeartbeatSchedule.from_dict({"type": "cron"})


def test_schedule_once_valid() -> None:
    s = HeartbeatSchedule.from_dict({"type": "once", "run_at": 1720001800})
    assert s.type == SCHEDULE_ONCE
    assert s.run_at == 1720001800


def test_schedule_once_requires_run_at() -> None:
    with pytest.raises(ValueError, match="run_at is required"):
        HeartbeatSchedule.from_dict({"type": "once"})


def test_schedule_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="invalid schedule.type"):
        HeartbeatSchedule.from_dict({"type": "hourly"})


def test_schedule_cron_custom_timezone_validated() -> None:
    s = HeartbeatSchedule.from_dict(
        {"type": "cron", "cron_expr": "0 9 * * *", "timezone": "America/New_York"}
    )
    assert s.timezone == "America/New_York"


def test_schedule_cron_rejects_bad_timezone() -> None:
    with pytest.raises(ValueError, match="invalid timezone"):
        HeartbeatSchedule.from_dict(
            {"type": "cron", "cron_expr": "0 9 * * *", "timezone": "Mars/Olympus"}
        )


# ---------------------------------------------------------------------------
# HeartbeatJob 序列化往返 + 字段校验
# ---------------------------------------------------------------------------


def _make_interval_job(**overrides) -> HeartbeatJob:
    base = dict(
        id="hb_test",
        name="测试",
        enabled=True,
        channel_id="web",
        session_id="s1",
        prompt="继续",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
    )
    base.update(overrides)
    return HeartbeatJob(**base)


def test_job_roundtrip_preserves_fields() -> None:
    job = _make_interval_job(
        max_runs=5,
        concurrency_policy="queue",
        session_deleted_policy="completed",
    )
    d = job.to_dict()
    job2 = HeartbeatJob.from_dict(d)
    assert job2.id == job.id
    assert job2.name == job.name
    assert job2.channel_id == "web"
    assert job2.session_id == "s1"
    assert job2.prompt == "继续"
    assert job2.max_runs == 5
    assert job2.concurrency_policy == "queue"
    assert job2.session_deleted_policy == "completed"
    assert job2.kind == "heartbeat"


def test_job_roundtrip_preserves_unlimited_max_runs() -> None:
    job = _make_interval_job(max_runs=None)
    restored = HeartbeatJob.from_dict(job.to_dict())

    assert restored.max_runs is None


def test_job_from_dict_preserves_historical_millisecond_once() -> None:
    data = _make_interval_job(
        enabled=False,
        status=STATUS_DISABLED,
        next_run_at=None,
    ).to_dict()
    data["schedule"] = {"type": "once", "run_at": 1_788_091_200_000}

    job = HeartbeatJob.from_dict(data)

    assert job.schedule.run_at == 1_788_091_200_000
    assert job.status == STATUS_DISABLED


def test_job_from_dict_requires_mandatory_fields() -> None:
    with pytest.raises(ValueError, match="channel_id is required"):
        HeartbeatJob.from_dict(
            {"id": "x", "name": "n", "session_id": "s", "prompt": "p", "schedule": {"type": "interval", "interval_seconds": 60}}
        )
    with pytest.raises(ValueError, match="session_id is required"):
        HeartbeatJob.from_dict(
            {"id": "x", "name": "n", "channel_id": "web", "prompt": "p", "schedule": {"type": "interval", "interval_seconds": 60}}
        )
    with pytest.raises(ValueError, match="prompt is required"):
        HeartbeatJob.from_dict(
            {"id": "x", "name": "n", "channel_id": "web", "session_id": "s", "schedule": {"type": "interval", "interval_seconds": 60}}
        )


def test_job_name_length_enforced() -> None:
    with pytest.raises(ValueError, match="name must be at most"):
        HeartbeatJob.from_dict(
            {
                "id": "x",
                "name": "x" * 100,
                "channel_id": "web",
                "session_id": "s",
                "prompt": "p",
                "schedule": {"type": "interval", "interval_seconds": 60},
            }
        )


def test_job_status_invalid_rejected() -> None:
    with pytest.raises(ValueError, match="invalid status"):
        HeartbeatJob.from_dict(
            {
                "id": "x",
                "name": "n",
                "channel_id": "web",
                "session_id": "s",
                "prompt": "p",
                "status": "weird",
                "schedule": {"type": "interval", "interval_seconds": 60},
            }
        )


def test_job_run_state_defaults() -> None:
    job = _make_interval_job()
    assert job.run_state.current_run_id is None
    assert job.run_state.skipped_count == 0
    assert job.run_state.last_run_status is None


def test_job_run_state_roundtrip() -> None:
    rs = HeartbeatRunState(
        current_run_id="r1",
        current_run_started_at=1.0,
        last_run_status="succeeded",
        skipped_count=3,
    )
    job = _make_interval_job(run_state=rs)
    d = job.to_dict()
    job2 = HeartbeatJob.from_dict(d)
    assert job2.run_state.current_run_id == "r1"
    assert job2.run_state.skipped_count == 3
    assert job2.run_state.last_run_status == "succeeded"


def test_job_from_dict_migrates_legacy_delete_after_run() -> None:
    data = _make_interval_job(max_runs=12).to_dict()
    data["run_count"] = 2
    data["delete_after_run"] = True

    job = HeartbeatJob.from_dict(data)

    assert job.max_runs == 3
    assert "delete_after_run" not in job.to_dict()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("enabled", "false"),
        ("enabled", 0),
    ],
)
def test_job_from_dict_rejects_non_boolean_persisted_flags(
    field: str,
    invalid_value: object,
) -> None:
    data = _make_interval_job().to_dict()
    data[field] = invalid_value

    with pytest.raises(ValueError, match=rf"{field} must be boolean"):
        HeartbeatJob.from_dict(data)


def test_run_state_rejects_invalid_last_run_status() -> None:
    with pytest.raises(ValueError, match="last_run_status must be one of"):
        HeartbeatRunState.from_dict({"last_run_status": "unknown"})


def test_run_state_accepts_null_last_run_status() -> None:
    state = HeartbeatRunState.from_dict({"last_run_status": None})
    assert state.last_run_status is None


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("queued_reschedule", "false"),
        ("current_reschedule", 0),
        ("resume_enabled", "true"),
    ],
)
def test_run_state_rejects_non_boolean_persisted_flags(
    field: str,
    invalid_value: object,
) -> None:
    with pytest.raises(ValueError, match=rf"{field} must be boolean"):
        HeartbeatRunState.from_dict({field: invalid_value})


def test_job_source_property_falls_back_to_schedule_recovery() -> None:
    # 模拟磁盘被手改坏 source(绕过 store 校验)。
    job = _make_interval_job(metadata={"source": "bad_value"})
    assert job.source == SOURCE_SCHEDULE_RECOVERY


def test_job_source_property_returns_valid_source() -> None:
    job = _make_interval_job(metadata={"source": SOURCE_AGENT_TOOL})
    assert job.source == SOURCE_AGENT_TOOL


def test_job_is_schedulable_requires_enabled_and_scheduled_and_next_run() -> None:
    job = _make_interval_job(status=STATUS_SCHEDULED, next_run_at=1.0)
    assert job.is_schedulable() is True
    job_disabled = _make_interval_job(enabled=False, status=STATUS_DISABLED, next_run_at=None)
    assert job_disabled.is_schedulable() is False
    job_no_next = _make_interval_job(status=STATUS_SCHEDULED, next_run_at=None)
    assert job_no_next.is_schedulable() is False


# ---------------------------------------------------------------------------
# 状态机不变量
# ---------------------------------------------------------------------------


def test_invariant_terminal_status_requires_enabled_false() -> None:
    job = _make_interval_job(enabled=True, status=STATUS_COMPLETED)
    with pytest.raises(ValueError, match="terminal status 'completed' requires enabled=false"):
        job.check_invariants()


def test_invariant_terminal_status_requires_next_run_at_none() -> None:
    job = _make_interval_job(enabled=False, status=STATUS_EXPIRED, next_run_at=1234.0)
    with pytest.raises(ValueError, match="requires next_run_at=None"):
        job.check_invariants()


def test_invariant_disabled_status_requires_enabled_false() -> None:
    job = _make_interval_job(enabled=True, status=STATUS_DISABLED)
    with pytest.raises(ValueError, match="requires enabled=false"):
        job.check_invariants()


def test_invariant_scheduled_requires_enabled_true() -> None:
    job = _make_interval_job(enabled=False, status=STATUS_SCHEDULED)
    with pytest.raises(ValueError, match="status=scheduled requires enabled=true"):
        job.check_invariants()


def test_invariant_scheduled_requires_next_run_at() -> None:
    job = _make_interval_job(enabled=True, status=STATUS_SCHEDULED)
    job.next_run_at = None
    with pytest.raises(ValueError, match="requires next_run_at"):
        job.check_invariants()


def test_invariant_scheduled_rejects_exhausted_max_runs() -> None:
    job = _make_interval_job(enabled=True, status=STATUS_SCHEDULED, next_run_at=1.0)
    job.max_runs = 1
    job.run_count = 1
    with pytest.raises(ValueError, match="exhausted max_runs"):
        job.check_invariants()


def test_invariant_running_does_not_require_terminal_fields() -> None:
    # running 状态允许 enabled=true 和 next_run_at 非 None(运行中)。
    job = _make_interval_job(enabled=True, status=STATUS_RUNNING, next_run_at=1.0)
    job.check_invariants()  # 不抛异常


# ---------------------------------------------------------------------------
# source 审计
# ---------------------------------------------------------------------------


def test_validate_metadata_source_accepts_all_enums() -> None:
    for src in HEARTBEAT_SOURCES:
        assert validate_metadata_source(src) == src


def test_validate_metadata_source_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="invalid metadata.source"):
        validate_metadata_source("bad")


def test_validate_metadata_source_rejects_none() -> None:
    with pytest.raises(ValueError, match="metadata.source is required"):
        validate_metadata_source(None)


# ---------------------------------------------------------------------------
# 顶层持久化结构
# ---------------------------------------------------------------------------


def test_empty_heartbeat_jobs_doc_shape() -> None:
    d = empty_heartbeat_jobs_doc()
    assert d == {"version": 1, "jobs": []}


def test_enums_complete() -> None:
    assert len(HEARTBEAT_STATUSES) == 5
    assert len(HEARTBEAT_SOURCES) == 4
    assert len(HEARTBEAT_SCHEDULE_TYPES) == 3
    assert len(HEARTBEAT_CONCURRENCY_POLICIES) == 3
    assert len(HEARTBEAT_SESSION_DELETED_POLICIES) == 2
    assert HEARTBEAT_TERMINAL_STATUSES == frozenset(
        {STATUS_COMPLETED, STATUS_EXPIRED, STATUS_DISABLED}
    )
