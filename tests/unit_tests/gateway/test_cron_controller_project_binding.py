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

from gateway.cron.controller import CronController
from gateway.cron.proactive_cron_sync import PROACTIVE_JOB_ID
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
    assert await controller.delete_job("job_a", user_id="alice")
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


def _listed_job(job_id: str, **fields):
    payload = {"id": job_id, **fields}
    return SimpleNamespace(**payload, to_dict=lambda payload=payload: dict(payload))


@pytest.mark.asyncio
async def test_list_jobs_filters_by_project_user_group_and_bot() -> None:
    """用户、群、机器人不传就是空串。不传项目编号则每个项目都返回。空项目编号只要默认项目。"""
    jobs = [
        _listed_job("home", project_id="", user_id="", group_id="", bot_id=""),
        _listed_job("job_a", project_id="proj_1", user_id="alice", group_id="", bot_id=""),
        _listed_job("job_c", project_id="", user_id="alice", group_id="g1", bot_id="b1"),
    ]

    async def list_jobs():
        return list(jobs)

    controller = CronController(
        store=SimpleNamespace(list_jobs=list_jobs),
        scheduler=_FakeScheduler(),
    )

    assert [job["id"] for job in await controller.list_jobs()] == ["home"]

    every_project = await controller.list_jobs(
        user_id="alice",
        group_id="",
        bot_id="",
    )
    assert [job["id"] for job in every_project] == ["job_a"]

    personal_project = await controller.list_jobs(
        project_id="proj_1",
        user_id="alice",
        group_id="",
        bot_id="",
    )
    assert [job["id"] for job in personal_project] == ["job_a"]

    # 显式空串与默认值一致：只命中空身份，不把 None 混进来。
    empty_owner = await controller.list_jobs(user_id="", group_id="", bot_id="")
    assert [job["id"] for job in empty_owner] == ["home"]

    enterprise = await controller.list_jobs(
        project_id="",
        user_id="alice",
        group_id="g1",
        bot_id="b1",
    )
    assert [job["id"] for job in enterprise] == ["job_c"]


@pytest.mark.asyncio
async def test_list_jobs_none_user_id_skips_user_filter() -> None:
    """显式 user_id=None 才跳过用户维；默认值必须是 ""，不能是 None。

    None 只留给确定需要全量列表的内部路径。对外入口应传空串，避免跨用户可见。
    """
    jobs = [
        _listed_job("home", project_id="", user_id="", group_id="", bot_id=""),
        _listed_job("job_a", project_id="proj_1", user_id="alice", group_id="", bot_id=""),
        _listed_job("job_c", project_id="", user_id="alice", group_id="g1", bot_id="b1"),
    ]

    async def list_jobs():
        return list(jobs)

    controller = CronController(
        store=SimpleNamespace(list_jobs=list_jobs),
        scheduler=_FakeScheduler(),
    )

    # 仅关键字参数：默认值在 __kwdefaults__。user_id 默认必须是 ""，不能是 None。
    kwdefaults = controller.list_jobs.__kwdefaults__
    assert kwdefaults is not None
    assert kwdefaults["user_id"] == ""
    assert kwdefaults["group_id"] == ""
    assert kwdefaults["bot_id"] == ""
    assert kwdefaults["project_id"] is None

    listed = await controller.list_jobs(user_id=None, group_id="", bot_id="")
    assert [job["id"] for job in listed] == ["home", "job_a"]


@pytest.mark.asyncio
async def test_list_jobs_hides_projects_the_gate_rejects() -> None:
    """身份对上了，项目现在不能跑，列表里仍然不出现。同一项目同一用户只问一次。"""
    jobs = [
        _listed_job("open", project_id="proj_1", user_id="alice", group_id="", bot_id=""),
        _listed_job("also_open", project_id="proj_1", user_id="alice", group_id="", bot_id=""),
        _listed_job("blocked", project_id="proj_hidden", user_id="alice", group_id="", bot_id=""),
    ]
    asked: list[tuple] = []

    async def project_execution_allowed(project_id, user_id=None, group_id=None, bot_id=None) -> bool:
        asked.append((project_id, user_id))
        return project_id != "proj_hidden"

    async def list_jobs():
        return list(jobs)

    controller = CronController(
        store=SimpleNamespace(list_jobs=list_jobs),
        scheduler=_FakeScheduler(project_execution_allowed=project_execution_allowed),
    )
    listed = await controller.list_jobs(
        project_id="proj_1",
        user_id="alice",
        group_id="",
        bot_id="",
    )
    assert [job["id"] for job in listed] == ["open", "also_open"]
    assert asked == [("proj_1", "alice")]

    listed_hidden = await controller.list_jobs(
        project_id="proj_hidden",
        user_id="alice",
        group_id="",
        bot_id="",
    )
    assert listed_hidden == []
    assert asked == [("proj_1", "alice"), ("proj_hidden", "alice")]


