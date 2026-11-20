"""CronController create/update 项目绑定规则（A3g：本地反查删除后）。

绑定统一由入口在目标 AgentServer 解析（web/tui 经 PROJECT_CRON_RESOLVE_BINDING，
agent 工具经 server 侧 CronTools），controller 把 marker 标记的结果作为不透明值
持久化。marker 缺失时仅放行无项目字段/默认项目；真实 project_id/project_dir
必须先经 AgentServer 解析。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_gateway.cron.controller import CronController
from agent_gateway.cron.proactive_cron_sync import PROACTIVE_JOB_ID
from jiuwenswarm.server.runtime.session.project_store import CronProjectBinding


def _listed_job(job_id: str, **fields):
    payload = {"id": job_id, **fields}
    return SimpleNamespace(**payload, to_dict=lambda payload=payload: dict(payload))


@pytest.mark.asyncio
async def test_list_jobs_filters_owner_before_admission_gate() -> None:
    """传 user_id 时先按属主过滤:只为自己的项目打闸门,不替其他用户付超时。"""
    jobs = [
        SimpleNamespace(group_id="", bot_id="", id="job_a", project_id="proj_alice", user_id="alice",
                        to_dict=lambda: {"id": "job_a", "user_id": "alice"}),
        SimpleNamespace(group_id="", bot_id="", id="job_b", project_id="proj_bob", user_id="bob",
                        to_dict=lambda: {"id": "job_b", "user_id": "bob"}),
        # 无属主的历史任务在带 user_id 的查询下不可见(与 Web 列表语义一致)。
        SimpleNamespace(group_id="", bot_id="", id="job_c", project_id="proj_legacy", user_id="",
                        to_dict=lambda: {"id": "job_c", "user_id": ""}),
    ]
    gate_calls: list[tuple[str | None, str | None]] = []

    async def list_jobs():
        return list(jobs)

    async def project_execution_allowed(project_id, user_id=None, group_id=None, bot_id=None):
        gate_calls.append((project_id, user_id))
        return True

    controller = CronController(
        store=SimpleNamespace(list_jobs=list_jobs),
        scheduler=SimpleNamespace(project_execution_allowed=project_execution_allowed),
    )

    result = await controller.list_jobs(user_id="alice")

    assert [job["id"] for job in result] == ["job_a"]
    assert gate_calls == [("proj_alice", "alice")]


@pytest.mark.asyncio
async def test_list_jobs_owner_without_jobs_issues_no_gate_rpc() -> None:
    """用户 0 任务时不得为 store 里其他用户的任务发起任何准入查询。"""
    jobs = [
        SimpleNamespace(group_id="", bot_id="", id="job_b", project_id="proj_bob", user_id="bob",
                        to_dict=lambda: {"id": "job_b"}),
    ]
    gate_calls: list[tuple[str | None, str | None]] = []

    async def list_jobs():
        return list(jobs)

    async def project_execution_allowed(project_id, user_id=None, group_id=None, bot_id=None):
        gate_calls.append((project_id, user_id))
        return True

    controller = CronController(
        store=SimpleNamespace(list_jobs=list_jobs),
        scheduler=SimpleNamespace(project_execution_allowed=project_execution_allowed),
    )

    assert await controller.list_jobs(user_id="alice") == []
    assert gate_calls == []


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

    async def project_execution_allowed(self, project_id, user_id=None, group_id=None, bot_id=None) -> bool:
        return True

    async def reload(self) -> None:
        return None


def _make_controller():
    CronController.reset_instance()
    return CronController(store=_RecordingStore(), scheduler=_FakeScheduler())


# ---------------------------------------------------------------------------
# 会话级 MCP 选择（mcp）随 job 落库 / patch 规范化
# ---------------------------------------------------------------------------


def _owned_job(**overrides):
    fields = {
        "id": "job-1",
        "name": "existing",
        "enabled": True,
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "targets": "web",
        "work_mode": "work",
        "project_id": "default",
        "user_id": "alice",
        "group_id": "g",
        "bot_id": "b",
        "session_id": None,
        "wake_offset_seconds": 30,
        "mode": "agent",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


