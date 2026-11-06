# -*- coding: utf-8 -*-
"""宿主侧 AgentSSAS rail 装配/收口/缓存并发逻辑单测.

覆盖 _build_ssas_rail 装配(禁用降级/共享代理/双检锁竞态兜底)、
_await_ssas_init_tasks 收口(等待/超时取消/剔除自愈)、backend 进程级
缓存(复用/指纹/上界治理/已关闭循环清扫)等评审关注的并发路径。
"""
import asyncio
import time

import pytest

import jiuwenswarm.server.runtime.agent_adapter.interface_deep as m

pytestmark = [
    pytest.mark.filterwarnings("ignore"),
    pytest.mark.skipif(
        not m._SSAS_AVAILABLE, reason="agent-ssas not installed"
    ),
]


def _cfg(tmp_path, **overrides):
    ssas = {"enabled": True, "ssas_home": str(tmp_path), "observe_only": True}
    ssas.update(overrides)
    return {"ssas": ssas}


async def test_disabled_or_missing_config_returns_none():
    assert (
        m.JiuWenSwarmDeepAdapter._build_ssas_rail({"ssas": {"enabled": False}})
        is None
    )
    assert m.JiuWenSwarmDeepAdapter._build_ssas_rail({"other": 1}) is None


async def test_enabled_build_await_init_and_report(tmp_path):
    rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(_cfg(tmp_path))
    assert rail is not None
    assert isinstance(rail._backend, m._SharedSSASBackendProxy)
    with m._SSAS_INIT_TASKS_LOCK:
        assert len(m._SSAS_INIT_TASKS) == 1
    await m._await_ssas_init_tasks()
    assert rail._backend._initialized is True
    assessment = await rail._backend.report_event(
        {
            "common": {"event_type": "tool_call_before", "timestamp": time.time()},
            "payload": {"tool_name": "ut_probe_tool", "arguments": {}},
            "metadata": {},
        }
    )
    assert assessment is not None
    with m._SSAS_INIT_TASKS_LOCK:
        assert len(m._SSAS_INIT_TASKS) == 0
    await rail._backend._ssas_backend.close()


async def test_backend_reuse_same_loop_and_config(tmp_path):
    cfg = _cfg(tmp_path)
    rail_a = m.JiuWenSwarmDeepAdapter._build_ssas_rail(cfg)
    with m._SSAS_INIT_TASKS_LOCK:
        assert len(m._SSAS_INIT_TASKS) == 1
    await m._await_ssas_init_tasks()
    rail_b = m.JiuWenSwarmDeepAdapter._build_ssas_rail(cfg)
    assert rail_b._backend._ssas_backend is rail_a._backend._ssas_backend
    with m._SSAS_INIT_TASKS_LOCK:
        assert len(m._SSAS_INIT_TASKS) == 0
    await rail_a._backend._ssas_backend.close()


async def test_rail_priority_knob(tmp_path):
    rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(
        _cfg(tmp_path, rail_priority=100)
    )
    assert rail.priority == 100
    await m._await_ssas_init_tasks()
    rail_bad = m.JiuWenSwarmDeepAdapter._build_ssas_rail(
        _cfg(tmp_path, rail_priority="abc")
    )
    assert rail_bad.priority == 80
    await rail._backend._ssas_backend.close()


def test_fingerprint_excludes_rail_knobs_and_is_order_insensitive():
    fp1 = m._ssas_backend_fingerprint(
        {"enabled": True, "rail_priority": 80, "decision_policy": "x", "event_ttl_days": 30}
    )
    fp2 = m._ssas_backend_fingerprint(
        {"event_ttl_days": 30, "decision_policy": "x", "enabled": False, "rail_priority": 99}
    )
    assert fp1 == fp2


async def test_init_failure_evicts_cache_for_retry(tmp_path):
    cfg = _cfg(tmp_path)
    rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(cfg)
    await m._await_ssas_init_tasks()
    backend = rail._backend._ssas_backend
    key = (asyncio.get_running_loop(), m._ssas_backend_fingerprint(cfg["ssas"]))
    assert m._SSAS_BACKEND_CACHE.get(key) is backend

    async def boom():
        raise RuntimeError("injected init failure")

    fail_task = asyncio.get_running_loop().create_task(boom())
    with m._SSAS_INIT_TASKS_LOCK:
        m._SSAS_INIT_TASKS.add(fail_task)
        m._SSAS_INIT_TASK_KEYS[fail_task] = (key, backend)
    fail_task.add_done_callback(m._log_ssas_init_result)
    await asyncio.sleep(0.05)
    assert m._SSAS_BACKEND_CACHE.get(key) is None

    rail_retry = m.JiuWenSwarmDeepAdapter._build_ssas_rail(cfg)
    await m._await_ssas_init_tasks()
    assert rail_retry._backend._ssas_backend is not backend
    assert rail_retry._backend._initialized is True
    await backend.close()
    await rail_retry._backend._ssas_backend.close()


