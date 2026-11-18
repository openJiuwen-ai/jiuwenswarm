# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""session.prepare: acceptance statuses, routing parity with chat.send, no disk writes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.server.agent_http_routes import ROUTES
from jiuwenswarm.server.dispatch import HANDLERS
from jiuwenswarm.server.handlers import _default, _shared, session_prepare
from jiuwenswarm.server.runtime import session_prepare_tracker
from jiuwenswarm.server.runtime.agent_adapter import (
    interface_deep as interface_deep_module,
)
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

SESSION_ID = "officeclaw_prepare_session"


@pytest.fixture
def flash_config(monkeypatch):
    config = {"flash": {"enabled": False}}
    monkeypatch.setattr(_shared, "get_config", lambda: config)
    return config


@pytest.fixture
def sessions_dir(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(_default, "_sessions_dir_for_request", lambda *_a, **_k: root)
    monkeypatch.setattr(_shared, "_sessions_dir_for_request", lambda *_a, **_k: root)
    return root


@pytest.fixture(autouse=True)
def prepare_env(monkeypatch, flash_config, sessions_dir):
    monkeypatch.delenv("JIUWENSWARM_SESSION_PREPARE", raising=False)
    monkeypatch.setattr(
        TenantAgentPool, "require_officeclaw_agent", staticmethod(lambda _request: None)
    )
    monkeypatch.setattr(
        session_prepare, "encode_agent_response_for_wire", lambda resp, response_id=None: resp
    )
    monkeypatch.setattr(agent_ws_server, "ensure_interface_deep_and_checkpointer", AsyncMock())
    monkeypatch.setattr(TenantAgentPool, "_session_prepare_queue", {})
    monkeypatch.setattr(TenantAgentPool, "_session_prepare_worker", None)
    session_prepare_tracker.clear()


def make_request(params=None, *, method=ReqMethod.SESSION_PREPARE, **overrides):
    fields = {
        "request_id": "prep-1",
        "channel_id": "officeclaw",
        "session_id": SESSION_ID,
        "agent_id": "assistant",
        "chat_id": "officeclaw_chat_1",
        "thread_id": "thread-1",
        "metadata": {"user_id": "officeclaw_user_1"},
        "req_method": method,
        "params": dict(params if params is not None else {"mode": "agent.plan"}),
    }
    fields.update(overrides)
    return AgentRequest(**fields)


def make_pool(*, owner=None, get_agent=None):
    pool = object.__new__(TenantAgentPool)
    agent = SimpleNamespace(prepare_session=AsyncMock())
    manager = SimpleNamespace(get_agent=get_agent or AsyncMock(return_value=agent))
    pool.get_agent_manager = AsyncMock(return_value=manager)
    pool.find_agent_for_session_nowait = Mock(return_value=owner)
    return pool, manager, agent


async def call_prepare(request, pool) -> AgentResponse:
    sent = []
    sink = SimpleNamespace(send_wire=AsyncMock(side_effect=sent.append))
    ctx = SimpleNamespace(request=request, sink=sink, services=SimpleNamespace(tenant_pool=lambda: pool))
    await session_prepare.handle_session_prepare(ctx)
    assert len(sent) == 1
    return sent[0]


async def drain_prepare():
    worker = TenantAgentPool._session_prepare_worker
    if worker is not None:
        await worker


def test_method_is_registered_for_ws_and_http():
    assert ReqMethod.SESSION_PREPARE.value == "session.prepare"
    assert HANDLERS[ReqMethod.SESSION_PREPARE].resolve_fn(False) is session_prepare.handle_session_prepare
    assert any(
        r.method == "session.prepare" and r.path == "/sessions/{session_id}/actions/prepare"
        for r in ROUTES
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_id": None},
        {"session_id": "  "},
        {"agent_id": None},
        {"params": {}},
        {"params": {"mode": 1}},
        {"channel_id": "web"},
        {"metadata": "user"},
    ],
)
async def test_invalid_params(overrides):
    pool, _, _ = make_pool()
    resp = await call_prepare(make_request(**overrides), pool)
    assert resp.ok is False
    assert resp.payload["code"] == "INVALID_PARAMS"
    pool.get_agent_manager.assert_not_awaited()


