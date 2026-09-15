# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HeartbeatSchedulerService 单元测试:due 扫描 / 并发 / 停止条件 / session / source / preview.

覆盖范围:
  - schedule 计算: interval 基于 now 重算不补跑; cron 复用 helper; once completed 保留。
  - 并发策略: skip 上一轮运行中跳过并记录 skipped。
  - 停止条件: max_runs 达上限 completed。
  - ghost task: job 删除后当前 run 被清理。
  - 会话生命周期: session 不可达按 session_deleted_policy 处理。
  - source 审计: scheduler 缺失/非法 source 记 warning 兜底 schedule_recovery。
  - 不传 params.mode(关键约束)。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.models import (
    HeartbeatJob,
    HeartbeatSchedule,
    SOURCE_SCHEDULE_RECOVERY,
    STATUS_COMPLETED,
    STATUS_DISABLED,
    STATUS_EXPIRED,
    STATUS_SCHEDULED,
)
from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import (
    HeartbeatExecutionService,
    SessionRunAdmission,
)
from jiuwenswarm.agents.harness.code.rails.heartbeat.scheduler import HeartbeatSchedulerService
from jiuwenswarm.agents.harness.code.rails.heartbeat.session_resolver import SessionSummary
from jiuwenswarm.agents.harness.code.rails.heartbeat.store import (
    HeartbeatJobStore,
    HeartbeatStoreDataError,
)


class _FakeExecution:
    """Record AgentServer-local dispatches without a Gateway dependency."""

    def __init__(self) -> None:
        self.messages: list = []
        self.cancelled_request_ids: list[str] = []
        self.busy_sessions: set[str] = set()
        self.active_runs: set[str] = set()


    async def dispatch(self, job, run_id: str, msg) -> bool:  # noqa: ANN001
        if job.session_id in self.busy_sessions:
            return False
        self.messages.append(msg)
        self.active_runs.add(run_id)
        return True

    async def cancel(self, request_id: str) -> bool:
        self.cancelled_request_ids.append(request_id)
        if request_id not in self.active_runs:
            return False
        self.active_runs.remove(request_id)
        return True

    def is_session_busy(
        self, session_id: str, *, exclude_run_id: str = ""
    ) -> bool:
        return session_id in self.busy_sessions

    def has_active_run(self, run_id: str) -> bool:
        return run_id in self.active_runs


class _FakeResolver:
    """可控的 session resolver:resolve 总返回非 None(模拟 session 存在)。"""

    def set_scheduler(self, scheduler) -> None:  # noqa: ANN001
        pass

    def resolve(self, channel_id: str, session_id: str) -> SessionSummary | None:
        return SessionSummary(session_id=session_id, channel_id=channel_id)

    async def on_session_deleted(self, session_id: str) -> None:
        pass


class _MissingResolver:
    """resolve 返回 None(模拟 session 不存在)。"""

    def set_scheduler(self, scheduler) -> None:  # noqa: ANN001
        pass

    def resolve(self, channel_id: str, session_id: str) -> SessionSummary | None:
        return None

    async def on_session_deleted(self, session_id: str) -> None:
        pass


@pytest.fixture
def setup(tmp_path: Path):
    store = HeartbeatJobStore(path=tmp_path / "hb.json")
    mh = _FakeExecution()
    sched = HeartbeatSchedulerService(store=store, execution_service=mh)
    # 注入可控 resolver(默认会尝试读真实 session 目录,测试里都返回 None)
    sched._session_resolver = _FakeResolver()
    return store, mh, sched


async def test_start_rejects_corrupt_store_without_false_running_state(
    setup,
) -> None:
    store, _, sched = setup
    store.path.write_text("{broken", encoding="utf-8")

    with pytest.raises(HeartbeatStoreDataError):
        await sched.start()

    assert sched.is_running() is False


# ---------------------------------------------------------------------------
# compute_next_run
# ---------------------------------------------------------------------------


def test_compute_next_run_interval_based_on_base_ts(setup) -> None:
    store, _, sched = setup
    import asyncio

    async def run() -> None:
        job = HeartbeatJob(
            id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
            schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 300}),
        )
        n = sched.compute_next_run(job, 1000.0)
        assert n == 1000.0 + 300

    asyncio.run(run())


def test_compute_next_run_once_returns_future_run_at(setup) -> None:
    store, _, sched = setup
    job = HeartbeatJob(
        id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "once", "run_at": 9999.0}),
    )
    assert sched.compute_next_run(job, 1000.0) == 9999.0


def test_compute_next_run_cron_uses_cron_helper(setup) -> None:
    store, _, sched = setup
    job = HeartbeatJob(
        id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "cron", "cron_expr": "0 9 * * *"}),
    )
    import time

    base = time.time()
    nxt = sched.compute_next_run(job, base)
    assert nxt is not None
    assert nxt > base  # 下一次触发在 now 之后


def test_compute_next_run_seven_field_cron_preserves_seconds(setup) -> None:
    _store, _, sched = setup
    timezone = ZoneInfo("Asia/Shanghai")
    base = datetime(2026, 9, 5, 10, 15, 20, tzinfo=timezone)
    job = HeartbeatJob(
        id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "cron", "cron_expr": "30 15 10 * * ? *", "timezone": "Asia/Shanghai"}
        ),
    )
    nxt = sched.compute_next_run(job, base.timestamp())
    assert nxt == datetime(2026, 9, 5, 10, 15, 30, tzinfo=timezone).timestamp()


def test_compute_next_run_unsupported_type_raises(setup) -> None:
    store, _, sched = setup
    job = HeartbeatJob(
        id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
    )
    # 篡改 type 触发 ValueError
    job.schedule.type = "bogus"
    with pytest.raises(ValueError, match="unsupported schedule type"):
        sched.compute_next_run(job, 1000.0)


# ---------------------------------------------------------------------------
# dispatch + 不传 mode(关键约束)
# ---------------------------------------------------------------------------


