# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A4P agent tools."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.a4p_runtime import (
    get_a4p_runtime,
    is_a4p_enabled,
    normalize_a4p_action_params,
    resolve_a4p_identity,
)
from jiuwenswarm.agents.harness.common.a4p_execution_context import (
    AUTHORIZATION_EXECUTION_CONTEXTS,
)
from jiuwenswarm.common.config import get_config


_ACTION_SCOPE_POLICY: dict[str, tuple[str, ...]] = {
    "bash": ("command",),
    "mcp_exec_command": ("command",),
    "create_terminal": ("cmd",),
    "write_file": ("file_path",),
    "edit_file": ("file_path",),
    "acp_chat": ("agent",),
}
_FILE_SCOPE_TOOLS = frozenset({"write_file", "edit_file"})
_GLOB_CHARS = frozenset("*?[")


def get_a4p_supported_action_names() -> tuple[str, ...]:
    return tuple(_ACTION_SCOPE_POLICY)


def _action_scope_schema_description() -> str:
    supported = ", ".join(
        f"{tool} -> {' or '.join(fields)}"
        for tool, fields in _ACTION_SCOPE_POLICY.items()
    )
    return (
        "Non-empty security-critical constraints using real tool schema keys. "
        f"Supported tool fields: {supported}. Other ordinary call parameters are allowed automatically. "
        "For Shell tools, authorize each complete exact command by default. Use a narrowly scoped wildcard "
        "only when one dynamic argument cannot be known in advance and the wildcard cannot absorb whitespace, "
        "Shell operators, redirections, or extra arguments. For file tools, file_path must be a concrete filename "
        "or a filename glob under a fixed directory; a directory path alone is invalid. "
        "Never use a bare * as the entire parameter or submit unresolved placeholders as scope constraints. "
        "Runtime values need not be known yet if their allowed scope can already be expressed."
    )


class _ActionScopeError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        action_index: int | None = None,
        action_name: str | None = None,
        required_scope_fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.action_index = action_index
        self.action_name = action_name
        self.required_scope_fields = required_scope_fields


def _has_scope_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_valid_file_scope(value: Any) -> bool:
    if not _has_scope_value(value):
        return False
    normalized = value.strip().replace("\\", "/")
    directory, separator, filename = normalized.rpartition("/")
    if not filename or any(char in directory for char in _GLOB_CHARS):
        return False
    return bool(separator) or not any(char in filename for char in _GLOB_CHARS)


def _agent_intent_summary(intent: Any) -> Any:
    if not isinstance(intent, dict):
        return intent
    summary = dict(intent)
    summary["actions"] = [
        {
            key: value
            for key, value in action.items()
            if key != "allowExtraParams"
        }
        for action in intent.get("actions") or []
        if isinstance(action, dict)
    ]
    return summary


