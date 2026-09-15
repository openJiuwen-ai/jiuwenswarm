# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime contract helpers for openjiuwen permission rails."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OpenJiuwenPermissionContract:
    """Runtime contract for the openjiuwen permission rail integration."""

    permission_rail_class: type[Any]
    permission_confirm_response_class: type[Any]
    before_tool_call_is_async: bool
    supports_update_config: bool
    supports_manual_approval_response: bool
    supports_permission_engine_factory: bool


def load_openjiuwen_permission_contract() -> OpenJiuwenPermissionContract:
    """Load and validate the openjiuwen permission rail contract."""
    try:
        from openjiuwen.harness.rails.security import PermissionInterruptRail
        from openjiuwen.harness.security import (
            PermissionConfirmResponse,
            build_permission_interrupt_rail,
        )
    except ImportError as exc:
        missing = exc.name or str(exc)
        raise RuntimeError(f"missing openjiuwen permission module: {missing}") from exc

    before_tool_call = getattr(PermissionInterruptRail, "before_tool_call", None)
    before_tool_call_is_async = inspect.iscoroutinefunction(before_tool_call)
    if not before_tool_call_is_async:
        raise RuntimeError("PermissionInterruptRail.before_tool_call must be async")

    return OpenJiuwenPermissionContract(
        permission_rail_class=PermissionInterruptRail,
        permission_confirm_response_class=PermissionConfirmResponse,
        before_tool_call_is_async=before_tool_call_is_async,
        supports_update_config=hasattr(PermissionInterruptRail, "update_config"),
        supports_manual_approval_response=True,
        supports_permission_engine_factory=callable(build_permission_interrupt_rail),
    )


def build_denied_permission_response(reason: str) -> Any:
    """Build the canonical denied permission response for openjiuwen rails."""
    try:
        from openjiuwen.harness.security import PermissionConfirmResponse
    except ImportError as exc:
        missing = exc.name or str(exc)
        raise RuntimeError(f"missing openjiuwen permission module: {missing}") from exc

    return PermissionConfirmResponse(
        approved=False,
        auto_confirm=False,
        feedback=f"[PERMISSION_DENIED] {reason}",
    )


def build_rejected_permission_response(reason: str) -> Any:
    """Build the canonical user-rejected permission response."""
    try:
        from openjiuwen.harness.security import PermissionConfirmResponse
    except ImportError as exc:
        missing = exc.name or str(exc)
        raise RuntimeError(f"missing openjiuwen permission module: {missing}") from exc

    return PermissionConfirmResponse(
        approved=False,
        auto_confirm=False,
        feedback=f"[PERMISSION_REJECTED] {reason}",
    )


def build_manual_approval_required_response(
    reason: str,
    metadata: dict[str, Any] | None = None,
    *,
    prompt_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical manual approval interrupt response."""
    value: dict[str, Any] = {
        "message": reason,
        "tool_name": "auto_permission",
    }
    if prompt_payload:
        value.update(prompt_payload)
    if metadata:
        value.update(metadata)
    response: dict[str, Any] = {
        "id": "auto_permission_manual_approval_required",
        "interrupt": True,
        "value": value,
    }
    if metadata:
        response["metadata"] = dict(metadata)
    return response


def classify_permission_result(result: Any) -> str:
    """Classify an openjiuwen before_tool_call result."""
    if result is None:
        return "allow"

    if _is_user_rejection_payload(result):
        return "user_rejection"
    if _is_permission_denied_payload(result):
        return "denied"

    approved = getattr(result, "approved", None)
    if approved is True:
        return "allow"
    if approved is False:
        return "denied"

    if isinstance(result, dict):
        if result.get("interrupt") is True:
            return "interrupt"
        if "id" in result and "value" in result:
            return "interrupt"

    return "interrupt"


def is_allow_result(result: Any) -> bool:
    """Return whether the base rail result means execution may continue."""
    return classify_permission_result(result) == "allow"


def is_denied_result(result: Any) -> bool:
    """Return whether the base rail result means execution was denied."""
    return classify_permission_result(result) == "denied"


def is_interrupt_result(result: Any) -> bool:
    """Return whether the base rail result means manual interaction is pending."""
    return classify_permission_result(result) == "interrupt"


def is_user_rejection_result(result: Any) -> bool:
    """Return whether the result represents a user rejection or cancellation."""
    return classify_permission_result(result) == "user_rejection"


def _is_user_rejection_payload(result: Any) -> bool:
    feedback = ""
    if isinstance(result, str):
        feedback = result
    elif isinstance(result, dict):
        raw_feedback = result.get("feedback")
        if raw_feedback is None and isinstance(result.get("value"), dict):
            raw_feedback = result["value"].get("feedback")
        feedback = str(raw_feedback or "")
    else:
        feedback = str(getattr(result, "feedback", "") or "")

    normalized = feedback.lower()
    if "[permission_rejected]" in normalized:
        return True
    if isinstance(result, str):
        return False
    return "cancelled" in normalized or "canceled" in normalized


def _is_permission_denied_payload(result: Any) -> bool:
    feedback = ""
    if isinstance(result, str):
        feedback = result
    elif isinstance(result, dict):
        raw_feedback = result.get("feedback")
        if raw_feedback is None and isinstance(result.get("value"), dict):
            raw_feedback = result["value"].get("feedback")
        feedback = str(raw_feedback or "")
    else:
        feedback = str(getattr(result, "feedback", "") or "")

    return "[permission_denied]" in feedback.lower()
