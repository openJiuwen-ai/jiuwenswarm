# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""permissions_config：Repository + access（yaml ``/permissions`` 段）。"""

from jiuwenswarm.gateway.config.permissions.repository import (
    PERMISSIONS_CONFIG_STORE_NAME,
    PermissionsConfigRepository,
)
from jiuwenswarm.gateway.config.section import (
    SectionDocument,
    YamlSectionCodec,
)

__all__ = [
    "PERMISSIONS_CONFIG_STORE_NAME",
    "PermissionsConfigRepository",
    "SectionDocument",
    "YamlSectionCodec",
]