async def test_invalid_tenant_id_is_an_error(monkeypatch):
    guard = AgentResponse(
        request_id="prep-1", channel_id="officeclaw", ok=False,
        payload={"error": "bad id", "code": "invalid_tenant_id"},
    )
    monkeypatch.setattr(TenantAgentPool, "require_officeclaw_agent", staticmethod(lambda _r: guard))
    pool, _, _ = make_pool()
    resp = await call_prepare(make_request(), pool)
    assert (resp.ok, resp.payload["code"]) == (False, "invalid_tenant_id")


async def test_unknown_agent_is_not_accepted(monkeypatch):
    guard = AgentResponse(
        request_id="prep-1", channel_id="officeclaw", ok=False,
        payload={"error": "tenant not registered", "code": "tenant_not_registered"},
    )
    monkeypatch.setattr(TenantAgentPool, "require_officeclaw_agent", staticmethod(lambda _r: guard))
    pool, _, _ = make_pool()
    resp = await call_prepare(make_request(), pool)
    assert resp.ok is True
    assert resp.payload == {"session_id": SESSION_ID, "accepted": False, "status": "unknown_agent"}


async def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_SESSION_PREPARE", "0")
    pool, _, _ = make_pool()
    resp = await call_prepare(make_request(), pool)
    assert resp.payload["status"] == "disabled"
    assert SESSION_ID not in TenantAgentPool._session_prewarm_tasks


@pytest.mark.parametrize(
    "params",
    [
        {"mode": "code.normal"},
        {"mode": "code.plan"},
        {"mode": "team"},
        {"mode": "agent", "work_mode": "code"},
        {"mode": "flash.unknown"},
    ],
)
async def test_mode_not_supported(params):
    pool, _, _ = make_pool()
    resp = await call_prepare(make_request(params), pool)
    assert resp.payload["accepted"] is False
    assert resp.payload["status"] == "mode_not_supported"
    pool.get_agent_manager.assert_not_awaited()


async def test_already_ready_when_session_runtime_exists():
    pool, _, _ = make_pool(owner=object())
    resp = await call_prepare(make_request(), pool)
    assert resp.payload == {"session_id": SESSION_ID, "accepted": False, "status": "already_ready"}
    pool.get_agent_manager.assert_not_awaited()


@pytest.mark.parametrize(
    ("params", "manager_mode", "canonical_mode"),
    [
        ({"mode": "agent.plan"}, "agent", "agent"),
        ({"mode": "flash"}, "flash", "flash"),
        ({"mode": "agent.plan", "project_dir": " D:/work/thread-1 "}, "agent", "agent"),
    ],
)
async def test_scheduled_builds_with_request_identity(params, manager_mode, canonical_mode, sessions_dir):
    pool, manager, agent = make_pool()
    request = make_request(params)

    resp = await call_prepare(request, pool)
    await drain_prepare()

    assert resp.payload == {"session_id": SESSION_ID, "accepted": True, "status": "scheduled"}
    project_dir = params.get("project_dir", "").strip() or None
    manager.get_agent.assert_awaited_once_with(
        channel_id="officeclaw", mode=manager_mode, project_dir=project_dir, sub_mode=None, request=request,
        session_id=SESSION_ID,
    )
    agent.prepare_session.assert_awaited_once_with(
        session_id=SESSION_ID,
        channel_id="officeclaw",
        mode=canonical_mode,
        adapter_mode=manager_mode,
        project_dir=project_dir,
        request=request,
    )
    # Read-only resolution: no metadata.json or session directory is created.
    assert list(sessions_dir.rglob("*")) == []


@pytest.mark.parametrize(
    "params",
    [
        {"mode": "agent.plan"},
        {"mode": "flash"},
        {"mode": "agent.plan", "project_dir": "D:/work/thread-2"},
    ],
)
@pytest.mark.parametrize("flash_enabled", [False, True])
async def test_prepare_selects_same_facade_as_chat(params, flash_enabled, flash_config):
    flash_config["flash"]["enabled"] = flash_enabled
    pool, prep_manager, _ = make_pool()
    prepare = make_request(params)
    await call_prepare(prepare, pool)
    await drain_prepare()

    chat_manager = SimpleNamespace(wait_for_session_prewarm=AsyncMock(), get_agent=AsyncMock(return_value=object()))
    chat = make_request({**params, "query": "hi"}, method=ReqMethod.CHAT_SEND, request_id="chat-1")
    ctx = SimpleNamespace(services=SimpleNamespace(agent_manager=chat_manager))
    await _default._prepare_code_mode_chat_turn(ctx, chat, "officeclaw", agent_manager=chat_manager)

    prep_kwargs = dict(prep_manager.get_agent.await_args.kwargs)
    prep_kwargs.pop("request")
    assert prep_kwargs == chat_manager.get_agent.await_args.kwargs
    assert prepare.params["mode"] == chat.params["mode"]
    assert prepare.resolved_adapter_mode == chat.resolved_adapter_mode


