# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 企业能力平面（独立于 jiuwenswarm.enterprise）.

- 产品形态闸门：``is_enterprise()``（只读 edition，统一从此包导出）
- 平面能力：``get_enterprise_plane`` / ``enterprise_enabled`` / ``require_enterprise_plane``
  （仅当代码真正依赖平面时使用；企业版组装失败会抛错，不回退个人版）
"""

from gateway.enterprise.edition import is_enterprise
from gateway.enterprise.plane import (
    EnterprisePlaneError,
    GatewayEnterprisePlane,
    enterprise_enabled,
    get_enterprise_plane,
    require_enterprise_plane,
    reset_enterprise_plane,
    try_build_enterprise_plane,
)

__all__ = [
    "EnterprisePlaneError",
    "GatewayEnterprisePlane",
    "enterprise_enabled",
    "get_enterprise_plane",
    "is_enterprise",
    "require_enterprise_plane",
    "reset_enterprise_plane",
    "try_build_enterprise_plane",
]
