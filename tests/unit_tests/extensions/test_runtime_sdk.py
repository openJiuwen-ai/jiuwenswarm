"""Extension runtime imports must preserve host class identity."""

import pytest

from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk import channel_runtime, gateway_adapter, im_runtime
from jiuwenswarm.gateway.channel_manager.base import (
    BaseChannel,
    ChannelMetadata,
    RobotMessageRouter,
)
from jiuwenswarm.gateway.im_pipeline.im_inbound import IMHistoryMessage
from jiuwenswarm.gateway.routing.keys import (
    SlackDeliveryTarget,
    make_builtin_delivery_target,
)
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)


def test_channel_runtime_uses_host_types():
    assert channel_runtime.BaseChannel is BaseChannel
    assert channel_runtime.ChannelMetadata is ChannelMetadata
    assert channel_runtime.RobotMessageRouter is RobotMessageRouter
    assert channel_runtime.RoutingTarget is RoutingTarget
    assert channel_runtime.SlackDeliveryTarget is SlackDeliveryTarget
    assert channel_runtime.make_builtin_delivery_target is make_builtin_delivery_target
    assert isinstance(channel_runtime.make_builtin_delivery_target("slack", chat_id="C1"), SlackDeliveryTarget)


def test_gateway_adapter_uses_host_contract():
    assert gateway_adapter.GatewayAdapter is GatewayAdapter
    assert gateway_adapter.build_error_response is build_error_response


def test_im_history_message_is_shared_with_host_pipeline():
    assert im_runtime.IMHistoryMessage is IMHistoryMessage
    record = im_runtime.IMHistoryMessage("U1", "Alice", "hello", 123)
    assert record.content == "hello"


def test_registry_stores_one_cron_hook_per_channel(monkeypatch):
    registry = ExtensionRegistry(None, {}, None)
    monkeypatch.setattr(ExtensionRegistry, "_instance", registry)
    hooks = object()
    registry.register_cron_hooks("slack", hooks)
    assert ExtensionRegistry.cron_hooks() == {"slack": hooks}
    with pytest.raises(ValueError, match="already registered"):
        registry.register_cron_hooks("slack", hooks)
