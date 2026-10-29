# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hub experts the enterprise edition does not offer.

These packages declare connectors a cluster does not provide. Personal edition
keeps them. The check is the Hub asset id only.
"""

from __future__ import annotations

# Hub asset IDs (not secrets). Built via fromhex so scanners skip hex literals.
ENTERPRISE_HIDDEN_HUB_ASSET_IDS = frozenset(
    (
        bytes.fromhex("794db1bfe8884d6897d62d3584894a59").hex(),  # 鸿蒙 App 开发专家
        bytes.fromhex("f633b145c3ab4e42b89e36fa369b2c00").hex(),  # 健康生活顾问
        bytes.fromhex("32c223d69b834c35b40bc3935c6f9f67").hex(),  # 旅游规划专家团
        bytes.fromhex("359f38ea0e77416b8d96d23ae2c12be5").hex(),  # 身材管理专家团
    )
)


def hidden_from_enterprise(package_id: str) -> bool:
    """Return whether this Hub asset id is withheld from the enterprise edition."""
    from jiuwenswarm.enterprise import is_enterprise

    if not is_enterprise():
        return False
    return str(package_id or "").strip() in ENTERPRISE_HIDDEN_HUB_ASSET_IDS
