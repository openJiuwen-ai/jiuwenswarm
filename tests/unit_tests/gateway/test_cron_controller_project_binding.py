"""CronController create/update project binding 多用户容忍（Phase 4）。

目录分离后 Gateway 部署侧项目表不包含用户侧 project_id；仅 AgentServer 已完成
绑定的请求可跳过 Gateway 本地反查。历史单用户请求仍应拒绝无效 project_id。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.server.runtime.session.project_store import CronProjectBinding


@pytest.mark.asyncio
async def test_delete_job_stops_runs_and_removes_sessions_before_job() -> None:
    calls = []
    job = SimpleNamespace(id="job_a", enabled=True, mode="code", user_id="alice")

    async def update_job(job_id, patch):
        calls.append("disable")

    async def reload():
        calls.append("reload")

    async def stop_job_runs(job_id):
        calls.append("stop")

    async def delete_sessions(job_id, user_id):
        calls.append("sessions")

    async def delete_job(job_id, *, force=False):
        calls.append("job")
        return True

    store = SimpleNamespace(
        get_job=AsyncMock(return_value=job), update_job=update_job, delete_job=delete_job
    )
    scheduler = SimpleNamespace(
        reload=reload, stop_job_runs=stop_job_runs, delete_cron_sessions=delete_sessions
    )
    controller = CronController(store=store, scheduler=scheduler)
    assert await controller.delete_job("job_a")
    assert calls == ["disable", "reload", "stop", "sessions", "job", "reload"]


@pytest.mark.asyncio
async def test_hide_project_jobs_disables_runs_and_keeps_records() -> None:
    """hide_project_jobs: 停用项目下任务并取消在途执行,任务记录原样保留。

    停用不按操作者 user_id 过滤:项目是共享资源,其他属主的任务也必须停用,
    否则项目恢复后会带着 enabled=True 直接回到触发状态。
    """
    calls = []
    jobs = [
        SimpleNamespace(id="job_a", project_id="proj_1", user_id="alice", enabled=True),
        SimpleNamespace(id="job_b", project_id="proj_1", user_id="alice", enabled=False),
        SimpleNamespace(id="job_c", project_id="proj_2", user_id="alice", enabled=True),
        SimpleNamespace(id="job_d", project_id="proj_1", user_id="bob", enabled=True),
    ]

    async def list_jobs():
        return list(jobs)

    async def update_job(job_id, patch):
        calls.append(("update", job_id, patch))

    async def reload():
        calls.append(("reload",))

    async def stop_project_runs(project_id):
        calls.append(("stop", project_id))

    store = SimpleNamespace(list_jobs=list_jobs, update_job=update_job)
    scheduler = _FakeScheduler(reload=reload, stop_project_runs=stop_project_runs)
    controller = CronController(store=store, scheduler=scheduler)

    result = await controller.hide_project_jobs("proj_1")

    # 该项目全部任务计入结果;已停用的不重复写,其他项目不受影响。
    assert result == {"stopped_cron_jobs": 3}
    assert ("update", "job_a", {"enabled": False}) in calls
    assert ("update", "job_b", {"enabled": False}) not in calls
    assert ("update", "job_c", {"enabled": False}) not in calls
    # 其他属主(bob)的任务同样被停用。
    assert ("update", "job_d", {"enabled": False}) in calls
    # 停用 → reload → 取消在途执行(不限定属主);没有任何 delete_job 调用。
    assert calls[-2:] == [("reload",), ("stop", "proj_1")]
    assert not any(call[0] == "delete" for call in calls)


@pytest.mark.asyncio
async def test_list_jobs_checks_gate_once_per_project() -> None:
    """list_jobs: 准入闸门按 (project_id, user_id) 去重,不逐任务串行 RPC。"""
    jobs = [
        SimpleNamespace(id="job_a", project_id="proj_1", user_id="alice", enabled=True,
                        to_dict=lambda: {"id": "job_a"}),
        SimpleNamespace(id="job_b", project_id="proj_1", user_id="alice", enabled=True,
                        to_dict=lambda: {"id": "job_b"}),
        SimpleNamespace(id="job_c", project_id="proj_1", user_id="bob", enabled=True,
                        to_dict=lambda: {"id": "job_c"}),
        SimpleNamespace(id="job_d", project_id="proj_2", user_id="alice", enabled=True,
                        to_dict=lambda: {"id": "job_d"}),
    ]
    gate_calls: list[tuple[str | None, str | None]] = []

    async def list_jobs():
        return list(jobs)

    async def project_execution_allowed(project_id, user_id=None):
        gate_calls.append((project_id, user_id))
        # proj_2 被隐藏:其任务不得进入列表。
        return project_id != "proj_2"

    store = SimpleNamespace(list_jobs=list_jobs)
    scheduler = SimpleNamespace(project_execution_allowed=project_execution_allowed)
    controller = CronController(store=store, scheduler=scheduler)

    result = await controller.list_jobs()

    # 4 个任务只查 3 次(同项目同属主共享判定),隐藏项目的任务被过滤。
    assert sorted(job["id"] for job in result) == ["job_a", "job_b", "job_c"]
    assert sorted(gate_calls) == [("proj_1", "alice"), ("proj_1", "bob"), ("proj_2", "alice")]


class _RecordingStore:
    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []

    async def create_job(self, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(**kwargs, to_dict=lambda: dict(kwargs))

    async def get_job(self, job_id: str):
        return SimpleNamespace(
            id=job_id,
            name="existing",
            enabled=True,
            expired=False,
            cron_expr="0 9 * * *",
            timezone="Asia/Shanghai",
            targets="web",
            work_mode="work",
            project_id="default",
            user_id=None,
        )

    async def update_job(self, job_id: str, patch: dict):
        self.update_calls.append((job_id, patch))
        return SimpleNamespace(id=job_id, **patch, to_dict=lambda: {"id": job_id, **patch})


class _FakeScheduler:
    """CronController 依赖的调度器接口子集:准入闸门开合 + 生命周期调用。"""

    def __init__(self, **methods) -> None:
        self.hiding_projects: set[str] = set()
        for name, func in methods.items():
            setattr(self, name, func)

    def close_project_admission(self, project_id: str) -> None:
        self.hiding_projects.add(project_id)

    def reopen_project_admission(self, project_id: str) -> None:
        self.hiding_projects.discard(project_id)

    async def project_execution_allowed(self, project_id, user_id=None) -> bool:
        return True

    async def reload(self) -> None:
        return None


def _make_controller():
    CronController.reset_instance()
    return CronController(store=_RecordingStore(), scheduler=_FakeScheduler())


@pytest.mark.asyncio
async def test_create_job_tolerates_user_side_project_id(monkeypatch) -> None:
    """显式真实 project_id 不在 Gateway 本地项目表时，信任调用方 work_mode。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_cron_project_binding",
        lambda project_id, project_dir, work_mode: CronProjectBinding(
            project_id="",
            work_mode="work",
            error=f"project not found: {project_id!r}",
            code="NOT_FOUND",
        ),
    )
    cc = _make_controller()
    job = await cc.create_job(
        {
            "name": "daily",
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
            "description": "hello",
            "targets": "web",
            "project_id": "proj_user_side",
            "project_dir": "/home/user/project",
            "work_mode": "code",
            "_agentos_project_binding_verified": True,
        }
    )

    assert job["project_id"] == "proj_user_side"
    assert job["work_mode"] == "code"
    create_call = cc._store.create_calls[0]
    assert create_call["project_id"] == "proj_user_side"
    assert create_call["work_mode"] == "code"
    assert "_agentos_project_binding_verified" not in create_call


