# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Release native member callback ownership only after terminal quiescence."""
from __future__ import annotations

import weakref

from openjiuwen.agent_teams.harness.native_harness import NativeHarness
from openjiuwen.harness_protocol.state import HarnessState

_tracked = weakref.WeakKeyDictionary()


async def track_native_context_lifecycle(agent: object) -> None:
    """Observe public native lifecycle events without retaining the native."""
    if not isinstance(agent, NativeHarness):
        return
    if agent in _tracked:
        return
    _tracked[agent] = True
    reference = weakref.ref(agent)

    async def on_state(*, new: HarnessState) -> None:
        if new is not HarnessState.TERMINATED:
            return
        native = reference()
        if native is None:
            return
        if native.active_round is not None:
            return
        # TERMINATED is emitted after hard-cancel has joined the active round.
        # AgentCallbackManager uses a unique instance namespace even when card
        # IDs match. Clearing it cannot remove another member's callbacks.
        await native.agent_callback_manager.clear()
        react = native.react_agent
        if react is not None:
            await react.agent_callback_manager.clear()
        # Pinned Core also caches inspection metadata under bound-method keys.
        # Invalidate these derived caches after unregistering the dead owner;
        # live callbacks keep their registrations and recompute their metadata.
        from openjiuwen.core.runner.callback import framework, decorator

        for module, name in ((framework, "_get_signature"), (decorator, "_sig_param_info")):
            clear_cache = getattr(getattr(module, name, None), "cache_clear", None)
            if callable(clear_cache):
                clear_cache()

    try:
        await agent.subscribe(on_state=on_state)
        if agent.state is HarnessState.TERMINATED:
            await on_state(new=HarnessState.TERMINATED)
    except BaseException:
        _tracked.pop(agent, None)
        raise
