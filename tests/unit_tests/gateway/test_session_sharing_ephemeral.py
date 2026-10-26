# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey, WebDeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import SessionSharingRegistry, SubRole
from jiuwenswarm.gateway.storage.backends.memory_ephemeral import MemoryEphemeralBackend
from jiuwenswarm.gateway.storage.state.sharing_codec import (
    SUBSCRIPTIONS_HASH,
    subscription_from_bytes,
    subscription_to_bytes,
)


@pytest.mark.asyncio
async def test_session_sharing_ephemeral_roundtrip() -> None:
    ephemeral = MemoryEphemeralBackend("session_sharing")
    registry = SessionSharingRegistry(ephemeral=ephemeral)
    rk = RoutingKey(
        user_id="u1",
        channel_id="web",
        app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"),
        session_id="sess-1",
    )
    delivery = WebDeliveryTarget(ws_id="ws-1")
    sub = await registry.register("sess-1", SubRole.GODVIEW, rk, delivery)
    rows = await ephemeral.hgetall(SUBSCRIPTIONS_HASH)
    assert sub.sub_id in rows
    restored = subscription_from_bytes(rows[sub.sub_id])
    assert restored.member_name == SubRole.GODVIEW
    assert restored.routing_key.session_id == "sess-1"

    await registry.unregister(sub.sub_id)
    assert await ephemeral.hgetall(SUBSCRIPTIONS_HASH) == {}


@pytest.mark.asyncio
async def test_session_sharing_hydrate_from_ephemeral() -> None:
    ephemeral = MemoryEphemeralBackend("session_sharing")
    rk = RoutingKey(
        user_id="u1",
        channel_id="web",
        app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"),
        session_id="sess-2",
    )
    delivery = WebDeliveryTarget(ws_id="ws-2")
    from jiuwenswarm.gateway.routing.session_sharing import Subscription

    sub = Subscription(
        sub_id="abc123",
        member_name="reviewer-1",
        routing_key=rk,
        delivery=delivery,
    )
    await ephemeral.hset(SUBSCRIPTIONS_HASH, sub.sub_id, subscription_to_bytes(sub))

    registry = SessionSharingRegistry(ephemeral=ephemeral)
    await registry.hydrate_from_ephemeral()
    assert registry.lookup_member("sess-2", "reviewer-1")[0].sub_id == "abc123"
