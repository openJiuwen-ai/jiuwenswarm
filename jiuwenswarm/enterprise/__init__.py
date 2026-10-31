# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业运行时平面：组装入口与宿主钩子.

- 产品形态闸门：``is_enterprise()``（只读 edition，统一从此包导出）
- 平面能力：``get_enterprise_plane`` / ``enterprise_enabled`` / ``require_enterprise_plane``
  （仅当代码真正依赖平面时使用；企业版组装失败会抛错，不回退个人版）
"""

from jiuwenswarm.enterprise.edition import is_enterprise
from jiuwenswarm.enterprise.plane import (
    EnterprisePlane,
    EnterprisePlaneError,
    enterprise_enabled,
    get_enterprise_plane,
    require_enterprise_plane,
    reset_enterprise_plane,
    resolve_tenant_strategy,
    try_build_enterprise_plane,
)

__all__ = [
    "EnterprisePlane",
    "EnterprisePlaneError",
    "enterprise_enabled",
    "get_enterprise_plane",
    "is_enterprise",
    "require_enterprise_plane",
    "reset_enterprise_plane",
    "resolve_tenant_strategy",
    "try_build_enterprise_plane",
]
