# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import (
    PublishIdentity,
    PublishProtocolError,
)
from jiuwenswarm.server.runtime.marketplace.hub_publish_port import parse_publish_result


@pytest.fixture
def payload():
    return dict(
        asset_type="agent-plugin",
        plugin_type="agent-plugin",
        asset_id="asset-1",
        plugin_id="asset-1",
        name="sales-assistant",
        version="1.0.0",
        status="ACTIVE",
        publish_result="pending_moderation",
        visibility="public",
        deduplicated=False,
    )


@pytest.mark.parametrize(
    "state",
    [
        "pending_moderation",
        "reviewing",
        "publish_success",
        "publish_failed",
        "future-state",
        None,
    ],
)
def test_preserves_market_result_without_interpreting_active(payload, state):
    payload["publish_result"] = state
    result = parse_publish_result(
        payload, PublishIdentity("plugin", "sales-assistant", "1.0.0")
    )
    assert result.publish_result == state


@pytest.mark.parametrize(
    "key,value",
    [
        ("plugin_id", "another-id"),
        ("name", "other"),
        ("version", "2.0.0"),
        ("deduplicated", "false"),
    ],
)
def test_rejects_inconsistent_result(payload, key, value):
    payload[key] = value
    with pytest.raises(PublishProtocolError):
        parse_publish_result(
            payload, PublishIdentity("plugin", "sales-assistant", "1.0.0")
        )


def test_update_cannot_return_another_asset(payload):
    with pytest.raises(PublishProtocolError):
        parse_publish_result(
            payload, PublishIdentity("plugin", "sales-assistant", "1.0.0", "asset-2")
        )


@pytest.mark.parametrize(
    "kind,hub_type",
    [
        ("skill", "skill"),
        ("skill", "swarmskill"),
        ("skill", "teamskills"),
        ("plugin", "agent-plugin"),
        ("agent_template", "agent-template"),
        ("agent_group", "agent-template"),
        ("mcp", "agent-mcp"),
    ],
)
def test_matches_resource_type(payload, kind, hub_type):
    payload.update(asset_type=hub_type, plugin_type=hub_type)
    assert (
        parse_publish_result(
            payload, PublishIdentity(kind, "sales-assistant", "1.0.0")
        ).identity.kind
        == kind
    )


@pytest.mark.parametrize(
    "types",
    [
        dict(asset_type="agent-mcp"),
        dict(plugin_type="agent-template"),
        dict(asset_type="agent-plugin", plugin_type="agent-mcp"),
        dict(asset_type="agent-plugin", plugin_type="unexpected"),
    ],
)
def test_rejects_wrong_or_conflicting_type(payload, types):
    payload.update(types)
    with pytest.raises(PublishProtocolError):
        parse_publish_result(
            payload, PublishIdentity("plugin", "sales-assistant", "1.0.0")
        )


def test_requires_resource_type(payload):
    payload.pop("asset_type")
    payload.pop("plugin_type")
    with pytest.raises(PublishProtocolError):
        parse_publish_result(
            payload, PublishIdentity("plugin", "sales-assistant", "1.0.0")
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("asset_id", " asset-1"),
        ("visibility", "internal"),
        ("publish_result", {}),
        ("deduplicated", 1),
    ],
)
def test_rejects_malformed_fields_without_echoing_payload(payload, key, value):
    payload.update(
        asset_type="agent-plugin", plugin_type="agent-plugin", secret="do-not-return"
    )
    payload[key] = value
    with pytest.raises(PublishProtocolError) as exc:
        parse_publish_result(
            payload, PublishIdentity("plugin", "sales-assistant", "1.0.0")
        )
    assert "do-not-return" not in str(exc.value)


def test_cannot_change_confirmed_visibility(payload):
    payload.update(asset_type="agent-plugin", plugin_type="agent-plugin")
    with pytest.raises(PublishProtocolError):
        parse_publish_result(
            payload,
            PublishIdentity("plugin", "sales-assistant", "1.0.0"),
            expected_visibility="private",
        )


@pytest.mark.parametrize("subtype", ["skill", "swarmskill", "teamskills"])
def test_skill_upload_uses_generic_asset_category_and_exact_plugin_subtype(
    payload, subtype
):
    payload.update(asset_type="plugin", plugin_type=subtype, visibility=None)
    result = parse_publish_result(
        payload, PublishIdentity("skill", "sales-assistant", "1.0.0")
    )
    assert result.publish_result == "pending_moderation"
    assert result.visibility is None


def test_generic_category_does_not_hide_wrong_subtype(payload):
    payload.update(asset_type="plugin", plugin_type="agent-plugin", visibility=None)
    with pytest.raises(PublishProtocolError):
        parse_publish_result(
            payload, PublishIdentity("skill", "sales-assistant", "1.0.0")
        )
