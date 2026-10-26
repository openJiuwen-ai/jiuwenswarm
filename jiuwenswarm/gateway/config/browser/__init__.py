# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""browser_config：Repository + access。"""

from jiuwenswarm.gateway.config.browser.repository import (
    BROWSER_CONFIG_STORE_NAME,
    BrowserConfigRepository,
)
from jiuwenswarm.gateway.config.section import (
    DbBodySectionCodec,
    SectionDocument,
    YamlSectionCodec,
)

__all__ = [
    "BROWSER_CONFIG_STORE_NAME",
    "BrowserConfigRepository",
    "DbBodySectionCodec",
    "SectionDocument",
    "YamlSectionCodec",
]
