from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    _CronToolsCronBackend,
    _extract_legacy_params,
)
from jiuwenswarm.agents.harness.common.tools.cron import cron_tools as cron_tools_module
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


class _RejectedGatewayPush:
    async def send_push(self, payload: dict) -> bool:
        _ = payload
        return False


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

    assert out["mode"] == "agent.work.normal"


def test_extract_legacy_params_inherits_context_mode_when_missing() -> None:
    context = SimpleNamespace(channel_id="web", session_id="sess-1", mode="team")
    payload = {
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "agentTurn", "message": "daily report"},
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["mode"] == "team.work.normal"


def test_extract_legacy_params_defaults_to_agent_without_context_mode() -> None:
    context = SimpleNamespace(channel_id="web", session_id="sess-1")
    payload = {
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "agentTurn", "message": "daily report"},
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["mode"] == "agent.work.normal"


def test_extract_legacy_params_passthrough_unknown_mode() -> None:
    context = SimpleNamespace(channel_id="web", session_id="sess-1", mode="future.mode")
    payload = {
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "agentTurn", "message": "daily report"},
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["mode"] == "future.mode"


@pytest.mark.asyncio
async def test_ensure_scheduler_requires_message_handler() -> None:
    tools = CronTools(agent_client=object(), message_handler=None)
    scheduler = await tools.ensure_scheduler()
    assert scheduler is None


@pytest.mark.asyncio
async def test_ensure_scheduler_never_starts_second_scheduler(tmp_path, monkeypatch) -> None:
    """Phase 4 单源收敛：AgentServer 不再启动第二个调度器（即使依赖齐备）。"""
    tools = CronTools(gateway_push=_FakeGatewayPush(), agent_client=object(), message_handler=object())
    tools._local_store = CronJobStore(path=tmp_path / "cron_jobs.json")

    scheduler = await tools.ensure_scheduler()

    assert scheduler is None
    assert tools._scheduler is None


@pytest.mark.asyncio
async def test_cron_tools_create_job_does_not_persist_locally(tmp_path, monkeypatch) -> None:
    """Phase 4 单源收敛：create_job 仅经 E2A 转发 Gateway，不写本地 cron_jobs.json。"""
    _setup_project_store(tmp_path, monkeypatch)
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    token = tools.push_cron_route(CronToolRoute(project_dir=""))
    try:
        job = await tools.create_job(
            {
                "id": "job-single-source",
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "hello",
                "targets": "web",
            }
        )
    finally:
        tools.reset_cron_route(token)

    assert job["id"] == "job-single-source"
    assert job["gateway_mutation_status"] == "submitted"
    # 已转发 Gateway（单源落库）
    assert len(push.payloads) == 1
    assert push.payloads[0]["body"]["action"] == "create"
    # 本地 cron_jobs.json 未被写入（单源）
    assert not (tmp_path / "cron_jobs.json").exists()


@pytest.mark.asyncio
async def test_cron_tools_read_gateway_snapshot_after_local_store_is_empty(tmp_path, monkeypatch) -> None:
    """AgentOS jobs remain manageable after an AgentServer process restart."""
    tools, _ = _make_cron_tools(tmp_path, monkeypatch)
    job = _make_job("gateway-only-job", name="from-gateway")
    monkeypatch.setattr(cron_tools_module, "_gateway_jobs_snapshots", {"user-a": {job.id: job}})

    assert await tools._local_store.list_jobs() == []
    token = tools.push_cron_route(CronToolRoute(request_id="req-a", user_id="user-a"))
    try:
        jobs = await tools.list_jobs()
    finally:
        tools.reset_cron_route(token)

    assert [row["id"] for row in jobs] == ["gateway-only-job"]
    token = tools.push_cron_route(CronToolRoute(request_id="req-a", user_id="user-a"))
    try:
        assert (await tools.get_job("gateway-only-job"))["name"] == "from-gateway"
    finally:
        tools.reset_cron_route(token)


