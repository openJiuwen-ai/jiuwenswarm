from __future__ import annotations

from datetime import datetime
import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.agents.harness.common.tools.cron import cron_runtime as runtime_module
from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    _CronToolsCronBackend,
    _add_xiaoyi_device_fields_to_cron_tools,
    _extract_legacy_params,
)
from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import CronToolRoute, CronTools
from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService
from jiuwenswarm.gateway.cron.store import CronJob, CronJobStore

import time


class _TestableScheduler(CronSchedulerService):
    def compute_next_run(self, job: CronJob, *, now_ts: float):
        return self._compute_next_run(job, now_ts=now_ts)


def _make_job(job_id="job-1", name="test", **overrides):
    defaults = {
        "id": job_id,
        "name": name,
        "enabled": True,
        "expired": False,
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "wake_offset_seconds": 300,
        "description": "reminder",
        "targets": "tui",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    defaults.update(overrides)
    return CronJob(**defaults)
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.device_tool_planner import (
    CronDeviceToolPlan,
)
from openjiuwen.harness.tools.cron import create_cron_tools
from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import CronTools


class _FakeCronTools:
    def __init__(self) -> None:
        self.routes: list[object] = []
        self.reset_tokens: list[str] = []
        self.create_payloads: list[dict] = []

    def push_cron_route(self, route):
        self.routes.append(route)
        return "token-1"

    def reset_cron_route(self, token):
        self.reset_tokens.append(token)

    async def create_job(self, payload: dict):
        self.create_payloads.append(payload)
        return payload

    async def list_jobs(self):
        return []

    async def get_job(self, job_id: str):
        _ = job_id
        return None

    async def update_job(self, job_id: str, payload: dict):
        return {"id": job_id, **payload}

    async def delete_job(self, job_id: str):
        _ = job_id
        return True

    async def toggle_job(self, job_id: str, enabled: bool):
        return {"id": job_id, "enabled": enabled}

    async def preview_job(self, job_id: str, count: int = 5):
        _ = (job_id, count)
        return []

    async def run_now(self, job_id: str):
        _ = job_id
        return {"run_id": "r-1"}


class _FakeGatewayPush:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def send_push(self, payload: dict) -> None:
        self.payloads.append(payload)


def _setup_project_store(tmp_path, monkeypatch):
    root = tmp_path / "agent"
    root.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_agent_root_dir",
        lambda: root,
    )
    from jiuwenswarm.server.runtime.session import project_store

    project_store.invalidate_cache()
    return project_store


def _make_cron_tools(tmp_path, monkeypatch) -> tuple[CronTools, _FakeGatewayPush]:
    push = _FakeGatewayPush()
    tools = CronTools(gateway_push=push, agent_client=object(), message_handler=object())
    tools._local_store = CronJobStore(path=tmp_path / "cron_jobs.json")

    async def _noop_reload() -> None:
        return None

    monkeypatch.setattr(tools, "_reload_scheduler", _noop_reload)
    return tools, push