@pytest.mark.asyncio
async def test_create_job_rejects_missing_project(monkeypatch) -> None:
    """不存在的项目仍拒绝新增 cron 绑定。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_cron_project_binding",
        lambda project_id, project_dir, work_mode: CronProjectBinding(
            project_id="",
            work_mode="work",
            error=f"project not found: {project_id!r}",
            code="NOT_FOUND",
        ),
    )
    cc = _make_controller()
    with pytest.raises(ValueError, match="project not found"):
        await cc.create_job(
            {
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "hello",
                "targets": "web",
                "project_id": "proj_hidden",
                "work_mode": "code",
            }
        )


@pytest.mark.asyncio
async def test_create_job_rejects_unresolved_project_in_single_user(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_cron_project_binding",
        lambda project_id, project_dir, work_mode: CronProjectBinding(
            project_id="",
            work_mode="work",
            error=f"project not found: {project_id!r}",
            code="NOT_FOUND",
        ),
    )
    with pytest.raises(ValueError, match="project not found"):
        await _make_controller().create_job(
            {
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "targets": "web",
                "project_id": "proj_missing",
            }
        )


@pytest.mark.asyncio
async def test_update_job_tolerates_user_side_project_id(monkeypatch) -> None:
    """patch 含显式真实 project_id 且不在本地项目表时，跳过本地反查。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_project_by_id",
        lambda project_id, cache_bust=True: None,
    )
    cc = _make_controller()
    await cc.update_job(
        "job-1",
        {
            "project_id": "proj_user_side",
            "work_mode": "code",
            "_agentos_project_binding_verified": True,
        },
    )

    _, patch = cc._store.update_calls[0]
    assert patch["project_id"] == "proj_user_side"
    assert patch["work_mode"] == "code"
    assert "_agentos_project_binding_verified" not in patch


