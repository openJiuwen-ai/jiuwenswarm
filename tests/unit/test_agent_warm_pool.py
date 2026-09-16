# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""AgentWarmPool 单测: take / put 上限 / refill 幂等 / drain / 合成 request."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenclaw.agentserver.warm_pool import (
    AgentWarmPool,
    build_warm_bootstrap_request,
)


@pytest.mark.unit
async def test_take_miss_then_refill_hits() -> None:
    """空池 take=miss; schedule_refill 补到 min_idle 后 take=hit."""
    pool = AgentWarmPool(min_idle=1)
    assert pool.take("web", "agent") is None

    builds = 0

    async def build() -> SimpleNamespace:
        nonlocal builds
        builds += 1
        return SimpleNamespace(name=f"agent_{builds}")

    pool.schedule_refill("web", "agent", build)
    await asyncio.sleep(0.05)  # 等后台补池完成
    assert builds == 1

    instance = pool.take("web", "agent")
    assert instance is not None and instance.name == "agent_1"
    # 取走后再排补池, 又补一个
    pool.schedule_refill("web", "agent", build)
    await asyncio.sleep(0.05)
    assert builds == 2 and len(pool) == 1


@pytest.mark.unit
async def test_refill_idempotent_while_inflight() -> None:
    """补池任务在飞时, 重复 schedule 不产生多余 build(幂等)."""
    pool = AgentWarmPool(min_idle=1)

    started = asyncio.Event()
    builds = 0

    async def slow_build() -> SimpleNamespace:
        nonlocal builds
        builds += 1
        started.set()
        await asyncio.sleep(0.1)
        return SimpleNamespace(name=f"agent_{builds}")

    for _ in range(5):
        pool.schedule_refill("web", "agent", slow_build)
    await started.wait()
    for _ in range(5):
        pool.schedule_refill("web", "agent", slow_build)  # 在飞期间重复排
    await asyncio.sleep(0.2)
    assert builds == 1, f"在飞期间不应重复 build, 实际 {builds}"


@pytest.mark.unit
async def test_refill_failure_is_silent() -> None:
    """build 抛异常静默终止, 不影响后续再排."""

    pool = AgentWarmPool(min_idle=1)
    calls = 0

    async def bad_build() -> SimpleNamespace:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    pool.schedule_refill("web", "agent", bad_build)
    await asyncio.sleep(0.05)
    assert calls == 1 and len(pool) == 0

    async def good_build() -> SimpleNamespace:
        return SimpleNamespace(name="ok")

    pool.schedule_refill("web", "agent", good_build)
    await asyncio.sleep(0.05)
    assert len(pool) == 1


@pytest.mark.unit
async def test_drain_and_pop_all() -> None:
    """drain 清空; pop_all 返回实例供调用方 cleanup."""
    pool = AgentWarmPool(min_idle=2)

    async def build() -> SimpleNamespace:
        return SimpleNamespace(name="x")

    pool.schedule_refill("web", "agent", build)
    await asyncio.sleep(0.05)
    assert len(pool) == 2

    drained = pool.pop_all()
    assert len(drained) == 2 and len(pool) == 0
    assert pool.take("web", "agent") is None

    pool.schedule_refill("web", "agent", build)
    await asyncio.sleep(0.05)
    pool.drain()
    assert len(pool) == 0


@pytest.mark.unit
async def test_max_per_combo_caps_queue() -> None:
    """已达标(min_idle)后不再补; 池内数量不超过 max_per_combo."""
    pool = AgentWarmPool(min_idle=1, max_per_combo=1)
    builds = 0

    async def build() -> SimpleNamespace:
        nonlocal builds
        builds += 1
        return SimpleNamespace(name=f"x{builds}")

    pool.schedule_refill("web", "agent", build)
    await asyncio.sleep(0.05)
    assert len(pool) == 1

    # 已达 min_idle, 重复 schedule 不再 build
    pool.schedule_refill("web", "agent", build)
    await asyncio.sleep(0.05)
    assert builds == 1 and len(pool) == 1


@pytest.mark.unit
def test_build_warm_bootstrap_request_routing() -> None:
    """合成 request 携带路由三元组(routing_cache_key 可提取)."""
    req = build_warm_bootstrap_request(("g1", "b1", "u1"))
    assert req.params == {"group_id": "g1", "bot_id": "b1", "user_id": "u1"}
    assert req.request_id.startswith("warm_")
    assert req.metadata is None

    from jiuwenclaw.agentserver.deep_agent.tenant_assembly import routing_cache_key

    assert routing_cache_key(req) == ("g1", "b1", "u1")

    empty = build_warm_bootstrap_request(None)
    assert routing_cache_key(empty) == ("", "", "")