class _FakePlanner:
    def __init__(
        self,
        result: CronDeviceToolPlan | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result or CronDeviceToolPlan((), (), ())
        self.error = error
        self.calls: list[dict] = []

    async def plan(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def test_extract_legacy_params_maps_implicit_web_to_context_channel() -> None:
    context = SimpleNamespace(
        channel_id="feishu_enterprise:open_id:abc",
        session_id="sess-1",
        metadata={"request_id": "req-1"},
    )
    payload = {
        "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
        "payload": {"kind": "agentTurn", "message": "ping"},
        "delivery": {"channel": "web"},
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    # normalize_target_channel_id keeps the canonical enterprise channel prefix.
    assert out["targets"] == "feishu_enterprise:open_id"


def test_extract_legacy_params_delivery_channel_takes_priority_over_targets() -> None:
    context = SimpleNamespace(channel_id="feishu_enterprise:open_id:abc")
    payload = {
        "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
        "payload": {"kind": "agentTurn", "message": "ping"},
        "delivery": {"channel": "web"},
        "targets": "wecom",
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["targets"] == "web"


def test_extract_legacy_params_context_mode_takes_priority_over_payload() -> None:
    context = SimpleNamespace(
        channel_id="web",
        session_id="sess-1",
        mode="agent.fast",
    )
    payload = {
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "agentTurn", "message": "daily report"},
        "mode": "team",
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["mode"] == "agent"


def test_extract_legacy_params_inherits_context_mode_when_missing() -> None:
    context = SimpleNamespace(channel_id="web", session_id="sess-1", mode="team")
    payload = {
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "agentTurn", "message": "daily report"},
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["mode"] == "team"


def test_extract_legacy_params_defaults_to_agent_without_context_mode() -> None:
    context = SimpleNamespace(channel_id="web", session_id="sess-1")
    payload = {
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "agentTurn", "message": "daily report"},
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["mode"] == "agent"


def test_runtime_cron_schemas_keep_device_intents_internal() -> None:
    tools = create_cron_tools(_FakeCronTools(), context=None)

    _add_xiaoyi_device_fields_to_cron_tools(tools)

    cards = {tool.card.name: tool.card for tool in tools}
    assert (
        "required_device_intents"
        not in cards["cron_create_job"].input_params["properties"]
    )
    assert "required_device_intents" not in (
        cards["cron"].input_params["properties"]["job"]["properties"]
    )


@pytest.mark.parametrize(
    "context_mode, payload_mode, expected",
    [
        ("design", None, "agent"),
        ("code.normal", None, "agent"),
        ("future.mode", None, "agent"),
        ("design", "team", "team"),
        ("code.normal", "agent", "agent"),
        ("team", None, "team"),
    ],
)
def test_extract_legacy_params_profile_modes_fall_back_to_agent(
    context_mode: str, payload_mode: str | None, expected: str
) -> None:
    context = SimpleNamespace(channel_id="web", session_id="sess-1", mode=context_mode)
    payload: dict = {
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "agentTurn", "message": "daily report"},
    }
    if payload_mode is not None:
        payload["mode"] = payload_mode

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["mode"] == expected


@pytest.mark.asyncio
async def test_ensure_scheduler_requires_message_handler() -> None:
    tools = CronTools(agent_client=object(), message_handler=None)
    scheduler = await tools.ensure_scheduler()
    assert scheduler is None


@pytest.mark.asyncio
async def test_cron_backend_create_job_pushes_and_resets_route() -> None:
    cron_tools = _FakeCronTools()
    backend = _CronToolsCronBackend(cron_tools=cron_tools, message_handler=None)
    context = SimpleNamespace(
        channel_id="web",
        session_id="sess-1",
        metadata={"request_id": "req-123"},
    )

    await backend.create_job(
        {
            "id": "job-1",
            "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
            "payload": {"kind": "agentTurn", "message": "hello"},
            "delivery": {"channel": "web"},
        },
        context=context,
    )

    assert len(cron_tools.routes) == 1
    assert cron_tools.routes[0].request_id == "req-123"
    assert cron_tools.routes[0].channel_id == "web"
    assert cron_tools.routes[0].session_id == "sess-1"
    assert cron_tools.reset_tokens == ["token-1"]
    assert cron_tools.create_payloads[0]["id"] == "job-1"


@pytest.mark.asyncio
async def test_cron_tools_create_job_resolves_route_project_dir(tmp_path, monkeypatch) -> None:
    project_store = _setup_project_store(tmp_path, monkeypatch)
    project_dir = tmp_path / "project-a"
    project_dir.mkdir()
    project = project_store.create_project("P1", str(project_dir))
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    token = tools.push_cron_route(CronToolRoute(project_dir=str(project_dir)))
    try:
        job = await tools.create_job(
            {
                "id": "job-1",
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "hello",
                "targets": "web",
            }
        )
    finally:
        tools.reset_cron_route(token)

    assert job["project_id"] == project.project_id
    synced = push.payloads[-1]["body"]["data"]
    assert synced["project_id"] == project.project_id
    assert "project_dir" not in job
    assert "project_dir" not in synced


@pytest.mark.asyncio
async def test_cron_tools_create_job_does_not_persist_source_workspace_cwd(
    tmp_path, monkeypatch,
) -> None:
    _setup_project_store(tmp_path, monkeypatch)
    source_dir = tmp_path / "设计工作空间"
    source_dir.mkdir()
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    token = tools.push_cron_route(
        CronToolRoute(
            project_dir=str(source_dir),
            project_id="default_design",
            work_mode="design",
        )
    )
    try:
        job = await tools.create_job(
            {
                "id": "job-source-ws",
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "hello",
                "targets": "web",
            }
        )
    finally:
        tools.reset_cron_route(token)

    assert job["project_id"] == "default_design"
    assert job["work_mode"] == "design"
    assert "project_dir" not in job
    synced = push.payloads[-1]["body"]["data"]
    assert synced["project_id"] == "default_design"
    assert "project_dir" not in synced
    local_job = (await tools._local_store.get_job("job-source-ws")).to_dict()
    assert local_job.get("project_dir", "") == ""


@pytest.mark.asyncio
async def test_cron_tools_create_job_uses_route_project_id_and_work_mode(
    tmp_path, monkeypatch,
) -> None:
    project_store = _setup_project_store(tmp_path, monkeypatch)
    project_dir = tmp_path / "shared-project"
    project_dir.mkdir()
    project_store.create_project("WorkP", str(project_dir), work_mode="work")
    code_project = project_store.create_project("CodeP", str(project_dir), work_mode="code")
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    token = tools.push_cron_route(
        CronToolRoute(
            project_dir=str(project_dir),
            project_id=code_project.project_id,
            work_mode="code",
        )
    )
    try:
        job = await tools.create_job(
            {
                "id": "job-route-project",
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "hello",
                "targets": "web",
            }
        )
    finally:
        tools.reset_cron_route(token)

    assert job["project_id"] == code_project.project_id
    assert job["work_mode"] == "code"
    synced = push.payloads[-1]["body"]["data"]
    assert synced["project_id"] == code_project.project_id
    assert synced["work_mode"] == "code"


@pytest.mark.asyncio
async def test_cron_tools_create_job_rejects_relative_project_dir(tmp_path, monkeypatch) -> None:
    _setup_project_store(tmp_path, monkeypatch)
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    token = tools.push_cron_route(CronToolRoute(project_dir="relative/path"))
    try:
        with pytest.raises(ValueError, match="project_dir must be an absolute path"):
            await tools.create_job(
                {
                    "id": "job-1",
                    "name": "daily",
                    "cron_expr": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                    "description": "hello",
                    "targets": "web",
                }
            )
    finally:
        tools.reset_cron_route(token)

    assert push.payloads == []
    assert await tools.list_jobs() == []


@pytest.mark.asyncio
async def test_cron_tools_update_job_persists_exec_cwd_without_rewriting_project_id(
    tmp_path, monkeypatch
) -> None:
    project_store = _setup_project_store(tmp_path, monkeypatch)
    real_dir = tmp_path / "project-b"
    real_dir.mkdir()
    project = project_store.create_project("P2", str(real_dir))
    isolated = tmp_path / "定时任务-2026-08-29-16-07-11"
    isolated.mkdir()
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    await tools._local_store.create_job(
        job_id="job-1",
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="hello",
        targets="web",
        project_id=project.project_id,
        work_mode="work",
    )

    job = await tools.update_job("job-1", {"project_dir": str(isolated)})

    assert job["project_id"] == project.project_id
    assert job["project_dir"] == str(isolated)
    synced_patch = push.payloads[-1]["body"]["data"]["patch"]
    assert synced_patch["project_dir"] == str(isolated)
    assert "project_id" not in synced_patch
    local_job = (await tools._local_store.get_job("job-1")).to_dict()
    assert local_job["project_dir"] == str(isolated)
    assert local_job["project_id"] == project.project_id


@pytest.mark.asyncio
async def test_cron_tools_update_isolated_dir_keeps_default_design(
    tmp_path, monkeypatch
) -> None:
    _setup_project_store(tmp_path, monkeypatch)
    isolated = tmp_path / "定时任务-isolated"
    isolated.mkdir()
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    await tools._local_store.create_job(
        job_id="job-design",
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="hello",
        targets="web",
        project_id="default_design",
        work_mode="design",
    )

    job = await tools.update_job("job-design", {"project_dir": str(isolated)})

    assert job["project_id"] == "default_design"
    assert job["work_mode"] == "design"
    assert job["project_dir"] == str(isolated)
    synced_patch = push.payloads[-1]["body"]["data"]["patch"]
    assert synced_patch["project_dir"] == str(isolated)
    assert "project_id" not in synced_patch


@pytest.mark.asyncio
async def test_cron_tools_update_job_validates_model(tmp_path, monkeypatch) -> None:
    _setup_project_store(tmp_path, monkeypatch)
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    await tools._local_store.create_job(
        job_id="job-1",
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="hello",
        targets="web",
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.cron.cron_tools.validate_cron_model",
        lambda raw: "checked-model" if raw == "valid-model" else None,
    )

    job = await tools.update_job("job-1", {"model_name": "valid-model"})

    assert job["model_name"] == "checked-model"
    synced_patch = push.payloads[-1]["body"]["data"]["patch"]
    assert synced_patch["model_name"] == "checked-model"


@pytest.mark.asyncio
async def test_cron_tools_create_job_tool_preserves_explicit_empty_project_dir(
    tmp_path, monkeypatch
) -> None:
    project_store = _setup_project_store(tmp_path, monkeypatch)
    project_dir = tmp_path / "project-c"
    project_dir.mkdir()
    project_store.create_project("P3", str(project_dir))
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    token = tools.push_cron_route(CronToolRoute(project_dir=str(project_dir)))
    try:
        job = await tools._create_job_tool(
            name="daily",
            cron_expr="0 9 * * *",
            timezone="Asia/Shanghai",
            description="hello",
            targets="web",
            project_dir="",
        )
    finally:
        tools.reset_cron_route(token)

    assert job["project_id"] == ""
    synced = push.payloads[-1]["body"]["data"]
    assert "project_dir" not in synced


_BASE_JOB = {
    "id": "job-wm",
    "name": "daily",
    "cron_expr": "0 9 * * *",
    "timezone": "Asia/Shanghai",
    "description": "hello",
    "targets": "web",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario, expected_wm, expected_pid", [
    pytest.param("web_default", "work", None, id="web_default_work"),
    pytest.param("explicit_project_id", "code", "code_proj", id="project_id_injects_code"),
    pytest.param("default_code", "code", "default_code", id="default_code_project"),
    pytest.param("default_design", "design", "default_design", id="default_design_project"),
    pytest.param("invalid", None, None, id="rejects_invalid_work_mode"),
])
async def test_cron_tools_create_job_work_mode(tmp_path, monkeypatch, scenario, expected_wm, expected_pid):
    project_store = _setup_project_store(tmp_path, monkeypatch)
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    base = dict(_BASE_JOB)
    code_project = None
    if scenario == "explicit_project_id":
        pd = tmp_path / "code-proj"
        pd.mkdir()
        code_project = project_store.create_project("CodeProj", str(pd), work_mode="code")
        base["project_id"] = code_project.project_id
    elif scenario == "default_code":
        base["project_id"] = "default_code"
    elif scenario == "default_design":
        base["project_id"] = "default_design"
    elif scenario == "invalid":
        base["work_mode"] = "invalid_mode"

    if scenario == "invalid":
        with pytest.raises(ValueError, match="invalid work_mode"):
            await tools.create_job(base)
        assert push.payloads == []
        return

    job = await tools.create_job(base)
    synced = push.payloads[-1]["body"]["data"]
    assert job["work_mode"] == expected_wm
    assert synced["work_mode"] == expected_wm
    if expected_pid == "code_proj":
        assert job["project_id"] == code_project.project_id
    elif expected_pid:
        assert job["project_id"] == expected_pid
        assert synced["project_id"] == expected_pid


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    pytest.param("patch_project_id", id="patch_pid_injects_work_mode"),
])
async def test_cron_tools_update_job_injects_work_mode(tmp_path, monkeypatch, scenario):
    project_store = _setup_project_store(tmp_path, monkeypatch)
    pd = tmp_path / "code-proj"
    pd.mkdir()
    project = project_store.create_project("CodeProj", str(pd), work_mode="code")
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    await tools._local_store.create_job(
        job_id="job-update", name="daily", cron_expr="0 9 * * *",
        timezone="Asia/Shanghai", description="hello", targets="web",
    )
    patch = {"project_id": project.project_id}
    job = await tools.update_job("job-update", patch)
    assert job["project_id"] == project.project_id
    assert job["work_mode"] == "code"
    synced_patch = push.payloads[-1]["body"]["data"]["patch"]
    assert synced_patch["project_id"] == project.project_id
    assert synced_patch["work_mode"] == "code"


