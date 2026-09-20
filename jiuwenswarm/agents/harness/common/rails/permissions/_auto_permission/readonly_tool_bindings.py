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

# LocalFunction exposes no public callable-identity API; pin the executable, not its name.
_TIMESTAMP_FUNC = convert_timestamp_to_utc8_time._func  # pylint: disable=protected-access
_ACP_DELEGATES = {name: getattr(acp, name) for name in ("read_terminal_output", "wait_for_terminal_exit")}


def _closure(func, factory, name):
    """Match the actual callable, never a declared name or __wrapped__ pointer."""
    code = factory.__code__
    for part in name.split("."):
        code = next(c for c in code.co_consts if isinstance(c, CodeType) and c.co_name == part)
    # Proxies can spoof __class__; isinstance alone does not prove executable identity.
    exact_function = type(func) is FunctionType  # pylint: disable=huawei-unidiomatic-typecheck
    if not exact_function or func.__code__ is not code or func.__globals__ is not factory.__globals__:
        raise ValueError("builtin callable mismatch")
    return dict(zip(code.co_freevars, (c.cell_contents for c in func.__closure__ or ()), strict=True))


def _method_matches(owner, expected, name):
    method = getattr(owner, name, None)
    # A subclass may change the method's downstream behavior even if the method is inherited.
    exact_owner = type(owner) is expected  # pylint: disable=huawei-unidiomatic-typecheck
    return exact_owner and matches_bound_method(
        method, expected_owner=owner, expected_func=getattr(expected, name)
    )


def _invoke_matches(resource):
    # Tool's metaclass installs these SDK wrappers on every instance.
    invoke = resource.invoke
    for factory, name in (
        (callbacks.create_emit_after_decorator, "decorator.async_wrapper"),
        # No public equivalent identifies this SDK-installed executable wrapper.
        (callbacks._make_transform_io_decorator, "async_wrapper"),  # pylint: disable=protected-access
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
        # Compare the currently installed callable with the verified implementation.
        name, func = invocation.tool_name, resource._func  # pylint: disable=protected-access
        if name == "convert_timestamp_to_utc8_time":
            return resource is convert_timestamp_to_utc8_time and func is _TIMESTAMP_FUNC
        if not session_id:
            return False
        if name.startswith("cron_"):
            method = name.removeprefix("cron_")
            backend = _closure(func, create_cron_tools, method + "_wrapper")["backend"]
            # Unwrap only this concrete delegating implementation, never a subclass.
            no_create = type(backend) is _NoCreateCronBackend  # pylint: disable=huawei-unidiomatic-typecheck
            if no_create:
                delegate = getattr(backend, method)
                # The private delegate is the actual receiver; no public identity API exists.
                inner = backend._inner  # pylint: disable=protected-access
                if not matches_bound_method(
                    delegate, expected_owner=inner, expected_func=getattr(_CronToolsCronBackend, method)
                ):
                    return False
                backend = inner
            # Verify the executable backend and its bound session, not declared tool metadata.
            return bool(
                _method_matches(backend, _CronToolsCronBackend, method)
                and _method_matches(backend, _CronToolsCronBackend, "_with_route")
                and _method_matches(backend._cron_tools, CronTools, method)  # pylint: disable=protected-access
                and backend._bound_context.session_id == session_id  # pylint: disable=protected-access
            )
        if name.startswith("heartbeat_"):
            closure = _closure(func, HeartbeatRuntimeBridge.build_tools, name.removeprefix("heartbeat_"))
            bridge = closure["self"]
            # The concrete service owns execution; the bridge has no public service-identity API.
            return bool(
                _method_matches(bridge, HeartbeatRuntimeBridge, "_send")
                and _method_matches(
                    bridge._service, HeartbeatRailRuntime, "handle_operation",  # pylint: disable=protected-access
                )
                and closure["context"].session_id == session_id
            )
        if name in _ACP_DELEGATES:
            closure = _closure(func, acp.get_tools, name + "_bound")
            return closure["session_id"] == session_id and getattr(acp, name) is _ACP_DELEGATES[name]
        return False
    except Exception:  # SDK implementation changes must never grant a fast path.
        return False