async def test_concurrent_prepare_is_already_running_and_chat_joins():
    entered, release = asyncio.Event(), asyncio.Event()
    agent = SimpleNamespace(prepare_session=AsyncMock())

    async def slow_get_agent(**_kwargs):
        entered.set()
        await release.wait()
        return agent

    pool, _, _ = make_pool(get_agent=slow_get_agent)
    first = await call_prepare(make_request(), pool)
    await entered.wait()
    second = await call_prepare(make_request(request_id="prep-2"), pool)

    assert first.payload["status"] == "scheduled"
    assert second.payload == {"session_id": SESSION_ID, "accepted": True, "status": "already_running"}

    chat_wait = asyncio.create_task(pool.wait_for_session_prewarm(SESSION_ID))
    await asyncio.sleep(0)
    assert not chat_wait.done()
    release.set()
    await chat_wait
    agent.prepare_session.assert_awaited_once()


async def test_build_failure_is_contained():
    pool, _, agent = make_pool()
    agent.prepare_session.side_effect = RuntimeError("boom")
    resp = await call_prepare(make_request(), pool)
    await drain_prepare()
    assert resp.payload["status"] == "scheduled"
    assert SESSION_ID not in TenantAgentPool._session_prewarm_tasks


async def test_facade_prepare_binds_tenant_context_only_with_request():
    facade = object.__new__(JiuWenSwarm)
    adapter = SimpleNamespace(prepare_session=AsyncMock())
    facade._ensure_adapter = Mock(return_value=adapter)
    calls = []
    facade._bind_tenant_request_context = lambda req: calls.append(("bind", req)) or ("t", "m")
    facade._reset_tenant_request_context = lambda t, m: calls.append(("reset", t, m))
    request = make_request()

    await facade.prepare_session(session_id=SESSION_ID, channel_id="officeclaw", mode="agent", adapter_mode="flash")
    assert calls == []
    assert "request" not in adapter.prepare_session.await_args.kwargs

    await facade.prepare_session(
        session_id=SESSION_ID, channel_id="officeclaw", mode="agent", adapter_mode="flash", request=request,
    )
    assert calls == [("bind", request), ("reset", "t", "m")]
    assert adapter.prepare_session.await_args.kwargs["request"] is request
    facade._ensure_adapter.assert_called_with(mode="flash")


async def test_deep_prepare_passes_request_to_session_adapter():
    deep = make_deep()
    child = SimpleNamespace(configure_session_runtime=AsyncMock())
    deep._get_or_create_session_adapter = AsyncMock(return_value=child)
    request = make_request()

    await prepare_on(deep, request)

    deep._get_or_create_session_adapter.assert_awaited_once_with(SESSION_ID, request=request)
    child.configure_session_runtime.assert_awaited_once()
    deep._forget_prepared_session(SESSION_ID)


async def test_queue_builds_one_at_a_time_and_caps_waiting():
    started, release = [], asyncio.Event()
    agent = SimpleNamespace(prepare_session=AsyncMock())

    async def slow_get_agent(**_kwargs):
        started.append(1)
        await release.wait()
        return agent

    pool, _, _ = make_pool(get_agent=slow_get_agent)
    max_queued = TenantAgentPool.SESSION_PREPARE_MAX_QUEUED
    statuses = []
    for i in range(max_queued + 2):
        resp = await call_prepare(make_request(session_id=f"sess-{i}", request_id=f"p-{i}"), pool)
        statuses.append((resp.payload["accepted"], resp.payload["status"]))
        await asyncio.sleep(0)

    # sess-0 is building, sess-1..8 wait, sess-9 is dropped.
    assert statuses[: max_queued + 1] == [(True, "scheduled")] * (max_queued + 1)
    assert statuses[-1] == (False, "queue_full")
    assert len(started) == 1
    assert (await call_prepare(make_request(session_id="sess-3"), pool)).payload["status"] == "already_running"

    release.set()
    await drain_prepare()
    assert len(started) == max_queued + 1
    assert agent.prepare_session.await_count == max_queued + 1


