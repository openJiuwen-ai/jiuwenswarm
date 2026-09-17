from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import CronToolRoute, CronTools
from jiuwenswarm.gateway.cron.store import CronJobStore
from jiuwenswarm.gateway.cron.tenant_registry import CronTenantRegistry


class _FakeGatewayPush:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def send_push(self, payload: dict) -> None:
        self.payloads.append(payload)


def _make_tools(tmp_path, monkeypatch, *, enterprise: bool) -> tuple[CronTools, _FakeGatewayPush]:
    push = _FakeGatewayPush()
    tools = CronTools(gateway_push=push, agent_client=object(), message_handler=object())
    tools._local_store = CronJobStore(path=tmp_path / "cron_jobs.json")

    async def _noop_reload() -> None:
        return None

    monkeypatch.setattr(tools, "_reload_scheduler", _noop_reload)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.enterprise_gate.enterprise_cron_enabled",
        lambda **_kwargs: enterprise,
    )
    monkeypatch.setattr(tools, "_enterprise_ready", lambda: enterprise)
    monkeypatch.setattr(
        CronTenantRegistry,
        "try_get_instance",
        classmethod(lambda cls: None),
    )
    return tools, push


_ENTERPRISE_JOB = {
    "id": "job-1",
    "name": "drink",
    "cron_expr": "0 9 * * *",
    "timezone": "Asia/Shanghai",
    "description": "喝水",
    "enabled": True,
    "work_mode": "work",
    "mode": "agent",
}


@pytest.mark.asyncio
async def test_personal_list_update_delete_preview_still_use_local_store(
    tmp_path, monkeypatch
) -> None:
    tools, push = _make_tools(tmp_path, monkeypatch, enterprise=False)
    await tools._local_store.create_job(
        job_id="job-1",
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="hello",
        targets="web",
    )

    listed = await tools.list_jobs()
    assert listed[0]["id"] == "job-1"

    updated = await tools.update_job("job-1", {"name": "renamed"})
    assert updated["name"] == "renamed"
    local = await tools._local_store.get_job("job-1")
    assert local is not None and local.name == "renamed"

    preview = await tools.preview_job("job-1", 2)
    assert len(preview) >= 1
    assert "push_at" in preview[0]

    assert await tools.delete_job("job-1") is True
    assert await tools._local_store.get_job("job-1") is None
    assert push.payloads  # personal still best-effort syncs to gateway


@pytest.mark.asyncio
async def test_enterprise_list_requires_route_identity(tmp_path, monkeypatch) -> None:
    tools, _push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    with pytest.raises(ValueError, match="requires group_id, bot_id and user_id"):
        await tools.list_jobs()


@pytest.mark.asyncio
async def test_enterprise_list_uses_bound_route_filters(tmp_path, monkeypatch) -> None:
    tools, _push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    captured: dict = {}

    async def _list_records(table, filters=None, order_by=None):
        captured["table"] = table
        captured["filters"] = filters
        return []

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.enterprise_config.db_queries.list_records",
        _list_records,
    )
    token = tools.push_cron_route(
        CronToolRoute(group_id="g1", bot_id="b1", user_id="u1", session_id="sess-1")
    )
    try:
        assert await tools.list_jobs() == []
    finally:
        tools.reset_cron_route(token)
    assert captured["table"] == "cron_job"
    assert captured["filters"] == {"group_id": "g1", "bot_id": "b1", "user_id": "u1"}


@pytest.mark.asyncio
async def test_enterprise_update_does_not_require_local_json(tmp_path, monkeypatch) -> None:
    tools, push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    monkeypatch.setattr(
        tools,
        "_get_job_enterprise",
        AsyncMock(
            side_effect=[
                dict(_ENTERPRISE_JOB),
                {**_ENTERPRISE_JOB, "name": "renamed"},
            ]
        ),
    )
    token = tools.push_cron_route(CronToolRoute(group_id="g1", bot_id="b1", user_id="u1"))
    try:
        result = await tools.update_job("job-1", {"name": "renamed"})
    finally:
        tools.reset_cron_route(token)

    assert result["name"] == "renamed"
    assert await tools._local_store.get_job("job-1") is None
    body = push.payloads[-1]["body"]["data"]
    assert body["job_id"] == "job-1"
    assert body["patch"]["name"] == "renamed"
    assert body["group_id"] == "g1"
    assert body["user_id"] == "u1"


@pytest.mark.asyncio
async def test_enterprise_delete_returns_true_only_after_job_absent(
    tmp_path, monkeypatch
) -> None:
    tools, push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    present = {"value": True}

    async def _get(job_id: str):
        _ = job_id
        return dict(_ENTERPRISE_JOB) if present["value"] else None

    async def _send_push(payload: dict) -> None:
        push.payloads.append(payload)
        present["value"] = False

    tools._gateway_push.send_push = _send_push  # type: ignore[method-assign]
    monkeypatch.setattr(tools, "_get_job_enterprise", _get)
    token = tools.push_cron_route(CronToolRoute(group_id="g1", bot_id="b1", user_id="u1"))
    try:
        assert await tools.delete_job("job-1") is True
    finally:
        tools.reset_cron_route(token)
    assert push.payloads[-1]["body"]["data"]["job_id"] == "job-1"
    assert push.payloads[-1]["body"]["data"]["group_id"] == "g1"


