"""Migrated Auto Permission invocation context slice."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

try:
    import json_repair
except ImportError:  # pragma: no cover - optional runtime dependency.
    json_repair = None

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    ToolInvocation,
)
from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
    normalize_send_file_paths,
    normalize_send_file_target_channels,
)
from jiuwenswarm.agents.harness.common.tools.command_runtime import (
    current_command_runtime_paths,
    resolve_command_workdir,
)
from jiuwenswarm.common.tool_display import extract_call_goal
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_REQUEST_ID,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_PERMISSION_QUEUE_USER_INPUT_KEY,
)

_SEND_FILE_TOOL_NAME = "send_file_to_user"


def normalize_invocation_tool_args(tool_name: str, value: Any) -> dict[str, Any]:
    """Return the adapter-owned argument mapping for one current invocation."""

    decoded: Any = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = repair_malformed_tool_arguments(value)
    if not isinstance(decoded, Mapping):
        return {}
    normalized = {str(key): item for key, item in decoded.items()}
    _, visible_args = extract_call_goal(normalized)
    normalized = dict(visible_args)
    if tool_name == _SEND_FILE_TOOL_NAME:
        normalized["abs_file_path_list"] = list(
            normalize_send_file_paths(normalized.get("abs_file_path_list"))
        )
        normalized["target_channels"] = list(
            normalize_send_file_target_channels(normalized.get("target_channels"))
        )
    return normalized


@dataclass(frozen=True)
class TrustedSendIdentity:
    """Host-owned identity for one exact send-file invocation."""

    session_id: str
    request_id: str
    tool_call_id: str
    channel_kind: str
    tool_name: str
    normalized_tool_args: Mapping[str, Any]


@dataclass(frozen=True)
class TrustedSendIdentityResolution:
    """Validated send identity or its fail-closed result."""

    invocation: ToolInvocation
    identity: TrustedSendIdentity | None = None
    error: str = ""


def _extract_invocation(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> ToolInvocation:
    ctx = args[0] if args else kwargs.get("ctx")
    tool_call = args[1] if len(args) > 1 else kwargs.get("tool_call")
    tool_name = kwargs.get("tool_name")
    tool_args = kwargs.get("tool_args")

    if tool_call is None and ctx is not None:
        inputs = getattr(ctx, "inputs", None)
        tool_call = _input_value(inputs, "tool_call")
        if tool_name is None:
            tool_name = _input_value(inputs, "tool_name")
        if tool_args is None:
            tool_args = _first_input_value(inputs, ("tool_args", "arguments", "args"))

    if tool_name is None and tool_call is not None:
        tool_name = _tool_call_value(tool_call, "name")
    if tool_args is None and tool_call is not None:
        tool_args = _tool_call_value(tool_call, "arguments")

    normalized_tool_name = str(tool_name or "").strip()
    normalized_tool_args = tool_args if tool_args is not None else {}
    normalized_tool_call = tool_call or SimpleNamespace(
        name=normalized_tool_name, arguments=normalized_tool_args
    )
    return ToolInvocation(
        ctx=ctx,
        tool_call=normalized_tool_call,
        tool_name=normalized_tool_name,
        tool_args=normalized_tool_args,
    )


def _input_value(inputs: Any, key: str) -> Any:
    if isinstance(inputs, Mapping):
        return inputs.get(key)
    return getattr(inputs, key, None)


def _first_input_value(inputs: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = _input_value(inputs, key)
        if value is not None:
            return value
    return None


def _tool_call_value(tool_call: Any, key: str) -> Any:
    if isinstance(tool_call, Mapping):
        for candidate_key in _tool_call_candidate_keys(key):
            value = tool_call.get(candidate_key)
            if value is not None:
                return value
        function_value = tool_call.get("function")
        if isinstance(function_value, Mapping):
            for candidate_key in _tool_call_candidate_keys(key):
                value = function_value.get(candidate_key)
                if value is not None:
                    return value
        return None
    for candidate_key in _tool_call_candidate_keys(key):
        value = getattr(tool_call, candidate_key, None)
        if value is not None:
            return value
    return None


def _tool_call_candidate_keys(key: str) -> tuple[str, ...]:
    if key == "arguments":
        return ("arguments", "args", "input", "parameters", "rawInput")
    return (key,)


def _resolve_trusted_send_identity(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    compatibility_invocation: ToolInvocation,
) -> TrustedSendIdentityResolution | None:
    """Resolve send identity only from the current host callback context."""

    ctx = args[0] if args else kwargs.get("ctx")
    inputs = getattr(ctx, "inputs", None) if ctx is not None else None
    host_tool_call = _input_value(inputs, "tool_call")
    host_tool_name_value = _input_value(inputs, "tool_name")
    host_tool_args = _first_input_value(inputs, ("tool_args", "arguments", "args"))
    host_tool_name = _normalized_identifier(host_tool_name_value)
    compatibility_tool_name = _normalized_identifier(compatibility_invocation.tool_name)
    host_is_send = host_tool_name == _SEND_FILE_TOOL_NAME
    compatibility_is_send = compatibility_tool_name == _SEND_FILE_TOOL_NAME
    if not host_is_send and not compatibility_is_send:
        return None

    trusted_invocation = ToolInvocation(
        ctx=ctx,
        tool_call=host_tool_call,
        tool_name=host_tool_name or _SEND_FILE_TOOL_NAME,
        tool_args=host_tool_args if host_tool_args is not None else {},
    )
    missing = (
        ctx is None
        or inputs is None
        or host_tool_call is None
        or not host_tool_name
        or host_tool_args is None
    )
    if missing:
        return TrustedSendIdentityResolution(
            invocation=trusted_invocation,
            error="send_file_authorization_context_missing",
        )
    if host_is_send != compatibility_is_send:
        return TrustedSendIdentityResolution(
            invocation=trusted_invocation,
            error="send_file_authorization_context_mismatch",
        )

    session_id = _host_session_id(ctx)
    request_id = _normalized_identifier(TOOL_PERMISSION_REQUEST_ID.get())
    tool_call_id = _host_tool_call_id(host_tool_call)
    channel_kind = _normalized_channel_identifier(TOOL_PERMISSION_CHANNEL_ID.get())
    if not session_id or not request_id:
        return TrustedSendIdentityResolution(
            invocation=trusted_invocation,
            error="send_file_authorization_context_missing",
        )
    if not tool_call_id or not channel_kind:
        return TrustedSendIdentityResolution(
            invocation=trusted_invocation,
            error="send_file_authorization_context_missing",
        )

    normalized_host_args = normalize_invocation_tool_args(
        _SEND_FILE_TOOL_NAME,
        host_tool_args,
    )
    identity = TrustedSendIdentity(
        session_id=session_id,
        request_id=request_id,
        tool_call_id=tool_call_id,
        channel_kind=channel_kind,
        tool_name=host_tool_name,
        normalized_tool_args=normalized_host_args,
    )
    if not _compatibility_send_matches_host(
        args=args,
        kwargs=kwargs,
        compatibility_invocation=compatibility_invocation,
        identity=identity,
    ):
        return TrustedSendIdentityResolution(
            invocation=trusted_invocation,
            error="send_file_authorization_context_mismatch",
        )
    return TrustedSendIdentityResolution(
        invocation=trusted_invocation,
        identity=identity,
    )


def _trusted_send_descriptor_matches(
    identity: TrustedSendIdentity, descriptor: Any
) -> bool:
    """Return whether policy evaluated the exact host-owned send payload."""

    return (
        getattr(descriptor, "tool_name", "") == identity.tool_name
        and getattr(descriptor, "untrusted_args", {}) == identity.normalized_tool_args
    )


def _compatibility_send_matches_host(
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    compatibility_invocation: ToolInvocation,
    identity: TrustedSendIdentity,
) -> bool:
    if compatibility_invocation.tool_name != identity.tool_name:
        return False
    if (
        normalize_invocation_tool_args(
            _SEND_FILE_TOOL_NAME,
            compatibility_invocation.tool_args,
        )
        != identity.normalized_tool_args
    ):
        return False

    expected_values = {
        "session_id": identity.session_id,
        "request_id": identity.request_id,
        "tool_call_id": identity.tool_call_id,
        "channel_kind": identity.channel_kind,
        "tool_name": identity.tool_name,
    }
    for key, expected in expected_values.items():
        if key not in kwargs:
            continue
        supplied = (
            _normalized_channel_identifier(kwargs[key])
            if key == "channel_kind"
            else _normalized_identifier(kwargs[key])
        )
        if supplied != expected:
            return False
    if "tool_args" in kwargs and (
        normalize_invocation_tool_args(
            _SEND_FILE_TOOL_NAME,
            kwargs["tool_args"],
        )
        != identity.normalized_tool_args
    ):
        return False

    supplied_tool_calls = [args[1]] if len(args) > 1 else []
    if "tool_call" in kwargs:
        supplied_tool_calls.append(kwargs["tool_call"])
    return all(
        _host_tool_call_id(tool_call) == identity.tool_call_id
        and _normalized_identifier(_tool_call_value(tool_call, "name"))
        == identity.tool_name
        and normalize_invocation_tool_args(
            _SEND_FILE_TOOL_NAME,
            _tool_call_value(tool_call, "arguments"),
        )
        == identity.normalized_tool_args
        for tool_call in supplied_tool_calls
    )


def _host_session_id(ctx: Any) -> str:
    session = getattr(ctx, "session", None)
    if session is None:
        return ""
    for attr_name in ("get_session_id", "session_id"):
        attr = getattr(session, attr_name, None)
        try:
            value = attr() if callable(attr) else attr
        except (RuntimeError, TypeError, ValueError):
            continue
        normalized = _normalized_identifier(value)
        if normalized:
            return normalized
    return ""


def _host_tool_call_id(tool_call: Any) -> str:
    for key in ("id", "tool_call_id", "call_id"):
        normalized = _normalized_identifier(_tool_call_value(tool_call, key))
        if normalized:
            return normalized
    return ""


def _normalized_identifier(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized_channel_identifier(value: Any) -> str:
    return _normalized_identifier(value).lower()


def _args_were_valid_json_object(tool_args: Any) -> bool:
    if isinstance(tool_args, Mapping):
        return True
    if not isinstance(tool_args, str):
        return False
    try:
        decoded = json.loads(tool_args)
    except json.JSONDecodeError:
        return False
    return isinstance(decoded, Mapping)


def repair_malformed_tool_arguments(tool_args: str) -> dict[str, Any] | None:
    """Repair execution arguments in the invocation adapter only."""

    if json_repair is None:
        return None
    try:
        repaired = json_repair.loads(tool_args)
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(repaired, Mapping):
        return None
    return {str(key): value for key, value in repaired.items()}


def _repair_invocation_args_for_execution(invocation: ToolInvocation) -> ToolInvocation:
    """Parse or repair a string object and freeze it into the live invocation."""

    if not isinstance(invocation.tool_args, str):
        return invocation
    try:
        decoded = json.loads(invocation.tool_args)
    except json.JSONDecodeError:
        decoded = repair_malformed_tool_arguments(invocation.tool_args)
    if not isinstance(decoded, Mapping):
        return invocation
    repaired = {str(key): value for key, value in decoded.items()}
    _write_invocation_tool_args(invocation, {}, repaired)
    return _replace_invocation_tool_args(invocation, repaired)


def _replace_invocation_tool_args(
    invocation: ToolInvocation, tool_args: dict[str, Any]
) -> ToolInvocation:
    return ToolInvocation(
        ctx=invocation.ctx,
        tool_call=invocation.tool_call,
        tool_name=invocation.tool_name,
        tool_args=tool_args,
    )


def _normalize_command_invocation_for_execution(
    invocation: ToolInvocation,
    kwargs: dict[str, Any],
) -> tuple[ToolInvocation, str]:
    """Freeze mcp_exec_command's effective workdir into its execution args."""

    if invocation.tool_name != "mcp_exec_command":
        return invocation, ""
    normalized_args = normalize_invocation_tool_args(
        invocation.tool_name,
        invocation.tool_args,
    )
    if not normalized_args or "cwd" in normalized_args:
        return invocation, "command_workdir_contract_invalid"
    try:
        resolved = resolve_command_workdir(
            normalized_args.get("workdir", "."),
            runtime_paths=current_command_runtime_paths(require_runtime_cwd=True),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return invocation, "command_workdir_contract_invalid"
    execution_args = {**normalized_args, "workdir": str(resolved)}
    try:
        _write_invocation_tool_args(invocation, kwargs, execution_args)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return invocation, "command_workdir_writeback_failed"
    return _replace_invocation_tool_args(invocation, execution_args), ""


def _write_invocation_tool_args(
    invocation: ToolInvocation,
    kwargs: dict[str, Any],
    tool_args: dict[str, Any],
) -> None:
    def execution_value(current: Any) -> Any:
        return json.dumps(tool_args) if isinstance(current, str) else tool_args

    tool_call = invocation.tool_call
    if isinstance(tool_call, dict):
        tool_call["arguments"] = execution_value(tool_call.get("arguments"))
    elif tool_call is not None:
        setattr(
            tool_call,
            "arguments",
            execution_value(getattr(tool_call, "arguments", None)),
        )
    inputs = getattr(invocation.ctx, "inputs", None)
    if isinstance(inputs, dict):
        inputs["tool_args"] = execution_value(inputs.get("tool_args"))
        nested = inputs.get("tool_call")
        if isinstance(nested, dict):
            nested["arguments"] = execution_value(nested.get("arguments"))
        elif nested is not None:
            setattr(
                nested,
                "arguments",
                execution_value(getattr(nested, "arguments", None)),
            )
    elif inputs is not None:
        setattr(
            inputs,
            "tool_args",
            execution_value(getattr(inputs, "tool_args", None)),
        )
        nested = getattr(inputs, "tool_call", None)
        if nested is not None:
            setattr(
                nested,
                "arguments",
                execution_value(getattr(nested, "arguments", None)),
            )
    if "tool_args" in kwargs:
        kwargs["tool_args"] = execution_value(kwargs["tool_args"])


def _resolve_session_id(ctx: Any | None, kwargs: dict[str, Any]) -> str | None:
    explicit_session_id = kwargs.get("session_id")
    if isinstance(explicit_session_id, str) and explicit_session_id.strip():
        return explicit_session_id.strip()
    if ctx is None:
        return None

    session = getattr(ctx, "session", None)
    if session is None:
        return None

    for attr_name in ("get_session_id", "session_id"):
        attr = getattr(session, attr_name, None)
        try:
            value = attr() if callable(attr) else attr
        except (RuntimeError, TypeError, ValueError):
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_request_id(ctx: Any | None, kwargs: dict[str, Any]) -> str | None:
    explicit_request_id = kwargs.get("request_id")
    if isinstance(explicit_request_id, str) and explicit_request_id.strip():
        return explicit_request_id.strip()
    if ctx is not None:
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, Mapping):
            value = extra.get("request_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
        for attr_name in ("host_request_id", "request_id"):
            value = getattr(ctx, attr_name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        inputs = getattr(ctx, "inputs", None)
        value = _input_value(inputs, "request_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    context_request_id = TOOL_PERMISSION_REQUEST_ID.get()
    if context_request_id.strip():
        return context_request_id.strip()
    return None


def _resolve_tool_call_id(tool_call: Any | None, kwargs: dict[str, Any]) -> str | None:
    explicit_tool_call_id = kwargs.get("tool_call_id")
    if isinstance(explicit_tool_call_id, str) and explicit_tool_call_id.strip():
        return explicit_tool_call_id.strip()
    if tool_call is None:
        return None
    for attr_name in ("id", "tool_call_id", "call_id"):
        value = getattr(tool_call, attr_name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_user_input(
    base_rail: Any,
    *,
    ctx: Any | None,
    tool_call_id: str | None,
    kwargs: dict[str, Any],
) -> Any:
    if "user_input" in kwargs:
        return kwargs["user_input"]
    if ctx is None or not tool_call_id:
        return None
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, Mapping) and ROOT_PERMISSION_QUEUE_USER_INPUT_KEY in extra:
        raw_input = extra[ROOT_PERMISSION_QUEUE_USER_INPUT_KEY]
        user_inputs = getattr(raw_input, "user_inputs", None)
        if isinstance(user_inputs, Mapping):
            return user_inputs.get(tool_call_id)
        if isinstance(raw_input, Mapping) and tool_call_id in raw_input:
            return raw_input[tool_call_id]
        return raw_input
    getter = getattr(base_rail, "_get_user_input", None)
    if not callable(getter):
        return None
    try:
        return getter(ctx, tool_call_id)
    except (RuntimeError, TypeError, ValueError):
        return None


def _normalize_channel_kind(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized or "unknown"


def _resolve_channel_kind(ctx: Any | None, kwargs: dict[str, Any]) -> str:
    """Return the host channel kind for permission policy decisions."""
    value = kwargs.get("channel_kind")
    if isinstance(value, str) and value.strip():
        return _normalize_channel_kind(value)
    if ctx is not None:
        attr_value = getattr(ctx, "channel_kind", None)
        if isinstance(attr_value, str) and attr_value.strip():
            return _normalize_channel_kind(attr_value)
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, Mapping):
            extra_value = extra.get("channel_kind")
            if isinstance(extra_value, str) and extra_value.strip():
                return _normalize_channel_kind(extra_value)
    return _normalize_channel_kind(TOOL_PERMISSION_CHANNEL_ID.get())
