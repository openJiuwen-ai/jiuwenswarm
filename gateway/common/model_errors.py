# Gateway 侧副本，源文件 jiuwenswarm/common/model_errors.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
"""Stable model-selection error codes shared by runtime and RPC layers."""

from __future__ import annotations

from typing import Any

MODEL_SELECTION_NOT_FOUND = "MODEL_SELECTION_NOT_FOUND"
MODEL_SELECTION_DISABLED = "MODEL_SELECTION_DISABLED"
MODEL_SELECTION_FORBIDDEN = "MODEL_SELECTION_FORBIDDEN"
MODEL_GROUP_INVALID = "MODEL_GROUP_INVALID"
MODEL_RUNTIME_UNAVAILABLE = "MODEL_RUNTIME_UNAVAILABLE"


class ModelSelectionError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.details}