@pytest.mark.asyncio
async def test_get_job_uses_the_same_identity_defaults() -> None:
    """不传时只命中默认项目和空身份。对不上返回空，且不询问项目能不能跑。"""
    jobs = {
        "home": _listed_job("home", project_id="", user_id="", group_id="", bot_id=""),
        "job_a": _listed_job("job_a", project_id="proj_1", user_id="alice", group_id="", bot_id=""),
    }

    async def get_job(job_id):
        return jobs.get(job_id)

    async def project_execution_allowed(*_args, **_kwargs):
        raise AssertionError("get_job must not ask whether the project can run")

    controller = CronController(
        store=SimpleNamespace(get_job=get_job),
        scheduler=_FakeScheduler(project_execution_allowed=project_execution_allowed),
    )

    assert (await controller.get_job("home"))["id"] == "home"
    assert await controller.get_job("job_a") is None
    found = await controller.get_job(
        "job_a",
        project_id="proj_1",
        user_id="alice",
        group_id="",
        bot_id="",
    )
    assert found["id"] == "job_a"
    assert await controller.get_job("job_a", project_id="proj_1", user_id="bob") is None
    assert await controller.get_job("missing") is None


@pytest.mark.asyncio
async def test_run_now_checks_tenant_before_trigger() -> None:
    """两边都先核对用户、群、机器人。对不上不触发。不传按空身份。不看项目编号。"""
    jobs = {
        "home": SimpleNamespace(id="home", user_id="", group_id="", bot_id="", project_id="proj_x"),
        "alice": SimpleNamespace(id="alice", user_id="alice", group_id="g1", bot_id="b1", project_id=""),
    }
    triggered: list[str] = []

    async def get_job(job_id):
        return jobs.get(job_id)

    async def trigger_run_now(job_id):
        triggered.append(job_id)
        return f"run-{job_id}"

    controller = CronController(
        store=SimpleNamespace(get_job=get_job),
        scheduler=SimpleNamespace(trigger_run_now=trigger_run_now),
    )

    assert await controller.run_now("home") == "run-home"
    assert triggered == ["home"]
    with pytest.raises(KeyError):
        await controller.run_now("alice")
    assert await controller.run_now("alice", user_id="alice", group_id="g1", bot_id="b1") == "run-alice"
    assert triggered == ["home", "alice"]
    with pytest.raises(KeyError):
        await controller.run_now("missing", user_id="alice", group_id="g1", bot_id="b1")


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


@pytest.mark.asyncio
async def test_create_job_tolerates_user_side_project_id() -> None:
    """marker 标记的绑定结果作为不透明值持久化，不查 Gateway 本地项目表。"""
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
async def test_create_job_rejects_unresolved_real_project_id() -> None:
    """marker 缺失 + 真实 project_id：必须先经目标 AgentServer 解析，明确失败。"""
    cc = _make_controller()
    with pytest.raises(ValueError, match="resolved by the target AgentServer"):
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
async def test_create_job_rejects_unresolved_project_dir() -> None:
    """marker 缺失 + project_dir：同样必须先经目标 AgentServer 解析。"""
    with pytest.raises(ValueError, match="resolved by the target AgentServer"):
        await _make_controller().create_job(
            {
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "targets": "web",
                "project_dir": "/home/user/project",
            }
        )


