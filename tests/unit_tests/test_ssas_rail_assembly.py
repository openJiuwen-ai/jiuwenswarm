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
    ssas = {
        "enabled": True,
        "ssas_home": str(tmp_path),
        "decision_policy": "observe_only",
    }
    ssas.update(overrides)
    return {"ssas": ssas}


@pytest.mark.asyncio
async def test_disabled_or_missing_config_returns_none():
    assert (
        m.JiuWenSwarmDeepAdapter._build_ssas_rail({"ssas": {"enabled": False}})
        is None
    )
    assert m.JiuWenSwarmDeepAdapter._build_ssas_rail({"other": 1}) is None


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_init_failure_evicts_cache_for_retry(tmp_path):
    cfg = _cfg(tmp_path)
    rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(cfg)
    await m._await_ssas_init_tasks()
    backend = rail._backend._ssas_backend
    key = (asyncio.get_running_loop(), m._ssas_backend_fingerprint(cfg["ssas"]))
    assert m._SSAS_BACKEND_CACHE.get(key) is backend

    async def boom():
        raise RuntimeError("injected init failure")

    class _EvictedBackend:
        """替身: 顶替缓存条目模拟初始化失败态, 记录后台 close 是否触达."""

        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    evicted = _EvictedBackend()
    fail_task = asyncio.get_running_loop().create_task(boom())
    with m._SSAS_INIT_TASKS_LOCK:
        m._SSAS_BACKEND_CACHE[key] = evicted
        m._SSAS_INIT_TASKS.add(fail_task)
        m._SSAS_INIT_TASK_KEYS[fail_task] = (key, evicted)
    fail_task.add_done_callback(m._log_ssas_init_result)
    # 确定性等待: await 该 Task 本身, done callback(_log_ssas_init_result)
    # 先于 wait 完成, 无需固定 sleep 依赖调度时序
    with pytest.raises(RuntimeError, match="injected init failure"):
        await asyncio.wait_for(fail_task, timeout=5.0)
    assert m._SSAS_BACKEND_CACHE.get(key) is None
    # 剔除的 backend 被后台关闭, 防构造期资源随重复失败装配累积泄漏
    pending_close = [t for t in m._SSAS_CLOSE_TASKS if not t.done()]
    if pending_close:
        await asyncio.wait_for(
            asyncio.gather(*pending_close, return_exceptions=True), timeout=5.0
        )
    # 协程完成与 done callback(_log_ssas_close_result)移出集合相差一个
    # 循环节拍, 补一拍确定性等待后再断言
    await asyncio.sleep(0)
    assert evicted.closed is True
    with m._SSAS_INIT_TASKS_LOCK:
        assert len(m._SSAS_CLOSE_TASKS) == 0

    rail_retry = m.JiuWenSwarmDeepAdapter._build_ssas_rail(cfg)
    await m._await_ssas_init_tasks()
    assert rail_retry._backend._ssas_backend is not backend
    assert rail_retry._backend._initialized is True
    await backend.close()
    await rail_retry._backend._ssas_backend.close()


@pytest.mark.asyncio
async def test_wait_timeout_cancels_and_evicts_hung_task(tmp_path):
    rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(_cfg(tmp_path))
    await m._await_ssas_init_tasks()

    async def hang():
        await asyncio.sleep(3600)

    class _HungBackend:
        """替身: 记录超时剔除后的后台 close 是否触达."""

        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    hung = asyncio.get_running_loop().create_task(hang())
    key = (asyncio.get_running_loop(), "fp_hang_probe")
    fake_backend = _HungBackend()
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
    # 确定性等待取消生效: 直接 await 被取消的 Task, 而非固定 sleep 赌调度时序
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(hung, timeout=5.0)
    assert hung.cancelled()
    with m._SSAS_INIT_TASKS_LOCK:
        assert hung not in m._SSAS_INIT_TASKS
        assert key not in m._SSAS_BACKEND_CACHE
    # 超时剔除的 backend 被后台关闭(防资源泄漏), 若 close Task 尚未完成
    # 则确定性等待其结束
    pending_close = [t for t in m._SSAS_CLOSE_TASKS if not t.done()]
    if pending_close:
        await asyncio.wait_for(
            asyncio.gather(*pending_close, return_exceptions=True), timeout=5.0
        )
    # 协程完成与 done callback(_log_ssas_close_result)移出集合相差一个
    # 循环节拍, 补一拍确定性等待后再断言
    await asyncio.sleep(0)
    assert fake_backend.closed is True
    with m._SSAS_INIT_TASKS_LOCK:
        assert len(m._SSAS_CLOSE_TASKS) == 0
    t1 = time.monotonic()
    await m._await_ssas_init_tasks()
    assert time.monotonic() - t1 < 1.0
    await rail._backend._ssas_backend.close()