@pytest.mark.asyncio
async def test_cron_tools_keep_agentos_snapshots_and_pending_views_user_scoped(tmp_path, monkeypatch) -> None:
    """Concurrent AgentOS users on one AgentServer must never share cron views."""
    tools, _ = _make_cron_tools(tmp_path, monkeypatch)
    job_a = _make_job("job-a", name="A", user_id="user-a")
    job_b = _make_job("job-b", name="B", user_id="user-b")
    monkeypatch.setattr(
        cron_tools_module,
        "_gateway_jobs_snapshots",
        {"user-a": {job_a.id: job_a}, "user-b": {job_b.id: job_b}},
    )

    token_a = tools.push_cron_route(CronToolRoute(request_id="req-a", user_id="user-a"))
    try:
        tools._pending_view_for_route()["pending-a"] = _make_job("pending-a", name="pending A")
        assert {row["id"] for row in await tools.list_jobs()} == {"pending-a", "job-a"}
    finally:
        tools.reset_cron_route(token_a)

    token_b = tools.push_cron_route(CronToolRoute(request_id="req-b", user_id="user-b"))
    try:
        assert [row["id"] for row in await tools.list_jobs()] == ["job-b"]
    finally:
        tools.reset_cron_route(token_b)


@pytest.mark.asyncio
async def test_cron_tools_reports_gateway_delivery_failure() -> None:
    """A disconnected Gateway must not be reported as a forwarded cron request."""
    tools = CronTools(gateway_push=_RejectedGatewayPush())

    with pytest.raises(RuntimeError, match="could not be delivered"):
        await tools._send("create", {"id": "job-1"})


@pytest.mark.asyncio
async def test_cron_tools_runtime_host_transport_waits_for_gateway_ack(
    monkeypatch,
) -> None:
    """The Runtime host adapter keeps develop's Gateway validation round trip."""
    payloads: list[dict] = []

    async def _send(payload: dict) -> bool:
        payloads.append(payload)
        cron_tools_module.resolve_gateway_cron_command_ack(
            payload["body"]["command_id"],
            {"data": {"id": "job-1"}},
        )
        return True

    monkeypatch.setattr(cron_tools_module, "send_runtime_push", _send)
    result = await CronTools()._send("get", {"job_id": "job-1"})

    assert result == {
        "action": "get",
        "status": "ok",
        "data": {"id": "job-1"},
    }
    assert payloads[0]["body"]["action"] == "get"


@pytest.mark.asyncio
async def test_cron_tools_delete_missing_job_returns_false_without_push(
    tmp_path, monkeypatch,
) -> None:
    """与迁移前 store 契约一致：job 不存在时 delete 返回 False，不提交 Gateway。"""
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    assert await tools.delete_job("no-such-job") is False
    assert push.payloads == []


@pytest.mark.asyncio
async def test_cron_tools_toggle_missing_job_raises_key_error(
    tmp_path, monkeypatch,
) -> None:
    """与迁移前 store 契约一致：job 不存在时 toggle 抛 KeyError（不假成功）。"""
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    with pytest.raises(KeyError):
        await tools.toggle_job("no-such-job", True)
    assert push.payloads == []


@pytest.mark.asyncio
async def test_cron_tools_delete_and_toggle_existing_job_still_push(
    tmp_path, monkeypatch,
) -> None:
    """job 存在（Gateway 快照可见）时 delete/toggle 照常提交 Gateway 单源。"""
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    job = _make_job("job-live", name="live")
    monkeypatch.setattr(
        cron_tools_module, "_gateway_jobs_snapshots", {"user-a": {job.id: job}}
    )

    token = tools.push_cron_route(CronToolRoute(request_id="req-op", user_id="user-a"))
    try:
        # 先 toggle 后 delete：delete 会把 job 记入 pending_deletes，
        # 同 scope 内后续 toggle 将不可见（与迁移前删除语义一致）。
        toggled = await tools.toggle_job("job-live", False)
        assert toggled["gateway_mutation_status"] == "submitted"
        assert await tools.delete_job("job-live") is True
    finally:
        tools.reset_cron_route(token)

    actions = [p["body"]["action"] for p in push.payloads]
    assert actions == ["toggle", "delete"]


@pytest.mark.asyncio
async def test_cron_tools_missing_agentos_snapshot_still_forwards_mutations(
    tmp_path, monkeypatch,
) -> None:
    """AgentOS snapshot sync is best-effort; Gateway remains authoritative."""
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    monkeypatch.setattr(cron_tools_module, "_gateway_jobs_snapshots", {})
    token = tools.push_cron_route(CronToolRoute(request_id="req-missing", user_id="user-a"))
    try:
        assert await tools.delete_job("job-live") is True
        toggled = await tools.toggle_job("job-toggle", False)
    finally:
        tools.reset_cron_route(token)

    assert toggled["gateway_mutation_status"] == "submitted"
    assert [p["body"]["action"] for p in push.payloads] == ["delete", "toggle"]