async def test_dispatch_does_not_set_mode_param(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    msg = sched._build_message(job, "run1", 1000.0)
    assert "mode" not in msg.params  # 关键:不传 mode
    assert msg.params["query"] == "p"
    assert msg.channel_id == "web"
    assert msg.session_id == "s1"
    assert msg.metadata["automation"]["kind"] == "heartbeat"
    assert msg.metadata["automation"]["job_id"] == job.id
    assert msg.metadata["automation"]["run_id"] == "run1"
    assert msg.params["automation"] == msg.metadata["automation"]
    assert "run" not in msg.params


async def test_dispatch_full_flow_marks_succeeded_and_reschedules(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    await store.update_job(job.id, {"next_run_at": 1.0})  # due
    await sched._tick_once()
    assert len(mh.messages) == 1
    running = await store.get_job(job.id)
    assert running.status == "running"
    await sched.on_run_finished(
        job.id, running.run_state.current_run_id, outcome="succeeded"
    )
    j = await store.get_job(job.id)
    assert j.run_count == 1
    assert j.run_state.last_run_status == "succeeded"
    assert j.next_run_at is not None
    assert j.next_run_at > 1.0  # 基于 now 重算


async def test_route_hydration_failure_finishes_claimed_run(setup) -> None:
    """A transient failure while building the message must not leave a ghost run."""
    store, _, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )

    class ResolveOnceThenFail(_FakeResolver):
        calls = 0

        def resolve(self, channel_id: str, session_id: str):  # noqa: ANN001
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("metadata temporarily unavailable")
            return super().resolve(channel_id, session_id)

    sched._session_resolver = ResolveOnceThenFail()
    result = await sched.trigger_run_now(job.id)
    current = await store.get_job(job.id)
    assert result["accepted"] is True
    assert current.status == STATUS_SCHEDULED
    assert current.run_state.current_run_id is None
    assert current.run_state.last_run_status == "failed"


# ---------------------------------------------------------------------------
# interval 不补跑历史积压
# ---------------------------------------------------------------------------


async def test_interval_does_not_backfill(setup) -> None:
    store, mh, sched = setup
    import time

    now = time.time()
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    # next_run_at 远早于 now(模拟服务离线很久)
    await store.update_job(job.id, {"next_run_at": now - 100000})
    await sched._tick_once()
    running = await store.get_job(job.id)
    await sched.on_run_finished(
        job.id, running.run_state.current_run_id, outcome="succeeded"
    )
    j = await store.get_job(job.id)
    # 执行一次后基于 now 重算,不补跑历史
    assert j.run_count == 1
    assert j.next_run_at >= now  # next 在 now 之后,不是 now - 100000 + 120


# ---------------------------------------------------------------------------
# once → completed 保留记录
# ---------------------------------------------------------------------------


async def test_once_schedule_marks_completed(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="once", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "once", "run_at": __import__("time").time() + 3600}),
        source="agent_tool",
    )
    await store.update_job(job.id, {"next_run_at": 1.0})
    await sched._tick_once()
    running = await store.get_job(job.id)
    await sched.on_run_finished(
        job.id, running.run_state.current_run_id, outcome="succeeded"
    )
    j = await store.get_job(job.id)
    assert j.status == STATUS_COMPLETED
    assert j.next_run_at is None
    assert j.run_count == 1
    # 记录保留
    assert await store.get_job(job.id) is not None


# ---------------------------------------------------------------------------
# max_runs 停止条件
# ---------------------------------------------------------------------------


async def test_max_runs_reached_marks_completed(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", max_runs=2,
    )
    for _ in range(2):
        await store.update_job(job.id, {"next_run_at": 1.0})
        await sched._tick_once()
        running = await store.get_job(job.id)
        await sched.on_run_finished(
            job.id, running.run_state.current_run_id, outcome="succeeded"
        )
    j = await store.get_job(job.id)
    assert j.run_count == 2
    assert j.status == STATUS_COMPLETED
    assert j.enabled is False


async def test_unlimited_job_keeps_running_beyond_default_limit(setup) -> None:
    store, _, sched = setup
    job = await store.create_job(
        name="unlimited", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", max_runs=None,
    )
    for _ in range(13):
        await store.update_job(job.id, {"next_run_at": 1.0})
        await sched._tick_once()
        running = await store.get_job(job.id)
        assert running is not None
        await sched.on_run_finished(
            job.id, running.run_state.current_run_id, outcome="succeeded"
        )

    current = await store.get_job(job.id)
    assert current is not None
    assert current.run_count == 13
    assert current.max_runs is None
    assert current.status == STATUS_SCHEDULED
    assert current.enabled is True
    assert current.next_run_at is not None


async def test_tick_normalizes_exhausted_legacy_scheduled_job_without_dispatch(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="legacy", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        source="agent_tool", max_runs=12,
    )
    data = json.loads(store.path.read_text(encoding="utf-8"))
    for item in data["jobs"]:
        if item["id"] == job.id:
            item["run_count"] = 12
            item["status"] = "scheduled"
            item["enabled"] = True
            item["next_run_at"] = 1.0
    store.path.write_text(json.dumps(data), encoding="utf-8")

    await sched.reload()
    await sched._tick_once()

    current = await store.get_job(job.id)
    assert mh.messages == []
    assert current.run_count == 12
    assert current.status == STATUS_COMPLETED
    assert current.enabled is False
    assert current.next_run_at is None


async def test_run_now_rejects_completed_job_without_exceeding_max_runs(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        source="agent_tool", max_runs=1,
    )
    first = await sched.trigger_run_now(job.id)
    await sched.on_run_finished(job.id, first["run_id"], outcome="succeeded")

    rejected = await sched.trigger_run_now(job.id)

    current = await store.get_job(job.id)
    assert rejected == {
        "accepted": False,
        "run_id": "",
        "session_id": "s1",
        "reason": "job_completed",
    }
    assert current.status == STATUS_COMPLETED
    assert current.run_count == 1
    assert len(mh.messages) == 1


async def test_legacy_single_run_job_honors_increased_max_runs(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", max_runs=1,
    )
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["jobs"][0]["delete_after_run"] = True
    store.path.write_text(json.dumps(data), encoding="utf-8")

    await store.update_job(job.id, {"max_runs": 5})
    await store.update_job(job.id, {"next_run_at": 1.0})
    await sched._tick_once()
    running = await store.get_job(job.id)
    await sched.on_run_finished(
        job.id, running.run_state.current_run_id, outcome="succeeded"
    )
    j = await store.get_job(job.id)
    assert j.status == STATUS_SCHEDULED
    assert j.run_count == 1
    assert j.max_runs == 5


# ---------------------------------------------------------------------------
# 并发策略 skip
# ---------------------------------------------------------------------------


