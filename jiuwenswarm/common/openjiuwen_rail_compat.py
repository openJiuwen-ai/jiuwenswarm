# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bridge supported rail behavior across openjiuwen SDK versions."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_COMPAT_FLAG = "_jiuwenswarm_kwargs_compat"


def filter_unsupported_kwargs(func: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop kwargs that ``func`` cannot accept unless it already takes **kwargs."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return kwargs
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return kwargs
    allowed = set(signature.parameters)
    return {key: value for key, value in kwargs.items() if key in allowed}


async def call_attach_output(instance: Any, *, steal: bool = False) -> Any:
    """Call ``attach_output``, dropping ``steal`` on dest-stable SDK builds.

    Official dest-stable ``DeepAgent.attach_output`` has no ``steal`` keyword;
    dest-stable jiuwenswarm still requests it for replica chat.send takeover.
    """
    attach = instance.attach_output
    kwargs = filter_unsupported_kwargs(attach, {"steal": True} if steal else {})
    return await attach(**kwargs)


def _wrap_init_for_extra_kwargs(cls: type) -> None:
    """Allow newer trajectory/signal kwargs against older rail constructors."""
    original = cls.__init__
    if getattr(original, _COMPAT_FLAG, False):
        return

    def _compat(self, *args, **kwargs):
        return original(self, *args, **filter_unsupported_kwargs(original, kwargs))

    setattr(_compat, _COMPAT_FLAG, True)
    cls.__init__ = _compat  # type: ignore[method-assign]


def _install_model_continue_compat(context_cls: type) -> None:
    """Use the older ReAct loop's steering continuation gate, without replacing it."""
    if callable(getattr(context_cls, "request_model_continue", None)):
        return

    def request_model_continue(self) -> None:
        # The stable ReAct loop already continues while steering is pending.
        # Retain the rail's specific reminder; only synthesize one if absent.
        queue = self.steering_queue
        # 旧 SDK 在公开队列上绑定 steering_inbox；不调用其私有窗口检查方法。
        inbox = getattr(queue, "steering_inbox", None)
        if queue is None or (inbox is not None and not inbox.current):
            raise RuntimeError("Model continuation requires an active steering queue on this SDK")
        if not self.has_pending_steering():
            self.push_steering("Continue the current task and provide the corrected final answer.")

    context_cls.request_model_continue = request_model_continue


def install_evolution_rail_kwargs_compat() -> None:
    """Install legacy rail constructor and steering-backed continuation bridges."""
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

    _install_model_continue_compat(AgentCallbackContext)
    try:
        from openjiuwen.harness.rails import (
            SkillCreateRail,
            SkillEvolutionRail,
            TeamSkillCreateRail,
            TeamSkillEvolutionRail,
        )
    except ImportError as exc:
        logger.debug("skip evolution rail kwargs compat: %s", exc)
        return

    for cls in (
        SkillEvolutionRail,
        TeamSkillEvolutionRail,
        SkillCreateRail,
        TeamSkillCreateRail,
    ):
        if isinstance(cls, type):
            _wrap_init_for_extra_kwargs(cls)