async def test_wait_timeout_cancels_and_evicts_hung_task(tmp_path):
    rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(_cfg(tmp_path))
    await m._await_ssas_init_tasks()

    async def hang():
        await asyncio.sleep(3600)

    hung = asyncio.get_running_loop().create_task(hang())
    key = (asyncio.get_running_loop(), "fp_hang_probe")
    fake_backend = object()
    with m._SSAS_INIT_TASKS_LOCK:
        m._SSAS_INIT_TASKS.add(hung)
        m._SSAS_INIT_TASK_KEYS[hung] = (key, fake_backend)
        m._SSAS_BACKEND_CACHE[key] = fake_backend
    old_timeout = m._SSAS_INIT_WAIT_TIMEOUT
    m._SSAS_INIT_WAIT_TIMEOUT = 0.05
    try:
        t0 = time.monotonic()
        await m._await_ssas_init_tasks()
        elapsed = time.monotonic() - t0
    finally:
        m._SSAS_INIT_WAIT_TIMEOUT = old_timeout
    assert 0.03 <= elapsed < 5.0
    await asyncio.sleep(0.05)
    assert hung.cancelled()
    with m._SSAS_INIT_TASKS_LOCK:
        assert hung not in m._SSAS_INIT_TASKS
        assert key not in m._SSAS_BACKEND_CACHE
    t1 = time.monotonic()
    await m._await_ssas_init_tasks()
    assert time.monotonic() - t1 < 1.0
    await rail._backend._ssas_backend.close()


async def test_stale_closed_loop_entries_swept_on_build(tmp_path):
    other_loop = asyncio.new_event_loop()

    async def hang():
        await asyncio.sleep(3600)

    hung = other_loop.create_task(hang())
    other_loop.close()
    stale_key = (other_loop, "fp_stale_probe")
    with m._SSAS_INIT_TASKS_LOCK:
        m._SSAS_BACKEND_CACHE[stale_key] = object()
        m._SSAS_INIT_TASKS.add(hung)
        m._SSAS_INIT_TASK_KEYS[hung] = (stale_key, object())

    rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(_cfg(tmp_path))
    assert rail is not None
    await m._await_ssas_init_tasks()
    with m._SSAS_INIT_TASKS_LOCK:
        assert stale_key not in m._SSAS_BACKEND_CACHE
        assert hung not in m._SSAS_INIT_TASKS
        assert hung not in m._SSAS_INIT_TASK_KEYS
    await rail._backend._ssas_backend.close()


async def test_sync_context_fails_open(tmp_path):
    rail = await asyncio.to_thread(
        m.JiuWenSwarmDeepAdapter._build_ssas_rail, _cfg(tmp_path)
    )
    assert rail is None


async def test_double_checked_lock_race_loser_closed_in_background(
    tmp_path, monkeypatch
):
    import agent_ssas.core.framework.access_adapter.agent_backend as backend_mod

    real_cls = backend_mod.AgentSSASBackend
    cfg = _cfg(tmp_path)
    key = (asyncio.get_running_loop(), m._ssas_backend_fingerprint(cfg["ssas"]))
    state: dict = {}

    class RacingBackend(real_cls):
        """构造期间模拟并发赢家抢占同键缓存, 触发双检锁落败分支."""

        def __init__(self, config):
            super().__init__(config)
            if not state.get("armed"):
                return
            if "winner" not in state:
                state["winner"] = real_cls(config)
                m._SSAS_BACKEND_CACHE[key] = state["winner"]
                state["loser"] = self

    monkeypatch.setattr(backend_mod, "AgentSSASBackend", RacingBackend)
    state["armed"] = True
    try:
        rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(cfg)
    finally:
        state["armed"] = False
        monkeypatch.undo()
    assert rail is not None
    await asyncio.sleep(0.1)
    assert rail._backend._ssas_backend is state["winner"]
    with m._SSAS_INIT_TASKS_LOCK:
        assert len(m._SSAS_CLOSE_TASKS) == 0
        m._SSAS_BACKEND_CACHE.pop(key, None)
    await state["winner"].close()
    await state["loser"].close()