async def test_concurrency_skip_skips_when_run_active(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", concurrency_policy="skip",
    )
    first = await sched.trigger_run_now(job.id)
    second = await sched.trigger_run_now(job.id)
    assert first["accepted"] is True
    assert second["accepted"] is False
    # 应 skip:不投递新消息,记录 skipped
    j = await store.get_job(job.id)
    assert j.run_state.skipped_count == 1
    assert j.run_state.last_run_status == "skipped"
    assert len(mh.messages) == 1  # only the first run was dispatched


async def test_concurrency_replace_cancels_exact_run_and_starts_new(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", concurrency_policy="replace",
    )
    first = await sched.trigger_run_now(job.id)
    second = await sched.trigger_run_now(job.id)
    j = await store.get_job(job.id)
    assert second["accepted"] is True
    assert mh.cancelled_request_ids == [first["run_id"]]
    assert j.run_state.current_run_id == second["run_id"]


async def test_replace_ignores_cancelled_old_stream_completion(setup) -> None:
    """The old stream's finally callback must not finish the replacement run."""
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", concurrency_policy="replace",
    )
    first = await sched.trigger_run_now(job.id)

    async def cancel_with_callback(request_id: str) -> bool:
        mh.cancelled_request_ids.append(request_id)
        await sched.on_run_finished(job.id, request_id, outcome="cancelled")
        return True

    mh.cancel = cancel_with_callback
    second = await sched.trigger_run_now(job.id)
    current = await store.get_job(job.id)
    assert current.run_state.current_run_id == second["run_id"]
    assert current.status == "running"
    assert first["run_id"] in mh.cancelled_request_ids


async def test_concurrency_queue_keeps_one_pending_and_consumes_it(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", concurrency_policy="queue",
    )
    first = await sched.trigger_run_now(job.id)
    second = await sched.trigger_run_now(job.id)
    third = await sched.trigger_run_now(job.id)
    queued = await store.get_job(job.id)
    assert second["accepted"] is True and second["queued"] is True
    assert third["accepted"] is False and third["queued"] is True
    assert third["reason"] == "already_queued"
    assert third["run_id"] == second["run_id"]
    assert queued.run_state.queued_run_id == second["run_id"]
    await sched.on_run_finished(job.id, first["run_id"], outcome="succeeded")
    active = await store.get_job(job.id)
    assert active.run_state.current_run_id == second["run_id"]
    assert len(mh.messages) == 2


async def test_due_heartbeat_waits_until_bound_session_is_idle(setup) -> None:
    store, mh, sched = setup
    mh.busy_sessions.add("s1")
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        source="agent_tool", next_run_at=1.0, now=1.0,
    )
    sched._now_fn = lambda: 30.0

    await sched._tick_once()

    current = await store.get_job(job.id)
    assert mh.messages == []
    assert current.run_state.current_run_id is None
    assert current.run_state.last_run_status is None
    assert current.next_run_at == 1.0

    sched._now_fn = lambda: 60.0
    await sched._tick_once()
    still_waiting = await store.get_job(job.id)
    assert mh.messages == []
    assert still_waiting.next_run_at == 1.0
    assert still_waiting.run_state.last_run_status is None

    mh.busy_sessions.remove("s1")
    sched._now_fn = lambda: 60.0
    await sched._tick_once()

    running = await store.get_job(job.id)
    assert len(mh.messages) == 1
    assert running.status == "running"
    assert running.run_state.current_run_id == mh.messages[0].id


async def test_due_heartbeat_skips_current_run_after_session_busy_timeout(setup) -> None:
    store, mh, sched = setup
    mh.busy_sessions.add("s1")
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        source="agent_tool", concurrency_policy="queue",
        next_run_at=1.0, now=1.0,
    )
    sched._now_fn = lambda: 61.0

    await sched._tick_once()

    current = await store.get_job(job.id)
    assert mh.messages == []
    assert current.run_state.current_run_id is None
    assert current.run_state.last_run_status == "skipped"
    assert current.run_state.last_error == "session_busy_timeout"
    assert current.run_state.queued_run_id is None
    assert current.next_run_at == 181.0


async def test_once_heartbeat_expires_after_session_busy_timeout(setup) -> None:
    store, mh, sched = setup
    mh.busy_sessions.add("s1")
    job = await store.create_job(
        name="once", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "once", "run_at": 1.0}),
        source="agent_tool", now=0.0,
    )
    sched._now_fn = lambda: 61.0

    await sched._tick_once()

    current = await store.get_job(job.id)
    assert mh.messages == []
    assert current.enabled is False
    assert current.status == "expired"
    assert current.next_run_at is None
    assert current.run_state.last_run_status == "skipped"
    assert current.run_state.last_error == "session_busy_timeout"


async def test_dispatch_race_returns_claim_to_busy_wait_path(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        source="agent_tool", next_run_at=1.0, now=1.0,
    )
    sched._now_fn = lambda: 100.0

    await sched._tick_once()
    running = await store.get_job(job.id)
    run_id = running.run_state.current_run_id
    assert run_id is not None
    assert run_id in sched._active_runs

    deferred = await sched.on_session_busy_after_dispatch(job.id, run_id)

    waiting = await store.get_job(job.id)
    assert deferred is True
    assert waiting.status == STATUS_SCHEDULED
    assert waiting.next_run_at == 1.0
    assert waiting.run_state.current_run_id is None
    assert run_id not in sched._active_runs

    # The queued message was consumed by MessageHandler in the real race.  Once
    # it reports busy, the next scheduler tick must wait rather than duplicate it.
    mh.messages.clear()
    mh.busy_sessions.add("s1")
    await sched._tick_once()
    assert mh.messages == []