@pytest.mark.asyncio
@pytest.mark.parametrize("patch, match", [
    pytest.param({"project_id": "proj_missing"}, "project not found", id="unknown_project_id"),
    pytest.param({"work_mode": "code"}, "work_mode cannot be patched alone", id="work_mode_alone"),
    pytest.param({"work_mode": "invalid"}, "invalid work_mode", id="invalid_work_mode"),
])
async def test_cron_tools_update_job_rejects_invalid_patch(tmp_path, monkeypatch, patch, match):
    _setup_project_store(tmp_path, monkeypatch)
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    await tools._local_store.create_job(
        job_id="job-reject", name="daily", cron_expr="0 9 * * *",
        timezone="Asia/Shanghai", description="hello", targets="web",
    )
    with pytest.raises(ValueError, match=match):
        await tools.update_job("job-reject", patch)
    assert push.payloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_pid, expected_pid",
    [
        pytest.param("default", "default", id="session_default_work"),
        pytest.param("default_code", "default_code", id="session_default_code"),
        pytest.param("default_design", "default_design", id="session_default_design"),
        pytest.param("", "", id="session_empty_like_manual"),
    ],
)
async def test_cron_tools_create_job_inherits_session_default_project_id(
    tmp_path, monkeypatch, session_pid, expected_pid
):
    """Issue #2653：对话创建未显式传 project_id 时注入会话 project_id。

    会话在默认项目下会落库 default/default_code（与手动未选的空串不同）；
    列表展示层须把二者统一显示为「-」。
    """
    _setup_project_store(tmp_path, monkeypatch)
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    token = tools.push_cron_route(CronToolRoute(project_id=session_pid))
    try:
        job = await tools.create_job(
            {
                "id": "job-session-default",
                "name": "reminder",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "drink",
                "targets": "web",
            }
        )
    finally:
        tools.reset_cron_route(token)

    assert job["project_id"] == expected_pid
    synced = push.payloads[-1]["body"]["data"]
    assert synced["project_id"] == expected_pid


