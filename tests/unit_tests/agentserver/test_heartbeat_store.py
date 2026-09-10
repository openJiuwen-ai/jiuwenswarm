# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HeartbeatJobStore 单元测试:CRUD + 状态机方法 + 状态机不变量 + reload.

覆盖范围:
  - 状态机: update/toggle 重新激活 completed/expired/disabled → scheduled + 重算 next_run_at。
  - 停止条件: terminal=true 执行后 completed 保留记录。
  - store reload: heartbeat_jobs.json 外部修改后 reload 生效。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.models import (
    HeartbeatSchedule,
    SOURCE_AGENT_TOOL,
    STATUS_COMPLETED,
    STATUS_DISABLED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
)
from jiuwenswarm.agents.harness.code.rails.heartbeat.store import (
    HeartbeatJobStore,
    HeartbeatStoreDataError,
)


@pytest.fixture
def store(tmp_path: Path) -> HeartbeatJobStore:
    return HeartbeatJobStore(path=tmp_path / "heartbeat_jobs.json")


def _interval_schedule(seconds: int = 120) -> HeartbeatSchedule:
    return HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": seconds})


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def test_create_job_assigns_id_and_defaults(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n",
        channel_id="web",
        session_id="s1",
        prompt="p",
        schedule=_interval_schedule(),
        source="agent_tool",
    )
    assert job.id.startswith("hb_")
    assert job.status == STATUS_SCHEDULED
    assert job.run_count == 0
    assert job.metadata["source"] == SOURCE_AGENT_TOOL


async def test_create_job_disabled_status(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n",
        channel_id="web",
        session_id="s1",
        prompt="p",
        schedule=_interval_schedule(),
        source="agent_tool",
        enabled=False,
    )
    assert job.status == STATUS_DISABLED
    assert job.enabled is False
    assert job.next_run_at is None


async def test_get_job_returns_none_for_missing(store: HeartbeatJobStore) -> None:
    assert await store.get_job("nope") is None
    assert await store.get_job("") is None


async def test_list_jobs_round_trip(store: HeartbeatJobStore) -> None:
    j1 = await store.create_job(
        name="a", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    j2 = await store.create_job(
        name="b", channel_id="web", session_id="s2", prompt="p",
        schedule=_interval_schedule(), source="web_rpc",
    )
    jobs = await store.list_jobs()
    assert len(jobs) == 2
    ids = {j.id for j in jobs}
    assert ids == {j1.id, j2.id}


async def test_seven_field_cron_survives_create_update_and_reload(
    store: HeartbeatJobStore,
) -> None:
    job = await store.create_job(
        name="seven-field", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "cron", "cron_expr": "0 0 9 * * ? *", "timezone": "Asia/Shanghai"}
        ),
        source="agent_tool",
    )
    updated = await store.update_job(
        job.id,
        {"schedule": {"type": "cron", "cron_expr": "30 0 10 * * ? *", "timezone": "Asia/Shanghai"}},
    )
    assert updated.schedule.cron_expr == "30 0 10 * * ? *"

    reloaded = await HeartbeatJobStore(path=store.path).get_job(job.id)
    assert reloaded is not None
    assert reloaded.schedule.cron_expr == "30 0 10 * * ? *"
    assert reloaded.next_run_at == updated.next_run_at


