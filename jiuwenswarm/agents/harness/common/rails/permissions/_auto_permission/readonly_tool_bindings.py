# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Executable identities for the closed builtin observation fast path."""

from types import CodeType, FunctionType

from openjiuwen.core.foundation.tool import LocalFunction
from openjiuwen.core.foundation.tool.base import _ToolMeta
from openjiuwen.core.runner.callback import decorator as callbacks
from openjiuwen.harness.tools.cron import create_cron_tools
from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
from jiuwenswarm.agents.harness.code.rails.heartbeat.tools import HeartbeatRuntimeBridge
from jiuwenswarm.agents.harness.common.tools import acp_output_tools as acp
from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import _CronToolsCronBackend, _NoCreateCronBackend
from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import CronTools
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.timestamp_tool import convert_timestamp_to_utc8_time
from jiuwenswarm.agents.harness.common.rails.permissions.tool_binding import matches_bound_method, resolve_tool_binding

_TIMESTAMP_FUNC = convert_timestamp_to_utc8_time._func
_ACP_DELEGATES = {name: getattr(acp, name) for name in ("read_terminal_output", "wait_for_terminal_exit")}


def _closure(func, factory, name):
    """Match the actual callable, never a declared name or __wrapped__ pointer."""
    code = factory.__code__
    for part in name.split("."):
        code = next(c for c in code.co_consts if isinstance(c, CodeType) and c.co_name == part)
    if type(func) is not FunctionType or func.__code__ is not code or func.__globals__ is not factory.__globals__:
        raise ValueError("builtin callable mismatch")
    return dict(zip(code.co_freevars, (c.cell_contents for c in func.__closure__ or ()), strict=True))


def _method_matches(owner, expected, name):
    method = getattr(owner, name, None)
    return type(owner) is expected and matches_bound_method(
        method, expected_owner=owner, expected_func=getattr(expected, name)
    )


def _invoke_matches(resource):
    # Tool's metaclass installs these SDK wrappers on every instance.
    invoke = resource.invoke
    for factory, name in (
        (callbacks.create_emit_after_decorator, "decorator.async_wrapper"),
        (callbacks._make_transform_io_decorator, "async_wrapper"),
        (callbacks.create_emit_before_decorator, "decorator.async_wrapper"),
    ):
        invoke = _closure(invoke, factory, name)["func"]
    lifecycle = _closure(invoke, _ToolMeta.__call__, "_lifecycle_invoke")
    original = lifecycle["_original_invoke"]
    return lifecycle["instance"] is resource and matches_bound_method(
        original, expected_owner=resource, expected_func=LocalFunction.invoke
    )


def trusted_readonly_binding(invocation, session_id: str) -> bool:
    """Prove current implementation and direct owner; any mismatch stays manual."""
    try:
        resource = resolve_tool_binding(invocation.ctx.agent, invocation.tool_name, LocalFunction)
        if resource is None or not _invoke_matches(resource):
            return False
        name, func = invocation.tool_name, resource._func
        if name == "convert_timestamp_to_utc8_time":
            return resource is convert_timestamp_to_utc8_time and func is _TIMESTAMP_FUNC
        if not session_id:
            return False
        if name.startswith("cron_"):
            method = name.removeprefix("cron_")
            backend = _closure(func, create_cron_tools, method + "_wrapper")["backend"]
            if type(backend) is _NoCreateCronBackend:
                delegate = getattr(backend, method)
                if not matches_bound_method(
                    delegate, expected_owner=backend._inner, expected_func=getattr(_CronToolsCronBackend, method)
                ):
                    return False
                backend = backend._inner
            return bool(
                _method_matches(backend, _CronToolsCronBackend, method)
                and _method_matches(backend, _CronToolsCronBackend, "_with_route")
                and _method_matches(backend._cron_tools, CronTools, method)
                and backend._bound_context.session_id == session_id
            )
        if name.startswith("heartbeat_"):
            closure = _closure(func, HeartbeatRuntimeBridge.build_tools, name.removeprefix("heartbeat_"))
            bridge = closure["self"]
            return bool(
                _method_matches(bridge, HeartbeatRuntimeBridge, "_send")
                and _method_matches(bridge._service, HeartbeatRailRuntime, "handle_operation")
                and closure["context"].session_id == session_id
            )
        if name in _ACP_DELEGATES:
            closure = _closure(func, acp.get_tools, name + "_bound")
            return closure["session_id"] == session_id and getattr(acp, name) is _ACP_DELEGATES[name]
        return False
    except Exception:  # SDK implementation changes must never grant a fast path.
        return False
