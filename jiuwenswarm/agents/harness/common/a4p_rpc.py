# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility shim — prefer ``a4p.rpc``."""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.a4p.rpc import (
    dispatch_a4p_request,
    get_a4p_req_methods,
)

__all__ = ["dispatch_a4p_request", "get_a4p_req_methods"]