async def test_two_real_heartbeats_share_sixty_second_busy_deadline(
    tmp_path: Path,
) -> None:
    now = [1.0]
    release_first = asyncio.Event()
    requests = []

    class Server:
        async def execute_internal_heartbeat(self, request) -> None:  # noqa: ANN001
            requests.append(request)
            await release_first.wait()

    store = HeartbeatJobStore(path=tmp_path / "real-admission.json")
    execution = HeartbeatExecutionService(Server(), SessionRunAdmission())
    scheduler = HeartbeatSchedulerService(
        store=store,
        execution_service=execution,
        session_resolver=_FakeResolver(),
        now_fn=lambda: now[0],
    )
    execution.set_scheduler(scheduler)
    jobs = []
    for name in ("first", "second"):
        jobs.append(
            await store.create_job(
                name=name,
                channel_id="web",
                session_id="shared",
                prompt=name,
                schedule=HeartbeatSchedule.from_dict(
                    {"type": "interval", "interval_seconds": 120}
                ),
                source="web_rpc",
                next_run_at=1.0,
                now=0.0,
            )
        )

    await scheduler._tick_once()
    await asyncio.sleep(0)
    assert len(requests) == 1
    running_job_id = requests[0].metadata["automation"]["job_id"]
    waiting_job = next(job for job in jobs if job.id != running_job_id)

    now[0] = 62.0
    await scheduler._tick_once()

    skipped = await store.get_job(waiting_job.id)
    assert skipped is not None
    assert skipped.run_state.last_run_status == "skipped"
    assert skipped.run_state.last_error == "session_busy_timeout"
    assert skipped.next_run_at == 182.0

    release_first.set()
    await asyncio.gather(*list(execution._tasks.values()))
    await scheduler._tick_once()
    assert len(requests) == 1


async def test_real_execution_timeout_finishes_persisted_run(
    tmp_path: Path,
) -> None:
    cancelled = asyncio.Event()

    class Server:
        async def execute_internal_heartbeat(self, request) -> None:  # noqa: ANN001
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    store = HeartbeatJobStore(path=tmp_path / "execution-timeout.json")
    admission = SessionRunAdmission()
    execution = HeartbeatExecutionService(
        Server(), admission, execution_timeout_seconds=0.02
    )
    scheduler = HeartbeatSchedulerService(
        store=store,
        execution_service=execution,
        session_resolver=_FakeResolver(),
        now_fn=lambda: 1.0,
    )
    execution.set_scheduler(scheduler)
    job = await store.create_job(
        name="timeout", channel_id="web", session_id="s1", prompt="continue",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        source="web_rpc", next_run_at=1.0, now=0.0,
    )

    await scheduler._tick_once()
    await asyncio.gather(*list(execution._tasks.values()))

    finished = await store.get_job(job.id)
    assert finished is not None
    assert cancelled.is_set()
    assert finished.run_state.current_run_id is None
    assert finished.run_state.last_run_status == "failed"
    assert finished.run_state.last_error == (
        "heartbeat execution timed out after 0.02 seconds"
    )
    assert finished.run_count == 1
    assert execution.active_session_ids() == set()


async def test_run_now_rejects_busy_manual_session(setup) -> None:
    store, mh, sched = setup
    mh.busy_sessions.add("s1")
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        source="agent_tool",
    )

    result = await sched.trigger_run_now(job.id)

    assert result["accepted"] is False
    assert result["reason"] == "session_busy"
    assert mh.messages == []


# ---------------------------------------------------------------------------
# 状态机: enabled=true 但 status!=scheduled 的不一致 job 被跳过
# ---------------------------------------------------------------------------


async def test_inconsistent_job_skipped(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    # 手改成 completed 但 enabled=true(不一致)
    await store.update_job(job.id, {"next_run_at": 1.0})
    # 直接改 store:模拟手改文件造成 enabled=true status=completed
    # 通过 update_job 不能达成(它维护不变量),直接读改文件
    import json

    data = json.loads(store.path.read_text(encoding="utf-8"))
    for item in data["jobs"]:
        if item["id"] == job.id:
            item["status"] = "completed"
            item["enabled"] = True  # 不一致
    store.path.write_text(json.dumps(data), encoding="utf-8")
    await sched._tick_once()
    # scheduler 跳过(status != scheduled),不投递
    assert len(mh.messages) == 0


# ---------------------------------------------------------------------------
# session 不可达 → session_deleted_policy
# ---------------------------------------------------------------------------


@pytest.fixture
def missing_setup(tmp_path: Path):
    store = HeartbeatJobStore(path=tmp_path / "hb.json")
    mh = _FakeExecution()
    sched = HeartbeatSchedulerService(store=store, execution_service=mh)
    sched._session_resolver = _MissingResolver()
    return store, mh, sched


async def test_missing_session_default_disable(missing_setup) -> None:
    store, _, sched = missing_setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="gone", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",  # 默认 session_deleted_policy=disable
    )
    await store.update_job(job.id, {"next_run_at": 1.0})
    await sched._tick_once()
    j = await store.get_job(job.id)
    assert j.status == STATUS_DISABLED
    assert j.enabled is False


async def test_missing_session_completed_policy(missing_setup) -> None:
    store, _, sched = missing_setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="gone", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", session_deleted_policy="completed",
    )
    await store.update_job(job.id, {"next_run_at": 1.0})
    await sched._tick_once()
    j = await store.get_job(job.id)
    assert j.status == STATUS_COMPLETED


async def test_on_session_deleted_disables_bound_jobs(setup) -> None:
    store, _, sched = setup
    j1 = await store.create_job(
        name="a", channel_id="web", session_id="sd1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    j2 = await store.create_job(
        name="b", channel_id="web", session_id="sd1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", session_deleted_policy="completed",
    )
    j_other = await store.create_job(
        name="c", channel_id="web", session_id="sd2", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    await sched.on_session_deleted("sd1")
    assert (await store.get_job(j1.id)).status == STATUS_DISABLED
    assert (await store.get_job(j2.id)).status == STATUS_COMPLETED
    # 其他 session 的 job 不受影响
    other = await store.get_job(j_other.id)
    assert other.status == STATUS_SCHEDULED


async def test_on_session_deleted_cancels_exact_active_run_before_completion(
    setup,
) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="active", channel_id="web", session_id="sd1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", session_deleted_policy="completed",
    )
    started = await sched.trigger_run_now(job.id)
    await sched.on_session_deleted("sd1")
    current = await store.get_job(job.id)
    assert mh.cancelled_request_ids == [started["run_id"]]
    assert current.status == STATUS_COMPLETED
    assert current.run_state.current_run_id is None
    assert started["run_id"] not in sched._active_runs


# ---------------------------------------------------------------------------
# source 兜底
# ---------------------------------------------------------------------------


