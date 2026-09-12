# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""租户级 agent 半成品池(被动补池): take + 后台补, 不回收.

背景: 新 session 首请求需付 create_instance 装配成本(tool_cards/rails/
sysop 等, 均租户级、与 session 无关)。本池在租户首请求完成后, 后台
预装配 min_idle 个"半成品"实例(已 create_instance、未绑定 session),
该租户后续新对话直接 take, 把装配成本移出请求路径。

可行性依据(代码审计):
- create_instance 产物与 session_id 零状态耦合; session 绑定
  (conversation_id 注入 / cron contextvar / 历史加载)全部发生在请求期;
- 绑定动作 = 登记进 AgentManager.agents[channel][mode][session_id],
  首请求经 _update_runtime_config 自动完成注入, 无需激活调用。

约束:
- 实例绑定 session 后不归还(规避 inflight/interrupt 状态清洗);
- params.workspace_dir 覆盖与 acp 通道不走池(由 AgentManager.get_agent 绕过);
- 配置/技能变更时 drain()(与 AgentManager.invalidate_assembly_cache 联动),
  池内旧配置实例作废; 半成品未绑定会话、未持有会话资源, 丢弃交由 GC
  (AgentManager.cleanup 时可先 pop_all 逐个 cleanup)。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# "装配一个半成品并入池"的协程工厂
BuildFn = Callable[[], Awaitable[Any]]


def build_warm_bootstrap_request(routing: tuple[str, str, str] | None) -> Any:
    """合成预热用的 bootstrap_request(仅需路由三元组, 供企业配置按租户键加载)."""
    group_id, bot_id, user_id = routing or ("", "", "")
    return SimpleNamespace(
        request_id=f"warm_{uuid.uuid4().hex[:12]}",
        params={"group_id": group_id, "bot_id": bot_id, "user_id": user_id},
        metadata=None,
    )


class AgentWarmPool:
    """每 AgentManager(租户)一个: (channel, mode) -> 半成品实例队列."""

    def __init__(self, min_idle: int = 1, max_per_combo: int = 2) -> None:
        self._min_idle = max(0, int(min_idle))
        self._max_per_combo = max(self._min_idle, int(max_per_combo))
        self._pool: dict[tuple[str, str], list[Any]] = {}
        self._refilling: set[tuple[str, str]] = set()

    @property
    def min_idle(self) -> int:
        return self._min_idle

    def take(self, channel_id: str, mode: str) -> Any | None:
        """取一个半成品; 返回 None 表示池空(调用方回退同步装配)."""
        key = (channel_id, mode)
        queue = self._pool.get(key)
        if not queue:
            logger.info("[AgentPerf] warm pool take=miss channel=%s mode=%s", channel_id, mode)
            return None
        instance = queue.pop()
        logger.info(
            "[AgentPerf] warm pool take=hit channel=%s mode=%s remaining=%d",
            channel_id, mode, len(queue),
        )
        return instance

    def pop_all(self) -> list[Any]:
        """取出全部半成品并清空(调用方决定是否逐个 cleanup)."""
        instances: list[Any] = []
        for queue in self._pool.values():
            instances.extend(queue)
        self._pool.clear()
        return instances

    def drain(self) -> None:
        """清空全部半成品(配置/技能变更时作废旧配置实例)."""
        dropped = len(self.pop_all())
        if dropped:
            logger.info("[AgentPerf] warm pool drain: dropped=%d", dropped)

    def schedule_refill(self, channel_id: str, mode: str, build: BuildFn) -> None:
        """后台补池到 min_idle(幂等: 同组合补池任务在飞时不重复)."""
        if self._min_idle <= 0:
            return
        key = (channel_id, mode)
        if len(self._pool.get(key) or []) >= self._min_idle or key in self._refilling:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # 同步段先标记再排任务: 防止连发 schedule 时任务尚未启动、检查全部通过
        self._refilling.add(key)
        loop.create_task(
            self._refill(key, channel_id, mode, build),
            name=f"warm-pool-{channel_id}-{mode}",
        )

    async def _refill(
        self, key: tuple[str, str], channel_id: str, mode: str, build: BuildFn
    ) -> None:
        try:
            while len(self._pool.get(key) or []) < self._min_idle:
                try:
                    instance = await build()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[AgentPerf] warm pool refill failed: %s", exc)
                    return
                if instance is None:
                    return
                # 补池期间可能已被 drain 或已满: 满则丢弃, 防陈旧堆积
                queue = self._pool.setdefault(key, [])
                if len(queue) >= self._max_per_combo:
                    logger.info(
                        "[AgentPerf] warm pool put=dropped(full) channel=%s mode=%s",
                        channel_id, mode,
                    )
                    return
                queue.append(instance)
                logger.info(
                    "[AgentPerf] warm pool refill done: channel=%s mode=%s size=%d",
                    channel_id, mode, len(queue),
                )
        finally:
            self._refilling.discard(key)

    def __len__(self) -> int:
        return sum(len(queue) for queue in self._pool.values())