@pytest.mark.asyncio
async def test_cron_tools_pending_scopes_recycled_after_ttl(tmp_path, monkeypatch) -> None:
    """pending scope 过期后由后续访问惰性回收，不随请求无限累积。"""
    tools, _ = _make_cron_tools(tmp_path, monkeypatch)

    token = tools.push_cron_route(CronToolRoute(request_id="req-old", user_id="user-a"))
    try:
        tools._pending_view_for_route()["job-ghost"] = _make_job("job-ghost")
    finally:
        tools.reset_cron_route(token)
    assert "request:req-old" in tools._pending_views

    # 把该 scope 的活跃时间回拨到 TTL 之外，再以新请求触发惰性回收
    tools._pending_scope_last_used["request:req-old"] = (
        time.monotonic() - cron_tools_module._PENDING_SCOPE_TTL_SEC - 1.0
    )
    token = tools.push_cron_route(CronToolRoute(request_id="req-new", user_id="user-a"))
    try:
        tools._pending_view_for_route()
    finally:
        tools.reset_cron_route(token)

    assert "request:req-old" not in tools._pending_views
    assert "request:req-old" not in tools._pending_scope_last_used
    assert "request:req-new" in tools._pending_scope_last_used


@pytest.mark.asyncio
async def test_cron_tools_pending_scopes_capped(tmp_path, monkeypatch) -> None:
    """request_id 病理抖动时 scope 数量受硬上限约束（按最久未使用驱逐）。"""
    tools, _ = _make_cron_tools(tmp_path, monkeypatch)
    cap = cron_tools_module._PENDING_SCOPE_MAX
    for i in range(cap + 5):
        scope = f"request:r{i}"
        tools._pending_scope_last_used[scope] = float(i)
        tools._pending_views[scope] = {}

    token = tools.push_cron_route(CronToolRoute(request_id="req-cap", user_id="user-a"))
    try:
        tools._pending_view_for_route()
    finally:
        tools.reset_cron_route(token)

    assert len(tools._pending_scope_last_used) <= cap
    # 最旧的 scope 被驱逐，当前 scope 保留
    assert "request:r0" not in tools._pending_scope_last_used
    assert "request:req-cap" in tools._pending_scope_last_used


@pytest.mark.asyncio
async def test_cron_tools_run_now_returns_gateway_run_id(tmp_path, monkeypatch) -> None:
    """P2：run_now 等待 Gateway 经 cron.run_now.ack 回传的 run_id。"""
    _setup_project_store(tmp_path, monkeypatch)
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    token = tools.push_cron_route(CronToolRoute(request_id="req-run", user_id="user-a"))
    try:
        task = asyncio.create_task(tools.run_now("job-1"))
        # 让 run_now 已发出 push 并进入等待 ack 状态
        await asyncio.sleep(0.05)
        cron_tools_module.resolve_gateway_run_ack("req-run", "job-1:1710000000")
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        tools.reset_cron_route(token)

    assert result["status"] == "ok"
    assert result["data"]["run_id"] == "job-1:1710000000"
    assert push.payloads[-1]["body"]["action"] == "run_now"


@pytest.mark.asyncio
async def test_cron_tools_run_now_without_request_id_degrades(tmp_path, monkeypatch) -> None:
    """单用户 legacy（无 request_id）不等待 ack，直接返回 submitted。"""
    _setup_project_store(tmp_path, monkeypatch)
    tools, _push = _make_cron_tools(tmp_path, monkeypatch)
    token = tools.push_cron_route(CronToolRoute())
    try:
        result = await tools.run_now("job-1")
    finally:
        tools.reset_cron_route(token)

    assert result["status"] == "submitted"
    assert not (result.get("data") or {}).get("run_id")