async def test_list_jobs_by_session(store: HeartbeatJobStore) -> None:
    await store.create_job(
        name="a", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    await store.create_job(
        name="b", channel_id="web", session_id="s2", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    s1_jobs = await store.list_jobs_by_session("s1")
    assert len(s1_jobs) == 1
    assert s1_jobs[0].session_id == "s1"


async def test_delete_job_physical_delete(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    assert await store.delete_job(job.id) is True
    assert await store.get_job(job.id) is None
    assert await store.delete_job(job.id) is False  # 已删除


# ---------------------------------------------------------------------------
# 原子状态机方法
# ---------------------------------------------------------------------------


async def test_claim_run_sets_current_run(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    decision, job, replaced = await store.claim_run(
        job.id,
        "run1",
        1000.0,
        trigger="run_now",
        reschedule=False,
    )
    assert decision == "run"
    assert replaced is None
    assert job.status == STATUS_RUNNING
    assert job.run_state.current_run_id == "run1"
    assert job.run_state.current_run_started_at == 1000.0


async def test_claim_run_coalesces_existing_queue_reservation(
    store: HeartbeatJobStore,
) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
        concurrency_policy="queue",
    )
    first, _, _ = await store.claim_run(
        job.id, "active", 1000.0, trigger="run_now", reschedule=False
    )
    queued, _, _ = await store.claim_run(
        job.id, "queued-1", 1001.0, trigger="run_now", reschedule=False
    )
    coalesced, job, _ = await store.claim_run(
        job.id, "queued-2", 1002.0, trigger="run_now", reschedule=False
    )

    assert (first, queued, coalesced) == ("run", "queued", "coalesced")
    assert job.run_state.queued_run_id == "queued-1"


async def test_finish_run_succeeded_increments_run_count(
    store: HeartbeatJobStore,
) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    await store.claim_run(
        job.id, "run1", 1000.0, trigger="scheduler", reschedule=False
    )
    matched, job = await store.finish_run(
        job.id,
        "run1",
        1001.0,
        outcome="succeeded",
        error=None,
        next_run_at=1120.0,
        terminal=False,
    )
    assert matched is True
    assert job.run_count == 1
    assert job.run_state.current_run_id is None
    assert job.run_state.last_run_status == "succeeded"
    assert job.status == STATUS_SCHEDULED


async def test_finish_run_failed_records_error(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    await store.claim_run(
        job.id, "run1", 1000.0, trigger="scheduler", reschedule=False
    )
    matched, job = await store.finish_run(
        job.id,
        "run1",
        1001.0,
        outcome="failed",
        error="boom",
        next_run_at=1120.0,
        terminal=False,
    )
    assert matched is True
    assert job.run_state.last_run_status == "failed"
    assert "boom" in (job.run_state.last_error or "")
    assert job.run_count == 1


async def test_claim_run_skip_increments_counter(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    await store.claim_run(
        job.id, "active", 1000.0, trigger="run_now", reschedule=False
    )
    first, _, _ = await store.claim_run(
        job.id, "skipped-1", 1001.0, trigger="run_now", reschedule=False
    )
    second, _, _ = await store.claim_run(
        job.id, "skipped-2", 1002.0, trigger="run_now", reschedule=False
    )
    job = await store.get_job(job.id)
    assert (first, second) == ("skip", "skip")
    assert job.run_state.skipped_count == 2
    assert job.run_state.last_run_status == "skipped"


async def test_finish_run_terminal_state_preserves_record(
    store: HeartbeatJobStore,
) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool", max_runs=2,
    )
    await store.claim_run(
        job.id, "run1", 999.0, trigger="run_now", reschedule=False
    )
    matched, job = await store.finish_run(
        job.id,
        "run1",
        1000.0,
        outcome="succeeded",
        error=None,
        next_run_at=None,
        terminal=True,
    )
    assert matched is True
    assert job.status == STATUS_COMPLETED
    assert job.enabled is False
    assert job.next_run_at is None
    # 记录保留(不物理删除)
    assert await store.get_job(job.id) is not None


async def test_terminal_run_marks_completed(store: HeartbeatJobStore) -> None:
    sched = HeartbeatSchedule.from_dict({"type": "once", "run_at": 9999.0})
    job = await store.create_job(
        name="once", channel_id="web", session_id="s1", prompt="p",
        schedule=sched, source="agent_tool",
        now=1.0,
    )
    await store.claim_run(
        job.id, "run1", 999.0, trigger="run_now", reschedule=False
    )
    matched, job = await store.finish_run(
        job.id,
        "run1",
        1000.0,
        outcome="succeeded",
        error=None,
        next_run_at=None,
        terminal=True,
    )
    assert matched is True
    assert job.status == STATUS_COMPLETED
    assert job.run_count == 1


# ---------------------------------------------------------------------------
# update / toggle 状态机联动
# ---------------------------------------------------------------------------


async def test_update_enabled_false_sets_disabled(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    await store.update_job(job.id, {"next_run_at": 1.0})
    updated = await store.update_job(job.id, {"enabled": False})
    assert updated.status == STATUS_DISABLED
    assert updated.enabled is False
    assert updated.next_run_at is None


async def test_late_pause_toggle_preserves_completed_status(
    store: HeartbeatJobStore,
) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool", max_runs=1,
    )
    await store.claim_run(
        job.id, "run1", 999.0, trigger="run_now", reschedule=False
    )
    await store.finish_run(
        job.id,
        "run1",
        1000.0,
        outcome="succeeded",
        error=None,
        next_run_at=None,
        terminal=True,
    )

    updated = await store.update_job(job.id, {"enabled": False})

    assert updated.status == STATUS_COMPLETED
    assert updated.enabled is False
    assert updated.next_run_at is None


async def test_toggle_requires_more_budget_before_reactivating_completed(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool", max_runs=1,
    )
    await store.claim_run(
        job.id, "run1", 999.0, trigger="run_now", reschedule=False
    )
    await store.finish_run(
        job.id,
        "run1",
        1000.0,
        outcome="succeeded",
        error=None,
        next_run_at=None,
        terminal=True,
    )
    assert (await store.get_job(job.id)).status == STATUS_COMPLETED
    with pytest.raises(ValueError, match="exhausted max_runs"):
        await store.toggle_job(job.id, True)

    reactivated = await store.update_job(job.id, {"max_runs": 2, "enabled": True})
    assert reactivated.status == STATUS_SCHEDULED
    assert reactivated.enabled is True


async def test_update_schedule_rejects_invalid(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    with pytest.raises(ValueError, match="at least"):
        await store.update_job(job.id, {"schedule": {"type": "interval", "interval_seconds": 10}})


async def test_update_patch_only_changes_provided_fields(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="orig", channel_id="web", session_id="s1", prompt="orig prompt",
        schedule=_interval_schedule(), source="agent_tool",
    )
    updated = await store.update_job(job.id, {"prompt": "new prompt"})
    assert updated.prompt == "new prompt"
    assert updated.name == "orig"  # 未在 patch 中的字段不变


async def test_update_missing_job_raises(store: HeartbeatJobStore) -> None:
    with pytest.raises(KeyError, match="job not found"):
        await store.update_job("nope", {"prompt": "x"})


async def test_update_rejects_invalid_concurrency(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    with pytest.raises(ValueError, match="invalid concurrency_policy"):
        await store.update_job(job.id, {"concurrency_policy": "bogus"})


# ---------------------------------------------------------------------------
# source 审计 + run state
# ---------------------------------------------------------------------------


async def test_create_validates_source_enum(store: HeartbeatJobStore) -> None:
    with pytest.raises(ValueError, match="invalid metadata.source"):
        await store.create_job(
            name="n", channel_id="web", session_id="s1", prompt="p",
            schedule=_interval_schedule(), source="bad_source",
        )


async def test_reschedule_ignored_for_terminal_state(store: HeartbeatJobStore) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool", max_runs=1,
    )
    await store.claim_run(
        job.id, "run1", 999.0, trigger="run_now", reschedule=False
    )
    await store.finish_run(
        job.id,
        "run1",
        1000.0,
        outcome="succeeded",
        error=None,
        next_run_at=None,
        terminal=True,
    )
    # completed 状态不接受 reschedule,保持不变
    await store.reschedule(job.id, 9999.0)
    job = await store.get_job(job.id)
    assert job.status == STATUS_COMPLETED
    assert job.next_run_at is None


# ---------------------------------------------------------------------------
# reload + 外部文件修改(ghost 清理)
# ---------------------------------------------------------------------------


async def test_reload_picks_up_external_file_changes(store: HeartbeatJobStore, tmp_path: Path) -> None:
    path = store.path
    # 外部写入一个 job。store 每次 list_jobs 都读盘(无内存缓存),
    # 因此外部修改后直接 list 即可读到;reload 是 scheduler 层的 mtime 检测行为。
    external_job = {
        "id": "hb_external",
        "kind": "heartbeat",
        "name": "外部",
        "enabled": True,
        "status": "scheduled",
        "channel_id": "web",
        "session_id": "s_ext",
        "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
        "metadata": {"source": "web_rpc"},
        "run_state": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": [external_job]}), encoding="utf-8")
    job = await store.get_job("hb_external")
    assert job is not None
    assert job.name == "外部"
    assert job.session_id == "s_ext"


async def test_invalid_entries_ignored_in_list(store: HeartbeatJobStore, tmp_path: Path) -> None:
    path = store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {"id": "bad", "name": ""},  # 缺字段,非法
                    {
                        "id": "hb_good",
                        "kind": "heartbeat",
                        "name": "好",
                        "enabled": True,
                        "status": "scheduled",
                        "channel_id": "web",
                        "session_id": "s1",
                        "prompt": "p",
                        "schedule": {"type": "interval", "interval_seconds": 120},
                        "metadata": {"source": "agent_tool"},
                        "run_state": {},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    jobs = await store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "hb_good"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("enabled", "false"),
        ("enabled", 0),
    ],
)
async def test_invalid_persisted_boolean_job_is_not_loaded(
    store: HeartbeatJobStore,
    field: str,
    invalid_value: object,
) -> None:
    valid = {
        "id": "hb_invalid_bool",
        "kind": "heartbeat",
        "name": "invalid",
        "enabled": True,
        "status": "scheduled",
        "channel_id": "web",
        "session_id": "s1",
        "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
        "metadata": {"source": "agent_tool"},
        "run_state": {},
    }
    valid[field] = invalid_value
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"version": 1, "jobs": [valid]}),
        encoding="utf-8",
    )

    assert await store.list_jobs() == []


async def test_invalid_persisted_run_status_is_not_loaded(
    store: HeartbeatJobStore,
) -> None:
    raw_job = {
        "id": "hb_invalid_status",
        "kind": "heartbeat",
        "name": "invalid",
        "enabled": True,
        "status": "scheduled",
        "channel_id": "web",
        "session_id": "s1",
        "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
        "metadata": {"source": "agent_tool"},
        "run_state": {"last_run_status": "unknown"},
    }
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"version": 1, "jobs": [raw_job]}),
        encoding="utf-8",
    )

    assert await store.list_jobs() == []


