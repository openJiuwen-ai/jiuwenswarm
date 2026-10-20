# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A4P agent tools."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.a4p.file_scope import normalize_read_path_params
from jiuwenswarm.agents.harness.common.a4p.preflight import evaluate_action, is_concrete_action
from jiuwenswarm.agents.harness.common.a4p.runtime import (
    get_a4p_runtime,
    is_a4p_preauthorization_active,
    normalize_a4p_action_params,
    resolve_a4p_identity,
)
from jiuwenswarm.agents.harness.common.a4p.execution_context import (
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
    "read_file": ("file_path",),
    "list_files": ("path",),
    "glob": ("path",),
    "grep": ("path",),
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
        "Shell operators, redirections, or extra arguments. For read_file/write_file/edit_file, file_path must be a "
        "concrete filename "
        "or a filename glob under a fixed directory; a directory path alone is invalid. "
        "For batch file writes in one task under the same fixed directory with the same extension, "
        "prefer one write_file action with a filename glob (e.g. /output/stories/*.md) over listing each file. "
        "Never use a bare * as the entire parameter or submit unresolved placeholders as scope constraints. "
        "Runtime values need not be known yet if their allowed scope can already be expressed."
        " For read_file, list_files, glob and grep, both authorization and execution must use explicit absolute paths "
        "without '..' segments (never omit path or use '.'). read_file permits a filename glob under one fixed "
        "directory; list_files/glob require an exact directory path; grep requires an exact file or directory path. "
        "glob authorizes recursive filename discovery; grep authorizes recursive content search including snippets. "
        "Search keywords and result limits may change inside the authorized root. Browsing or filename search "
        "does not grant read_file permission, and reading never grants writing."
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
        try:
            params = normalize_read_path_params(name, params, scope=True)
        except (ValueError, OSError, RuntimeError) as exc:
            raise _ActionScopeError(
                code="A4P_ACTION_SCOPE_INVALID",
                message=str(exc),
                action_index=index,
                action_name=name,
                required_scope_fields=required_fields,
            ) from exc
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


async def _resolve_cron_authorization_target(
    cron_job_id: str, *, user_id: str, request_id: str = "", session_id: str = "",
) -> dict[str, Any] | None:
    from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
        query_authoritative_cron_job,
    )

    job = await query_authoritative_cron_job(
        cron_job_id, user_id=user_id, request_id=request_id, session_id=session_id,
    )
    if job is None:
        return None
    return {
        "type": "cronJob",
        "cronJobId": job["id"],
        "name": job.get("name"),
        "description": job.get("description"),
        "persistent": True,
        "enabled": bool(job.get("enabled", True)),
        "activationRequiredAfterApproval": True,
        "schedule": {
            "cronExpression": job.get("cron_expr"),
            "timezone": job.get("timezone"),
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
    if not is_a4p_preauthorization_active(get_config()):
        return {
            "ok": False,
            "error": "A4P preauthorization is available only in default permission mode with A4P enabled",
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
        try:
            cron_target = await _resolve_cron_authorization_target(
                cron_job_id, user_id=execution_context.user_id,
                request_id=execution_context.request_id, session_id=session_id,
            )
        except Exception as exc:
            AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
                session_id, request_id=execution_context.request_id,
                status="failed", error=str(exc),
            )
            return {"ok": False, "error": str(exc), "code": "CRON_JOB_QUERY_FAILED"}
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
    preflight: dict[str, Any] = {
        "permittedActions": [],
        "unapprovedActions": [],
        "deniedActions": [],
    }
    pending_actions: list[dict[str, Any]] = []
    for action in normalized_actions:
        # Future cron runs cannot inherit this interactive session's permissions.
        # Keep their complete scope, including when replacing an existing token.
        if cron_job_id:
            pending_actions.append(action)
            continue
        public_action = {"name": action["name"], "params": action["params"]}
        level = await evaluate_action(session_id, action)
        if level == "deny":
            preflight["deniedActions"].append(public_action)
            continue
        if level == "allow":
            preflight["permittedActions"].append(public_action)
            continue
        # Verification accepts concrete calls, not set containment of patterns.
        if level == "ask" and is_concrete_action(action):
            try:
                token = await runtime.find_valid_intent_token(
                    session_id=session_id,
                    identity=identity,
                    action=action["name"],
                    params=action["params"],
                )
            except Exception:
                token = None
            if token is not None:
                preflight["permittedActions"].append(public_action)
                continue
        pending_actions.append(action)

    preflight["unapprovedActions"] = [
        {"name": action["name"], "params": action["params"]}
        for action in pending_actions
    ]
    if preflight["deniedActions"]:
        error = "Permission policy denies candidate actions; revise the plan before requesting authorization"
        AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
            session_id, request_id=execution_context.request_id,
            status="failed", error=error,
        )
        return {
            "ok": False,
            "code": "A4P_ACTION_POLICY_DENIED",
            "error": error,
            "authorizationRequired": False,
            **preflight,
        }
    if not pending_actions:
        AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
            session_id, request_id=execution_context.request_id,
            status="approved",
        )
        return {
            "ok": True,
            "authorizationRequired": False,
            "approved": False,
            "partial": False,
            "token": None,
            "cronJobId": None,
            **preflight,
        }
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
            actions=pending_actions,
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
            "authorizationRequired": True,
            **preflight,
        }
    token = payload.get("intentToken") if isinstance(payload.get("intentToken"), dict) else {}
    approved = bool(payload.get("approved")) and bool(token)
    AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
        session_id,
        request_id=execution_context.request_id,
        status="approved" if approved else "rejected",
        error=str(payload.get("rejectReason") or ""),
    )
    if approved:
        newly_authorized = payload.get("authorizedActions")
        if newly_authorized is None:
            newly_authorized = (token.get("intent") or {}).get("actions", [])
        newly_authorized = [
            {"name": action["name"], "params": action["params"]}
            for action in newly_authorized
        ]
        pending_public = preflight["unapprovedActions"]
        preflight["permittedActions"].extend(
            action for action in pending_public if action in newly_authorized
        )
        preflight["unapprovedActions"] = [
            action for action in pending_public if action not in newly_authorized
        ]
    result = {
        "ok": approved,
        "authorizationRequired": True,
        **preflight,
        "requestId": payload.get("requestId"),
        "approved": bool(payload.get("approved")),
        "partial": bool(payload.get("partial")),
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
            "Before reading, listing, searching, writing or running Shell commands, submit candidate scopes for "
            f"{supported_actions}. Do not read files first to prepare authorization. The backend filters session "
            "actions already allowed by policy or covered by valid tokens; uncertain scopes still require approval.\n\n"
            "Without cronJobId, authorization belongs to the current Web session and may cover only the current "
            "stage. Reuse approved scopes when runtime values match. With cronJobId, authorize the complete future "
            "scope of an existing disabled cron job; cron authorization cannot be staged or rely on interactive "
            "approval during execution.\n\n"
            "Inspect permittedActions, unapprovedActions and deniedActions: permittedActions combines policy-allowed, "
            "token-covered and newly approved actions; unapprovedActions still lack approval, "
            "including user-unselected actions; deniedActions are prohibited by policy. Execute only permittedActions. "
            "Keep a cron job disabled until every planned action is permitted.\n\n"
            "This tool prepares authorization and does not execute actions. ok=true with authorizationRequired=false "
            "means no new approval or token was needed; approved may be false. Actual calls still check permissions. "
            "Requires A4P enabled and an interactive Web session."
        ),
        input_params={
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "description": (
                        "Non-empty list of candidate tool action scopes; the backend filters session requests."
                    ),
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