async def test_build_message_source_fallback(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, sched = setup
    warnings: list[tuple] = []
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.heartbeat.scheduler.logger.warning",
        lambda *args: warnings.append(args),
    )
    job = HeartbeatJob(
        id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        metadata={"source": "bad_value"},
    )
    msg = sched._build_message(job, "run1", 1.0)
    assert msg.metadata["automation"]["source"] == SOURCE_SCHEDULE_RECOVERY
    assert warnings and "missing or invalid source" in warnings[0][0]


# ---------------------------------------------------------------------------
# ghost task 清理
# ---------------------------------------------------------------------------


async def test_reload_clears_ghost_runs(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    # 模拟一个活跃 run 在内存中
    sched._active_runs["ghost_run"] = (job.id, 1000.0)
    # 删除 job → reload 应清理 ghost run
    await store.delete_job(job.id)
    await sched.reload()
    assert "ghost_run" not in sched._active_runs


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


def test_preview_interval(setup) -> None:
    store, _, sched = setup
    job = HeartbeatJob(
        id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 600}),
    )
    out = sched.preview_next_runs(job, count=3)
    assert len(out) == 3
    assert all("run_at" in x and "iso" in x for x in out)


def test_preview_cron(setup) -> None:
    store, _, sched = setup
    job = HeartbeatJob(
        id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "cron", "cron_expr": "0 9 * * *"}),
    )
    out = sched.preview_next_runs(job, count=2)
    assert len(out) == 2


def test_preview_seven_field_cron_preserves_second_precision(setup) -> None:
    _store, _, sched = setup
    job = HeartbeatJob(
        id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "cron", "cron_expr": "30 15 10 * * ? *", "timezone": "Asia/Shanghai"}
        ),
    )
    out = sched.preview_next_runs(job, count=2)
    assert len(out) == 2
    assert all(datetime.fromtimestamp(item["run_at"], ZoneInfo("Asia/Shanghai")).second == 30 for item in out)


async def test_fixed_year_seven_field_cron_runs_once_then_expires(setup) -> None:
    store, _, sched = setup
    timezone = ZoneInfo("Asia/Shanghai")
    base = datetime(2026, 9, 5, 10, 15, 20, tzinfo=timezone)
    due = datetime(2099, 9, 5, 10, 15, 30, tzinfo=timezone)
    schedule = HeartbeatSchedule.from_dict(
        {"type": "cron", "cron_expr": "30 15 10 5 9 ? 2099", "timezone": "Asia/Shanghai"}
    )
    job = await store.create_job(
        name="fixed-year", channel_id="web", session_id="s1", prompt="p",
        schedule=schedule, source="agent_tool", now=base.timestamp(),
    )
    assert job.next_run_at == due.timestamp()

    sched._now_fn = lambda: base.timestamp()
    preview = sched.preview_next_runs(job, count=5)
    assert len(preview) == 1
    assert preview[0]["run_at"] == due.timestamp()

    sched._now_fn = lambda: due.timestamp() + 1
    decision = await sched._start_run(
        job, "run-fixed-year", due.timestamp(), trigger="scheduler", reschedule=True
    )
    assert decision == "run"
    claimed = await store.get_job(job.id)
    assert claimed is not None
    assert claimed.next_run_at is None

    assert await sched.on_run_finished(job.id, "run-fixed-year", outcome="succeeded") is True
    finished = await store.get_job(job.id)
    assert finished is not None
    assert finished.status == "expired"
    assert finished.enabled is False


def test_preview_formats_job_timezone(setup) -> None:
    _store, _mh, sched = setup
    assert sched._format_preview(0.0, timezone="UTC")["iso"].endswith("+00:00")


def test_preview_once_returns_one_or_empty(setup) -> None:
    store, _, sched = setup
    import time

    future = time.time() + 3600
    job = HeartbeatJob(
        id="x", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "once", "run_at": future}),
    )
    out = sched.preview_next_runs(job, count=5)
    assert len(out) == 1  # once 只返回 1 条
    # 过去的 once 返回空
    past_job = HeartbeatJob(
        id="y", name="n", enabled=True, channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "once", "run_at": 1.0}),
    )
    assert sched.preview_next_runs(past_job, count=5) == []


# ---------------------------------------------------------------------------
# cancel_run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cancel_delivery", ["ack", "callback", "not_found"])
@pytest.mark.parametrize("pause_schedule", [False, True])
@pytest.mark.parametrize("concurrency_policy", ["skip", "queue"])
async def test_cancel_once_run_now_after_scheduled_tick(
    tmp_path: Path, cancel_delivery: str, pause_schedule: bool,
    concurrency_policy: str,
) -> None:
    """Cancelling an early manual run must persist after its once slot is consumed."""
    now = [1000.0]
    store = HeartbeatJobStore(path=tmp_path / "hb.json")
    execution = _FakeExecution()
    sched = HeartbeatSchedulerService(
        store=store, execution_service=execution, now_fn=lambda: now[0],
    )
    sched._session_resolver = _FakeResolver()
    job = await store.create_job(
        name="once", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "once", "run_at": 1100.0}),
        source="web_rpc", now=now[0], concurrency_policy=concurrency_policy,
    )
    started = await sched.trigger_run_now(job.id, reschedule=False)
    run_id = started["run_id"]
    assert started["accepted"] is True
    now[0] = 1110.0
    await sched._tick_once()
    running = await store.get_job(job.id)
    assert running.next_run_at is None
    assert running.run_state.skipped_count == (1 if concurrency_policy == "skip" else 0)
    assert running.run_state.current_run_id == run_id
    if concurrency_policy == "queue":
        assert running.run_state.queued_run_id is not None

    async def cancel(request_id: str) -> bool:
        await _FakeExecution.cancel(execution, request_id)
        if cancel_delivery == "callback":
            await sched.on_run_finished(job.id, request_id, outcome="cancelled")
        return cancel_delivery != "not_found"

    execution.cancel = cancel
    result = await sched.cancel_run(job.id, pause_schedule=pause_schedule)

    # Read through a new Store, so an in-memory-only cleanup cannot pass.
    persisted = await HeartbeatJobStore(path=store.path).get_job(job.id)
    assert result["cancel_status"] == (
        "not_found" if cancel_delivery == "not_found" else "cancelled"
    )
    assert persisted.status == (STATUS_DISABLED if pause_schedule else STATUS_EXPIRED)
    assert persisted.enabled is False
    assert persisted.next_run_at is None
    assert persisted.run_count == 0
    assert persisted.run_state.current_run_id is None
    assert persisted.run_state.resume_status is None
    assert persisted.run_state.last_run_status == "cancelled"
    assert persisted.run_state.last_cancel_status == result["cancel_status"]
    assert persisted.run_state.queued_run_id is None
    assert run_id not in sched._active_runs
    assert run_id not in sched._cancel_intents
    assert execution.active_runs == set()
    assert await sched.on_run_finished(job.id, run_id, outcome="succeeded") is False
    await sched._tick_once()
    assert len(execution.messages) == 1
    assert (await store.get_job(job.id)).to_dict() == persisted.to_dict()


