# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HeartbeatController 单元测试:Web/RPC + Agent Tool + source 审计 + 资源限制 + 禁止字段.

覆盖范围:
  - source 审计: controller 创建/更新时强制写入并校验 metadata.source 枚举。
  - Agent Tool: heartbeat_create_job 自动继承当前 channel_id/session_id。
  - Agent Tool: 独立周期任务意图应拒绝或提示 cron_create_job(通过 schema/forbidden 字段拦截)。
  - 停止义务: schema 含 heartbeat_update_job(enabled=false) / heartbeat_cancel_run 要求。
  - 资源限制: max_active_jobs_per_session / global 超限拦截。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.controller import HeartbeatController
from jiuwenswarm.agents.harness.code.rails.heartbeat.models import HeartbeatSchedule
from jiuwenswarm.agents.harness.code.rails.heartbeat.scheduler import HeartbeatSchedulerService
from jiuwenswarm.agents.harness.code.rails.heartbeat.store import HeartbeatJobStore


class _FakeExecution:
    def is_session_busy(self, session_id: str, *, exclude_run_id: str = "") -> bool:
        return False

    def has_active_run(self, run_id: str) -> bool:
        return False

    async def dispatch(self, job, run_id: str, msg) -> bool:  # noqa: ANN001
        return True

    async def cancel(self, run_id: str) -> bool:
        return True


@pytest.fixture
def ctrl(tmp_path: Path):
    store = HeartbeatJobStore(path=tmp_path / "hb.json")
    sched = HeartbeatSchedulerService(store=store, execution_service=_FakeExecution())
    controller = HeartbeatController(store=store, scheduler=sched)
    return controller


def _interval_schedule(seconds: int = 120) -> HeartbeatSchedule:
    return HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": seconds})


async def _claim_running_job(
    ctrl: HeartbeatController,
    job_id: str,
    run_id: str,
) -> None:
    decision, _, _ = await ctrl._store.claim_run(
        job_id,
        run_id,
        1000.0,
        trigger="run_now",
        reschedule=False,
    )
    assert decision == "run"


async def _complete_job(
    ctrl: HeartbeatController,
    job_id: str,
    run_id: str,
) -> None:
    await _claim_running_job(ctrl, job_id, run_id)
    matched, _ = await ctrl._store.finish_run(
        job_id,
        run_id,
        1001.0,
        outcome="succeeded",
        error=None,
        next_run_at=None,
        terminal=True,
    )
    assert matched is True


async def test_web_rpc_create_requires_channel_and_session(ctrl: HeartbeatController) -> None:
    with pytest.raises(ValueError, match="channel_id is required"):
        await ctrl.create_job({"name": "x", "session_id": "s", "prompt": "p", "source": "web_rpc",
                               "schedule": {"type": "interval", "interval_seconds": 120}})
    with pytest.raises(ValueError, match="session_id is required"):
        await ctrl.create_job({"name": "x", "channel_id": "web", "prompt": "p", "source": "web_rpc",
                               "schedule": {"type": "interval", "interval_seconds": 120}})


# ---------------------------------------------------------------------------
# 禁止字段拦截(mode/model/approval/sandbox/worktree)
# ---------------------------------------------------------------------------


async def test_create_rejects_mode(ctrl: HeartbeatController) -> None:
    with pytest.raises(ValueError, match="forbidden"):
        await ctrl.create_job({
            "name": "x", "channel_id": "web", "session_id": "s", "prompt": "p", "source": "web_rpc",
            "schedule": {"type": "interval", "interval_seconds": 120}, "mode": "agent",
        })


async def test_create_rejects_model(ctrl: HeartbeatController) -> None:
    with pytest.raises(ValueError, match="forbidden"):
        await ctrl.create_job({
            "name": "x", "channel_id": "web", "session_id": "s", "prompt": "p", "source": "web_rpc",
            "schedule": {"type": "interval", "interval_seconds": 120}, "model": "claude-x",
        })


