# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""rail 回调反注册工具.

背景: rail 回调注册在进程级 CallbackFramework 上, 事件名 = f"{card.id}_{event}"
(card 级、同 bot 所有会话共享)。每个新会话都会注册一套 rail 回调, 会话结束若
不反注册, 回调在共享事件键上无限累积: trigger() 遍历并 await 全部历史回调,
成本 O(累计会话数) —— 每轮新建会话的时延随时间线性增长的根因; 同时泄漏的
回调持有死 rail/session 对象引用, 造成内存泄漏。

会话/实例销毁时调用 unregister_all_rails(deep_agent) 可将注册在
_callback_manager 上的回调全部释放(unregister_rail 同时清理 outer 与
bridged-to-react 两级, 以及 pending 队列, 防止 lazy-init 复活)。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def unregister_all_rails(deep_agent: Any) -> int:
    """反注册 DeepAgent 实例上全部 rail(含未完成 lazy-init 的 pending rail).

    Args:
        deep_agent: openjiuwen harness DeepAgent 实例(或暴露
            _registered_rails/_pending_rails/unregister_rail 的等价对象).

    Returns:
        实际处理的 rail 数量.
    """
    registered = list(getattr(deep_agent, "_registered_rails", None) or [])
    pending = list(getattr(deep_agent, "_pending_rails", None) or [])
    rails = registered + pending
    if not rails:
        return 0

    unregistered = 0
    for rail in rails:
        try:
            await deep_agent.unregister_rail(rail)
            unregistered += 1
        except Exception as exc:  # noqa: BLE001
            # 单个 rail 失败不阻断其余清理; framework 侧回调在 uninit 前已移除
            logger.warning(
                "[rail_cleanup] unregister_rail failed, skip: rail=%s error=%s",
                type(rail).__name__,
                exc,
            )
    logger.info(
        "[rail_cleanup] unregistered rails: total=%d ok=%d card=%s",
        len(rails),
        unregistered,
        getattr(getattr(deep_agent, "card", None), "id", "?"),
    )
    return len(rails)