class TestExtractLegacyParamsKindAt:
    def test_kind_at_converts_to_cron_expr(self) -> None:
        context = SimpleNamespace(
            channel_id="web",
            session_id="sess-1",
            metadata={"request_id": "req-1"},
        )
        payload = {
            "schedule": {"kind": "at", "at": "2026-07-24T18:25:31+08:00"},
            "payload": {"kind": "agentTurn", "message": "喝水提醒"},
            "delivery": {"mode": "announce"},
        }

        out = _extract_legacy_params(payload, context=context, require_schedule=True)

        assert out["cron_expr"] == "25 18 24 7 5"
        assert out["timezone"] == "Asia/Shanghai"
        assert out["description"] == "喝水提醒"
        assert "wake_offset_seconds" not in out
        assert out["delete_after_run"] is True

    def test_kind_at_without_at_field_raises(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"kind": "at"},
            "payload": {"kind": "agentTurn", "message": "提醒"},
            "delivery": {"mode": "announce"},
        }

        with pytest.raises(ValueError, match="schedule.at"):
            _extract_legacy_params(payload, context=context, require_schedule=True)

    def test_kind_at_invalid_iso_raises(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"kind": "at", "at": "not-a-date"},
            "payload": {"kind": "agentTurn", "message": "提醒"},
            "delivery": {"mode": "announce"},
        }

        with pytest.raises(ValueError, match="Cannot convert"):
            _extract_legacy_params(payload, context=context, require_schedule=True)

    def test_kind_at_preserves_timezone(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"kind": "at", "at": "2026-01-01T09:00:00+09:00", "tz": "Asia/Tokyo"},
            "payload": {"kind": "agentTurn", "message": "朝会"},
            "delivery": {"mode": "announce"},
        }

        out = _extract_legacy_params(payload, context=context, require_schedule=True)

        assert out["timezone"] == "Asia/Tokyo"
        assert out["cron_expr"] == "0 9 1 1 4"
        assert out["delete_after_run"] is True

    def test_kind_at_forces_delete_after_run_true_even_when_false(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"kind": "at", "at": "2026-07-24T18:25:31+08:00"},
            "payload": {"kind": "agentTurn", "message": "提醒"},
            "delivery": {"mode": "announce"},
            "deleteAfterRun": False,
        }

        out = _extract_legacy_params(payload, context=context, require_schedule=True)

        assert out["delete_after_run"] is True
        assert out["cron_expr"] == "25 18 24 7 5"

    def test_kind_every_still_raises(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"kind": "every", "interval": "5m"},
            "payload": {"kind": "agentTurn", "message": "提醒"},
            "delivery": {"mode": "announce"},
        }

        with pytest.raises(ValueError, match="Unsupported schedule.kind"):
            _extract_legacy_params(payload, context=context, require_schedule=True)