async def test_stuck_build_does_not_block_the_queue(monkeypatch):
    monkeypatch.setattr(TenantAgentPool, "SESSION_PREPARE_BUILD_WAIT_SEC", 0.05)
    stuck, built = asyncio.Event(), []
    agent = SimpleNamespace(prepare_session=AsyncMock())

    async def get_agent(**kwargs):
        if kwargs["session_id"] == "sess-stuck":
            await stuck.wait()
        built.append(kwargs["session_id"])
        return agent

    pool, _, _ = make_pool(get_agent=get_agent)
    await call_prepare(make_request(session_id="sess-stuck"), pool)
    await call_prepare(make_request(session_id="sess-next", request_id="p-2"), pool)
    await asyncio.wait_for(drain_prepare(), timeout=2)

    assert built == ["sess-next"]
    stuck_task = TenantAgentPool._session_prewarm_tasks["sess-stuck"]
    assert not stuck_task.done()
    stuck.set()
    await stuck_task
    assert built == ["sess-next", "sess-stuck"]


async def test_chat_drops_queued_prepare_and_builds_itself():
    release = asyncio.Event()
    built = []

    async def get_agent(**_kwargs):
        await release.wait()
        return SimpleNamespace(prepare_session=AsyncMock(side_effect=lambda **kw: built.append(kw["session_id"])))

    pool, _, _ = make_pool(get_agent=get_agent)
    await call_prepare(make_request(session_id="busy"), pool)
    await asyncio.sleep(0)
    await call_prepare(make_request(session_id="queued"), pool)
    assert "queued" in TenantAgentPool._session_prepare_queue

    # The chat for "queued" must not wait behind "busy".
    await asyncio.wait_for(pool.wait_for_session_prewarm("queued"), timeout=1)
    assert "queued" not in TenantAgentPool._session_prepare_queue

    release.set()
    await drain_prepare()
    assert built == ["busy"]


def make_deep(ttl=600.0, retry=600.0):
    deep = object.__new__(JiuWenSwarmDeepAdapter)
    deep.PREPARED_SESSION_RECLAIM_RETRY_SEC = retry
    deep._is_session_scoped_adapter = False
    deep._session_adapters = {}
    deep._session_adapter_locks = {}
    deep._session_adapter_last_used = {}
    deep._session_adapter_versions = {}
    deep._session_adapter_reload_failures = {}
    deep._session_adapter_config_version = 0
    deep._sessions_awaiting_tool_permission = set()
    deep.PREPARED_SESSION_UNUSED_TTL_SEC = ttl
    child = SimpleNamespace(configure_session_runtime=AsyncMock())

    async def create(session_id, **_kwargs):
        deep._session_adapters.setdefault(session_id, child)
        return child

    deep._get_or_create_session_adapter = create
    deep.cleanup_session_adapter = AsyncMock(return_value=True)
    return deep


async def prepare_on(deep, request=None):
    if request is not None:
        session_prepare_tracker.mark(SESSION_ID, "building")  # what the pool task does first
    await deep.prepare_session(
        session_id=SESSION_ID, channel_id="officeclaw", mode="agent", request=request,
    )


async def wait_prepared_tasks(deep):
    await asyncio.sleep(0.05)
    tasks = list(deep._prepared_session_tasks())
    if tasks:
        await asyncio.gather(*tasks)


async def test_unused_prepared_session_is_reclaimed():
    deep = make_deep(ttl=0.01)
    await prepare_on(deep, make_request())
    assert deep.is_prepared_session_unused(SESSION_ID)
    await wait_prepared_tasks(deep)
    deep.cleanup_session_adapter.assert_awaited_once_with(SESSION_ID)
    assert not deep.is_prepared_session_unused(SESSION_ID)


