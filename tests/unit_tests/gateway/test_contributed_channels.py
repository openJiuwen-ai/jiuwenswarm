# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""A package can add a channel or replace a built-in with operator consent."""

import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.extensions import overrides
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.gateway.channel_manager.base import ChannelType
from jiuwenswarm.extensions.channel_contributions import (
    ChannelSpec,
    contributed_channel_ids,
    contributed_spec_for,
    register_channel_spec,
    unregister_channel_spec,
)
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.im_pipeline.im_session_input import is_shared_im_channel
from jiuwenswarm.gateway.routing.keys import (
    SlackDeliveryTarget,
    WebDeliveryTarget,
    make_delivery_target,
)
from jiuwenswarm.runtime.cron.models import (
    is_valid_target_channel_id,
    normalize_target_channel_id,
)

STUB_ROOT = Path(__file__).parent / "contributed_channels" / "echo_slack"


@pytest.fixture(autouse=True)
def clean_contributions():
    yield
    for channel_id in contributed_channel_ids():
        unregister_channel_spec(channel_id)


@pytest.fixture
def allow_slack(monkeypatch):
    monkeypatch.setattr(overrides, "allowed_overrides", lambda: frozenset({"channels.slack"}))


def test_extension_replaces_builtin_delivery_with_consent(allow_slack):
    assert contributed_spec_for("slack") is None
    assert isinstance(make_delivery_target("slack"), SlackDeliveryTarget)
    asyncio.run(ExtensionLoader(registry=object()).load_extension(STUB_ROOT))

    spec = contributed_spec_for("slack")
    assert spec.source == "echo-slack 0.0.1"
    assert spec.replaces == "slack"
    assert spec.factory({"bot_token": "t"}, None)[0].channel_id == "slack"
    assert isinstance(make_delivery_target("slack"), SlackDeliveryTarget)


def test_replacement_requires_manifest_and_operator(monkeypatch):
    with pytest.raises(ValueError, match="replaces"):
        register_channel_spec(ChannelSpec("slack", factory=lambda *_: (), source="pkg"))
    monkeypatch.setattr(overrides, "allowed_overrides", frozenset)
    with pytest.raises(ValueError, match="extensions.allow_overrides"):
        register_channel_spec(ChannelSpec("slack", factory=lambda *_: (), replaces="slack", source="pkg"))
    assert contributed_channel_ids() == ()


def test_replacement_cannot_claim_another_id(allow_slack):
    with pytest.raises(ValueError, match="id it replaces"):
        register_channel_spec(ChannelSpec("matrix", factory=lambda *_: (), replaces="slack"))
    with pytest.raises(ValueError, match="not built in"):
        register_channel_spec(ChannelSpec("matrix", factory=lambda *_: (), replaces="matrix"))


def test_new_channel_has_delivery_and_duplicate_is_refused():
    def target(channel_id, **_):
        return WebDeliveryTarget(channel_id=channel_id, ws_id="matrix")

    register_channel_spec(ChannelSpec("matrix", factory=lambda *_: (), delivery=target, source="first"))
    assert make_delivery_target("matrix").ws_id == "matrix"
    with pytest.raises(ValueError, match="already contributed by first"):
        register_channel_spec(ChannelSpec("matrix", factory=lambda *_: (), source="second"))
    unregister_channel_spec("matrix")
    assert make_delivery_target("matrix").ws_id == ""


def test_new_channel_declares_cron_delivery_and_shared_im_input():
    assert not is_valid_target_channel_id("matrix")
    assert not is_shared_im_channel("matrix")
    register_channel_spec(ChannelSpec("matrix", factory=lambda *_: ()))
    assert not is_valid_target_channel_id("matrix")
    assert not is_shared_im_channel("matrix")
    unregister_channel_spec("matrix")

    register_channel_spec(ChannelSpec(
        "matrix",
        factory=lambda *_: (),
        delivery=lambda *_args, **_kwargs: None,
        shared_im_input=True,
    ))
    assert is_valid_target_channel_id("matrix")
    assert normalize_target_channel_id("matrix") == "matrix"
    assert is_shared_im_channel("matrix:room")
    unregister_channel_spec("matrix")
    assert not is_valid_target_channel_id("matrix")
    assert not is_shared_im_channel("matrix")


def test_contributed_channel_needs_a_factory():
    with pytest.raises(ValueError, match="factory"):
        register_channel_spec(ChannelSpec("matrix"))
    with pytest.raises(ValueError, match="channel id"):
        register_channel_spec(ChannelSpec(123, factory=lambda *_: ()))


def test_channel_registration_binds_agent_client():
    client = object()
    handler = type("Handler", (), {"agent_client": client})()
    manager = ChannelManager(handler)

    class Channel:
        channel_id = "matrix"
        app_id = "default"

        def on_message(self, _callback):
            pass

        def bind_agent_client(self, value):
            assert value is client

    manager.register_channel(Channel())
    manager.register_channel_with_inbound(Channel(), lambda _message: None)


def test_builtin_ids_need_explicit_replacement(allow_slack):
    for channel in ChannelType:
        if channel.value == "slack":
            continue
        with pytest.raises(ValueError, match="replaces"):
            register_channel_spec(ChannelSpec(channel.value, factory=lambda *_: (), source="pkg"))