@pytest.mark.asyncio
async def test_cron_tools_run_now_ack_timeout_degrades(tmp_path, monkeypatch) -> None:
    """P2：ack 超时降级为 submitted，不阻塞 agent 主流程。"""
    _setup_project_store(tmp_path, monkeypatch)
    tools, _push = _make_cron_tools(tmp_path, monkeypatch)
    monkeypatch.setattr(cron_tools_module, "_RUN_NOW_ACK_TIMEOUT_SEC", 0.1)
    token = tools.push_cron_route(CronToolRoute(request_id="req-timeout", user_id="user-a"))
    try:
        result = await tools.run_now("job-1")
    finally:
        tools.reset_cron_route(token)

    assert result["status"] == "submitted"


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
async def test_cron_backend_uses_bound_context_for_operations_without_context() -> None:
    """list/get/delete/toggle/preview/run_now 无显式 context 时回退 build_tools() 绑定的稳定上下文。"""
    cron_tools = _FakeCronTools()
    backend = _CronToolsCronBackend(cron_tools=cron_tools, message_handler=None)
    context = SimpleNamespace(
        channel_id="web",
        session_id="sess-bound",
        metadata={"request_id": "req-bound"},
        user_id="user-42",
    )
    backend.bind_context(context)

    await backend.list_jobs()
    await backend.get_job("job-1")
    assert await backend.delete_job("job-1") is True
    await backend.toggle_job("job-1", True)
    await backend.preview_job("job-1", 3)
    await backend.run_now("job-1")

    # 每个操作都 push/reset 了 route，且携带绑定上下文的路由键
    assert len(cron_tools.routes) == 6
    for route in cron_tools.routes:
        assert route.request_id == "req-bound"
        assert route.channel_id == "web"
        assert route.session_id == "sess-bound"
        assert route.user_id == "user-42"
    assert len(cron_tools.reset_tokens) == 6


