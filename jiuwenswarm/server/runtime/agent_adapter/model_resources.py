# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process lifetime ownership for SDK providers' shared HTTP connections."""

from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass, replace
from typing import Any


_LIFETIME_LOCK = threading.RLock()


@dataclass(frozen=True)
class _ConnectionScope:
    provider: Any
    config: Any
    loop: asyncio.AbstractEventLoop | None


class _ProviderConnections:
    """Keep shared pools until every Runtime has reached Kernel shutdown.

    Closing an individual model must not close a provider's shared pool: another
    model, Team member or in-flight request may still use that connection. This
    Kernel resource therefore belongs to the process lifetime and is closed
    only by the Kernel shutdown barrier after execution owners have stopped.
    """

    def __init__(self) -> None:
        self._scopes: dict[tuple[Any, Any], _ConnectionScope] = {}

    def register(self, provider: Any, config: Any) -> None:
        provider_type = type(provider)
        connection_key = getattr(provider_type, "connection_key", None)
        close_connections = getattr(provider_type, "aclose_connections", None)
        if not callable(connection_key) or not callable(close_connections):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        key = (provider_type, connection_key(config))
        self._scopes.setdefault(key, _ConnectionScope(provider_type, config, loop))

    async def close(self) -> None:
        with _LIFETIME_LOCK:
            scopes = list(self._scopes.items())
        failures = []
        for key, scope in scopes:

            async def close_scope(owned: _ConnectionScope = scope) -> None:
                await owned.provider.aclose_connections([owned.config])

            try:
                owner = scope.loop
                if owner is None or owner is asyncio.get_running_loop():
                    await close_scope()
                elif owner.is_running():
                    completed = asyncio.run_coroutine_threadsafe(close_scope(), owner)
                    await asyncio.wrap_future(completed)
                else:
                    raise RuntimeError(
                        "model connection cleanup event loop is unavailable"
                    )
            except Exception as exc:
                failures.append(exc)
            else:
                with _LIFETIME_LOCK:
                    self._scopes.pop(key, None)
        if failures:
            raise RuntimeError(
                f"model connection cleanup failed for {len(failures)} scope(s)"
            ) from failures[0]


def register_model_connections(kernel: Any, identity: Any, model: Any) -> None:
    provider = getattr(model, "_client", None)
    config = getattr(model, "model_client_config", None)
    if provider is None or config is None:
        return
    provider_type = type(provider)
    if not callable(getattr(provider_type, "aclose_connections", None)):
        return
    from jiuwenswarm.runtime.kernel import ResourceKey

    # Profile/config retirement cannot match this process-scoped identity.
    owner_identity = replace(
        identity,
        project_dir="",
        work_mode="__model_connections__",
        agent_definition="",
        capabilities="",
        configuration="",
    )
    key = ResourceKey(owner_identity, "model_connections", "process", "", "", "")
    with _LIFETIME_LOCK:
        owner = kernel.own_lifetime(
            key, _ProviderConnections, close=lambda value: value.close()
        )
        owner.register(provider, config)


async def close_model_entry(model: Any) -> None:
    """Close a model's own public resource; shared provider pools have an owner."""
    close = getattr(model, "aclose", None)
    if not callable(close):
        close = getattr(model, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result
