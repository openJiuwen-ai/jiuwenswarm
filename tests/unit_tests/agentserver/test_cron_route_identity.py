# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron tool route identity.

Regression coverage for the AgentOS failure where cron tools executed in a
task/thread that never inherited the per-turn contextvars: the tool route had
an empty ``request_id``/``session_id``/``user_id``, so jobs were persisted
without an owner and the Gateway command ACK could not be routed back.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tools.cron import cron_tools as cron_tools_module
from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    _CronToolsCronBackend,
)
from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import (
    CronToolRoute,
    CronTools,
)
from jiuwenswarm.runtime.cron.store import CronJobStore
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _CRON_TOOL_BOUND,
    _CRON_TOOL_SESSION_ID,
    _CRON_TOOL_USER_ID,
    _RuntimeCronToolContext,
    _resolve_cron_session_identity,
)

SESSION_ID = "web_1a19f5594fb_c34d9fdc65eb"
USER_ID = "lfq"


def _route(context: object):
    return _CronToolsCronBackend._route_from_context(context)


class _AckGatewayPush:
    """Gateway push stub that immediately resolves the command ACK."""

    supports_cron_command_ack = True

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def send_push(self, payload: dict) -> bool:
        self.payloads.append(payload)
        cron_tools_module.resolve_gateway_cron_command_ack(
            payload["body"]["command_id"], {"data": {"id": "job-1"}}
        )
        return True


def _make_tools(tmp_path, push) -> CronTools:
    tools = CronTools(gateway_push=push, agent_client=object(), message_handler=object())
    tools._local_store = CronJobStore(path=tmp_path / "cron_jobs.json")
    return tools


def _setup_project_store(tmp_path, monkeypatch) -> None:
    root = tmp_path / "agent"
    root.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_agent_root_dir",
        lambda: root,
    )
    from jiuwenswarm.server.runtime.session import project_store

    project_store.invalidate_cache()


def test_bind_session_populates_route() -> None:
    ctx = _RuntimeCronToolContext(tool_scope="t")
    ctx.bind_session(session_id=SESSION_ID, user_id=USER_ID)

    route = _route(ctx)

    assert (route.user_id, route.session_id) == (USER_ID, SESSION_ID)
    assert route.channel_id == "web"


def test_fallback_when_bound_but_contextvars_empty() -> None:
    """The observed production state: BOUND=True while the vars are empty."""
    ctx = _RuntimeCronToolContext(tool_scope="t")
    ctx.bind_session(session_id=SESSION_ID, user_id=USER_ID)
    token_bound = _CRON_TOOL_BOUND.set(True)
    token_session = _CRON_TOOL_SESSION_ID.set(None)
    token_user = _CRON_TOOL_USER_ID.set(None)
    try:
        route = _route(ctx)
        assert (route.user_id, route.session_id) == (USER_ID, SESSION_ID)
    finally:
        _CRON_TOOL_BOUND.reset(token_bound)
        _CRON_TOOL_SESSION_ID.reset(token_session)
        _CRON_TOOL_USER_ID.reset(token_user)


def test_remember_current_binding_does_not_clobber_pinned_identity() -> None:
    ctx = _RuntimeCronToolContext(tool_scope="t")
    ctx.bind_session(session_id=SESSION_ID, user_id=USER_ID)

    ctx.remember_current_binding()

    assert (ctx.user_id, ctx.session_id) == (USER_ID, SESSION_ID)


def test_thread_execution_uses_pinned_identity() -> None:
    ctx = _RuntimeCronToolContext(tool_scope="t")
    ctx.bind_session(session_id=SESSION_ID, user_id=USER_ID)
    seen: dict[str, str | None] = {}

    def worker() -> None:
        seen["user_id"] = ctx.user_id
        seen["session_id"] = ctx.session_id

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen == {"user_id": USER_ID, "session_id": SESSION_ID}


def test_bind_session_keeps_existing_owner_on_mismatch() -> None:
    ctx = _RuntimeCronToolContext(tool_scope="t")
    ctx.bind_session(session_id=SESSION_ID, user_id=USER_ID)

    ctx.bind_session(session_id=SESSION_ID, user_id="someone-else")

    assert ctx.user_id == USER_ID


def test_unbound_context_has_no_owner() -> None:
    assert _route(_RuntimeCronToolContext(tool_scope="t")).user_id == ""


def test_resolve_identity_prefers_request() -> None:
    sid, uid = _resolve_cron_session_identity(
        SimpleNamespace(user_id="alice"), "sess-1"
    )

    assert (sid, uid) == ("sess-1", "alice")


def test_resolve_identity_falls_back_to_session_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda sid, **kwargs: {"user_id": "bob"},
    )

    assert _resolve_cron_session_identity(None, "sess-2") == ("sess-2", "bob")


def test_resolve_identity_reads_channel_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda sid, **kwargs: {"channel_metadata": {"user_id": "carol"}},
    )

    assert _resolve_cron_session_identity(None, "sess-3")[1] == "carol"


@pytest.mark.asyncio
async def test_create_push_carries_owner(tmp_path) -> None:
    push = _AckGatewayPush()
    tools = _make_tools(tmp_path, push)
    token = tools.push_cron_route(
        CronToolRoute(
            request_id="req-1",
            channel_id="web",
            session_id=SESSION_ID,
            user_id=USER_ID,
        )
    )
    try:
        await tools._send("create", {"name": "reminder"})
    finally:
        tools.reset_cron_route(token)

    payload = push.payloads[0]
    assert payload["metadata"]["_jiuwenswarm_cron_owner_user_id"] == USER_ID
    assert payload["session_id"] == SESSION_ID
    assert payload["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_create_job_inherits_route_user(tmp_path, monkeypatch) -> None:
    _setup_project_store(tmp_path, monkeypatch)
    push = _AckGatewayPush()
    tools = _make_tools(tmp_path, push)
    token = tools.push_cron_route(CronToolRoute(project_dir="", user_id=USER_ID))
    try:
        job = await tools.create_job(
            {
                "name": "daily",
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "description": "reminder",
                "targets": "web",
            }
        )
    finally:
        tools.reset_cron_route(token)

    assert job["user_id"] == USER_ID
    assert push.payloads[0]["body"]["data"]["user_id"] == USER_ID


@pytest.mark.asyncio
async def test_create_job_refuses_ownerless_in_agentos(tmp_path, monkeypatch) -> None:
    _setup_project_store(tmp_path, monkeypatch)
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    tools = _make_tools(tmp_path, _AckGatewayPush())
    token = tools.push_cron_route(CronToolRoute(project_dir=""))
    try:
        with pytest.raises(RuntimeError, match="owner identity is missing"):
            await tools.create_job(
                {
                    "name": "daily",
                    "cron_expr": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                    "description": "reminder",
                    "targets": "web",
                }
            )
    finally:
        tools.reset_cron_route(token)
