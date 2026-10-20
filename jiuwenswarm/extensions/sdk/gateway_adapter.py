# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Host adapter base class used by AgentServer extensions."""

from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)

__all__ = ["GatewayAdapter", "build_error_response"]