def _normalize_actions(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, list) or not actions:
        raise _ActionScopeError(
            code="A4P_ACTION_SCOPE_INVALID",
            message="actions must be a non-empty list",
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(actions):
        if not isinstance(item, dict):
            raise _ActionScopeError(
                code="A4P_ACTION_SCOPE_INVALID",
                message=f"action at index {index} must be an object",
                action_index=index,
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise _ActionScopeError(
                code="A4P_ACTION_SCOPE_INVALID",
                message=f"action at index {index} is missing name",
                action_index=index,
            )
        required_fields = _ACTION_SCOPE_POLICY.get(name)
        if required_fields is None:
            raise _ActionScopeError(
                code="A4P_ACTION_SCOPE_UNSUPPORTED",
                message=(
                    f"A4P intent scope is not configured for tool: {name}; "
                    "use the normal tool approval flow"
                ),
                action_index=index,
                action_name=name,
            )
        raw_params = item.get("params")
        if not isinstance(raw_params, dict):
            raise _ActionScopeError(
                code="A4P_ACTION_SCOPE_INVALID",
                message=f"action {name} at index {index} requires a non-empty params object",
                action_index=index,
                action_name=name,
                required_scope_fields=required_fields,
            )
        params = dict(raw_params)
        # Ignore obsolete Agent input while keeping the SDK protocol internal.
        params.pop("allowExtraParams", None)
        params = normalize_a4p_action_params(name, params)
        if not params:
            raise _ActionScopeError(
                code="A4P_ACTION_SCOPE_INVALID",
                message=f"action {name} at index {index} requires non-empty params",
                action_index=index,
                action_name=name,
                required_scope_fields=required_fields,
            )
        if not any(_has_scope_value(params.get(field)) for field in required_fields):
            fields = " or ".join(required_fields)
            raise _ActionScopeError(
                code="A4P_ACTION_SCOPE_INVALID",
                message=(
                    f"action {name} at index {index} must constrain a non-empty {fields}"
                ),
                action_index=index,
                action_name=name,
                required_scope_fields=required_fields,
            )
        if name in _FILE_SCOPE_TOOLS and not _is_valid_file_scope(params.get("file_path")):
            raise _ActionScopeError(
                code="A4P_ACTION_SCOPE_INVALID",
                message=(
                    f"action {name} at index {index} file_path must be a concrete filename "
                    "or a filename glob under a fixed directory; a directory path alone is invalid"
                ),
                action_index=index,
                action_name=name,
                required_scope_fields=required_fields,
            )
        normalized.append(
            {
                "name": name,
                "params": params,
                "allowExtraParams": True,
            }
        )
    return normalized


async def _resolve_cron_authorization_target(cron_job_id: str) -> dict[str, Any] | None:
    from jiuwenswarm.gateway.cron.store import CronJobStore

    job = await CronJobStore().get_job(cron_job_id)
    if job is None:
        return None
    return {
        "type": "cronJob",
        "cronJobId": job.id,
        "name": job.name,
        "description": job.description,
        "persistent": True,
        "enabled": bool(job.enabled),
        "activationRequiredAfterApproval": True,
        "schedule": {
            "cronExpression": job.cron_expr,
            "timezone": job.timezone,
        },
    }


async def request_a4p_intent_authorization(
    actions: list[dict[str, Any]],
    validity_seconds: int | None = None,
    cron_job_id: str | None = None,
    reason: str | None = None,
    *,
    _session_id: str | None = None,
) -> dict[str, Any]:
    """Request a user-authorized A4P intent token for future tool calls."""
    if not is_a4p_enabled(get_config()):
        return {
            "ok": False,
            "error": "A4P is disabled",
            "code": "A4P_DISABLED",
        }
    session_id = str(_session_id or "").strip()
    execution_context = AUTHORIZATION_EXECUTION_CONTEXTS.get(session_id)
    if execution_context is None or not execution_context.is_interactive_web:
        return {
            "ok": False,
            "error": "A4P intent authorization requires an interactive Web session",
            "code": "A4P_WEB_SESSION_REQUIRED",
        }
    channel_id = execution_context.channel_id
    try:
        normalized_actions = _normalize_actions(actions)
    except _ActionScopeError as exc:
        AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
            session_id,
            request_id=execution_context.request_id,
            status="failed",
            error=str(exc),
        )
        return {
            "ok": False,
            "error": str(exc),
            "code": exc.code,
            "actionIndex": exc.action_index,
            "actionName": exc.action_name,
            "requiredScopeFields": list(exc.required_scope_fields),
        }
    metadata = execution_context.metadata
    identity = resolve_a4p_identity(
        metadata=metadata,
        agent_name=execution_context.agent_id,
    )
    validity = None
    if validity_seconds is not None:
        validity = max(1, int(validity_seconds))

    cron_job_id = str(cron_job_id or "").strip()
    cron_target = None
    if cron_job_id:
        cron_target = await _resolve_cron_authorization_target(cron_job_id)
        if cron_target is None:
            AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
                session_id,
                request_id=execution_context.request_id,
                status="failed",
                error=f"Cron job not found: {cron_job_id}",
            )
            return {
                "ok": False,
                "error": f"Cron job not found: {cron_job_id}",
                "code": "CRON_JOB_NOT_FOUND",
            }
        if cron_target["enabled"]:
            error = f"Cron job must be disabled before A4P authorization: {cron_job_id}"
            AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
                session_id,
                request_id=execution_context.request_id,
                status="failed",
                error=error,
            )
            return {
                "ok": False,
                "error": error,
                "code": "CRON_JOB_MUST_BE_DISABLED",
            }

    runtime = get_a4p_runtime()
    AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
        session_id,
        request_id=execution_context.request_id,
        status="pending",
    )
    try:
        payload = await runtime.request_intent_authorization(
            session_id=session_id,
            channel_id=channel_id,
            identity=identity,
            actions=normalized_actions,
            cron_job_id=cron_job_id or None,
            cron_authorization_target=cron_target,
            validity_seconds=validity,
            reason=reason,
            authorizer_route=execution_context.authorizer_route,
        )
    except Exception as exc:  # noqa: BLE001
        AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
            session_id,
            request_id=execution_context.request_id,
            status="failed",
            error=str(exc),
        )
        return {
            "ok": False,
            "error": f"A4P intent authorization failed: {exc}",
            "code": "A4P_AUTHORIZATION_FAILED",
        }
    token = payload.get("intentToken") if isinstance(payload.get("intentToken"), dict) else {}
    approved = bool(payload.get("approved")) and bool(token)
    AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
        session_id,
        request_id=execution_context.request_id,
        status="approved" if approved else "rejected",
        error=str(payload.get("rejectReason") or ""),
    )
    result = {
        "ok": approved,
        "requestId": payload.get("requestId"),
        "approved": bool(payload.get("approved")),
        "rejectReason": payload.get("rejectReason"),
        "token": {
            "tokenId": token.get("tokenId"),
            "subject": token.get("subject"),
            "intent": _agent_intent_summary(token.get("intent")),
            "expireAt": token.get("expireAt"),
        }
        if token
        else None,
        "cronJobId": cron_job_id or None,
    }
    verification_result = payload.get("verificationResult")
    if not approved and isinstance(verification_result, dict):
        result["code"] = verification_result.get("code")
    if cron_job_id and approved:
        result["cronJobEnabled"] = False
        result["nextAction"] = {
            "tool": "cron_toggle_job",
            "arguments": {"job_id": cron_job_id, "enabled": True},
        }
    return result


