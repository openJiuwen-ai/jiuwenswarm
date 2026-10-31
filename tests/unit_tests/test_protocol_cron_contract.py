# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""cron 数据结构契约：``openjiuwen_protocol.cron_models`` 为唯一 source of truth。

两仓只子类化并补充行为（to_dict / from_dict），不得重新声明字段或改变默认可变性。
本用例锁定「可变 dataclass + 字段集合 + 默认值 + 常量」四项，防止协议仓或本仓
单侧漂移（历史上 4 个上游提交曾让 ``CronRunState`` 漂掉 7 个字段）。
"""

from __future__ import annotations

import dataclasses

from openjiuwen_protocol import cron_models as protocol

from gateway.runtime.cron import models as gateway_models
from jiuwenswarm.runtime.cron import models as jiuwenswarm_models


def _field_specs(cls: type) -> list[tuple[str, object]]:
    return [(field.name, field.default) for field in dataclasses.fields(cls)]


def test_cron_job_and_run_state_are_mutable_dataclasses() -> None:
    for cls in (protocol.CronJob, protocol.CronRunState):
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen is False
    assert protocol.CronTarget.__dataclass_params__.frozen is True


def test_repo_models_inherit_fields_without_redeclaring() -> None:
    for name in ("CronJob", "CronRunState", "CronTarget"):
        expected = _field_specs(getattr(protocol, name))
        assert _field_specs(getattr(gateway_models, name)) == expected
        assert _field_specs(getattr(jiuwenswarm_models, name)) == expected
    # 可变性随子类保留（gateway 侧 CronJob 允许调度器回写字段）
    job = gateway_models.CronJob(
        id="job",
        name="demo",
        enabled=True,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
    )
    job.targets = "web"
    assert job.targets == "web"


def test_cron_defaults_match_contract() -> None:
    job = protocol.CronJob(
        id="job",
        name="demo",
        enabled=True,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
    )
    assert job.wake_offset_seconds == 0
    assert job.description == ""
    assert job.expired is False
    assert job.targets == ""
    assert job.session_id is None
    assert job.created_at is None
    assert job.updated_at is None
    assert job.chat_type is None
    assert job.mode == protocol.CRON_JOB_DEFAULT_MODE
    assert job.delete_after_run is False
    assert job.timeout_seconds is None
    assert job.project_id == ""
    assert job.last_session_id is None
    assert job.model_name is None
    assert job.model_selection is None
    assert job.mcp is None
    assert job.app_id == ""
    assert job.user_id == ""
    assert job.credential_ref == ""
    assert job.work_mode == protocol.DEFAULT_WEB_WORK_MODE
    assert job.group_id == ""
    assert job.bot_id == ""
    assert job.next_run_at is None

    state = protocol.CronRunState(
        run_id="run",
        job_id="job",
        wake_at_iso="",
        push_at_iso="",
    )
    assert state.status == "pending"
    assert state.placeholder_sent is False
    assert state.pushed_final is False
    assert state.started_at is None
    assert state.finished_at is None
    assert state.exec_user_id is None
    assert state.execution_session_allocated is False
    assert state.manually_triggered is False
    assert state.live_final_forwarded is False
    assert state.live_seq == 0
    assert state.notification_revision == 0
    assert state.stream_error_seen is False


def test_repo_constants_match_protocol() -> None:
    for module in (gateway_models, jiuwenswarm_models):
        assert module.CRON_JOB_NAME_MAX_LENGTH == protocol.CRON_JOB_NAME_MAX_LENGTH
        assert (
            module.CRON_JOB_DESCRIPTION_MAX_LENGTH
            == protocol.CRON_JOB_DESCRIPTION_MAX_LENGTH
        )
        assert module.CRON_JOB_DEFAULT_MODE == protocol.CRON_JOB_DEFAULT_MODE
        assert module.DEFAULT_WEB_WORK_MODE == protocol.DEFAULT_WEB_WORK_MODE
