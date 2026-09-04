"""Stable model-selection error codes shared by runtime and RPC layers."""

from __future__ import annotations

from typing import Any


MODEL_SELECTION_NOT_FOUND = "MODEL_SELECTION_NOT_FOUND"
MODEL_SELECTION_DISABLED = "MODEL_SELECTION_DISABLED"
MODEL_SELECTION_FORBIDDEN = "MODEL_SELECTION_FORBIDDEN"
MODEL_GROUP_INVALID = "MODEL_GROUP_INVALID"
MODEL_GROUP_NO_AVAILABLE_ROUTE = "MODEL_GROUP_NO_AVAILABLE_ROUTE"
MODEL_REQUEST_CONFIG_INVALID = "MODEL_REQUEST_CONFIG_INVALID"
MODEL_SELECTION_REFERENCED = "MODEL_SELECTION_REFERENCED"
MODEL_RUNTIME_UNAVAILABLE = "MODEL_RUNTIME_UNAVAILABLE"
TEAM_MODEL_SELECTION_STALE = "TEAM_MODEL_SELECTION_STALE"


class ModelSelectionError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.details}