@pytest.mark.parametrize("schedule_data", [
    {"type": "once", "run_at": 1100.0},
    {"type": "interval", "interval_seconds": 120},
    {"type": "cron", "cron_expr": "* * * * *"},
])
@pytest.mark.parametrize("cancel_at", [1099.0, 1100.0, 1110.0])
async def test_cancel_run_now_preserves_unconsumed_schedule(
    tmp_path: Path, schedule_data: dict, cancel_at: float,
) -> None:
    """A due timestamp is still runnable until the scheduler consumes it."""
    now = [1000.0]
    store = HeartbeatJobStore(path=tmp_path / "hb.json")
    sched = HeartbeatSchedulerService(
        store=store, execution_service=_FakeExecution(), now_fn=lambda: now[0],
    )
    sched._session_resolver = _FakeResolver()
    job = await store.create_job(
        name="pending", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(schedule_data),
        source="web_rpc", now=now[0], next_run_at=1100.0,
    )
    await sched.trigger_run_now(job.id, reschedule=False)
    now[0] = cancel_at
    await sched.cancel_run(job.id)
    restored = await store.get_job(job.id)
    assert restored.status == STATUS_SCHEDULED
    assert restored.enabled is True
    assert restored.next_run_at == 1100.0
    assert restored.run_count == 0
    assert restored.run_state.current_run_id is None


async def test_cancel_consumed_once_with_real_execution(tmp_path: Path) -> None:
    """Exercise the actual task cancellation, admission release and finally callback."""
    entered = asyncio.Event()

    class Server:  # pylint: disable=too-few-public-methods
        """Keep execution active until its real task receives cancellation."""

        async def execute_internal_heartbeat(self, _request) -> None:
            """Signal dispatch and wait without invoking an LLM."""
            entered.set()
            await asyncio.Event().wait()

    now = [1000.0]
    admission = SessionRunAdmission()
    execution = HeartbeatExecutionService(Server(), admission)
    store = HeartbeatJobStore(path=tmp_path / "hb.json")
    sched = HeartbeatSchedulerService(
        store=store, execution_service=execution, now_fn=lambda: now[0],
    )
    sched._session_resolver = _FakeResolver()
    execution.set_scheduler(sched)
    job = await store.create_job(
        name="once", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "once", "run_at": 1100.0}),
        source="web_rpc", now=now[0],
    )
    try:
        started = await sched.trigger_run_now(job.id)
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        now[0] = 1110.0
        await sched._tick_once()
        result = await sched.cancel_run(job.id)
        persisted = await HeartbeatJobStore(path=store.path).get_job(job.id)
        assert result["cancel_status"] == "cancelled"
        assert persisted.status == STATUS_EXPIRED
        assert persisted.run_state.last_run_status == "cancelled"
        assert persisted.run_state.current_run_id is None
        assert persisted.run_count == 0
        assert not execution.has_active_run(started["run_id"])
        assert not admission.is_heartbeat_active(job.session_id)
        assert started["run_id"] not in sched._active_runs
    finally:
        await execution.stop()


@pytest.mark.parametrize("schedule_data", [
    {"type": "once", "run_at": 1200.0},
    {"type": "interval", "interval_seconds": 120},
])
async def test_cancel_consumed_once_preserves_edited_schedule(tmp_path: Path, schedule_data: dict):
    """A newer plan must outrank the original once run's resume snapshot."""
    now = [1000.0]
    store = HeartbeatJobStore(path=tmp_path / "hb.json")
    sched = HeartbeatSchedulerService(
        store=store, execution_service=_FakeExecution(), now_fn=lambda: now[0],
    )
    sched._session_resolver = _FakeResolver()
    job = await store.create_job(
        name="once", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "once", "run_at": 1100.0}),
        source="web_rpc", now=now[0],
    )
    await sched.trigger_run_now(job.id)
    now[0] = 1110.0
    await sched._tick_once()
    await store.update_job(job.id, {"schedule": schedule_data, "next_run_at": 1200.0})
    await sched.cancel_run(job.id)
    restored = await store.get_job(job.id)
    assert restored.status == STATUS_SCHEDULED
    assert restored.enabled is True
    assert restored.next_run_at == 1200.0
    assert restored.schedule.type == schedule_data["type"]
    assert restored.run_state.current_run_id is None


async def test_once_due_replacement_preserves_reserved_run(tmp_path: Path) -> None:
    """Cancelling for replacement must not expire a still-pending once slot."""
    now = [1000.0]
    store = HeartbeatJobStore(path=tmp_path / "hb.json")
    execution = _FakeExecution()
    sched = HeartbeatSchedulerService(
        store=store, execution_service=execution, now_fn=lambda: now[0],
    )
    sched._session_resolver = _FakeResolver()
    job = await store.create_job(
        name="once", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "once", "run_at": 1100.0}),
        source="web_rpc", now=now[0], concurrency_policy="replace",
    )
    started = await sched.trigger_run_now(job.id)
    now[0] = 1110.0
    await sched._tick_once()
    current = await store.get_job(job.id)
    assert current.status == "running"
    assert current.run_state.current_run_id != started["run_id"]
    assert current.run_state.current_trigger == "scheduler"
    assert current.run_state.queued_run_id is None
    assert execution.cancelled_request_ids == [started["run_id"]]
    assert len(execution.messages) == 2
    await sched.cancel_run(job.id)
    assert (await store.get_job(job.id)).status == STATUS_EXPIRED


