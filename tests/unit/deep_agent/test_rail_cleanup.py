# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""rail 回调泄漏清理测试.

背景: 每个新会话创建的 DeepAgent 实例会把 rail 回调注册到进程级
CallbackFramework, 事件名 = f"{card.id}_{event}"(card 级、同 bot 所有
会话共享)。会话结束若无反注册, 回调在共享事件键上无限累积, trigger()
遍历成本 O(累计会话数) —— 长稳测试中每轮新建会话的时延线性增长根因。
"""

from __future__ import annotations

import uuid

import pytest

from openjiuwen.core.common.schema.card import BaseCard  # noqa: F401
from openjiuwen.core.runner.runner import Runner
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, AgentRail
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent

from jiuwenclaw.agentserver.deep_agent.rail_cleanup import unregister_all_rails

# 本测试用到的两个事件均注册在 outer DeepAgent 的 card 级事件键上
_EVENTS = (AgentCallbackEvent.BEFORE_INVOKE, AgentCallbackEvent.BEFORE_TASK_ITERATION)


class _ProbeRail(AgentRail):
    """覆盖 outer 事件钩子的最小 rail, 附带 uninit 异常开关."""

    def __init__(self, fail_uninit: bool = False) -> None:
        self.fail_uninit = fail_uninit
        self.uninited = 0

    async def before_invoke(self, ctx) -> None:  # noqa: ANN001
        return None

    async def before_task_iteration(self, ctx) -> None:  # noqa: ANN001
        return None

    def uninit(self, agent) -> None:  # noqa: ANN001
        self.uninited += 1
        if self.fail_uninit:
            raise RuntimeError("probe uninit failure")


def _count_card_callbacks(card_id: str) -> int:
    framework = Runner.callback_framework
    return sum(
        len(framework.list_callbacks(f"{card_id}_{event}")) for event in _EVENTS
    )


@pytest.fixture()
def shared_card_id() -> str:
    """每测试独立的 card id, 避免污染进程级 framework 的其他事件键."""
    return f"test_card_{uuid.uuid4().hex}"


@pytest.fixture()
def _cleanup_framework_events(shared_card_id):
    yield
    import asyncio

    framework = Runner.callback_framework
    for event in _EVENTS:
        try:
            asyncio.get_event_loop().run_until_complete(
                framework.unregister_event(f"{shared_card_id}_{event}")
            )
        except Exception:  # noqa: BLE001
            pass


@pytest.mark.unit
async def test_rails_accumulate_on_shared_card_key(shared_card_id):
    """泄漏复现: 同 card.id 的两个实例先后注册 rail, 共享事件键上回调翻倍."""
    agent_session_1 = DeepAgent(AgentCard(id=shared_card_id))
    agent_session_2 = DeepAgent(AgentCard(id=shared_card_id))

    await agent_session_1.register_rail(_ProbeRail())
    first_round_count = _count_card_callbacks(shared_card_id)

    await agent_session_2.register_rail(_ProbeRail())
    second_round_count = _count_card_callbacks(shared_card_id)

    # 每实例 2 个事件钩子; 第二个实例注册后共享键上回调应翻倍(泄漏现象)
    assert first_round_count == 2
    assert second_round_count == 4


@pytest.mark.unit
async def test_unregister_all_rails_releases_only_own_callbacks(shared_card_id):
    """清理实例 1 的 rail 后, 共享键上只剩实例 2 的回调."""
    agent_session_1 = DeepAgent(AgentCard(id=shared_card_id))
    agent_session_2 = DeepAgent(AgentCard(id=shared_card_id))
    await agent_session_1.register_rail(_ProbeRail())
    await agent_session_2.register_rail(_ProbeRail())
    assert _count_card_callbacks(shared_card_id) == 4

    removed = await unregister_all_rails(agent_session_1)

    assert removed == 1
    assert _count_card_callbacks(shared_card_id) == 2


@pytest.mark.unit
async def test_unregister_all_rails_idempotent(shared_card_id):
    agent = DeepAgent(AgentCard(id=shared_card_id))
    await agent.register_rail(_ProbeRail())

    assert await unregister_all_rails(agent) == 1
    assert await unregister_all_rails(agent) == 0
    assert _count_card_callbacks(shared_card_id) == 0


@pytest.mark.unit
async def test_unregister_all_rails_covers_pending_rails(shared_card_id):
    """未完成 lazy-init 的 pending rail 也必须被清理(防止 init 后复活)."""
    agent = DeepAgent(AgentCard(id=shared_card_id))
    pending_rail = _ProbeRail()
    agent._pending_rails.append(pending_rail)
    await agent.register_rail(_ProbeRail())

    removed = await unregister_all_rails(agent)

    assert removed == 2
    assert pending_rail not in agent._pending_rails


@pytest.mark.unit
async def test_unregister_all_rails_tolerates_rail_failures(shared_card_id):
    """单个 rail uninit 抛异常不应中断其余 rail 的反注册."""
    agent = DeepAgent(AgentCard(id=shared_card_id))
    bad_rail = _ProbeRail(fail_uninit=True)
    await agent.register_rail(bad_rail)
    await agent.register_rail(_ProbeRail())
    assert _count_card_callbacks(shared_card_id) == 4

    removed = await unregister_all_rails(agent)  # 不应抛出

    assert removed == 2
    assert bad_rail.uninited == 1
    assert _count_card_callbacks(shared_card_id) == 0


@pytest.mark.unit
async def test_adapter_cleanup_releases_rails(shared_card_id):
    """JiuWenClawDeepAdapter.cleanup() 须释放其 _instance 上的全部 rail."""
    from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

    adapter = JiuWenClawDeepAdapter.__new__(JiuWenClawDeepAdapter)
    adapter._instance = DeepAgent(AgentCard(id=shared_card_id))
    await adapter._instance.register_rail(_ProbeRail())
    assert _count_card_callbacks(shared_card_id) == 2

    await adapter.cleanup()

    assert _count_card_callbacks(shared_card_id) == 0

    # 二次调用安全(清理后实例可能仍被引用)
    await adapter.cleanup()