async def test_session_create_prewarm_is_not_tracked():
    deep = make_deep(ttl=0.01)
    await prepare_on(deep)
    assert not deep.is_prepared_session_unused(SESSION_ID)
    await wait_prepared_tasks(deep)
    deep.cleanup_session_adapter.assert_not_awaited()


async def test_existing_session_is_not_tracked_by_prepare():
    deep = make_deep(ttl=0.01)
    deep._session_adapters[SESSION_ID] = object()
    await prepare_on(deep, make_request())
    assert not deep.is_prepared_session_unused(SESSION_ID)


async def test_chat_use_cancels_reclaim():
    deep = make_deep(ttl=0.05)
    await prepare_on(deep, make_request())
    del deep._get_or_create_session_adapter  # chat goes through the real method
    deep._reload_session_adapter_if_stale = AsyncMock()
    deep._refresh_session_skills_if_stale = AsyncMock()

    await deep._get_or_create_session_adapter(SESSION_ID, request=make_request(method=ReqMethod.CHAT_SEND))
    assert not deep.is_prepared_session_unused(SESSION_ID)
    await asyncio.sleep(0.1)
    deep.cleanup_session_adapter.assert_not_awaited()


@pytest.mark.parametrize("guard", ["awaiting_permission", "lock_held"])
async def test_reclaim_respects_idle_eviction_guards(guard):
    deep = make_deep(ttl=0.01)
    await prepare_on(deep, make_request())
    lock = asyncio.Lock()
    if guard == "awaiting_permission":
        deep._sessions_awaiting_tool_permission.add(SESSION_ID)
    else:
        deep._session_adapter_locks[SESSION_ID] = lock
        await lock.acquire()
    await wait_prepared_tasks(deep)
    deep.cleanup_session_adapter.assert_not_awaited()
    if lock.locked():
        lock.release()


async def test_config_sync_reloads_unused_prepared_session_under_session_lock():
    deep = make_deep()
    await prepare_on(deep, make_request())
    seen_locked = []

    async def reload(sid, _adapter):
        seen_locked.append(deep._session_adapter_locks[sid].locked())
        deep._session_adapter_reload_failures[sid] = (1, 0.0)

    deep._reload_session_adapter_if_stale = reload
    deep._mark_session_adapters_stale_for_reload({"react": {}}, None)
    await wait_prepared_tasks(deep)

    assert seen_locked == [True]
    # A background failure must not suppress the chat's own lazy retry.
    assert SESSION_ID not in deep._session_adapter_reload_failures
    deep._forget_prepared_session(SESSION_ID)


async def test_config_sync_skips_sessions_already_used_by_chat():
    deep = make_deep()
    await prepare_on(deep, make_request())
    deep._forget_prepared_session(SESSION_ID)
    deep._reload_session_adapter_if_stale = AsyncMock()
    deep._mark_session_adapters_stale_for_reload({"react": {}}, None)
    await wait_prepared_tasks(deep)
    deep._reload_session_adapter_if_stale.assert_not_awaited()


# ---- hit / joined / hit_reloaded / miss logging ----


@pytest.fixture
def result_log(monkeypatch):
    session_prepare_tracker.clear()
    log = Mock()
    monkeypatch.setattr(interface_deep_module, "logger", log)

    def results():
        out = []
        for call in log.info.call_args_list:
            if str(call.args[0]).startswith("[session.prepare] chat result="):
                out.append(call.args[1:])
        return out

    yield results
    session_prepare_tracker.clear()


def chat_request(**overrides):
    overrides.setdefault("request_id", "chat-1")
    return make_request({"mode": "agent", "query": "hi"}, method=ReqMethod.CHAT_SEND, **overrides)


def make_chat_deep(**kwargs):
    deep = make_deep(**kwargs)
    del deep._get_or_create_session_adapter  # chat goes through the real method
    deep._reload_session_adapter_if_stale = AsyncMock()
    deep._refresh_session_skills_if_stale = AsyncMock()
    deep._skill_enabled_version = 0
    deep._session_instance_config = {}
    deep._session_instance_mode = "agent"
    deep._session_instance_sub_mode = None
    deep._config_base_cache = {}
    deep.mark_session_skills_current = Mock()
    built = SimpleNamespace(
        create_instance=AsyncMock(), start_interaction=AsyncMock(),
        configure_session_runtime=AsyncMock(), _instance=None,
    )
    deep._new_session_scoped_adapter = lambda _sid, **_kw: built
    return deep