@pytest.mark.asyncio
async def test_create_job_without_marker_allows_default_project_and_no_fields() -> None:
    """无项目字段的请求（系统路径，如 proactive 自动注册）与默认项目 ID 不需要 marker。"""
    cc = _make_controller()
    job = await cc.create_job(
        {
            "name": "system-path",
            "cron_expr": "0 9 * * *",
            "targets": "web",
            "mode": "proactive.tick",
            "id": PROACTIVE_JOB_ID,
        },
        internal_proactive_sync=True,
    )
    assert job["project_id"] == ""
    assert job["work_mode"] == "work"

    job_default = await cc.create_job(
        {
            "name": "default-bound",
            "cron_expr": "0 9 * * *",
            "targets": "web",
            "project_id": "default_code",
        }
    )
    assert job_default["project_id"] == "default_code"


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


@pytest.mark.asyncio
async def test_update_job_rejects_unresolved_project_fields() -> None:
    """marker 缺失 + 项目字段：必须先经目标 AgentServer 解析。"""
    cc = _make_controller()
    with pytest.raises(ValueError, match="resolved by the target AgentServer"):
        await cc.update_job("job-1", {"project_id": "proj_user_side"})
    with pytest.raises(ValueError, match="resolved by the target AgentServer"):
        await cc.update_job("job-1", {"project_dir": "/home/user/project"})


@pytest.mark.asyncio
async def test_update_job_rejects_work_mode_only_patch() -> None:
    """work_mode 单独 patch 沿用原规则拒绝（仅能伴随项目重解析生效）。"""
    cc = _make_controller()
    with pytest.raises(ValueError, match="requires project re-resolution"):
        await cc.update_job("job-1", {"work_mode": "code"})


# ---------------------------------------------------------------------------
# 会话级 MCP 选择（mcp）随 job 落库 / patch 规范化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_normalizes_and_passes_mcp_to_store(monkeypatch) -> None:
    """create 时 mcp 做 strip/去空/去重后透传 store；不校验存在性。"""
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
    assert create_call["group_id"] == ""
    assert create_call["bot_id"] == ""
    assert create_call["user_id"] == ""


@pytest.mark.asyncio
async def test_enterprise_create_job_requires_three_identities(monkeypatch) -> None:
    """企业版三个身份缺一个就拒绝，不补成 default。"""
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("GATEWAY_DB_HOST", "db.example")
    cc = _make_controller()
    with pytest.raises(ValueError, match="group_id, bot_id and user_id"):
        await cc.create_job(
            {
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "hello",
                "targets": "web",
                "group_id": "g",
                "bot_id": "b",
            }
        )
    assert cc._store.create_calls == []


@pytest.mark.asyncio
async def test_enterprise_create_job_keeps_identity_and_skips_login_binding(
    monkeypatch,
) -> None:
    """企业版原样保存三个身份，只校验模型名，不绑凭据、不存 MCP。"""
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("GATEWAY_DB_HOST", "db.example")
    monkeypatch.setattr(
        "gateway.cron.enterprise_model.resolve_enterprise_cron_model",
        AsyncMock(return_value="some-model"),
    )
    cc = _make_controller()
    job = await cc.create_job(
        {
            "name": "daily",
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
            "description": "hello",
            "targets": "web",
            "group_id": " team ",
            "bot_id": "bot-1",
            "user_id": "alice",
            "model_name": "some-model",
            "mcp": ["files"],
            "wake_offset_seconds": 999999,
            "_agentos_project_binding_verified": True,
            "project_id": "proj_user_side",
            "work_mode": "work",
        }
    )
    create_call = cc._store.create_calls[0]
    assert create_call["group_id"] == "team"
    assert create_call["bot_id"] == "bot-1"
    assert create_call["user_id"] == "alice"
    assert create_call["credential_ref"] == ""
    assert create_call["mcp"] is None
    assert create_call["model_selection"] is None
    assert create_call["model_name"] == "some-model"
    assert create_call["project_id"] == "proj_user_side"
    assert isinstance(create_call["wake_offset_seconds"], int)
    assert create_call["wake_offset_seconds"] < 999999
    assert job["group_id"] == "team"


@pytest.mark.asyncio
async def test_update_job_normalizes_mcp_patch(monkeypatch) -> None:
    """patch mcp：非空列表规范化；空列表/null 归 None（清除选择）。"""
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