class TestExtractLegacyParamsSystemEventPayload:
    def test_system_event_converts_to_agent_turn(self) -> None:
        context = SimpleNamespace(
            channel_id="web",
            session_id="sess-1",
            metadata={"request_id": "req-1"},
        )
        payload = {
            "schedule": {"kind": "cron", "expr": "0 33 16 24 7 ? 2026"},
            "payload": {"kind": "systemEvent", "text": "该喝水了！记得补充水分哦"},
            "delivery": {"mode": "announce"},
        }

        out = _extract_legacy_params(payload, context=context, require_schedule=True)

        assert out["description"] == "该喝水了！记得补充水分哦"
        assert out["cron_expr"] == "0 33 16 24 7 ? 2026"

    def test_system_event_text_used_as_description(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
            "payload": {"kind": "systemEvent", "text": "系统消息内容"},
            "delivery": {"channel": "web"},
        }

        out = _extract_legacy_params(payload, context=context, require_schedule=True)

        assert out["description"] == "系统消息内容"

    def test_agent_turn_message_takes_priority_over_system_event_text(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
            "payload": {"kind": "agentTurn", "message": "agentTurn消息"},
            "delivery": {"channel": "web"},
        }

        out = _extract_legacy_params(payload, context=context, require_schedule=True)

        assert out["description"] == "agentTurn消息"

    def test_other_payload_kind_raises(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
            "payload": {"kind": "unknownType", "text": "提醒"},
            "delivery": {"channel": "web"},
        }

        with pytest.raises(ValueError, match="Unsupported payload.kind"):
            _extract_legacy_params(payload, context=context, require_schedule=True)


