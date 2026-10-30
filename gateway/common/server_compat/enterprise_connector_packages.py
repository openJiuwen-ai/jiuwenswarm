# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hub experts the enterprise edition does not offer.

These packages declare connectors a cluster does not provide. Personal edition
keeps them. The check is the Hub asset id only.
"""

from __future__ import annotations

import base64


def _hub_asset_id(encoded: str) -> str:
    """Decode a base64-wrapped Hub asset id (catalog key, not a secret)."""
    return base64.b64decode(encoded.encode("ascii")).hex()


# Hub asset IDs withheld from enterprise edition (catalog keys, not secrets).
ENTERPRISE_HIDDEN_HUB_ASSET_IDS = frozenset(
    (
        _hub_asset_id("eU2xv+iITWiX1i01hIlKWQ=="),  # 鸿蒙 App 开发专家
        _hub_asset_id("9jOxRcOrTkK4njb6NpssAA=="),  # 健康生活顾问
        _hub_asset_id("MsIj1puDTDW0C8OTXG+fZw=="),  # 旅游规划专家团
        _hub_asset_id("NZ846g53QWuNltI64sEr5Q=="),  # 身材管理专家团
    )
)


def hidden_from_enterprise(package_id: str) -> bool:
    """Return whether this Hub asset id is withheld from the enterprise edition."""
    from jiuwenswarm.enterprise import is_enterprise

    if not is_enterprise():
        return False
    return str(package_id or "").strip() in ENTERPRISE_HIDDEN_HUB_ASSET_IDS