async def prepared_chat_deep(**kwargs):
    deep = make_chat_deep(**kwargs)
    deep._session_adapters[SESSION_ID] = SimpleNamespace(configure_session_runtime=AsyncMock())
    deep._mark_prepared_session_unused(SESSION_ID)
    session_prepare_tracker.mark(SESSION_ID, "ready")
    return deep


def test_miss_reasons():
    assert session_prepare_tracker.miss_reason(None) == "not_prepared"
    for state, reason in [
        ("dropped", "cancelled_in_queue"),
        ("queue_full", "queue_full"),
        ("failed", "prepare_failed"),
        ("reclaimed", "reclaimed"),
        ("ready", "lost_after_ready"),
    ]:
        assert session_prepare_tracker.miss_reason({"state": state}) == reason


def test_tracker_is_bounded(monkeypatch):
    monkeypatch.setattr(session_prepare_tracker, "MAX_TRACKED_SESSIONS", 3)
    session_prepare_tracker.clear()
    for i in range(5):
        session_prepare_tracker.mark(f"s{i}", "queued")
    assert [session_prepare_tracker.take(f"s{i}") is not None for i in range(5)] == [False, False, True, True, True]


async def test_hit_logged_once_on_first_chat(result_log):
    deep = await prepared_chat_deep()
    await deep._get_or_create_session_adapter(SESSION_ID, request=chat_request())
    await deep._get_or_create_session_adapter(SESSION_ID, request=chat_request(request_id="chat-2"))
    assert [r[:2] for r in result_log()] == [("hit", SESSION_ID)]


async def test_joined_includes_join_wait(result_log):
    deep = await prepared_chat_deep()
    session_prepare_tracker.mark_joined(SESSION_ID, 250.0)
    await deep._get_or_create_session_adapter(SESSION_ID, request=chat_request())
    (result, sid, wait_ms), = result_log()
    assert (result, sid) == ("joined", SESSION_ID)
    assert wait_ms >= 250.0


async def test_hit_reloaded_when_config_changed_before_chat(result_log):
    deep = await prepared_chat_deep()
    deep._session_adapter_config_version = 1  # sync happened; instance still at 0
    await deep._get_or_create_session_adapter(SESSION_ID, request=chat_request())
    assert result_log()[0][0] == "hit_reloaded"


async def test_hit_reloaded_when_chat_waits_for_background_reload(result_log):
    deep = await prepared_chat_deep()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_reload(_sid, _adapter):
        entered.set()
        await release.wait()

    deep._reload_session_adapter_if_stale = slow_reload
    deep._reload_prepared_sessions_in_background()
    await entered.wait()
    chat = asyncio.create_task(deep._get_or_create_session_adapter(SESSION_ID, request=chat_request()))
    await asyncio.sleep(0.02)
    assert not chat.done()
    deep._reload_session_adapter_if_stale = AsyncMock()
    release.set()
    await chat
    (result, _sid, wait_ms), = result_log()
    assert result == "hit_reloaded"
    assert wait_ms >= 15


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (None, "not_prepared"), ("dropped", "cancelled_in_queue"),
        ("failed", "prepare_failed"), ("reclaimed", "reclaimed"),
    ],
)
async def test_miss_logged_with_reason(result_log, state, reason):
    deep = make_chat_deep()
    if state:
        session_prepare_tracker.mark(SESSION_ID, state)
    await deep._get_or_create_session_adapter(SESSION_ID, request=chat_request())
    assert result_log() == [(reason, SESSION_ID)]


async def test_no_result_for_prepare_or_other_channels(result_log):
    deep = make_chat_deep()
    await deep._get_or_create_session_adapter(SESSION_ID, request=make_request())
    await deep._get_or_create_session_adapter("web-1", request=chat_request(channel_id="web", session_id="web-1"))
    assert result_log() == []


async def test_reclaim_marks_tracker():
    session_prepare_tracker.clear()
    deep = make_deep(ttl=0.01)
    await prepare_on(deep, make_request())
    await wait_prepared_tasks(deep)
    assert session_prepare_tracker.take(SESSION_ID) == {"state": "reclaimed"}


