# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-adapter facade over a lazy Worker-owned, scope-isolated A2X lease."""

from __future__ import annotations

import asyncio
from typing import Any


def resource_key(config: dict, *, kind: str = "a2x-client"):
    from jiuwenswarm.runtime.kernel import KernelIdentity, ResourceKey, identity_reference

    loop = asyncio.get_running_loop()
    identity = KernelIdentity.for_agent(project_dir="", work_mode="a2x", definition="registry",
                                       capabilities=config.get("role"), configuration=config)
    # The entry's close_loop retains this loop, preventing id reuse while alive.
    return ResourceKey(identity, kind, str(config.get("base_url", "")).rstrip("/"), str(id(loop)),
                       identity_reference(config.get("api_key")), identity_reference(config))


class LazyA2XClient:
    def __init__(self, config: dict) -> None:
        self._config = dict(config)
        self._key = resource_key(config)
        self._lock = asyncio.Lock()
        self._lease = None
        self._closed = False
        # Registration belongs to this adapter, never to the shared transport.
        self._jiuwen_blank_agent_registration: dict[str, str] = {}

    @property
    def base_url(self):
        return self._config["base_url"].rstrip("/")

    @property
    def timeout(self):
        return self._config["timeout"]

    @property
    def api_key(self):
        return self._config["api_key"]

    async def _get_client(self):
        from jiuwenswarm.runtime.kernel import process_kernel

        async with self._lock:
            if self._closed:
                raise RuntimeError("A2X client lease is closed")
            if self._lease is None:
                def construct():
                    from jiuwenswarm.agents.harness.team.a2x.client import AsyncA2XRegistryClient

                    return AsyncA2XRegistryClient(**{name: self._config[name]
                        for name in ("base_url", "timeout", "api_key", "ownership_file")})

                async def prepare():
                    operation = asyncio.create_task(asyncio.to_thread(construct))
                    try:
                        return await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        # A synchronous constructor cannot be preempted. Retain
                        # and close its eventual resource before owner shutdown.
                        client = await operation
                        await client.aclose()
                        raise

                self._lease = await process_kernel().acquire_async(
                    self._key, prepare, close=lambda client: client.aclose())
            return self._lease.value

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def invoke(*args, **kwargs):
            client = await self._get_client()
            return await getattr(client, name)(*args, **kwargs)

        return invoke

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()

    async def aclose(self):
        async with self._lock:
            self._closed = True
            lease, self._lease = self._lease, None
        if lease is not None:
            await lease.release()


async def lease_a2x_client(config_base: dict[str, Any]) -> tuple[LazyA2XClient, dict]:
    from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import resolve_a2x_config

    config = resolve_a2x_config(config_base)
    return LazyA2XClient(config), config
