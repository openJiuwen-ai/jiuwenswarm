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

# agent-ssas 是必装依赖(uv.lock 固定): 缺失属安装/锁定失败, 本模块用例
# 会在收集/运行期直接 error 硬红, 不设 skipif——避免依赖缺失时整模块跳过
# 伪装成"通过"
pytestmark = [pytest.mark.filterwarnings("ignore")]


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
        """替身: 记录已关闭循环清扫路径的释放契约(close 会被调用)."""

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
        pending_close = [
            task for task in m._SSAS_CLOSE_TASKS if not task.done()
        ]
    # 已关闭循环条目的释放契约: backend 构造期即建立 SQLite 连接等同步
    # 资源, 不随循环销毁释放——清扫必须经独立线程的全新事件循环后台执行
    # close(条目所属循环已关闭, 无法派发), 与初始化失败/收口超时两条剔除
    # 路径的"后台关闭"契约一致(evicted.closed is True), 三条路径的释放
    # 契约由本用例与另两用例分别显式锁定; wait_for+gather 确定性等待清扫
    # 派发的全部 close Task 完成(to_thread 线程内 asyncio.run 退出即 close
    # 落定), 超时上限与同文件其余三处一致(5s)防用例悬挂
    await asyncio.wait_for(
        asyncio.gather(*pending_close, return_exceptions=True), timeout=5.0
    )
    assert stale_backend.closed is True
    await rail._backend._ssas_backend.close()


@pytest.mark.asyncio
async def test_fresh_loop_close_degrades_on_dead_loop_object(tmp_path, caplog):
    """清扫触发的 fresh-loop close 触达死循环绑定对象时降级告警收敛.

    复刻真实 backend 的已知限制: close() 首行取消绑定在已关闭循环上的
    周期清理 Task, 若该 Task 已挂起, cancel 经已关闭循环的 call_soon
    调度回调抛 RuntimeError, 阻断后续存储释放——替身以已关闭循环的
    call_soon 直接触达等价复刻该异常路径(同一 RuntimeError: 死循环
    调度访问, 且不遗留 pending Task 触发 GC 告警), 验证降级契约:
    close Task 在超时上限内收敛(不悬挂)、异常不冒泡为泛化失败、
    告警明确指向等待上游 agent-ssas 同步释放入口 close_nowait。
    """
    other_loop = asyncio.new_event_loop()
    other_loop.close()  # 从未运行的循环直接关闭: 充当"已关闭循环"替身

    class _DeadLoopBackend:
        """替身: close 内触达已关闭循环绑定对象复刻真实 backend 行为.

        真实 backend 的清理 Task.cancel 经 __schedule_callbacks 走已关闭
        循环的 call_soon 抛 RuntimeError; 替身直接调用同一入口等价复刻,
        异常后 self.closed 赋值不可达, 模拟存储释放被阻断的降级路径。
        """

        def __init__(self, loop):
            self._loop = loop
            self.closed = False

        async def close(self):
            self._loop.call_soon(lambda: None)  # RuntimeError: loop is closed
            self.closed = True

    dead_backend = _DeadLoopBackend(other_loop)
    stale_key = (other_loop, "fp_dead_loop_probe")
    with m._SSAS_INIT_TASKS_LOCK:
        m._SSAS_BACKEND_CACHE[stale_key] = dead_backend

    import logging

    # setup_logger 对 jiuwenswarm.* logger 设 propagate=False(应用日志直挂
    # 自身 handler), caplog 的捕获 handler 挂在 root 收不到——显式将
    # caplog.handler 挂接到 interface_deep logger, 用毕移除
    m.logger.addHandler(caplog.handler)
    try:
        caplog.at_level(logging.WARNING, logger=m.logger.name)
        rail = m.JiuWenSwarmDeepAdapter._build_ssas_rail(_cfg(tmp_path))
        assert rail is not None
        await m._await_ssas_init_tasks()
        with m._SSAS_INIT_TASKS_LOCK:
            assert stale_key not in m._SSAS_BACKEND_CACHE
            pending_close = [
                task for task in m._SSAS_CLOSE_TASKS if not task.done()
            ]
        # close Task 可能在 _await_ssas_init_tasks() 期间已完成(不在
        # pending_close 中), gather 空集立即返回; 未完成则确定性等待
        # 其收敛, 超时上限与同文件其余三处一致(5s)防用例悬挂
        await asyncio.wait_for(
            asyncio.gather(*pending_close, return_exceptions=True), timeout=5.0
        )
        # 协程完成与 done callback 移出集合相差一个循环节拍, 补一拍后断言
        await asyncio.sleep(0)
    finally:
        m.logger.removeHandler(caplog.handler)
    # 降级契约: backend 未被实际释放(存储释放被死循环对象阻断), 但
    # close Task 收敛、不再冒泡泛化异常, 且告警明确指向等待上游 close_nowait
    assert dead_backend.closed is False
    with m._SSAS_INIT_TASKS_LOCK:
        assert len(m._SSAS_CLOSE_TASKS) == 0
    assert any("close_nowait" in r.getMessage() for r in caplog.records)
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