def get_tools(session_id: str | None = None) -> list[Any]:
    """Return A4P tools for Agent registration."""
    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard, ToolExposure

    supported_actions = ", ".join(get_a4p_supported_action_names())
    card = ToolCard(
        name="request_a4p_intent_authorization",
        exposure=ToolExposure.DIRECT,
        parallel_safe=False,
        description=(
            "Request user approval for one currently known A4P intent-authorization stage covering calls to "
            f"{supported_actions}. Actions may cover all known protected calls or only the current stage; request "
            "another authorization only when a later stage is not covered by an approved scope. Without cronJobId, "
            "each token is added to the current Web session, where staged authorization is supported. With cronJobId, "
            "one complete future scope is bound to an existing disabled cron job; cron authorization cannot be staged "
            "or depend on interactive approval during execution. Runtime-dependent arguments do not require another "
            "authorization if their actual values match an approved scope. This tool authorizes actions but does not "
            "execute them."
        ),
        input_params={
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "description": "Non-empty list of supported tool action scopes.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Actual supported tool name.",
                            },
                            "params": {
                                "description": _action_scope_schema_description(),
                                "type": "object",
                                "minProperties": 1,
                            },
                        },
                        "required": ["name", "params"],
                    },
                },
                "validitySeconds": {
                    "type": "integer",
                    "description": (
                        "Optional token validity in seconds. Session tokens default to one hour; "
                        "cron tokens default to 30 days."
                    ),
                },
                "cronJobId": {
                    "type": "string",
                    "description": (
                        "Optional cron job id returned by cron_create_job. When set, the token is "
                        "stored for that cron job and used during future scheduled runs instead of "
                        "the current interactive session. The existing job must be disabled."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Optional display-only explanation; it does not constrain the token."
                    ),
                },
            },
            "required": ["actions"],
        },
    )

    async def bound_func(**arguments: Any) -> dict[str, Any]:
        # Keep the tool's JSON contract independent of Python parameter naming.
        for wire_name, parameter_name in (
            ("validitySeconds", "validity_seconds"),
            ("cronJobId", "cron_job_id"),
        ):
            if wire_name in arguments:
                arguments[parameter_name] = arguments.pop(wire_name)
        return await request_a4p_intent_authorization(
            **arguments,
            _session_id=str(session_id or "").strip(),
        )

    return [LocalFunction(card=card, func=bound_func)]


__all__ = [
    "get_a4p_supported_action_names",
    "get_tools",
    "request_a4p_intent_authorization",
]
