# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified read/write portal for interrupt-recovery session flags.

背景：adapter 的 prepare hook 持有"临时 session"（``create_agent_session`` +
``pre_run``，走 checkpointer 磁盘态），而 rails（``before_invoke`` 等）读取的是
运行时 ``_interaction_session``（内存态）。两者是不同 Python 对象，标志只写
其中一个等价于对另一个不可见——历史上多次 bug 均源于此。

``SessionFlagProxy`` 把"双写 + 双读"收敛到一个入口：

- ``update_state`` 扇出到所有注册的 session 实例（落盘态 + 内存态）；
- ``get_state`` 依次查询，返回第一个非 None 值（None 是本模块标志的通用
  "已清除/不存在"标记）；
- 鸭子类型兼容现有单 session 标志函数（``mark_interrupt_recovery_injected``
  等），调用方把 proxy 当 session 传入即可，无需改动函数签名。

用法::

    proxy = build_flag_proxy(session, runtime_session)
    mark_interrupt_recovery_injected(proxy)          # 自动写两处
    if is_interrupt_recovery_injected(proxy): ...    # 自动读两处
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


class SessionFlagProxy:
    """Fan-out session-state adapter for interrupt-recovery flags."""

    __slots__ = ("_sessions",)

    def __init__(self, sessions: Iterable[Any] | None = None) -> None:
        seen: list[Any] = []
        for session in sessions or ():
            if session is None:
                continue
            if any(session is existing for existing in seen):
                continue  # 同一对象只保留一份
            seen.append(session)
        self._sessions = seen

    @property
    def sessions(self) -> list[Any]:
        return list(self._sessions)

    def update_state(self, state: dict[str, Any]) -> None:
        """Write state to every registered session instance."""
        for session in self._sessions:
            try:
                session.update_state(state)
            except Exception:  # noqa: BLE001 — 单实例失败不阻断其余实例
                logger.warning(
                    "[SessionFlagProxy] update_state failed on %s",
                    type(session).__name__,
                    exc_info=True,
                )

    def get_state(self, key: str) -> Any:
        """Return the first non-None value for ``key`` across sessions.

        None 是本模块标志的通用清除标记（clear 函数写 None），因此读到
        None 视为"该实例未持有"，继续查下一个；False/空串等真实值原样返回。
        """
        for session in self._sessions:
            try:
                value = session.get_state(key)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[SessionFlagProxy] get_state(%r) failed on %s",
                    key,
                    type(session).__name__,
                    exc_info=True,
                )
                continue
            if value is not None:
                return value
        return None

    def clear_state(self, key: str) -> None:
        """Clear ``key`` on every registered session instance."""
        self.update_state({key: None})


def build_flag_proxy(*sessions: Any) -> SessionFlagProxy:
    """Build a proxy from any mix of sessions / proxies / None.

    传入的 ``SessionFlagProxy`` 会被展开合并，避免嵌套。
    """
    flattened: list[Any] = []

    def _flatten(candidate: Any) -> None:
        if candidate is None:
            return
        if isinstance(candidate, SessionFlagProxy):
            for inner in candidate.sessions:
                _flatten(inner)
            return
        if any(candidate is existing for existing in flattened):
            return
        flattened.append(candidate)

    for session in sessions:
        _flatten(session)
    return SessionFlagProxy(flattened)
