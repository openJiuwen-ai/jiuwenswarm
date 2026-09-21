# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Drop newer SDK kwargs when the installed openjiuwen package is older."""

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


def install_evolution_rail_kwargs_compat() -> None:
    """Older rails may lack signal_trigger / trajectory_span_processor parameters."""
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
