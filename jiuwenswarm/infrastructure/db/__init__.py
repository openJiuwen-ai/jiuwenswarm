# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""企业库 / Gateway 基础设施配置入口。

``Database`` 请从 ``jiuwenswarm.infrastructure.db.database`` 导入，避免本包
``__init__`` 在缺 ``openjiuwen_runtime`` 的 CI 中被 settings 单测拖垮。
"""

from jiuwenswarm.infrastructure.db.settings import (
    Settings,
    get_settings,
    load_env,
)

__all__ = (
    "Settings",
    "get_settings",
    "load_env",
)
