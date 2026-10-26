# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""request_identity：user_id 顶层 + routing(group/bot/gateway)。"""

from __future__ import annotations

from jiuwenswarm.common.request_identity import (
    ROUTING_METADATA_KEY,
    apply_routing_metadata,
    merge_routing_into_params,
    normalize_routing_identity,
    web_routing_identity,
)


def test_normalize_routing_identity_first_wins() -> None:
    identity = normalize_routing_identity(
        {"user_id": ["u1"], "bot_id": ["b1"]},
        {"user_id": "u2", "group_id": "g1", "gateway_id": "gw1"},
    )
    assert identity == {
        "user_id": "u1",
        "bot_id": "b1",
        "group_id": "g1",
        "gateway_id": "gw1",
    }


def test_apply_routing_metadata_splits_user_id_and_routing() -> None:
    meta = apply_routing_metadata(
        {
            "method": "command.goal",
            "user_id": "stale",
            "bot_id": "stale",
            "query": {"bot_id": ["wire"]},
        },
        {"user_id": "u1", "bot_id": "b1", "group_id": "g1"},
    )
    assert meta["user_id"] == "u1"
    assert meta[ROUTING_METADATA_KEY] == {
        "bot_id": "b1",
        "group_id": "g1",
    }
    assert "bot_id" not in meta
    assert "user_id" not in meta[ROUTING_METADATA_KEY]
    assert meta["method"] == "command.goal"
    assert meta["query"] == {"bot_id": ["wire"]}


def test_web_routing_identity_reads_top_level_user_and_routing() -> None:
    identity = web_routing_identity(
        {
            "user_id": "from_top",
            "routing": {"bot_id": "from_routing", "user_id": "ignored"},
            "query": {"bot_id": ["from_query"]},
            "bot_id": "from_top_bot",
        }
    )
    assert identity == {"user_id": "from_top", "bot_id": "from_routing"}
    # 不兼容 routing.user_id
    assert web_routing_identity({"routing": {"user_id": "legacy_only"}}) == {}
    assert web_routing_identity({"query": {"bot_id": ["only_query"]}}) == {}


def test_merge_into_params_for_local_handlers_only() -> None:
    params = merge_routing_into_params(
        {"action": "list"},
        {"user_id": "u1", "routing": {"bot_id": "b1"}},
    )
    assert params == {"action": "list", "bot_id": "b1", "user_id": "u1"}