async def test_pool_records_prepare_states():
    session_prepare_tracker.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    agent = SimpleNamespace(prepare_session=AsyncMock())

    async def get_agent(**_kwargs):
        entered.set()
        await release.wait()
        return agent

    pool, _, _ = make_pool(get_agent=get_agent)
    await call_prepare(make_request(session_id="a"), pool)
    await call_prepare(make_request(session_id="b"), pool)
    await entered.wait()
    assert session_prepare_tracker._entries["a"]["state"] == "building"
    assert session_prepare_tracker._entries["b"]["state"] == "queued"

    await pool.wait_for_session_prewarm("b")
    assert session_prepare_tracker._entries["b"]["state"] == "dropped"

    joiner = asyncio.create_task(pool.wait_for_session_prewarm("a"))
    await asyncio.sleep(0.02)
    release.set()
    await joiner
    await drain_prepare()
    assert session_prepare_tracker._entries["a"]["state"] == "ready"
    assert session_prepare_tracker._entries["a"]["join_wait_ms"] >= 15

    agent.prepare_session.side_effect = RuntimeError("boom")
    await call_prepare(make_request(session_id="c"), pool)
    await drain_prepare()
    assert session_prepare_tracker._entries["c"]["state"] == "failed"
    session_prepare_tracker.clear()


# ---- review follow-ups ----


async def test_chat_still_joins_running_prewarm_after_dropping_queued_prepare(monkeypatch):
    release = asyncio.Event()

    async def running_prewarm():
        await release.wait()

    task = asyncio.create_task(running_prewarm())
    monkeypatch.setattr(TenantAgentPool, "_session_prewarm_tasks", {SESSION_ID: task})
    TenantAgentPool._session_prepare_queue[SESSION_ID] = lambda: True
    pool, _, _ = make_pool()

    waiter = asyncio.create_task(pool.wait_for_session_prewarm(SESSION_ID))
    await asyncio.sleep(0.02)
    assert SESSION_ID not in TenantAgentPool._session_prepare_queue
    assert not waiter.done()  # still joins the running build instead of racing it
    release.set()
    await waiter


async def test_reclaim_retries_after_guard_and_then_reclaims():
    deep = make_deep(ttl=0.01, retry=0.02)
    await prepare_on(deep, make_request())
    lock = asyncio.Lock()
    deep._session_adapter_locks[SESSION_ID] = lock
    await lock.acquire()
    await asyncio.sleep(0.05)
    deep.cleanup_session_adapter.assert_not_awaited()
    assert deep.is_prepared_session_unused(SESSION_ID)  # kept for the retry and the hit log

    lock.release()
    await asyncio.sleep(0.1)
    deep.cleanup_session_adapter.assert_awaited_once_with(SESSION_ID)


async def test_chat_after_failed_reclaim_guard_still_logs_hit_and_is_not_reclaimed(result_log):
    deep = await prepared_chat_deep(ttl=0.01, retry=0.02)
    lock = deep._session_adapter_locks.setdefault(SESSION_ID, asyncio.Lock())
    await lock.acquire()  # e.g. a background reload holds the session lock at expiry
    await asyncio.sleep(0.05)
    chat = asyncio.create_task(deep._get_or_create_session_adapter(SESSION_ID, request=chat_request()))
    await asyncio.sleep(0)
    lock.release()
    await chat
    await asyncio.sleep(0.1)

    assert [r[0] for r in result_log()] == ["hit"]
    deep.cleanup_session_adapter.assert_not_awaited()


async def test_use_during_prepare_configure_logs_hit_and_skips_reclaim(result_log):
    deep = make_chat_deep(ttl=0.05)
    configuring, release = asyncio.Event(), asyncio.Event()
    built = deep._new_session_scoped_adapter(SESSION_ID)

    async def slow_configure(**_kwargs):
        configuring.set()
        await release.wait()

    built.configure_session_runtime = slow_configure
    prepare = asyncio.create_task(prepare_on(deep, make_request()))
    await configuring.wait()
    await deep._get_or_create_session_adapter(SESSION_ID, request=chat_request())
    release.set()
    await prepare
    await asyncio.sleep(0.1)

    assert [r[0] for r in result_log()] == ["hit"]
    assert not deep.is_prepared_session_unused(SESSION_ID)
    deep.cleanup_session_adapter.assert_not_awaited()