async def test_cancel_run_pause_schedule(setup) -> None:
    store, _, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    started = await sched.trigger_run_now(job.id)
    result = await sched.cancel_run(job.id, pause_schedule=True)
    assert result["paused"] is True
    assert result["cancelled_run_id"] == started["run_id"]
    assert result["cancel_status"] == "cancelled"
    j = await store.get_job(job.id)
    assert j.status == STATUS_DISABLED
    assert j.run_state.last_cancel_status == "cancelled"


async def test_cancel_completed_job_does_not_change_terminal_state(setup) -> None:
    store, _, sched = setup
    job = await store.create_job(
        name="completed", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict(
            {"type": "interval", "interval_seconds": 120}
        ),
        source="agent_tool", max_runs=1,
    )
    await store.update_job(job.id, {"next_run_at": 1.0})
    await sched._tick_once()
    running = await store.get_job(job.id)
    await sched.on_run_finished(
        job.id, running.run_state.current_run_id, outcome="succeeded"
    )

    result = await sched.cancel_run(job.id, pause_schedule=True)
    persisted = await HeartbeatJobStore(path=store.path).get_job(job.id)

    assert result == {
        "job_id": job.id,
        "cancelled_run_id": None,
        "cancel_status": "idle",
        "paused": False,
        "reason": "job_terminal",
    }
    assert persisted is not None
    assert persisted.status == STATUS_COMPLETED
    assert persisted.enabled is False
    assert persisted.run_count == 1


async def test_cancel_run_reports_exact_stream_not_found(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    await sched.trigger_run_now(job.id)

    async def not_found(request_id: str) -> bool:
        mh.cancelled_request_ids.append(request_id)
        return False

    mh.cancel = not_found
    result = await sched.cancel_run(job.id)
    current = await store.get_job(job.id)
    assert result["cancel_status"] == "not_found"
    assert current.run_state.last_cancel_status == "not_found"
    assert current.run_state.last_cancel_error == (
        "exact AgentServer heartbeat execution was not found"
    )


async def test_cancel_failure_preserves_authoritative_active_run(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    started = await sched.trigger_run_now(job.id)

    async def fail_cancel(request_id: str) -> bool:
        raise RuntimeError(f"cancel transport failed for {request_id}")

    mh.cancel = fail_cancel
    result = await sched.cancel_run(job.id)
    current = await store.get_job(job.id)
    assert result["cancel_status"] == "failed"
    assert current.status == "running"
    assert current.run_state.current_run_id == started["run_id"]
    assert started["run_id"] in sched._active_runs


async def test_cancel_failure_can_pause_without_erasing_live_run(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    started = await sched.trigger_run_now(job.id)

    async def fail_cancel(request_id: str) -> bool:
        raise RuntimeError(f"cancel transport failed for {request_id}")

    mh.cancel = fail_cancel
    result = await sched.cancel_run(job.id, pause_schedule=True)
    current = await store.get_job(job.id)
    assert result["cancel_status"] == "failed"
    assert result["paused"] is True
    assert current.status == STATUS_DISABLED
    assert current.enabled is False
    assert current.run_state.current_run_id == started["run_id"]


async def test_cancel_caller_interruption_clears_cancel_intent(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    started = await sched.trigger_run_now(job.id)

    async def wait_for_cancel(request_id: str) -> bool:
        await asyncio.Event().wait()
        return True

    mh.cancel = wait_for_cancel
    cancel_task = asyncio.create_task(sched.cancel_run(job.id))
    await asyncio.sleep(0)
    cancel_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancel_task

    current = await store.get_job(job.id)
    assert started["run_id"] not in sched._cancel_intents
    assert current.run_state.current_run_id == started["run_id"]


async def test_cancel_pause_survives_stream_finally_callback(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    started = await sched.trigger_run_now(job.id)

    async def cancel_with_callback(request_id: str) -> bool:
        await sched.on_run_finished(job.id, request_id, outcome="cancelled")
        return True

    mh.cancel = cancel_with_callback
    await sched.cancel_run(job.id, pause_schedule=True)
    current = await store.get_job(job.id)
    assert current.status == STATUS_DISABLED
    assert current.enabled is False
    assert current.run_state.current_run_id is None
    assert started["run_id"] not in sched._active_runs


async def test_trigger_run_now_missing_session_disabled(missing_setup) -> None:
    store, _, sched = missing_setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="gone", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    await sched.trigger_run_now(job.id)
    j = await store.get_job(job.id)
    # session 不存在 → missing → disable
    assert j.status == STATUS_DISABLED


async def test_run_now_disabled_job_preserves_disabled_schedule(setup) -> None:
    store, _, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", enabled=False,
    )
    started = await sched.trigger_run_now(job.id, reschedule=False)
    running = await store.get_job(job.id)
    assert running.status == "running"
    assert running.enabled is False
    await sched.on_run_finished(job.id, started["run_id"], outcome="succeeded")
    restored = await store.get_job(job.id)
    assert restored.status == STATUS_DISABLED
    assert restored.enabled is False
    assert restored.next_run_at is None


async def test_disable_during_running_job_survives_completion(setup) -> None:
    store, _, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool",
    )
    started = await sched.trigger_run_now(job.id)
    await store.update_job(job.id, {"enabled": False})
    await sched.on_run_finished(job.id, started["run_id"], outcome="succeeded")
    current = await store.get_job(job.id)
    assert current.status == STATUS_DISABLED
    assert current.enabled is False
    assert current.next_run_at is None


async def test_scheduler_interval_keeps_claim_time_anchor(tmp_path: Path) -> None:
    now = [100.0]
    store = HeartbeatJobStore(path=tmp_path / "anchor.json")
    mh = _FakeExecution()
    sched = HeartbeatSchedulerService(
        store=store, execution_service=mh, now_fn=lambda: now[0]
    )
    sched._session_resolver = _FakeResolver()
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", next_run_at=1.0, now=1.0,
    )
    await sched._tick_once()
    running = await store.get_job(job.id)
    assert running.next_run_at == 220.0
    now[0] = 175.0
    await sched.on_run_finished(
        job.id, running.run_state.current_run_id, outcome="succeeded"
    )
    assert (await store.get_job(job.id)).next_run_at == 220.0


async def test_run_now_reschedule_anchors_from_completion(tmp_path: Path) -> None:
    now = [100.0]
    store = HeartbeatJobStore(path=tmp_path / "run-now-anchor.json")
    sched = HeartbeatSchedulerService(
        store=store, execution_service=_FakeExecution(), now_fn=lambda: now[0]
    )
    sched._session_resolver = _FakeResolver()
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", now=1.0,
    )
    started = await sched.trigger_run_now(job.id, reschedule=True)
    now[0] = 175.0
    await sched.on_run_finished(job.id, started["run_id"], outcome="succeeded")
    assert (await store.get_job(job.id)).next_run_at == 295.0


async def test_reload_recovers_orphan_run_immediately(tmp_path: Path) -> None:
    store = HeartbeatJobStore(path=tmp_path / "lease.json")
    mh = _FakeExecution()
    now = 100_000.0
    sched = HeartbeatSchedulerService(
        store=store, execution_service=mh, now_fn=lambda: now
    )
    sched._session_resolver = _FakeResolver()
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", now=1.0,
    )
    await store.claim_run(
        job.id,
        "stale-run",
        2.0,
        trigger="scheduler",
        reschedule=True,
        next_run_at_after_claim=122.0,
    )
    await sched.reload()
    recovered = await store.get_job(job.id)
    assert recovered.status == STATUS_SCHEDULED
    assert recovered.run_state.current_run_id is None
    assert recovered.run_state.last_run_status == "failed"