# ---------------------------------------------------------------------------
# 会话级 MCP 选择（mcp）随 job 落库 / patch 规范化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_normalizes_and_passes_mcp_to_store(monkeypatch) -> None:
    """create 时 mcp 做 strip/去空/去重后透传 store；不校验存在性。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_cron_project_binding",
        lambda project_id, project_dir, work_mode: CronProjectBinding(
            project_id="",
            work_mode="work",
            error=None,
            code="",
        ),
    )
    cc = _make_controller()
    await cc.create_job(
        {
            "name": "daily",
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
            "description": "hello",
            "targets": "web",
            "mcp": [" feishu-doc ", "github", "github", "", 123],
        }
    )

    create_call = cc._store.create_calls[0]
    assert create_call["mcp"] == ["feishu-doc", "github"]


@pytest.mark.asyncio
async def test_create_job_without_mcp_passes_none(monkeypatch) -> None:
    """未传 mcp → store 收到 None（保持既有行为，旧 job 兜底一致）。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_cron_project_binding",
        lambda project_id, project_dir, work_mode: CronProjectBinding(
            project_id="",
            work_mode="work",
            error=None,
            code="",
        ),
    )
    cc = _make_controller()
    await cc.create_job(
        {
            "name": "daily",
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
            "description": "hello",
            "targets": "web",
        }
    )

    create_call = cc._store.create_calls[0]
    assert create_call["mcp"] is None


@pytest.mark.asyncio
async def test_update_job_normalizes_mcp_patch(monkeypatch) -> None:
    """patch mcp：非空列表规范化；空列表/null 归 None（清除选择）。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_project_by_id",
        lambda project_id, cache_bust=True: None,
    )
    cc = _make_controller()
    await cc.update_job(
        "job-1",
        {"mcp": [" a ", "b", "b", ""], "_agentos_project_binding_verified": True},
    )
    _, patch = cc._store.update_calls[0]
    assert patch["mcp"] == ["a", "b"]

    await cc.update_job(
        "job-1",
        {"mcp": [], "_agentos_project_binding_verified": True},
    )
    _, patch = cc._store.update_calls[1]
    assert patch["mcp"] is None


@pytest.mark.asyncio
async def test_hide_holds_admission_until_commit():
    import asyncio
    from unittest.mock import AsyncMock

    scheduler = _FakeScheduler(reload=AsyncMock(), stop_project_runs=AsyncMock())
    store = SimpleNamespace(list_jobs=AsyncMock(return_value=[]))
    controller = CronController(store=store, scheduler=scheduler)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def commit():
        assert "project" in scheduler.hiding_projects
        assert controller.mutation_lock.locked()
        entered.set()
        await release.wait()

    hiding = asyncio.create_task(controller.hide_project_jobs("project", commit=commit))
    await entered.wait()
    contender = asyncio.create_task(controller.mutation_lock.acquire())
    await asyncio.sleep(0)
    assert not contender.done()
    release.set()
    await hiding
    await contender
    controller.mutation_lock.release()
    assert "project" not in scheduler.hiding_projects


@pytest.mark.asyncio
async def test_hide_stop_failure_never_commits():
    from unittest.mock import AsyncMock

    scheduler = _FakeScheduler(
        reload=AsyncMock(), stop_project_runs=AsyncMock(side_effect=RuntimeError("stop failed"))
    )
    controller = CronController(store=SimpleNamespace(list_jobs=AsyncMock(return_value=[])), scheduler=scheduler)
    commit = AsyncMock()
    with pytest.raises(RuntimeError, match="stop failed"):
        await controller.hide_project_jobs("project", commit=commit)
    commit.assert_not_awaited()
    assert not scheduler.hiding_projects