@pytest.mark.asyncio
async def test_stale_closed_loop_entries_swept_on_build(tmp_path):
    other_loop = asyncio.new_event_loop()

    def _hang_then_close():
        # 在独立线程内驱动 other_loop(运行中循环内禁止跨 loop run_until_complete):
        # 先让 hang 真正挂起, 再取消并等待其完成, 避免 other_loop.close() 后
        # 遗留 pending Task 触发 "Task was destroyed but it is pending!" 告警;
        # 清扫条件是 task.get_loop().is_closed(), 与 Task 是否 pending 无关,
        # 故已完成的 Task 仍可验证已关闭循环条目的清扫逻辑
        async def hang():
            await asyncio.sleep(3600)

        hung = other_loop.create_task(hang())
        other_loop.run_until_complete(asyncio.sleep(0))
        hung.cancel()
        try:
            other_loop.run_until_complete(hung)
        except asyncio.CancelledError:
            pass
        other_loop.close()
        return hung

    hung = await asyncio.to_thread(_hang_then_close)
    stale_key = (other_loop, "fp_stale_probe")

    class _StaleBackend:
        """替身: 记录已关闭循环清扫路径的释放契约(close 不会被调用)."""

        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    stale_backend = _StaleBackend()
    with m._SSAS_INIT_TASKS_LOCK:
        m._SSAS_BACKEND_CACHE[stale_key] = stale_backend
        m._SSAS_INIT_TASKS.add(hung)
        m._SSAS_INIT_TASK_KEYS[hung] = (stale_key, stale_backend)

    rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(_cfg(tmp_path))
    assert rail is not None
    await m._await_ssas_init_tasks()
    with m._SSAS_INIT_TASKS_LOCK:
        assert stale_key not in m._SSAS_BACKEND_CACHE
        assert hung not in m._SSAS_INIT_TASKS
        assert hung not in m._SSAS_INIT_TASK_KEYS
    # 已关闭循环条目的释放契约: 仅丢弃引用, 不得尝试 close——条目所属循环
    # 已关闭, 无法调度 close Task(资源随循环销毁整体回收)。这与初始化失败/
    # 收口超时两条剔除路径的"后台关闭"契约(evicted.closed is True)不同,
    # 三条路径的释放契约由本用例与另两用例分别显式锁定
    assert stale_backend.closed is False
    await rail._backend._ssas_backend.close()


@pytest.mark.asyncio
async def test_sync_context_fails_open(tmp_path):
    rail = await asyncio.to_thread(
        m.JiuWenSwarmDeepAdapter._build_ssas_rail, _cfg(tmp_path)
    )
    assert rail is None


@pytest.mark.asyncio
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
    # 确定性等待竞态落败者的后台 close Task 完成, 而非固定 sleep 赌调度时序
    pending_close = [t for t in m._SSAS_CLOSE_TASKS if not t.done()]
    if pending_close:
        await asyncio.wait_for(
            asyncio.gather(*pending_close, return_exceptions=True), timeout=5.0
        )
    assert rail._backend._ssas_backend is state["winner"]
    with m._SSAS_INIT_TASKS_LOCK:
        assert len(m._SSAS_CLOSE_TASKS) == 0
        m._SSAS_BACKEND_CACHE.pop(key, None)
    await state["winner"].close()
    await state["loser"].close()