@pytest.mark.asyncio
async def test_hide_commit_rejection_restores_original_enabled_jobs():
    jobs = {
        "enabled": SimpleNamespace(id="enabled", project_id="project", enabled=True),
        "disabled": SimpleNamespace(id="disabled", project_id="project", enabled=False),
    }

    async def update_job(job_id, patch):
        jobs[job_id].enabled = patch["enabled"]

    scheduler = _FakeScheduler(reload=AsyncMock(), stop_project_runs=AsyncMock())
    store = SimpleNamespace(list_jobs=AsyncMock(side_effect=lambda: list(jobs.values())), update_job=update_job)
    controller = CronController(store=store, scheduler=scheduler)

    async def reject_commit():
        assert not jobs["enabled"].enabled
        raise RuntimeError("SESSION_BUSY")

    with pytest.raises(RuntimeError, match="SESSION_BUSY"):
        await controller.hide_project_jobs("project", commit=reject_commit)

    assert jobs["enabled"].enabled is True
    assert jobs["disabled"].enabled is False
    assert scheduler.reload.await_count == 2
    assert "project" not in scheduler.hiding_projects


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


@pytest.mark.asyncio
async def test_personal_update_rejects_a_different_user() -> None:
    cc = _make_controller()
    cc._store.get_job = AsyncMock(return_value=_owned_job())
    with pytest.raises(KeyError, match="job not found"):
        await cc.update_job("job-1", {"name": "renamed"}, user_id="bob", group_id="g", bot_id="b")
    assert cc._store.update_calls == []


@pytest.mark.asyncio
async def test_enterprise_update_requires_identity_and_drops_mcp(monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(
        "gateway.cron.enterprise_model.resolve_enterprise_cron_model",
        AsyncMock(return_value="ok-model"),
    )
    cc = _make_controller()
    cc._store.get_job = AsyncMock(return_value=_owned_job())
    with pytest.raises(KeyError, match="job not found"):
        await cc.update_job("job-1", {"name": "x"}, user_id="alice", group_id="g", bot_id="")
    await cc.update_job(
        "job-1",
        {
            "name": "kept",
            "model_name": "m",
            "mcp": ["files"],
            "credential_ref": "secret",
            "group_id": "hacked",
            "_agentos_project_binding_verified": True,
        },
        user_id=" alice ",
        group_id="g",
        bot_id="b",
    )
    _, patch = cc._store.update_calls[0]
    assert patch["name"] == "kept"
    assert patch["model_name"] == "ok-model"
    assert "mcp" not in patch
    assert "credential_ref" not in patch
    assert "group_id" not in patch


@pytest.mark.asyncio
async def test_delete_job_ownership_and_enterprise_skips_sessions(monkeypatch) -> None:
    calls = []
    job = _owned_job(enabled=True, mode="agent")

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
    assert await controller.delete_job("job-1") is False
    assert calls == []
    assert await controller.delete_job("job-1", user_id="alice", group_id="g", bot_id="b") is True
    assert calls == ["disable", "reload", "stop", "sessions", "job", "reload"]

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    calls.clear()
    assert await controller.delete_job("job-1", user_id="alice", group_id="g") is False
    assert calls == []
    assert await controller.delete_job(
        "job-1", user_id="alice", group_id="g", bot_id="b"
    ) is True
    assert calls == ["job", "reload"]
    calls.clear()
    assert await controller.delete_job("job-1", skip_ownership=True) is True
    assert calls == ["job", "reload"]


@pytest.mark.asyncio
async def test_preview_job_rejects_mismatched_owner() -> None:
    cc = CronController(
        store=SimpleNamespace(get_job=AsyncMock(return_value=_owned_job(cron_expr="0 9 * * *"))),
        scheduler=_FakeScheduler(),
    )
    with pytest.raises(KeyError, match="job not found"):
        await cc.preview_job("job-1", user_id="bob", group_id="g", bot_id="b")
    preview = await cc.preview_job("job-1", count=1, user_id="alice", group_id="g", bot_id="b")
    assert preview and "push_at" in preview[0]
