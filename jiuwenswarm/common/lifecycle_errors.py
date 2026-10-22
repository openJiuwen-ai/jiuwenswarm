# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session lifecycle exception types shared by Gateway and AgentServer."""

from __future__ import annotations


class LifecycleError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}