@pytest.mark.asyncio
async def test_enterprise_delete_does_not_lie_when_job_still_present(
    tmp_path, monkeypatch
) -> None:
    tools, _push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.cron.cron_tools._ENTERPRISE_MUTATION_CONFIRM_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.cron.cron_tools._ENTERPRISE_MUTATION_POLL_INTERVAL",
        0.01,
    )
    monkeypatch.setattr(tools, "_get_job_enterprise", AsyncMock(return_value=dict(_ENTERPRISE_JOB)))
    token = tools.push_cron_route(CronToolRoute(group_id="g1", bot_id="b1", user_id="u1"))
    try:
        with pytest.raises(RuntimeError, match="delete was not confirmed"):
            await tools.delete_job("job-1")
    finally:
        tools.reset_cron_route(token)


@pytest.mark.asyncio
async def test_enterprise_delete_missing_job_returns_false(tmp_path, monkeypatch) -> None:
    tools, push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    monkeypatch.setattr(tools, "_get_job_enterprise", AsyncMock(return_value=None))
    token = tools.push_cron_route(CronToolRoute(group_id="g1", bot_id="b1", user_id="u1"))
    try:
        assert await tools.delete_job("missing") is False
    finally:
        tools.reset_cron_route(token)
    assert push.payloads == []


@pytest.mark.asyncio
async def test_enterprise_delete_inprocess_registry_returns_controller_result(
    tmp_path, monkeypatch
) -> None:
    tools, push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    monkeypatch.setattr(tools, "_get_job_enterprise", AsyncMock(return_value=dict(_ENTERPRISE_JOB)))

    class _Registry:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def handle_push_action(self, **kwargs):
            self.calls.append(kwargs)
            return {"deleted": True}

    registry = _Registry()
    monkeypatch.setattr(
        CronTenantRegistry, "try_get_instance", classmethod(lambda cls: registry)
    )
    token = tools.push_cron_route(CronToolRoute(group_id="g1", bot_id="b1", user_id="u1"))
    try:
        assert await tools.delete_job("job-1") is True
    finally:
        tools.reset_cron_route(token)
    assert push.payloads == []
    assert registry.calls[0]["action"] == "delete"
    assert registry.calls[0]["params"]["group_id"] == "g1"


@pytest.mark.asyncio
async def test_enterprise_delete_rejects_non_bool_controller_result(
    tmp_path, monkeypatch
) -> None:
    tools, push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    monkeypatch.setattr(tools, "_get_job_enterprise", AsyncMock(return_value=dict(_ENTERPRISE_JOB)))

    class _Registry:
        async def handle_push_action(self, **kwargs):
            _ = kwargs
            return {"deleted": "false"}

    monkeypatch.setattr(
        CronTenantRegistry, "try_get_instance", classmethod(lambda cls: _Registry())
    )
    token = tools.push_cron_route(CronToolRoute(group_id="g1", bot_id="b1", user_id="u1"))
    try:
        with pytest.raises(RuntimeError, match="invalid cron delete response"):
            await tools.delete_job("job-1")
    finally:
        tools.reset_cron_route(token)
    assert push.payloads == []


@pytest.mark.asyncio
async def test_enterprise_update_ignores_identity_in_patch(tmp_path, monkeypatch) -> None:
    tools, push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    monkeypatch.setattr(
        tools,
        "_get_job_enterprise",
        AsyncMock(
            side_effect=[
                dict(_ENTERPRISE_JOB),
                {**_ENTERPRISE_JOB, "name": "renamed"},
            ]
        ),
    )
    token = tools.push_cron_route(CronToolRoute(group_id="g1", bot_id="b1", user_id="u1"))
    try:
        await tools.update_job(
            "job-1",
            {"name": "renamed", "group_id": "attacker", "user_id": "other"},
        )
    finally:
        tools.reset_cron_route(token)
    body = push.payloads[-1]["body"]["data"]
    assert body["group_id"] == "g1"
    assert body["user_id"] == "u1"
    assert "group_id" not in body["patch"]
    assert "user_id" not in body["patch"]


@pytest.mark.asyncio
async def test_enterprise_preview_uses_job_store_not_local_json(tmp_path, monkeypatch) -> None:
    tools, _push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    monkeypatch.setattr(tools, "_get_job_enterprise", AsyncMock(return_value=dict(_ENTERPRISE_JOB)))
    token = tools.push_cron_route(CronToolRoute(group_id="g1", bot_id="b1", user_id="u1"))
    try:
        rows = await tools.preview_job("job-1", 2)
    finally:
        tools.reset_cron_route(token)
    assert len(rows) >= 1
    assert "push_at" in rows[0]
    assert await tools._local_store.get_job("job-1") is None


@pytest.mark.asyncio
async def test_enterprise_toggle_forwards_identity_and_confirms(tmp_path, monkeypatch) -> None:
    tools, push = _make_tools(tmp_path, monkeypatch, enterprise=True)
    monkeypatch.setattr(
        tools,
        "_get_job_enterprise",
        AsyncMock(
            side_effect=[
                dict(_ENTERPRISE_JOB),
                {**_ENTERPRISE_JOB, "enabled": False},
            ]
        ),
    )
    token = tools.push_cron_route(CronToolRoute(group_id="g1", bot_id="b1", user_id="u1"))
    try:
        result = await tools.toggle_job("job-1", False)
    finally:
        tools.reset_cron_route(token)
    assert result["enabled"] is False
    assert push.payloads[-1]["body"]["data"]["enabled"] is False
    assert push.payloads[-1]["body"]["data"]["bot_id"] == "b1"