async def test_reload_reattaches_exact_live_stream(tmp_path: Path) -> None:
    store = HeartbeatJobStore(path=tmp_path / "live.json")
    mh = _FakeExecution()
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", now=1.0,
    )
    await store.claim_run(
        job.id,
        "live-run",
        2.0,
        trigger="scheduler",
        reschedule=True,
        next_run_at_after_claim=122.0,
    )
    live_task = __import__("asyncio").create_task(__import__("asyncio").sleep(3600))
    mh.active_runs.add("live-run")
    sched = HeartbeatSchedulerService(store=store, execution_service=mh)
    sched._session_resolver = _FakeResolver()
    try:
        await sched.reload()
        assert sched._active_runs["live-run"][0] == job.id
        assert (await store.get_job(job.id)).run_state.current_run_id == "live-run"
    finally:
        live_task.cancel()
        await __import__("asyncio").gather(live_task, return_exceptions=True)


async def test_resource_limit_skip_advances_due_time(tmp_path: Path) -> None:
    store = HeartbeatJobStore(path=tmp_path / "limit.json")
    mh = _FakeExecution()
    sched = HeartbeatSchedulerService(
        store=store, execution_service=mh, now_fn=lambda: 100.0
    )
    sched._session_resolver = _FakeResolver()
    jobs = []
    for name in ("a", "b"):
        jobs.append(
            await store.create_job(
                name=name, channel_id="web", session_id="s1", prompt="p",
                schedule=HeartbeatSchedule.from_dict(
                    {"type": "interval", "interval_seconds": 120}
                ),
                source="agent_tool", next_run_at=1.0, now=1.0,
            )
        )
    sched.set_limits(
        {"max_active_jobs_per_session": 1, "max_active_jobs_global": 100}
    )
    await sched._tick_once()
    assert len(mh.messages) == 1
    statuses = []
    for job in jobs:
        current = await store.get_job(job.id)
        assert current.next_run_at == 220.0
        statuses.append((current.status, current.run_state.last_run_status))
    assert sum(status == "running" for status, _ in statuses) == 1
    assert sum(last == "skipped" for _, last in statuses) == 1


async def test_resource_admission_prioritizes_previously_skipped_job(
    tmp_path: Path,
) -> None:
    now = [100.0]
    store = HeartbeatJobStore(path=tmp_path / "limit-fairness.json")
    mh = _FakeExecution()
    sched = HeartbeatSchedulerService(
        store=store, execution_service=mh, now_fn=lambda: now[0]
    )
    sched._session_resolver = _FakeResolver()
    jobs = []
    for name in ("a", "b"):
        jobs.append(
            await store.create_job(
                name=name, channel_id="web", session_id="s1", prompt="p",
                schedule=HeartbeatSchedule.from_dict(
                    {"type": "interval", "interval_seconds": 120}
                ),
                source="agent_tool", next_run_at=1.0, now=1.0,
            )
        )
    sched.set_limits(
        {"max_active_jobs_per_session": 1, "max_active_jobs_global": 100}
    )
    await sched._tick_once()
    first_running = None
    for candidate in jobs:
        if (await store.get_job(candidate.id)).run_state.current_run_id is not None:
            first_running = candidate
            break
    assert first_running is not None
    first_state = await store.get_job(first_running.id)
    await sched.on_run_finished(
        first_running.id,
        first_state.run_state.current_run_id,
        outcome="succeeded",
    )

    now[0] = 220.0
    await sched._tick_once()

    assert len(mh.messages) == 2
    assert mh.messages[0].metadata["automation"]["job_id"] != (
        mh.messages[1].metadata["automation"]["job_id"]
    )


async def test_replace_cancel_failure_does_not_dispatch_replacement(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", concurrency_policy="replace",
    )
    first = await sched.trigger_run_now(job.id)

    async def fail_cancel(request_id: str) -> bool:
        raise RuntimeError(f"cannot cancel {request_id}")

    mh.cancel = fail_cancel
    replacement = await sched.trigger_run_now(job.id)
    current = await store.get_job(job.id)
    assert replacement["accepted"] is False
    assert replacement["reason"] == "replacement_cancel_failed"
    assert len(mh.messages) == 1
    assert current.run_state.current_run_id == first["run_id"]
    assert current.run_state.queued_run_id is None


async def test_replace_caller_interruption_rolls_back_reservation(setup) -> None:
    store, mh, sched = setup
    job = await store.create_job(
        name="n", channel_id="web", session_id="s1", prompt="p",
        schedule=HeartbeatSchedule.from_dict({"type": "interval", "interval_seconds": 120}),
        source="agent_tool", concurrency_policy="replace",
    )
    first = await sched.trigger_run_now(job.id)

    async def wait_for_cancel(request_id: str) -> bool:
        await asyncio.Event().wait()
        return True

    mh.cancel = wait_for_cancel
    replacement_task = asyncio.create_task(sched.trigger_run_now(job.id))
    await asyncio.sleep(0)
    replacement_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacement_task

    current = await store.get_job(job.id)
    assert first["run_id"] not in sched._cancel_intents
    assert current.run_state.current_run_id == first["run_id"]
    assert current.run_state.queued_run_id is None
