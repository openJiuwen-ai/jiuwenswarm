# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Package management facade exposed to the AgentServer assembly."""

from jiuwenswarm.extensions_v2.management.registry import (
    CODE_MANAGER_NOT_INITIALIZED,
    ExtensionManager,
)

__all__ = ["CODE_MANAGER_NOT_INITIALIZED", "ExtensionManager"]