@pytest.mark.asyncio
async def test_cron_backend_explicit_context_wins_over_bound_context() -> None:
    cron_tools = _FakeCronTools()
    backend = _CronToolsCronBackend(cron_tools=cron_tools, message_handler=None)
    backend.bind_context(
        SimpleNamespace(
            channel_id="web",
            session_id="sess-bound",
            metadata={"request_id": "req-bound"},
            user_id="user-42",
        )
    )
    explicit = SimpleNamespace(
        channel_id="tui",
        session_id="sess-explicit",
        metadata={"request_id": "req-explicit"},
    )

    await backend.create_job(
        {
            "id": "job-2",
            "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
            "payload": {"kind": "agentTurn", "message": "hi"},
            "delivery": {"channel": "tui"},
        },
        context=explicit,
    )

    assert len(cron_tools.routes) == 1
    assert cron_tools.routes[0].request_id == "req-explicit"
    assert cron_tools.routes[0].channel_id == "tui"
    assert cron_tools.routes[0].session_id == "sess-explicit"
    assert cron_tools.routes[0].user_id == ""


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
    assert synced["project_dir"] == str(project_dir)
    assert synced["project_id"] == project.project_id


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
    # 本测试断言 route 上下文 model_name 透传，不依赖外部模型配置：patch 掉
    # 严格校验，与 test_cron_tools_update_job_validates_model 保持一致。
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.cron.cron_tools.validate_cron_model",
        lambda raw: str(raw).strip() or None,
    )

    token = tools.push_cron_route(
        CronToolRoute(
            project_dir=str(project_dir),
            project_id=code_project.project_id,
            work_mode="code",
            model_name="deepseek-v4-flash",
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
    assert job["model_name"] == "deepseek-v4-flash"
    synced = push.payloads[-1]["body"]["data"]
    assert synced["project_id"] == code_project.project_id
    assert synced["work_mode"] == "code"
    assert synced["model_name"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_cron_tools_create_job_uses_route_user_id(tmp_path, monkeypatch) -> None:
    """AgentOS 下 agent 创建的 job 继承路由上下文 user_id，web 端按用户隔离时可见。"""
    _setup_project_store(tmp_path, monkeypatch)
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    token = tools.push_cron_route(CronToolRoute(user_id="user-42"))
    try:
        job = await tools.create_job(
            {
                "id": "job-route-user",
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "hello",
                "targets": "web",
            }
        )
    finally:
        tools.reset_cron_route(token)

    assert job["user_id"] == "user-42"
    synced = push.payloads[-1]["body"]["data"]
    assert synced["user_id"] == "user-42"


@pytest.mark.asyncio
async def test_cron_tools_create_job_prefers_explicit_user_id_over_route(tmp_path, monkeypatch) -> None:
    """显式工具参数 user_id 优先于路由上下文（如 web 手动创建链路透传）。"""
    _setup_project_store(tmp_path, monkeypatch)
    tools, push = _make_cron_tools(tmp_path, monkeypatch)

    token = tools.push_cron_route(CronToolRoute(user_id="user-route"))
    try:
        job = await tools.create_job(
            {
                "id": "job-explicit-user",
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "hello",
                "targets": "web",
                "user_id": "user-explicit",
            }
        )
    finally:
        tools.reset_cron_route(token)

    assert job["user_id"] == "user-explicit"
    synced = push.payloads[-1]["body"]["data"]
    assert synced["user_id"] == "user-explicit"


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
async def test_cron_tools_update_job_resolves_project_dir_and_syncs_public_patch(
    tmp_path, monkeypatch
) -> None:
    project_store = _setup_project_store(tmp_path, monkeypatch)
    project_dir = tmp_path / "project-b"
    project_dir.mkdir()
    project = project_store.create_project("P2", str(project_dir))
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    await tools._local_store.create_job(
        job_id="job-1",
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="hello",
        targets="web",
    )

    job = await tools.update_job(
        "job-1",
        {"project_dir": str(project_dir), "project_id": "proj_should_be_ignored"},
    )

    assert job["project_id"] == project.project_id
    synced_patch = push.payloads[-1]["body"]["data"]["patch"]
    # work_mode 改造后:project_dir 已被消费删除,project_id 由 agent 侧解析后
    # 写入 sync_patch 供 gateway 直接持久化(避免 gateway 重复解析)。
    assert "project_dir" not in synced_patch
    assert synced_patch["project_id"] == project.project_id
    assert synced_patch["work_mode"] == project.work_mode
    # Phase 4 单源收敛:update 不本地持久化,返回值 = existing 视图 + patch,
    # 不包含 project_dir 字段且已含解析后的 project_id。
    assert "project_dir" not in job
    assert job["project_id"] == project.project_id


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
    assert synced["project_dir"] == ""


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
    pytest.param("patch_project_dir", id="patch_dir_re_resolves_with_work_mode"),
])
async def test_cron_tools_update_job_injects_work_mode(tmp_path, monkeypatch, scenario):
    project_store = _setup_project_store(tmp_path, monkeypatch)
    pd = tmp_path / "code-proj"
    pd.mkdir()
    project = project_store.create_project("CodeProj", str(pd), work_mode="code")
    tools, push = _make_cron_tools(tmp_path, monkeypatch)
    create_kwargs = {"work_mode": "code"} if scenario == "patch_project_dir" else {}
    await tools._local_store.create_job(
        job_id="job-update", name="daily", cron_expr="0 9 * * *",
        timezone="Asia/Shanghai", description="hello", targets="web", **create_kwargs,
    )
    patch = (
        {"project_dir": str(pd)} if scenario == "patch_project_dir"
        else {"project_id": project.project_id}
    )
    job = await tools.update_job("job-update", patch)
    assert job["project_id"] == project.project_id
    assert job["work_mode"] == "code"
    synced_patch = push.payloads[-1]["body"]["data"]["patch"]
    assert synced_patch["project_id"] == project.project_id
    assert synced_patch["work_mode"] == "code"
    if scenario == "patch_project_dir":
        assert "project_dir" not in synced_patch


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

        assert out["cron_expr"] == "31 25 18 24 7 ? 2026"
        assert out["timezone"] == "Asia/Shanghai"
        assert out["description"] == "喝水提醒"
        assert "wake_offset_seconds" not in out

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
        assert out["cron_expr"] == "0 0 9 1 1 ? 2026"

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

    def test_missed_trigger_within_window_schedules_immediate(self) -> None:
        svc = _TestableScheduler.__new__(_TestableScheduler)

        job = _make_job(
            job_id="one-shot",
            cron_expr="31 25 18 24 7 ? 2026",
            timezone="Asia/Shanghai",
            wake_offset_seconds=0,
        )

        trigger_ts = datetime(2026, 7, 24, 18, 25, 31, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        now_ts = trigger_ts + 2.0

        push_dt, wake_dt, run_id = svc.compute_next_run(job, now_ts=now_ts)

        assert push_dt == datetime(2026, 7, 24, 18, 25, 31, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert wake_dt == push_dt
        assert run_id.startswith("one-shot:")

    def test_missed_trigger_beyond_window_raises(self) -> None:
        svc = _TestableScheduler.__new__(_TestableScheduler)

        job = _make_job(
            job_id="old-shot",
            cron_expr="0 0 9 1 1 ? 2025",
            timezone="Asia/Shanghai",
            wake_offset_seconds=0,
        )

        now_ts = datetime(2026, 7, 24, 18, 25, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

        with pytest.raises(Exception, match="failed to find next date"):
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
