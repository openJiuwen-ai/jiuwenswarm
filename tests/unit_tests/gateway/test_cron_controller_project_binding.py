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
