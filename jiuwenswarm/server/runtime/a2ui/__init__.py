# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A2UI feature package.

This package owns jiuwenswarm's A2UI integration. Core agentserver code should
depend on the public helpers exposed here instead of reaching into protocol
or renderer details.
"""

from jiuwenswarm.server.runtime.a2ui.config import A2UIConfig, get_a2ui_config, is_a2ui_enabled

__all__ = [
    "A2UIConfig",
    "get_a2ui_config",
    "is_a2ui_enabled",
]