async def test_corrupt_store_is_rejected_without_overwrite(
    store: HeartbeatJobStore,
) -> None:
    original = "{broken json"
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(original, encoding="utf-8")

    with pytest.raises(HeartbeatStoreDataError):
        await store.list_jobs()
    with pytest.raises(HeartbeatStoreDataError):
        await store.create_job(
            name="must-not-overwrite",
            channel_id="web",
            session_id="s1",
            prompt="p",
            schedule=_interval_schedule(),
            source="agent_tool",
        )

    assert store.path.read_text(encoding="utf-8") == original


async def test_count_active_jobs(store: HeartbeatJobStore) -> None:
    await store.create_job(
        name="a", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    await store.create_job(
        name="b", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    await store.create_job(
        name="c", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool", enabled=False,
    )
    assert await store.count_active_jobs_for_session("s1") == 2  # c 是 disabled
    assert await store.count_active_jobs_global() == 2


# ---------------------------------------------------------------------------
# 文件锁 + 原子写
# ---------------------------------------------------------------------------


async def test_persisted_file_is_valid_json(store: HeartbeatJobStore, tmp_path: Path) -> None:
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="agent_tool",
    )
    raw = store.path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["version"] == 1
    assert isinstance(data["jobs"], list)
    assert data["jobs"][0]["id"] == job.id
    assert "delete_after_run" not in data["jobs"][0]


async def test_concurrent_create_enforces_limit_atomically(store: HeartbeatJobStore) -> None:
    import asyncio

    async def create(name: str):
        return await store.create_job(
            name=name,
            channel_id="web",
            session_id="same-session",
            prompt="p",
            schedule=_interval_schedule(),
            source="agent_tool",
            max_active_jobs_per_session=1,
            max_active_jobs_global=10,
        )

    results = await asyncio.gather(create("a"), create("b"), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, ValueError) for item in results) == 1