async def test_update_rejects_sandbox(ctrl: HeartbeatController) -> None:
    job = await ctrl.create_job({
        "name": "x", "channel_id": "web", "session_id": "s1",
        "prompt": "p", "source": "agent_tool",
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    with pytest.raises(ValueError, match="forbidden"):
        await ctrl.update_job(job["id"], {"sandbox": "dangerous"})


# ---------------------------------------------------------------------------
# source 审计
# ---------------------------------------------------------------------------


async def test_create_rejects_bad_source(ctrl: HeartbeatController) -> None:
    with pytest.raises(ValueError, match="invalid metadata.source"):
        await ctrl.create_job({
            "name": "x", "channel_id": "web", "session_id": "s", "prompt": "p", "source": "bad",
            "schedule": {"type": "interval", "interval_seconds": 120},
        })


async def test_create_default_source_web_rpc(ctrl: HeartbeatController) -> None:
    job = await ctrl.create_job({
        "name": "x", "channel_id": "web", "session_id": "s", "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    assert job["metadata"]["source"] == "web_rpc"


# ---------------------------------------------------------------------------
# 资源限制
# ---------------------------------------------------------------------------


async def test_min_interval_enforced(ctrl: HeartbeatController) -> None:
    with pytest.raises(ValueError, match="at least"):
        await ctrl.create_job({
            "name": "x", "channel_id": "web", "session_id": "s", "prompt": "p", "source": "web_rpc",
            "schedule": {"type": "interval", "interval_seconds": 30},  # < 60
        })


async def test_max_active_jobs_per_session_enforced(ctrl: HeartbeatController, monkeypatch) -> None:
    ctrl.set_limits({"max_active_jobs_per_session": 2, "max_active_jobs_global": 100,
                     "min_interval_seconds": 60})

    async def _count_session(_sid: str) -> int:
        # 模拟已有 2 个活跃 job(达到上限)
        return 2

    async def _count_global() -> int:
        return 2

    monkeypatch.setattr(ctrl._store, "count_active_jobs_for_session", _count_session)
    monkeypatch.setattr(ctrl._store, "count_active_jobs_global", _count_global)
    with pytest.raises(ValueError, match="max_active_jobs_per_session"):
        await ctrl.create_job({
            "name": "x", "channel_id": "web", "session_id": "s", "prompt": "p", "source": "web_rpc",
            "schedule": {"type": "interval", "interval_seconds": 120},
        })


async def test_reenable_enforces_active_job_limit(ctrl: HeartbeatController) -> None:
    await ctrl.create_job({
        "name": "active", "channel_id": "web", "session_id": "s", "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    disabled = await ctrl.create_job({
        "name": "disabled", "channel_id": "web", "session_id": "s", "prompt": "p",
        "enabled": False,
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    ctrl.set_limits(
        {
            "max_active_jobs_per_session": 1,
            "max_active_jobs_global": 100,
            "min_interval_seconds": 60,
        }
    )
    with pytest.raises(ValueError, match="max_active_jobs_per_session"):
        await ctrl.toggle_job(disabled["id"], True)


# ---------------------------------------------------------------------------
# toggle / 重新激活
# ---------------------------------------------------------------------------


async def test_toggle_rejects_completed_job_until_max_runs_is_increased(ctrl: HeartbeatController) -> None:
    job = await ctrl.create_job({
        "name": "x", "channel_id": "web", "session_id": "s1", "prompt": "p",
        "source": "agent_tool", "max_runs": 1,
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    await _complete_job(ctrl, job["id"], "r1")
    assert (await ctrl.get_job(job["id"]))["status"] == "completed"
    with pytest.raises(ValueError, match="max_runs already reached"):
        await ctrl.toggle_job(job["id"], True)

    # 显式提高停止条件后才允许重新激活。
    reactivated = await ctrl.update_job(job["id"], {"max_runs": 2, "enabled": True})
    assert reactivated["status"] == "scheduled"
    assert reactivated["enabled"] is True
    assert reactivated["next_run_at"] is not None


# ---------------------------------------------------------------------------
# delete / preview / list / meta
# ---------------------------------------------------------------------------


async def test_delete_job(ctrl: HeartbeatController) -> None:
    job = await ctrl.create_job({
        "name": "x", "channel_id": "web", "session_id": "s1", "prompt": "p",
        "source": "agent_tool",
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    result = await ctrl.delete_job(job["id"])
    assert result["deleted"] is True
    assert await ctrl.get_job(job["id"]) is None


async def test_delete_running_job_cancels_exact_run_before_physical_delete(
    ctrl: HeartbeatController, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await ctrl.create_job({
        "name": "x", "channel_id": "web", "session_id": "s1", "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    await _claim_running_job(ctrl, job["id"], "run-active")
    calls: list[tuple[str, bool]] = []

    async def cancel_run(job_id: str, *, pause_schedule: bool = False):
        calls.append((job_id, pause_schedule))
        return {
            "job_id": job_id,
            "cancelled_run_id": "run-active",
            "cancel_status": "cancelled",
        }

    monkeypatch.setattr(ctrl._scheduler, "cancel_run", cancel_run)
    result = await ctrl.delete_job(job["id"], access_session_id="s1")
    assert result == {"deleted": True}
    assert calls == [(job["id"], True)]
    assert await ctrl._store.get_job(job["id"]) is None


async def test_delete_running_job_stops_when_exact_cancel_fails(
    ctrl: HeartbeatController, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await ctrl.create_job({
        "name": "x", "channel_id": "web", "session_id": "s1", "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    await _claim_running_job(ctrl, job["id"], "run-active")

    async def cancel_run(job_id: str, *, pause_schedule: bool = False):
        return {"job_id": job_id, "cancel_status": "failed"}

    monkeypatch.setattr(ctrl._scheduler, "cancel_run", cancel_run)
    with pytest.raises(RuntimeError, match="could not be cancelled"):
        await ctrl.delete_job(job["id"], access_session_id="s1")
    assert await ctrl._store.get_job(job["id"]) is not None


async def test_preview_job(ctrl: HeartbeatController) -> None:
    job = await ctrl.create_job({
        "name": "x", "channel_id": "web", "session_id": "s1", "prompt": "p",
        "source": "agent_tool",
        "schedule": {"type": "interval", "interval_seconds": 600},
    })
    result = await ctrl.preview_job(job["id"], count=3)
    assert len(result["next"]) == 3


async def test_list_jobs_filtered_by_session(ctrl: HeartbeatController) -> None:
    for session_id, name in (("s1", "a"), ("s2", "b")):
        await ctrl.create_job({
            "name": name, "channel_id": "web", "session_id": session_id,
            "prompt": "p", "source": "agent_tool",
            "schedule": {"type": "interval", "interval_seconds": 120},
        })
    result = await ctrl.list_jobs({"session_id": "s1"})
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["session_id"] == "s1"


async def test_list_jobs_has_stable_created_at_order(
    ctrl: HeartbeatController,
) -> None:
    late = await ctrl._store.create_job(
        name="late", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="web_rpc", now=20.0,
    )
    early = await ctrl._store.create_job(
        name="early", channel_id="web", session_id="s1", prompt="p",
        schedule=_interval_schedule(), source="web_rpc", now=10.0,
    )
    result = await ctrl.list_jobs({"session_id": "s1"})
    assert [job["id"] for job in result["jobs"]] == [early.id, late.id]


async def test_default_scope_and_mutations_enforce_session_ownership(
    ctrl: HeartbeatController,
) -> None:
    first = await ctrl.create_job({
        "name": "a", "channel_id": "web", "session_id": "s1", "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    await ctrl.create_job({
        "name": "b", "channel_id": "web", "session_id": "s2", "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    visible = await ctrl.list_jobs({}, access_session_id="s1")
    assert [job["session_id"] for job in visible["jobs"]] == ["s1"]
    with pytest.raises(PermissionError):
        await ctrl.list_jobs(
            {"scope": "all_visible"}, access_session_id="s1"
        )
    with pytest.raises(PermissionError):
        await ctrl.update_job(
            first["id"], {"name": "stolen"}, access_session_id="s2"
        )


async def test_once_create_and_schedule_update_recompute_next_run(
    ctrl: HeartbeatController,
) -> None:
    import time

    future = time.time() + 600
    job = await ctrl.create_job({
        "name": "once", "channel_id": "web", "session_id": "s1", "prompt": "p",
        "schedule": {"type": "once", "run_at": future},
    })
    assert job["next_run_at"] == pytest.approx(future)
    updated = await ctrl.update_job(
        job["id"], {"schedule": {"type": "interval", "interval_seconds": 300}}
    )
    assert updated["next_run_at"] > time.time()
    assert abs(updated["next_run_at"] - future) > 200


async def test_toggle_reactivates_disabled_and_expired_jobs(
    ctrl: HeartbeatController,
) -> None:
    import time

    disabled = await ctrl.create_job({
        "name": "disabled", "channel_id": "web", "session_id": "s1", "prompt": "p",
        "enabled": False,
        "schedule": {"type": "interval", "interval_seconds": 120},
    })
    assert disabled["status"] == "disabled"
    reenabled = await ctrl.toggle_job(disabled["id"], True)
    assert reenabled["status"] == "scheduled"
    assert reenabled["next_run_at"] is not None

    expired = await ctrl.create_job({
        "name": "expired", "channel_id": "web", "session_id": "s1", "prompt": "p",
        "schedule": {"type": "once", "run_at": time.time() - 60},
    })
    assert expired["status"] == "expired"
    future = time.time() + 600
    reactivated = await ctrl.update_job(
        expired["id"],
        {"enabled": True, "schedule": {"type": "once", "run_at": future}},
    )
    assert reactivated["status"] == "scheduled"
    assert reactivated["next_run_at"] == pytest.approx(future)


async def test_create_rejects_client_id_unknown_fields_and_string_booleans(
    ctrl: HeartbeatController,
) -> None:
    base = {
        "name": "x", "channel_id": "web", "session_id": "s", "prompt": "p",
        "schedule": {"type": "interval", "interval_seconds": 120},
    }
    with pytest.raises(ValueError, match="unknown fields"):
        await ctrl.create_job({**base, "id": "client-selected"})
    with pytest.raises(ValueError, match="enabled must be boolean"):
        await ctrl.create_job({**base, "enabled": "false"})


async def test_get_meta(ctrl: HeartbeatController) -> None:
    meta = ctrl.get_meta()
    assert "limits" in meta
    assert "scheduled" in meta["statuses"]
    assert "interval" in meta["schedule_types"]
    assert "skip" in meta["concurrency_policies"]
    assert meta["run_count_semantics"].startswith("increments for succeeded")
    assert "delete_after_run" in meta["deprecated_fields"]
    assert "session_busy_wait_timeout_seconds" not in meta["limits"]


def test_limits_are_normalized_for_meta(ctrl: HeartbeatController) -> None:
    ctrl.set_limits(
        {
            "min_interval_seconds": "120",
            "max_active_jobs_per_session": "3",
            "max_active_jobs_global": "9",
            "default_max_runs": "null",
            "execution_timeout_seconds": "45.5",
            "user_preemption_timeout_seconds": "2.5",
        }
    )
    limits = ctrl.get_meta()["limits"]
    assert limits["min_interval_seconds"] == 120
    assert limits["max_active_jobs_per_session"] == 3
    assert limits["max_active_jobs_global"] == 9
    assert limits["default_max_runs"] is None
    assert limits["execution_timeout_seconds"] == 45.5
    assert limits["user_preemption_timeout_seconds"] == 2.5


def test_limits_cannot_advertise_interval_below_model_floor(
    ctrl: HeartbeatController,
) -> None:
    with pytest.raises(ValueError, match="min_interval_seconds must be at least 60"):
        ctrl.set_limits({"min_interval_seconds": 30})


@pytest.mark.parametrize(
    "key", ["execution_timeout_seconds", "user_preemption_timeout_seconds"]
)
def test_runtime_timeouts_must_be_positive(
    ctrl: HeartbeatController, key: str
) -> None:
    with pytest.raises(ValueError, match=f"{key} must be greater than zero"):
        ctrl.set_limits({key: 0})