class TestComputeNextRunMissedTriggerWindow:
    """When croniter fails on a one-shot job, check if the missed trigger is within
    the window and schedule immediate execution instead of marking expired."""

    def test_5field_cron_returns_next_cycle_after_just_missed(self) -> None:
        svc = _TestableScheduler.__new__(_TestableScheduler)

        job = _make_job(
            job_id="daily-930",
            cron_expr="30 9 * * *",
            timezone="Asia/Shanghai",
            wake_offset_seconds=0,
        )

        now_ts = datetime(2026, 7, 24, 9, 30, 5, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

        push_dt, wake_dt, run_id = svc.compute_next_run(job, now_ts=now_ts)

        assert push_dt == datetime(2026, 7, 25, 9, 30, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert wake_dt == push_dt
        assert run_id.startswith("daily-930:")

    def test_5field_impossible_cron_raises_failed_to_find_date(self) -> None:
        svc = _TestableScheduler.__new__(_TestableScheduler)

        job = _make_job(
            job_id="impossible",
            cron_expr="0 9 31 2 *",
            timezone="Asia/Shanghai",
            wake_offset_seconds=0,
        )

        now_ts = datetime(2026, 7, 24, 18, 25, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

        with pytest.raises(Exception, match="failed to find"):
            svc.compute_next_run(job, now_ts=now_ts)

    def test_recurring_job_still_works(self) -> None:
        svc = _TestableScheduler.__new__(_TestableScheduler)

        job = _make_job(
            job_id="recurring",
            cron_expr="*/5 * * * *",
            timezone="Asia/Shanghai",
            wake_offset_seconds=0,
        )

        now_ts = datetime(2026, 7, 24, 18, 30, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

        push_dt, wake_dt, run_id = svc.compute_next_run(job, now_ts=now_ts)

        assert push_dt.minute == 35
        assert wake_dt == push_dt
async def test_cron_backend_uses_original_schema_for_ordinary_job() -> None:
    cron_tools = _FakeCronTools()
    backend = _CronToolsCronBackend(cron_tools=cron_tools, message_handler=None)
    context = SimpleNamespace(
        channel_id="web",
        session_id="sess-1",
        metadata={"request_id": "req-123"},
    )

    await backend.create_job(
        {
            "name": "ordinary",
            "cron_expr": "*/5 * * * *",
            "timezone": "Asia/Shanghai",
            "description": "hello",
        },
        context=context,
    )

    assert len(cron_tools.create_payloads) == 1
    assert "required_device_intents" not in cron_tools.create_payloads[0]


@pytest.mark.asyncio
async def test_cron_backend_preflights_device_intents_before_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_tools = _FakeCronTools()
    planner = _FakePlanner(
        CronDeviceToolPlan(
            ("create_note", "create_alarm"),
            ("CreateNote", "CreateAlarm"),
            ("CreateNote", "CreateAlarm"),
        )
    )
    backend = _CronToolsCronBackend(
        cron_tools=cron_tools,
        message_handler=None,
        device_tool_planner=planner,
    )
    context = SimpleNamespace(
        channel_id="xiaoyi",
        session_id="sess-1",
        metadata={
            "request_id": "req-123",
            "xiaoyi_push_id": "push-1",
        },
    )
    checked: list[str] = []

    async def fake_check(intent: str) -> dict:
        checked.append(intent)
        return {"authorized": True, "code": 0}

    monkeypatch.setattr(
        runtime_module,
        "execute_plugin_privilege_check",
        fake_check,
    )

    await backend.create_job(
        {
            "name": "device",
            "cron_expr": "0 0 9 * * ? *",
            "timezone": "Asia/Shanghai",
            "description": "write note and create alarm",
        },
        context=context,
    )

    assert planner.calls[0]["description"] == "write note and create alarm"
    assert checked == ["CreateNote", "CreateAlarm"]
    assert cron_tools.create_payloads[0]["required_device_intents"] == [
        "CreateNote",
        "CreateAlarm",
    ]
    assert cron_tools.create_payloads[0]["xiaoyi_push_id"] == "push-1"


@pytest.mark.asyncio
async def test_cron_backend_does_not_create_when_privilege_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_tools = _FakeCronTools()
    planner = _FakePlanner(
        CronDeviceToolPlan(
            ("create_note", "create_alarm"),
            ("CreateNote", "CreateAlarm"),
            ("CreateNote", "CreateAlarm"),
        )
    )
    backend = _CronToolsCronBackend(
        cron_tools=cron_tools,
        message_handler=None,
        device_tool_planner=planner,
    )
    context = SimpleNamespace(
        channel_id="xiaoyi",
        session_id="sess-1",
        metadata={
            "request_id": "req-123",
            "xiaoyi_push_id": "push-1",
        },
    )

    async def fake_check(intent: str) -> dict:
        return {"authorized": intent != "CreateAlarm"}

    monkeypatch.setattr(
        runtime_module,
        "execute_plugin_privilege_check",
        fake_check,
    )

    with pytest.raises(RuntimeError, match="denied"):
        await backend.create_job(
            {
                "name": "device",
                "cron_expr": "0 0 9 * * ? *",
                "timezone": "Asia/Shanghai",
                "description": "write note and create alarm",
            },
            context=context,
        )

    assert cron_tools.create_payloads == []


@pytest.mark.asyncio
async def test_cron_backend_does_not_create_when_privilege_check_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_tools = _FakeCronTools()
    planner = _FakePlanner(
        CronDeviceToolPlan(
            ("create_note",),
            ("CreateNote",),
            ("CreateNote",),
        )
    )
    backend = _CronToolsCronBackend(
        cron_tools=cron_tools,
        message_handler=None,
        device_tool_planner=planner,
    )
    context = SimpleNamespace(
        channel_id="xiaoyi",
        session_id="sess-1",
        metadata={
            "request_id": "req-123",
            "xiaoyi_push_id": "push-1",
        },
    )

    async def fake_check(intent: str) -> dict:
        raise asyncio.TimeoutError(intent)

    monkeypatch.setattr(
        runtime_module,
        "execute_plugin_privilege_check",
        fake_check,
    )

    with pytest.raises(asyncio.TimeoutError):
        await backend.create_job(
            {
                "name": "device",
                "cron_expr": "0 0 9 * * ? *",
                "timezone": "Asia/Shanghai",
                "description": "write note",
            },
            context=context,
        )

    assert cron_tools.create_payloads == []


@pytest.mark.asyncio
async def test_cron_backend_does_not_create_when_planning_fails(
) -> None:
    cron_tools = _FakeCronTools()
    planner = _FakePlanner(error=RuntimeError("planning failed"))
    backend = _CronToolsCronBackend(
        cron_tools=cron_tools,
        message_handler=None,
        device_tool_planner=planner,
    )
    context = SimpleNamespace(
        channel_id="xiaoyi",
        session_id="sess-1",
        metadata={
            "request_id": "req-123",
            "xiaoyi_push_id": "push-1",
        },
    )
    with pytest.raises(RuntimeError, match="planning failed"):
        await backend.create_job(
            {
                "name": "device",
                "cron_expr": "0 0 9 * * ? *",
                "timezone": "Asia/Shanghai",
                "description": "unsupported action",
            },
            context=context,
        )

    assert cron_tools.create_payloads == []


@pytest.mark.asyncio
async def test_cron_backend_routes_non_privileged_device_tool_without_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_tools = _FakeCronTools()
    planner = _FakePlanner(
        CronDeviceToolPlan(
            ("upload_file",),
            ("FileUploadForClaw",),
            (),
        )
    )
    backend = _CronToolsCronBackend(
        cron_tools=cron_tools,
        message_handler=None,
        device_tool_planner=planner,
    )
    context = SimpleNamespace(
        channel_id="xiaoyi",
        session_id="sess-1",
        metadata={
            "request_id": "req-123",
            "xiaoyi_push_id": "push-1",
        },
    )

    async def unexpected_check(intent: str) -> dict:
        raise AssertionError(f"unexpected privilege check: {intent}")

    monkeypatch.setattr(
        runtime_module,
        "execute_plugin_privilege_check",
        unexpected_check,
    )

    await backend.create_job(
        {
            "name": "upload",
            "cron_expr": "0 0 9 * * ? *",
            "timezone": "Asia/Shanghai",
            "description": "upload file",
        },
        context=context,
    )

    assert cron_tools.create_payloads[0]["required_device_intents"] == [
        "FileUploadForClaw"
    ]
    assert cron_tools.create_payloads[0]["xiaoyi_push_id"] == "push-1"

class TestExtractLegacyParamsKindCronClearsDeleteAfterRun:
    """间隔/周期 schedule 必须清除一次性 delete_after_run。

    回归：一次性任务（kind=at + delete_after_run=true）改成间隔后，
    遗留的 delete_after_run 会让调度器在首次触发后把任务标记为过期禁用。
    """

    def test_kind_cron_expr_clears_delete_after_run_even_when_true(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"kind": "cron", "expr": "*/30 * * * *"},
            "payload": {"kind": "agentTurn", "message": "该喝水啦！记得去喝杯水 💧"},
            "delivery": {"mode": "announce"},
            "deleteAfterRun": True,
        }

        out = _extract_legacy_params(payload, context=context, require_schedule=False)

        assert out["delete_after_run"] is False
        assert out["cron_expr"] == "*/30 * * * *"

    def test_cron_expr_without_kind_clears_delete_after_run(self) -> None:
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "schedule": {"expr": "*/15 * * * *"},
            "payload": {"kind": "agentTurn", "message": "提醒"},
            "deleteAfterRun": True,
        }

        out = _extract_legacy_params(payload, context=context, require_schedule=False)

        assert out["delete_after_run"] is False

    def test_payload_only_patch_keeps_explicit_delete_after_run(self) -> None:
        """不带 cron 表达式的 patch 不强制清除，保留显式传入的值。"""
        context = SimpleNamespace(channel_id="web", session_id="sess-1")
        payload = {
            "payload": {"kind": "agentTurn", "message": "新提醒内容"},
            "deleteAfterRun": True,
        }

        out = _extract_legacy_params(payload, context=context, require_schedule=False)

        assert out["delete_after_run"] is True


@pytest.mark.asyncio
async def test_cron_backend_create_job_rejected_inside_cron_run() -> None:
    """调度执行现场禁止再建新任务（防任务翻倍兜底）。

    scheduler._run_agent 会在 request metadata 下发 {"cron": {"job_id",
    "run_id"}}；处于调度执行中的会话调 create_job 必须直接拒绝，
    避免"每天/每周…"任务描述被模型再建一遍调度。
    """
    cron_tools = _FakeCronTools()
    backend = _CronToolsCronBackend(cron_tools=cron_tools, message_handler=None)
    context = SimpleNamespace(
        channel_id="web",
        session_id="__cron___18f2c1_ab12cd34",
        metadata={
            "request_id": "req-123",
            "cron": {"job_id": "job-9", "run_id": "run-9"},
            "targets": "web",
        },
    )

    with pytest.raises(ValueError, match="调度执行现场"):
        await backend.create_job(
            {
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "每天提醒喝水",
            },
            context=context,
        )

    assert cron_tools.create_payloads == []
    assert cron_tools.routes == []


@pytest.mark.asyncio
async def test_cron_backend_create_job_allowed_without_cron_metadata() -> None:
    """普通对话（metadata 无 cron 字段）不受兜底防线影响。"""
    cron_tools = _FakeCronTools()
    backend = _CronToolsCronBackend(cron_tools=cron_tools, message_handler=None)
    context = SimpleNamespace(
        channel_id="web",
        session_id="sess-1",
        metadata={"request_id": "req-123"},
    )

    await backend.create_job(
        {
            "name": "daily",
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
            "description": "hello",
        },
        context=context,
    )

    assert len(cron_tools.create_payloads) == 1
