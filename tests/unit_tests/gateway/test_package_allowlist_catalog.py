# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""管理端 Hub 目录去掉企业版不提供的四个包。"""

from gateway.enterprise.config.manager_config.routers.application_config_routers import (
    _without_hidden_hub_items,
)


def test_catalog_route_omits_hidden_hub_assets() -> None:
    payload = {
        "items": [
            {"id": "794db1bfe8884d6897d62d3584894a59", "kind": "agent_template"},
            {"id": "asset-a", "kind": "agent_template"},
            {"id": "32c223d69b834c35b40bc3935c6f9f67", "kind": "agent_group"},
            {"id": "group-b", "kind": "agent_group"},
        ],
        "error": "",
    }
    kept = _without_hidden_hub_items(payload)
    assert [item["id"] for item in kept["items"]] == ["asset-a", "group-b"]
    assert payload["items"][0]["id"] == "794db1bfe8884d6897d62d3584894a59"