@pytest.mark.parametrize(
    ("requested", "locked", "prefer_locked", "expected"),
    [
        (None, "", False, None),
        (None, "D:/locked", False, "D:/locked"),
        ("D:/req", "D:/locked", False, "D:/req"),
        ("D:/req", "D:/locked", True, "D:/locked"),
        ("D:/req", "", True, "D:/req"),
    ],
)
def test_readonly_project_dir_keeps_each_callers_rules(monkeypatch, requested, locked, prefer_locked, expected):
    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *_a, **_k: {"project_dir": locked})
    resolved = _default.resolve_readonly_project_dir(
        make_request(), None, requested, prefer_locked=prefer_locked,
    )
    assert resolved == expected


async def test_prepare_ignores_legacy_dir_fields_like_an_agent_chat():
    pool, manager, _agent = make_pool()
    resp = await call_prepare(make_request({"mode": "agent", "cwd": "D:/cwd", "trusted_dirs": ["D:/t"]}), pool)
    await drain_prepare()
    assert resp.payload["status"] == "scheduled"
    assert manager.get_agent.await_args.kwargs["project_dir"] is None


async def test_prepare_writes_locked_project_dir_back_to_request(monkeypatch):
    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *_a, **_k: {"project_dir": "D:/locked"})
    pool, manager, agent = make_pool()
    request = make_request({"mode": "agent", "project_dir": "D:/stale"})
    await call_prepare(request, pool)
    await drain_prepare()
    assert manager.get_agent.await_args.kwargs["project_dir"] == "D:/locked"
    built_with = agent.prepare_session.await_args.kwargs["request"]
    assert built_with.params["project_dir"] == built_with.metadata["project_dir"] == "D:/locked"
    assert built_with.metadata["user_id"] == "officeclaw_user_1"


async def test_chat_joins_until_prepare_configure_finishes():
    configuring, release = asyncio.Event(), asyncio.Event()
    pool, _, agent = make_pool()

    async def slow_prepare_session(**_kwargs):
        configuring.set()
        await release.wait()

    agent.prepare_session = slow_prepare_session
    await call_prepare(make_request(), pool)
    await configuring.wait()
    chat_wait = asyncio.create_task(pool.wait_for_session_prewarm(SESSION_ID))
    await asyncio.sleep(0)
    assert not chat_wait.done()
    release.set()
    await chat_wait
    await drain_prepare()


async def test_prepare_skips_configure_when_chat_built_the_session_first(result_log):
    deep = make_chat_deep(ttl=0.05)
    session_prepare_tracker.mark(SESSION_ID, "building")
    built = await deep._get_or_create_session_adapter(SESSION_ID, request=chat_request())
    await deep.prepare_session(session_id=SESSION_ID, channel_id="officeclaw", mode="agent", request=make_request())
    built.configure_session_runtime.assert_not_awaited()
    assert not deep.is_prepared_session_unused(SESSION_ID)
    assert [r[0] for r in result_log()] == ["lost_after_building"]


async def test_queued_prepare_is_skipped_once_chat_took_the_session():
    pool, manager, agent = make_pool()
    pool.prepare_session_in_background(
        request=make_request(), mode="agent", adapter_mode="agent", project_dir=None,
    )
    session_prepare_tracker.take(SESSION_ID)  # the chat's own build logged its result
    await drain_prepare()
    manager.get_agent.assert_not_awaited()
    agent.prepare_session.assert_not_awaited()
    assert session_prepare_tracker.take(SESSION_ID) is None


async def test_non_chat_use_cancels_reclaim_but_leaves_the_hit_to_the_first_chat(result_log):
    deep = await prepared_chat_deep()
    await deep._get_or_create_session_adapter(SESSION_ID, request=None)
    assert not deep.is_prepared_session_unused(SESSION_ID) and result_log() == []
    await deep._get_or_create_session_adapter(SESSION_ID, request=chat_request())
    await deep._get_or_create_session_adapter(SESSION_ID, request=chat_request(request_id="chat-2"))
    assert [r[:2] for r in result_log()] == [("hit", SESSION_ID)]
