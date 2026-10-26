# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentOS extension entry (discovered by ExtensionLoader)."""

from jiuwenswarm.extensions.agentos.agentos_router.extension import (
    AgentOSRouter,
    register_extensions,
)

__all__ = ["AgentOSRouter", "register_extensions"]
