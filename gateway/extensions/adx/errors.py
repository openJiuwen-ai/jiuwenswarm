# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Routing errors shared by the ADX WebSocket and file adapters."""

from __future__ import annotations


class AdxRoutingError(RuntimeError):
    """A caller-visible routing or transport error; requests are never replayed."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
