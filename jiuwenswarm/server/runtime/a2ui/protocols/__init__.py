# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Versioned A2UI protocol registry exports."""

from jiuwenswarm.server.runtime.a2ui.protocol import (
    A2UIProtocolSpec,
    get_protocol_spec,
)

__all__ = ["A2UIProtocolSpec", "get_protocol_spec"]
